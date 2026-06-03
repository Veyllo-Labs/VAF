"""
Whare Wananga -- predict-then-verify learning runner (core loop).

For a probe-safe tool, the model PREDICTS the tool's reaction (success or a specific error)
for a call it chooses, the tool is executed, prediction is compared to reality, and this
repeats a few times. "Learned" = predictions stop being wrong. The attempts are then
distilled into the three baskets (aronui / tuatea / tuarua) and persisted via the store.

Two modes by side_effect_class: probe-safe tools (== "none") are exercised normally; side-
effecting tools are learned via the ERROR/VALIDATION path -- probed ONLY with invalid/
incomplete inputs that the tool rejects before acting, so no real effect occurs (training
halts if an invalid probe is unexpectedly accepted). Uses the agent to execute tools and
the tool's query_llm for LLM calls (works for API + local providers).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from vaf.whare_wananga import store

_ERROR_MARKERS = (
    "[error]", "unknown action", "failed to", "not found", "invalid", "no such",
    "missing required", "traceback", "exception", "permission denied", "could not",
)


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE)
    cand = m.group(1) if m else None
    if not cand:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e != -1 and e > s:
            cand = text[s:e + 1]
    if not cand:
        return None
    try:
        return json.loads(cand)
    except Exception:
        return None


def _classify_actual(result: str) -> str:
    """Heuristic: did the tool call succeed or error?"""
    low = (result or "").lower()
    return "error" if any(mk in low for mk in _ERROR_MARKERS) else "success"


def _safe(s: Any, n: int = 400) -> str:
    return str(s if s is not None else "")[:n]


def _tool_params(tool) -> Any:
    return (getattr(tool, "parameters", None)
            or getattr(tool, "input_schema", None)
            or getattr(tool, "args_schema", None)
            or {})


def train_tool(agent, tool_name: str, max_attempts: int = 21,
               progress: Optional[Callable[[dict], None]] = None) -> Dict[str, Any]:
    """Run the predict-then-verify loop for one tool (probe mode or error/validation mode)."""
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
    # Safety tiering by side_effect_class:
    #   none         -> probe normally (real success observed).
    #   reversible   -> ERROR-PATH: probe only with invalid/incomplete inputs the tool rejects
    #                   before acting (no real effect); a stray effect would be reversible anyway.
    #   irreversible -> GATED: a single accepted probe could be a real irreversible effect
    #                   (sent mail / payment / deletion) -> never probed here.
    if sec == "none":
        mode = "probe"
    elif sec == "reversible":
        mode = "error_path"
    else:
        return {"ok": False, "tool": tool_name, "skipped": True,
                "reason": f"side_effect_class={sec}: irreversible tools are not probed (gated for safety)"}

    description = getattr(tool, "description", "") or ""
    params = _tool_params(tool)
    schema_hash = store.compute_tool_hash(tool)

    # Training sandbox: the trainer may only execute the tool being trained and its
    # connection-class siblings (e.g. other whatsapp_* tools) -- never arbitrary tools.
    from vaf.whare_wananga.preconditions import tool_class
    allowed = tool_class(tool_name, list(getattr(agent, "tools", {}).keys()))

    def _probe(name: str, args: dict) -> str:
        if name not in allowed:
            return f"[Error] training sandbox: '{name}' is outside the class of '{tool_name}'"
        try:
            return agent.execute_tool(name, args)
        except Exception as e:
            return f"[Error] {e}"

    rec = store.load(tool_name) or store.new_record(
        tool_name, side_effect_class=sec, tool_schema_hash=schema_hash, source="whare_wananga")
    rec["tool_schema_hash"] = schema_hash
    rec["status"] = "learning"
    rec.setdefault("predict_records", [])

    attempts: List[dict] = []
    hits = 0
    halted = False
    _emit({"event": "start", "tool": tool_name, "max_attempts": max_attempts, "mode": mode})

    for i in range(max_attempts):
        if mode == "probe":
            _sys = (
                "You are probing a tool to learn how it behaves. Choose ONE concrete call to make "
                "and PREDICT its outcome BEFORE it runs. Vary probes across attempts: try valid inputs "
                "AND deliberately invalid/edge inputs to learn the tool's error behaviour. "
                "Respond ONLY with JSON: {\"args\": {<tool args>}, "
                "\"predicted_outcome\": \"success\" | \"error\", "
                "\"predicted\": \"<one sentence: what you expect / which error>\"}.")
        else:
            _sys = (
                "This tool HAS SIDE EFFECTS, so you must NEVER make a real or valid call. Probe ONLY "
                "with INVALID or INCOMPLETE inputs -- omit required arguments, use empty or wrong-type "
                "values -- so the tool REJECTS the call with a validation error BEFORE doing anything. "
                "The goal is to learn the argument contract and error messages safely. PREDICT the "
                "rejection. Respond ONLY with JSON: {\"args\": {<invalid/incomplete args>}, "
                "\"predicted_outcome\": \"error\", \"predicted\": \"<which validation error you expect>\"}.")
        predict_prompt = [
            {"role": "system", "content": _sys},
            {"role": "user", "content": (
                f"Tool: {tool_name}\nDescription: {description}\n"
                f"Parameters: {json.dumps(params)[:1200]}\n"
                f"Attempt {i + 1}/{max_attempts}. Earlier attempts: {json.dumps(attempts)[:1000]}")},
        ]
        raw = tool.query_llm(predict_prompt, max_tokens=400, temperature=0.4) or ""
        plan = _extract_json(raw) or {}
        args = plan.get("args") if isinstance(plan.get("args"), dict) else {}
        predicted_outcome = str(plan.get("predicted_outcome") or "").lower()
        if predicted_outcome not in ("success", "error"):
            predicted_outcome = "success"
        predicted = _safe(plan.get("predicted") or raw, 200)

        result = _probe(tool_name, args)
        actual_outcome = _classify_actual(result if isinstance(result, str) else str(result))

        match = (predicted_outcome == actual_outcome)
        if match:
            hits += 1

        attempt = {
            "intent": _safe(json.dumps(args), 200),
            "predicted": f"{predicted_outcome}: {predicted}",
            "actual": f"{actual_outcome}: {_safe(result, 200)}",
            "match": match,
        }
        attempts.append(attempt)
        rec["predict_records"].append(attempt)
        rec["uses"] = (rec.get("uses", 0) or 0) + 1
        if actual_outcome == "success":
            rec["success"] = (rec.get("success", 0) or 0) + 1
        else:
            rec["fail"] = (rec.get("fail", 0) or 0) + 1

        _emit({"event": "attempt", "i": i + 1, "max": max_attempts, "match": match,
               "predicted_outcome": predicted_outcome, "actual_outcome": actual_outcome, "hits": hits})

        if mode == "error_path" and actual_outcome == "success":
            # An invalid probe was NOT rejected -> the tool may have performed a real action.
            # Stop immediately to avoid repeated side effects.
            rec.setdefault("tuatea", {}).setdefault("pitfalls", []).append({
                "text": "WARNING: an invalid probe was not rejected (possible real side effect); training halted.",
                "source": "whare_wananga", "seen": 1})
            halted = True
            _emit({"event": "halt", "i": i + 1, "reason": "invalid probe not rejected"})
            break

    # Distil the three baskets from the attempts.
    distil_prompt = [
        {"role": "system", "content": (
            "Summarise how to correctly operate this tool, learned from the probe attempts. "
            "Respond ONLY with JSON: {\"aronui\": {\"when_to_use\": str, \"output_shape\": str}, "
            "\"tuatea\": {\"pitfalls\": [str]}, "
            "\"tuarua\": {\"procedure\": [str], \"verification\": [str]}}.")},
        {"role": "user", "content": (
            f"Tool: {tool_name}\nDescription: {description}\n"
            f"Parameters: {json.dumps(params)[:1000]}\n"
            f"Attempts:\n{json.dumps(attempts, indent=2)[:2500]}")},
    ]
    draw = tool.query_llm(distil_prompt, max_tokens=700, temperature=0.3) or ""
    d = _extract_json(draw) or {}
    if isinstance(d.get("aronui"), dict):
        a = d["aronui"]
        rec["aronui"]["when_to_use"] = _safe(a.get("when_to_use"), 400) or rec["aronui"].get("when_to_use", "")
        rec["aronui"]["output_shape"] = _safe(a.get("output_shape"), 400) or rec["aronui"].get("output_shape", "")
    if isinstance(d.get("tuatea"), dict) and isinstance(d["tuatea"].get("pitfalls"), list):
        for p in d["tuatea"]["pitfalls"][:10]:
            rec["tuatea"]["pitfalls"].append({"text": _safe(p, 200), "source": "whare_wananga", "seen": 1})
    if isinstance(d.get("tuarua"), dict):
        t = d["tuarua"]
        if isinstance(t.get("procedure"), list):
            rec["tuarua"]["procedure"] = [_safe(s, 200) for s in t["procedure"][:12]]
        if isinstance(t.get("verification"), list):
            rec["tuarua"]["verification"] = [_safe(s, 200) for s in t["verification"][:12]]

    # "Learned" = predictions converged (over the attempts actually made).
    done = len(attempts)
    rate = (hits / done) if done else 0.0
    rec["confidence"] = round(rate, 2)
    rec["status"] = "confirmed" if (not halted and done >= 3 and rate >= 0.8) else "draft"
    rec["source"] = "whare_wananga"
    rec["learn_mode"] = mode
    store.save(rec)

    summary = {"ok": True, "tool": tool_name, "attempts": done, "hits": hits, "mode": mode,
               "halted": halted, "confidence": rec["confidence"], "status": rec["status"]}
    _emit({"event": "done", **summary})
    return summary
