"""
Whare Wananga -- predict-then-verify learning runner (core loop).

For a tool, the model PREDICTS the tool's reaction (success or a specific error) for a call
it chooses, the tool is executed, prediction is compared to reality. Learning runs in
phases:

  1. initial LEARN batch (LEARN_N probes) -> distil the three baskets (aronui/tuatea/tuarua)
  2. VALIDATE batch (VALIDATE_N probes): predict each outcome from the learned knowledge.
       - all correct  -> "learned" (status=confirmed), done.
       - otherwise     -> REFINE batch (REFINE_N probes) -> re-distil -> validate again.
  3. repeat up to MAX_ROUNDS, then stop (status=draft if never fully validated).

Safety tiering by side_effect_class: none -> probe normally; reversible -> ERROR-PATH (probe
only with invalid/incomplete inputs the tool rejects before acting, no real effect, halt if
unexpectedly accepted); irreversible -> gated (never probed). A tool may override the reversible
tier by declaring `whare_wananga_full_probe = True` -- for a sandboxed/ephemeral executor (e.g.
python_sandbox: Docker-isolated, host bridge opt-in) accepting a probe is harmless and leaves
nothing permanent, so it is probed in full instead of the error path. Class sandbox: only the
trained tool + its connection-class siblings may be executed. Uses the agent to execute tools
and the tool's query_llm for LLM calls (works for API + local providers).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from vaf.whare_wananga import store

_ERROR_MARKERS = (
    "[error]", "error:", "unknown action", "failed to", "not found", "invalid", "no such",
    "missing required", "is required", "are required", "traceback", "exception",
    "permission denied", "access denied", "forbidden", "not allowed", "not permitted",
    "is protected", "could not",
)

# Phase sizes (Tohunga principle: repeat until predictions are perfect).
LEARN_N = 21        # initial learning batch — build the tool_knowledge before the final test
VALIDATE_N = 9      # a validation batch must be predicted 9/9 (judge-graded) to confirm
REFINE_N = 6        # refinement batch size when a validation batch fails
MAX_ROUNDS = 4      # cap on validate->refine rounds (bounds cost/time)

# Challenge phase (after a clean 9/9): the JUDGE invents the inputs (the agent can't self-select
# easy probes) -> the agent predicts -> the judge grades. A final test of true understanding.
CHALLENGE_PASS = 3        # judge-posed scenarios the agent must pass to clear the challenge
CHALLENGE_ROUND_FAILS = 3 # fails within a round that trigger a re-distil + a fresh round
CHALLENGE_MAX_FAILS = 10  # total challenge fails before giving up (tool deemed not truly learned)

# Generous output budget for every LLM call here so the JSON answer is NEVER truncated -- a
# reasoning model can burn a small budget entirely inside <think> and cut the answer off.
# Set comfortably above anything these small JSON replies need.
_MAX_TOKENS = 8000


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first valid JSON object out of an LLM reply. Robust to local reasoning models
    that wrap their answer in <think>...</think> and to prose / braces inside string values."""
    if not text:
        return None
    # Local reasoning models emit a <think> block before the answer; its prose contains stray
    # braces that wreck naive find/rfind. Strip it, but keep the raw text as a fallback in case
    # the model put the JSON *inside* the think block.
    stripped = re.sub(r"<think>.*?</think>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"</?think>", " ", stripped, flags=re.IGNORECASE)
    for candidate in (stripped, text):
        m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", candidate, re.IGNORECASE)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
        # Scan each '{' as a potential object start, find its brace-balanced close, return the
        # first substring that parses as JSON.
        for s in (i for i, c in enumerate(candidate) if c == "{"):
            depth = 0
            for e in range(s, len(candidate)):
                ch = candidate[e]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(candidate[s:e + 1])
                        except Exception:
                            break
    return None


def _classify_actual(result: str) -> str:
    """Heuristic: did the tool call succeed or error? Tools signal errors in varied ways -- a
    leading 'Error' / '[ERROR]' / 'Exception' / 'Traceback' (with or without brackets), or a
    known error phrase anywhere in the response."""
    low = (result or "").strip().lower()
    if low.startswith(("error", "[error", "exception", "traceback")):
        return "error"
    return "error" if any(mk in low for mk in _ERROR_MARKERS) else "success"


def _safe(s: Any, n: int = 400) -> str:
    return str(s if s is not None else "")[:n]


def _tool_params(tool) -> Any:
    return (getattr(tool, "parameters", None)
            or getattr(tool, "input_schema", None)
            or getattr(tool, "args_schema", None)
            or {})


def _force_invalid(args: Any, params: Any) -> dict:
    """Keep an error-path probe safely rejectable. The model's *already-invalid* probes are kept
    as-is (so different invalid inputs -- empty, wrong field, etc. -- are observed); only a
    probe that would be a COMPLETE, valid call is neutralised (its required fields dropped) so the
    tool rejects it before acting. Net effect: no real side effect and the safety halt can't fire,
    while the probes still vary instead of being a single empty `{}`."""
    a = dict(args) if isinstance(args, dict) else {}
    required = params.get("required") if isinstance(params, dict) else None
    if not (isinstance(required, list) and required):
        return a  # no declared required fields -> nothing to neutralise (halt stays the net)
    _empty = (None, "", [], {})
    already_invalid = any((r not in a) or (a.get(r) in _empty) for r in required)
    if already_invalid:
        return a  # the model already made it rejectable -> keep it (preserves variety)
    for r in required:  # a complete valid call -> drop required fields so it gets rejected
        a.pop(r, None)
    return a


def _seed_context() -> str:
    """Dynamic, tool-agnostic environment hints for FULL-probe prediction prompts, so the model
    uses inputs that are actually VALID for THIS live system instead of inventing wrong-OS values
    (the read_file probe once guessed C:\\Windows paths on Linux and only ever saw 'access denied').
    Provides a small reusable scratch dir + readable file the model may point any path/file/dir
    argument at, plus real OS/cwd facts. Generic -- the model maps the real values onto whatever
    arguments the tool takes, so it works for custom tools too (no per-tool code)."""
    import os
    import platform
    import tempfile
    try:
        osname = platform.system() or os.name
    except Exception:
        osname = os.name
    cwd = os.getcwd()
    seed_file = scratch = None
    try:
        scratch = os.path.join(tempfile.gettempdir(), "ww_seed")          # stable -> not accumulated
        os.makedirs(os.path.join(scratch, "subdir"), exist_ok=True)
        seed_file = os.path.join(scratch, "seed.txt")
        with open(seed_file, "w", encoding="utf-8") as fh:
            fh.write("Whare Wananga seed file.\nline two\nline three\n")
    except Exception:
        seed_file = scratch = None
    real = None
    for cand in ("README.md", "readme.md", "pyproject.toml", "setup.py"):
        p = os.path.join(cwd, cand)
        if os.path.isfile(p):
            real = p
            break
    parts = [
        "ENVIRONMENT -- this is a REAL live system; use values that actually work HERE (do NOT "
        "invent wrong-OS paths such as C:\\... on a non-Windows host):",
        f"- OS: {osname}; current working directory: {cwd}",
    ]
    if seed_file:
        parts.append(f"- A known-readable text file you MAY use: {seed_file}")
        parts.append(f"- A known directory you MAY use: {scratch} (contains seed.txt and subdir/)")
    if real:
        parts.append(f"- A real project file you MAY use: {real}")
    parts.append("For any argument that takes a path / file / directory (or similar resource), prefer "
                 "these REAL values so a valid call actually SUCCEEDS -- you must observe the success "
                 "shape, not only errors. Still include some deliberately invalid/edge inputs too.")
    return "\n".join(parts)


def train_tool(agent, tool_name: str, progress: Optional[Callable[[dict], None]] = None,
               learn_n: int = LEARN_N, validate_n: int = VALIDATE_N, refine_n: int = REFINE_N,
               max_rounds: int = MAX_ROUNDS, challenge_pass: int = CHALLENGE_PASS,
               challenge_round_fails: int = CHALLENGE_ROUND_FAILS,
               challenge_max_fails: int = CHALLENGE_MAX_FAILS) -> Dict[str, Any]:
    """Run the phased predict-then-verify learning loop for one tool. Returns a summary."""
    def _emit(ev: dict) -> None:
        if progress:
            try:
                progress(ev)
            except Exception:
                pass

    tool = getattr(agent, "tools", {}).get(tool_name)
    if tool is None:
        return {"ok": False, "tool": tool_name, "error": "unknown tool"}

    sec = getattr(tool, "side_effect_class", "none") or "none"
    # A sandboxed/ephemeral executor (e.g. python_sandbox: Docker-isolated, host-tool bridge
    # opt-in) declares this to opt out of the error path: accepting a probe just runs harmless
    # self-contained code and leaves nothing permanent, so full probing is safe and is the only
    # way to actually learn it (its whole job is to ACCEPT and run code -> the error path would
    # halt the instant a probe is accepted).
    full_probe = bool(getattr(tool, "whare_wananga_full_probe", False))
    # Safety tiering by side_effect_class:
    #   none / full_probe -> probe normally (real success observed).
    #   reversible        -> ERROR-PATH: probe only with invalid/incomplete inputs the tool
    #                        rejects before acting (no real effect); a stray effect is reversible.
    #   irreversible      -> GATED: a single accepted probe could be a real irreversible effect
    #                        (sent mail / payment / deletion) -> never probed here.
    if sec == "none" or full_probe:
        mode = "probe"
    elif sec == "reversible":
        mode = "error_path"
    else:
        return {"ok": False, "tool": tool_name, "skipped": True,
                "reason": f"side_effect_class={sec}: irreversible tools are not probed (gated for safety)"}

    description = getattr(tool, "description", "") or ""
    params = _tool_params(tool)
    schema_hash = store.compute_tool_hash(tool)

    # Training sandbox: only the trained tool + its connection-class siblings may be executed,
    # PLUS this tool's declared prerequisites (the "plan first" tools), so a tool that needs setup
    # can have it run before probing.
    from vaf.whare_wananga.preconditions import tool_class
    _all_tools = getattr(agent, "tools", {})
    prereqs = [p for p in (getattr(tool, "whare_wananga_prereqs", ()) or ()) if isinstance(p, str) and p in _all_tools]
    allowed = set(tool_class(tool_name, list(_all_tools.keys()))) | set(prereqs)

    def _probe(name: str, args: dict) -> str:
        if name not in allowed:
            return f"[Error] training sandbox: '{name}' is outside the class of '{tool_name}'"
        # Drive execute_tool in training mode so the interactive plan/confirmation gates are skipped
        # (otherwise a write/confirmation tool returns [CANCELLED] and the probe is meaningless). The
        # flag is set only for the duration of this call and always restored.
        prev = getattr(agent, "_ww_training", False)
        try:
            agent._ww_training = True
            return agent.execute_tool(name, args)
        except Exception as e:
            return f"[Error] {e}"
        finally:
            agent._ww_training = prev

    rec = store.load(tool_name) or store.new_record(
        tool_name, side_effect_class=sec, tool_schema_hash=schema_hash, source="whare_wananga")
    rec["tool_schema_hash"] = schema_hash
    rec["status"] = "learning"
    # Each training run is a fresh assessment: start the predict-then-verify catalogue AND the
    # three baskets empty so a re-train doesn't mix in (or pile up, e.g. the halt warning) a
    # previous run's results. The baskets are re-distilled from scratch on a clean run.
    rec["predict_records"] = []
    rec["uses"] = rec["success"] = rec["fail"] = 0
    rec["aronui"] = {"when_to_use": "", "output_shape": "", "notes": []}
    rec["tuatea"] = {"pitfalls": []}
    rec["tuarua"] = {"procedure": [], "verification": []}

    all_attempts: List[dict] = []
    state = {"halted": False}
    _emit({"event": "start", "tool": tool_name, "mode": mode, "learn_n": learn_n,
           "validate_n": validate_n, "refine_n": refine_n, "max_rounds": max_rounds})

    _seed_ctx = ""
    if mode == "probe" and full_probe:
        # Sandboxed executor: probe in full but ONLY with harmless, self-contained snippets so
        # nothing permanent happens (Docker-isolated; do not enable any tool/host bridge).
        _sys = (
            "You are probing a sandboxed code-execution tool to learn how it behaves. Choose ONE "
            "concrete call and PREDICT its outcome BEFORE it runs. Use ONLY short, SELF-CONTAINED, "
            "HARMLESS snippets: pure computation, string/list/math ops, and deliberately broken "
            "code to learn the error shape (syntax errors, NameError, ZeroDivisionError, bad types). "
            "NEVER read or write files, NEVER import os/sys/subprocess/socket for side effects, "
            "NEVER access the network, and NEVER enable any tool bridge. The \"code\" argument is "
            "REQUIRED and MUST contain runnable Python (never empty). Respond ONLY with JSON, e.g. "
            "{\"args\": {\"code\": \"print(2 + 2)\"}, \"predicted_outcome\": \"success\", "
            "\"predicted\": \"prints 4\"} or for an error probe "
            "{\"args\": {\"code\": \"print(1/0)\"}, \"predicted_outcome\": \"error\", "
            "\"predicted\": \"ZeroDivisionError\"}.")
    elif mode == "probe":
        _seed_ctx = _seed_context()
        _sys = (
            "You are probing a tool to learn how it behaves. Choose ONE concrete call to make "
            "and PREDICT its outcome BEFORE it runs. Vary probes: valid inputs AND deliberately "
            "invalid/edge inputs to learn the tool's error behaviour. Respond ONLY with JSON: "
            "{\"args\": {<tool args>}, \"predicted_outcome\": \"success\" | \"error\", "
            "\"predicted\": \"<one sentence: what you expect / which error>\"}.\n\n" + _seed_ctx)
    else:
        _sys = (
            "This tool HAS SIDE EFFECTS, so you must NEVER make a valid call (a valid call would "
            "actually change something). Probe ONLY with INVALID or INCOMPLETE inputs so the tool "
            "REJECTS the call with a validation error BEFORE doing anything -- this is how you learn "
            "its argument contract and error messages SAFELY. Make EVERY probe clearly invalid: send "
            "{} (no args at all), OR set a required field to \"\" / null / a wrong type. Do NOT fill "
            "in plausible real values, and do NOT guess a working call. PREDICT the rejection. "
            "Respond ONLY with JSON, e.g. {\"args\": {}, \"predicted_outcome\": \"error\", "
            "\"predicted\": \"missing required field\"} or "
            "{\"args\": {\"<a required field>\": \"\"}, \"predicted_outcome\": \"error\", "
            "\"predicted\": \"empty value rejected\"}.")

    def _judge(args: dict, predicted_outcome: str, predicted_text: str, actual: str) -> dict:
        """LLM judge: did the agent's prediction match reality? Transient infra errors pass."""
        judge_prompt = [
            {"role": "system", "content": (
                "You are an impartial JUDGE deciding whether an agent has learned a tool. The agent "
                "predicted what calling the tool would do; then the tool was actually called. Decide "
                "whether the agent's prediction was CORRECT. Judge UNDERSTANDING, not luck: a correctly "
                "predicted error is a PASS. Transient/infrastructure problems are NOT the tool failing "
                "and NOT the agent's fault -- if the actual response is a rate limit, quota, timeout, or "
                "network error, return \"pass\". Respond ONLY with JSON: "
                "{\"verdict\": \"pass\" | \"fail\", \"reason\": \"<one short sentence>\"}.")},
            {"role": "user", "content": (
                f"Tool: {tool_name}\nArgs the agent chose: {_safe(json.dumps(args), 600)}\n"
                f"Agent predicted: {predicted_outcome} -- {predicted_text}\n"
                f"Actual tool response: {_safe(actual, 600)}")},
        ]
        raw = tool.query_llm(judge_prompt, max_tokens=_MAX_TOKENS, temperature=0.0) or ""
        jd = _extract_json(raw) or {}
        verdict = str(jd.get("verdict") or "").lower()
        if verdict not in ("pass", "fail"):  # fall back to the heuristic if the judge misbehaves
            verdict = "pass" if (predicted_outcome == _classify_actual(actual)) else "fail"
        return {"verdict": verdict, "reason": _safe(jd.get("reason") or "", 200)}

    def _knowledge_brief() -> str:
        """Compact view of the distilled tool_knowledge, so the agent predicts FROM the document
        (empty until the first distillation; populated for every repeat afterwards)."""
        a = rec.get("aronui", {})
        u = rec.get("tuarua", {})
        pit = [p.get("text") for p in rec.get("tuatea", {}).get("pitfalls", [])]
        return _safe(json.dumps({"when_to_use": a.get("when_to_use", ""),
                                 "output_shape": a.get("output_shape", ""),
                                 "pitfalls": pit, "procedure": u.get("procedure", []),
                                 "verification": u.get("verification", [])}), 900)

    def _one_probe(phase: str) -> bool:
        """One predict-then-verify probe. Returns whether the prediction matched (validation
        is graded by the LLM judge; learning uses the cheap heuristic)."""
        predict_prompt = [
            {"role": "system", "content": _sys},
            {"role": "user", "content": (
                f"Tool: {tool_name}\nDescription: {description}\n"
                f"Parameters: {json.dumps(params)[:1200]}\n"
                f"What you have learned so far (use it to predict): {_knowledge_brief()}\n"
                f"Phase: {phase}. Earlier attempts: {json.dumps(all_attempts[-6:])[:1000]}")},
        ]
        raw = tool.query_llm(predict_prompt, max_tokens=_MAX_TOKENS, temperature=0.4) or ""
        plan = _extract_json(raw) or {}
        args = plan.get("args") if isinstance(plan.get("args"), dict) else {}
        predicted_outcome = str(plan.get("predicted_outcome") or "").lower()
        if predicted_outcome not in ("success", "error"):
            predicted_outcome = "success"
        predicted = _safe(plan.get("predicted") or raw, 200)

        # Error path is for side-effecting tools: GUARANTEE the executed probe is invalid (drop
        # required fields) so the tool rejects it before acting. This removes the real side effect
        # AND the spurious halt that fired when the model occasionally proposed a valid call.
        if mode == "error_path":
            args = _force_invalid(args, params)

        result = _probe(tool_name, args)
        result_s = result if isinstance(result, str) else str(result)
        actual_outcome = _classify_actual(result_s)

        # Validation is graded by an LLM JUDGE (decides pass/fail and treats transient infra
        # errors like rate limits as not-a-failure); learning probes use the cheap heuristic
        # since their match is only informational.
        verdict = reason = None
        if phase == "validate":
            jv = _judge(args, predicted_outcome, predicted, result_s)
            verdict, reason = jv["verdict"], jv["reason"]
            match = (verdict == "pass")
        else:
            match = (predicted_outcome == actual_outcome)

        attempt = {
            "intent": _safe(json.dumps(args), 200),
            "predicted": f"{predicted_outcome}: {predicted}",
            "actual": f"{actual_outcome}: {_safe(result_s, 200)}",
            "match": match, "phase": phase,
        }
        if verdict is not None:
            attempt["verdict"] = verdict
            attempt["judge_reason"] = reason
        all_attempts.append(attempt)
        rec["predict_records"].append(attempt)
        rec["uses"] = (rec.get("uses", 0) or 0) + 1
        if actual_outcome == "success":
            rec["success"] = (rec.get("success", 0) or 0) + 1
        else:
            rec["fail"] = (rec.get("fail", 0) or 0) + 1

        hits_total = sum(1 for a in all_attempts if a.get("match"))
        _emit({"event": "attempt", "phase": phase, "match": match, "i": len(all_attempts),
               "hits": hits_total, "predicted_outcome": predicted_outcome,
               "actual_outcome": actual_outcome, "verdict": verdict, "reason": reason,
               "intent": attempt["intent"], "actual": attempt["actual"]})

        if mode == "error_path" and actual_outcome == "success":
            rec.setdefault("tuatea", {}).setdefault("pitfalls", []).append({
                "text": "WARNING: an invalid probe was not rejected (possible real side effect); training halted.",
                "source": "whare_wananga", "seen": 1})
            state["halted"] = True
            _emit({"event": "halt", "reason": "invalid probe not rejected"})
        return match

    def _distil() -> None:
        distil_prompt = [
            {"role": "system", "content": (
                "Summarise how to correctly operate this tool, learned from the probe attempts. "
                "Capture the ARGUMENT CONTRACT in tuatea.pitfalls: which arguments are REQUIRED and "
                "the exact error seen when one is missing, empty, or the wrong type -- quote the "
                "tool's error text (e.g. an empty required field returning '[ERROR] ... No X "
                "provided'). Respond ONLY with JSON: "
                "{\"aronui\": {\"when_to_use\": str, \"output_shape\": str}, "
                "\"tuatea\": {\"pitfalls\": [str]}, "
                "\"tuarua\": {\"procedure\": [str], \"verification\": [str]}}.")},
            {"role": "user", "content": (
                f"Tool: {tool_name}\nDescription: {description}\n"
                f"Parameters: {json.dumps(params)[:1000]}\n"
                f"Attempts:\n{json.dumps(all_attempts, indent=2)[:3000]}")},
        ]
        draw = tool.query_llm(distil_prompt, max_tokens=_MAX_TOKENS, temperature=0.3) or ""
        d = _extract_json(draw) or {}
        # Diagnostic: if the distillation didn't parse, keep a snippet of the raw reply on the
        # record so the failure (truncation / malformed JSON) is visible; clear it on success.
        rec.pop("_distil_debug", None)
        if not d:
            rec["_distil_debug"] = _safe(draw, 800)
        if isinstance(d.get("aronui"), dict):
            a = d["aronui"]
            rec["aronui"]["when_to_use"] = _safe(a.get("when_to_use"), 400) or rec["aronui"].get("when_to_use", "")
            rec["aronui"]["output_shape"] = _safe(a.get("output_shape"), 400) or rec["aronui"].get("output_shape", "")
        if isinstance(d.get("tuatea"), dict) and isinstance(d["tuatea"].get("pitfalls"), list):
            # overwrite distilled pitfalls (re-distil replaces, no duplicates)
            rec["tuatea"]["pitfalls"] = [
                {"text": _safe(p, 200), "source": "whare_wananga", "seen": 1}
                for p in d["tuatea"]["pitfalls"][:10]]
        if isinstance(d.get("tuarua"), dict):
            t = d["tuarua"]
            if isinstance(t.get("procedure"), list):
                rec["tuarua"]["procedure"] = [_safe(s, 200) for s in t["procedure"][:12]]
            if isinstance(t.get("verification"), list):
                rec["tuarua"]["verification"] = [_safe(s, 200) for s in t["verification"][:12]]
        # Persist mid-run so the dashboard can show the three baskets right after the initial
        # learning phase (before validation) and refresh them after each refinement round.
        store.save(rec)
        _emit({"event": "distil"})  # the agent just consolidated knowledge -> "idea" beat in the UI

    def _has_knowledge() -> bool:
        """Did distillation actually fill any of the three baskets?"""
        a, t, u = rec.get("aronui", {}), rec.get("tuatea", {}), rec.get("tuarua", {})
        return bool((a.get("when_to_use") or "").strip() or (a.get("output_shape") or "").strip()
                    or t.get("pitfalls") or u.get("procedure") or u.get("verification"))

    def _challenge_probe() -> bool:
        """Final challenge: the JUDGE invents the input (not the agent), the agent predicts the
        outcome, the tool runs, the judge grades pass/fail. Returns whether it passed."""
        # 1) The judge invents a fresh, independent test input -- safety rules mirror the probe mode.
        if full_probe:
            inv_rules = ("Invent ONE short, SELF-CONTAINED, HARMLESS snippet as the input (pure "
                         "computation or a deliberately broken one); never files/network/os/subprocess, "
                         "never any tool bridge.")
        elif mode == "error_path":
            inv_rules = ("Invent ONE INVALID or INCOMPLETE input the tool must reject before acting "
                         "(omit a required field / wrong type); never a valid call that could act.")
        else:
            inv_rules = ("Invent ONE realistic input, favouring an edge case the agent might get "
                         "wrong. " + _seed_ctx)
        invent_prompt = [
            {"role": "system", "content": (
                "You are the JUDGE running a final challenge to test whether the agent TRULY "
                f"understands this tool. {inv_rules} Pick something non-obvious that has not been "
                "tried yet. Respond ONLY with JSON: {\"args\": {<tool args>}, \"note\": \"<why this "
                "is a good test>\"}.")},
            {"role": "user", "content": (
                f"Tool: {tool_name}\nDescription: {description}\n"
                f"Parameters: {json.dumps(params)[:1200]}\n"
                f"What the agent learned: {json.dumps(rec.get('aronui', {}))[:400]} "
                f"pitfalls={json.dumps([p.get('text') for p in rec.get('tuatea', {}).get('pitfalls', [])])[:400]}\n"
                f"Already-tried inputs (avoid repeats): {json.dumps([a.get('intent') for a in all_attempts[-8:]])[:600]}")},
        ]
        iraw = tool.query_llm(invent_prompt, max_tokens=_MAX_TOKENS, temperature=0.7) or ""
        cargs = (_extract_json(iraw) or {}).get("args")
        cargs = cargs if isinstance(cargs, dict) else {}
        # Error path: the judge's challenge input must also be forced invalid (no real side effect,
        # no spurious halt) -- same rule as the learn/validate probes.
        if mode == "error_path":
            cargs = _force_invalid(cargs, params)

        # 2) The agent predicts the outcome for the judge's fixed input.
        predict_prompt = [
            {"role": "system", "content": (
                "Predict what this EXACT tool call will do BEFORE it runs, using what you have "
                "learned. Respond ONLY with JSON: {\"predicted_outcome\": \"success\" | \"error\", "
                "\"predicted\": \"<one sentence: what you expect / which error>\"}.")},
            {"role": "user", "content": (
                f"Tool: {tool_name}\nDescription: {description}\n"
                f"Call args (fixed by the judge): {json.dumps(cargs)[:800]}\n"
                f"What you have learned (use it to predict): {_knowledge_brief()}")},
        ]
        praw = tool.query_llm(predict_prompt, max_tokens=_MAX_TOKENS, temperature=0.3) or ""
        pplan = _extract_json(praw) or {}
        predicted_outcome = str(pplan.get("predicted_outcome") or "").lower()
        if predicted_outcome not in ("success", "error"):
            predicted_outcome = "success"
        predicted = _safe(pplan.get("predicted") or praw, 200)

        # 3) execute + 4) judge grades
        result = _probe(tool_name, cargs)
        result_s = result if isinstance(result, str) else str(result)
        actual_outcome = _classify_actual(result_s)
        jv = _judge(cargs, predicted_outcome, predicted, result_s)
        verdict, reason = jv["verdict"], jv["reason"]
        ok = (verdict == "pass")

        attempt = {
            "intent": _safe(json.dumps(cargs), 200),
            "predicted": f"{predicted_outcome}: {predicted}",
            "actual": f"{actual_outcome}: {_safe(result_s, 200)}",
            "match": ok, "phase": "challenge", "verdict": verdict, "judge_reason": reason,
            "scenario_by": "judge",
        }
        all_attempts.append(attempt)
        rec["predict_records"].append(attempt)
        rec["uses"] = (rec.get("uses", 0) or 0) + 1
        if actual_outcome == "success":
            rec["success"] = (rec.get("success", 0) or 0) + 1
        else:
            rec["fail"] = (rec.get("fail", 0) or 0) + 1
        _emit({"event": "attempt", "phase": "challenge", "match": ok, "i": len(all_attempts),
               "hits": sum(1 for a in all_attempts if a.get("match")),
               "predicted_outcome": predicted_outcome, "actual_outcome": actual_outcome,
               "verdict": verdict, "reason": reason, "intent": attempt["intent"], "actual": attempt["actual"]})
        if mode == "error_path" and actual_outcome == "success":
            rec.setdefault("tuatea", {}).setdefault("pitfalls", []).append({
                "text": "WARNING: a judge challenge input was unexpectedly accepted (possible side effect); halted.",
                "source": "whare_wananga", "seen": 1})
            state["halted"] = True
            _emit({"event": "halt", "reason": "challenge input not rejected"})
        return ok

    def _declare_and_finish(canary_note=None) -> dict:
        """Learn from the DECLARATION only (description + schema, no further probing) and return the
        summary. Used when a tool can't be probed safely: no required fields, a forced-invalid
        canary was accepted, or a probe was unexpectedly accepted mid-run (the tool validates
        inconsistently -- rejects {} but accepts a partial call). `_distil` overwrites any halt
        warning, so the baskets come out clean."""
        _emit({"event": "declare", "tool": tool_name, "canary": canary_note})
        _distil()
        learned = _has_knowledge()
        rec["status"] = "confirmed" if learned else "draft"
        rec["challenge_passed"] = False
        rec["learn_mode"] = "declare"
        rec["confidence"] = 0.0  # not measured -- learned from the declaration, not probed
        store.save(rec)
        summary = {"ok": True, "tool": tool_name, "mode": "declare", "declared": True,
                   "confirmed": learned, "challenge_passed": False, "halted": False,
                   "rounds": 0, "attempts": 0, "hits": 0, "confidence": 0.0, "status": rec["status"]}
        _emit({"event": "done", **summary})
        return summary

    # --- Phase 0: decide whether this side-effecting tool can be PROBED safely at all ---
    # A reversible tool is normally learned via the error path (forced-invalid probes the tool
    # rejects). Two cases can't be probed safely and instead learn from the DECLARATION only
    # (description + schema, no execution -> no side effect, no scary halt):
    #   (a) NO required fields  -> nothing to invalidate, and such tools rarely reject input.
    #   (b) CANARY accepted     -> the tool DECLARES required fields but doesn't enforce them (it
    #       creates with defaults on empty); a single forced-invalid canary that is NOT rejected
    #       proves probing would just be accepted side effects.
    _required_fields = (params.get("required") if isinstance(params, dict) else None) or []
    declare_only = (mode == "error_path" and not _required_fields)
    canary_note = None
    if mode == "error_path" and not declare_only:
        canary = _probe(tool_name, _force_invalid({}, params))
        if _classify_actual(canary) != "error":
            declare_only = True
            canary_note = _safe(canary, 200)   # the tool accepted an invalid call -> can't probe

    if declare_only:
        return _declare_and_finish(canary_note)

    # --- Phase 0b: prerequisites ("plan first") — run the tool's declared setup tools ONCE so
    #     the precondition is in place before probing. Only for full/none probing (error-path
    #     probes are forced invalid and rejected, so a precondition wouldn't change anything). ---
    if mode == "probe" and prereqs:
        _emit({"event": "prep_start", "prereqs": prereqs})
        for pname in prereqs:
            ptool = _all_tools.get(pname)
            prep_prompt = [
                {"role": "system", "content": (
                    "You are preparing a PREREQUISITE so the target tool can then be exercised. Make "
                    "ONE valid call to the prerequisite tool that establishes the state the target "
                    "needs (e.g. set a plan). Respond ONLY with JSON: {\"args\": {<args>}}.")},
                {"role": "user", "content": (
                    f"Prerequisite tool: {pname}\nDescription: {getattr(ptool, 'description', '') or ''}\n"
                    f"Parameters: {json.dumps(_tool_params(ptool))[:800]}\n"
                    f"Target tool to be learned next: {tool_name} -- {description}")},
            ]
            praw = tool.query_llm(prep_prompt, max_tokens=_MAX_TOKENS, temperature=0.3) or ""
            pargs = (_extract_json(praw) or {}).get("args")
            pargs = pargs if isinstance(pargs, dict) else {}
            presult = _probe(pname, pargs)
            _emit({"event": "prep", "tool": pname, "intent": _safe(json.dumps(pargs), 200),
                   "actual": _safe(presult, 200)})

    # --- Phase 1: initial learning (build the tool_knowledge before the final test) ---
    for _ in range(learn_n):
        if state["halted"]:
            break
        _one_probe("learn")
    if state["halted"]:
        # A forced-invalid probe was unexpectedly accepted mid-run -> the tool rejected the {} canary
        # but accepts a partial/edge invalid call (inconsistent validation). It can't be probed
        # safely, so fall back to declaration learning instead of leaving a halted draft.
        return _declare_and_finish()
    _distil()

    # GATE: the 21 learning probes exist to PRODUCE the tool_knowledge file. If distillation
    # yielded nothing, there is nothing for the judge to validate against -- stop here and
    # surface it as a bug instead of running a meaningless validation phase against an empty file.
    if not state["halted"] and not _has_knowledge():
        rec["status"] = "draft"
        store.save(rec)
        summary = {"ok": False, "tool": tool_name, "mode": mode, "no_knowledge": True,
                   "attempts": len(all_attempts), "status": "draft",
                   "error": ("No tool_knowledge was distilled from the learning probes "
                             "(the three baskets are empty) -- stopped before the judge. "
                             "Inspect _distil_debug on the record.")}
        _emit({"event": "error", "reason": "no_knowledge", **summary})
        return summary

    # --- Phases 2/3: validate (9) -> refine (6) until a full validation passes ---
    confirmed = False
    rounds = 0
    while not state["halted"] and rounds < max_rounds:
        rounds += 1
        _emit({"event": "validate_start", "round": rounds, "n": validate_n})
        vhits = 0
        for _ in range(validate_n):
            if state["halted"]:
                break
            if _one_probe("validate"):
                vhits += 1
        _emit({"event": "validate_result", "round": rounds, "hits": vhits, "n": validate_n})
        if not state["halted"] and vhits == validate_n:
            confirmed = True
            break
        # Stage 2 failed. Update the document with everything so far BEFORE repeating Stage 1,
        # so the refine probes work from corrected knowledge; refine; then update AGAIN before the
        # Stage 2 retry, so the next validation predicts from the freshest document.
        if not state["halted"]:
            _distil()
        for _ in range(refine_n):
            if state["halted"]:
                break
            _one_probe("learn")
        if not state["halted"]:
            _distil()

    # --- Phase 4: the JUDGE's challenge (only after a clean 9/9). The judge invents the inputs
    #     so the agent can't self-select easy probes -- the final test of true understanding.
    #     Need `challenge_pass` passes; `challenge_round_fails` fails in a round trigger a re-distil
    #     + a fresh round; `challenge_max_fails` total fails -> give up (not truly learned). ---
    challenge_passed = False
    challenge_fails = 0
    if confirmed and not state["halted"]:
        _emit({"event": "challenge_start", "need": challenge_pass, "max_fails": challenge_max_fails})
        while not state["halted"] and not challenge_passed and challenge_fails < challenge_max_fails:
            round_pass = round_fail = 0
            while (round_pass < challenge_pass and round_fail < challenge_round_fails
                   and challenge_fails < challenge_max_fails and not state["halted"]):
                if _challenge_probe():
                    round_pass += 1
                else:
                    round_fail += 1
                    challenge_fails += 1
                _emit({"event": "challenge_progress", "round_pass": round_pass,
                       "round_fail": round_fail, "total_fails": challenge_fails})
            if round_pass >= challenge_pass:
                challenge_passed = True
            elif round_fail >= challenge_round_fails and challenge_fails < challenge_max_fails and not state["halted"]:
                _distil()  # 3 fails in a round -> update the file, then a fresh challenge round
            _emit({"event": "challenge_round", "round_pass": round_pass, "round_fail": round_fail,
                   "total_fails": challenge_fails, "passed": challenge_passed})
        _emit({"event": "challenge_result", "passed": challenge_passed, "total_fails": challenge_fails})

    # --- finalize ---
    done = len(all_attempts)
    hits_total = sum(1 for a in all_attempts if a.get("match"))
    rec["confidence"] = round((hits_total / done) if done else 0.0, 2)
    rec["status"] = "confirmed" if confirmed else "draft"
    rec["challenge_passed"] = challenge_passed
    rec["source"] = "whare_wananga"
    rec["learn_mode"] = mode
    rec["rounds"] = rounds
    store.save(rec)

    summary = {"ok": True, "tool": tool_name, "mode": mode, "rounds": rounds,
               "confirmed": confirmed, "challenge_passed": challenge_passed,
               "challenge_fails": challenge_fails, "halted": state["halted"], "attempts": done,
               "hits": hits_total, "confidence": rec["confidence"], "status": rec["status"]}
    _emit({"event": "done", **summary})
    return summary
