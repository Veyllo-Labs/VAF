# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Agent Workflow Builder
======================
Gives the main agent the ability to create and run workflows at runtime.

Two modes
---------
  run_temp — Ephemeral: define a multi-step plan, execute it immediately,
             then discard. No file written. Ideal for one-off complex tasks.
             Available to the agent in any session (not admin-only).

  create   — Persistent: save a workflow to ~/.vaf/workflows/ so it appears
             in the WebUI and can be re-used later. Admin-only (same gate as
             create_agent_tool so regular users cannot write arbitrary code).

  list     — List agent-created persistent workflows.
  delete   — Remove an agent-created persistent workflow. Admin-only.

How _agent is injected
-----------------------
execute_tool() in agent.py injects self as tool_args["_agent"], matching the
pattern used by create_agent_tool and python_sandbox. Because run() uses
**kwargs, Python does NOT automatically assign kwargs["_agent"] to
self._agent — run() must do this explicitly at the top:

    _injected = kwargs.get("_agent")
    if _injected is not None:
        self._agent = _injected

This gives the tool access to:
  - self._agent.tools     → full live tool registry for run_temp execution
  - self._agent._current_user_role / _current_user_scope_id → admin check

run_temp execution
-------------------
  1. Build WorkflowStep objects from the step dicts the agent provides.
  2. Collect the registry: the workflow primitives with self._agent.tools on top.
  3. Check every step before anything starts (_preflight_steps): one refusal
     naming all steps that cannot run, in the same turn.
  4. Run in the background, or WorkflowEngine.execute() synchronously.
  5. Return the result string.

Nothing is written to disk. The in-memory WORKFLOW_TEMPLATES dict is not
modified either — the engine receives the steps directly.

Persistent workflow files
--------------------------
  Written to ~/.vaf/workflows/{workflow_id}.py.
  First line: # created_by: agent
  Content: WORKFLOW = { ... }   (a Python literal, templates.write_workflow_file)
  The agent may only edit/delete files it created (first-line check).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional

from vaf.core.web_ticker import MirroredStdout
from vaf.tools.base import BaseTool


# Per-step weak-model repair (the generic input-repair layer only remaps
# TOP-LEVEL argument aliases; nested step objects need their own). Live
# incident: a model authored {"action": "web_search", "description": "Wetter
# suchen (Berlin)", "name": "step_1_wetter"} - tool named in 'action',
# instruction in 'description', no 'input' - and the old nested schema
# requirement rejected the whole call ("'input' is a required property"),
# after which the model never recovered the thread.
_STEP_INPUT_ALIASES = ("input", "code", "task", "prompt", "instruction", "query",
                       "description", "name")


def _repair_raw_step(s: dict) -> dict:
    """Return a shallow copy of one raw step with weak-model field shapes
    repaired: tool <- tool|agent_id|action (step-level 'action' is unambiguous;
    the run_temp/create discriminator lives on the TOP level), input <- the
    first non-empty alias in _STEP_INPUT_ALIASES; an args-only step gets a
    synthesized label so the engine's args_template path can run it. A step
    with truly nothing usable keeps input empty and the caller errors as
    before. Pure."""
    out = dict(s or {})
    if not out.get("tool"):
        alias = out.get("agent_id") or out.get("action")
        if isinstance(alias, str) and alias.strip():
            out["tool"] = alias.strip()
    if not str(out.get("input") or "").strip():
        for key in _STEP_INPUT_ALIASES:
            v = out.get(key)
            if isinstance(v, dict):
                v = json.dumps(v, ensure_ascii=False)
            if isinstance(v, str) and v.strip():
                out["input"] = v.strip()
                break
        else:
            if isinstance(out.get("args"), dict) and out["args"]:
                out["input"] = f"Run {out.get('tool') or 'coding_agent'}"
    return out


# Step tool names a model writes that are not tools, or are tools a workflow should not
# run, mapped to the tool that takes the SAME arguments (tests/test_workflow_step_preflight.py
# checks that for every real source tool). python_exec takes `code` like python_sandbox, and
# the sandbox is the lane a workflow may run without asking, while python_exec refuses there
# without the user's grant. `bash` is not here: it is a workflow tool of its own, and
# mapping it to the sandbox kept its `command` for a tool that needs `code`, so every such
# step failed in the background (live incident).
_STEP_TOOL_ALIASES = {
    "python_exec": "python_sandbox",
    "python":      "python_sandbox",
    "search":      "web_search",
    "write":       "write_file",
    "read":        "read_file",
}


def _resolve_step_tool(tool_val: Any) -> str:
    """The tool a step names, with the aliases above applied; coding_agent when it names none."""
    tool_str = str(tool_val or "").strip() or "coding_agent"
    return _STEP_TOOL_ALIASES.get(tool_str.lower(), tool_str)


_HOST_BASH_REASON = ("a workflow runs its steps without asking the user, and host_bash needs "
                     "the user's OK for every command.")
_HOST_BASH_INSTEAD = ("Call host_bash directly in the chat instead, one call per command (the "
                      "user confirms each).")
_HOST_BASH_UNAVAILABLE = ("host_bash is not available to you here; a command without network can "
                          "be a bash step, otherwise tell the user to run it.")


def _offered(*names: str) -> tuple:
    """The named tools the caller of this run may use, handed to its turn (`advise_tools`).

    Outside a tool call (the `@workflow` lane, `vaf workflow run`, a direct run) nobody can be
    handed anything and every name counts as offered, so the wording stays what it was."""
    from vaf.core.tool_dispatch import advise_tools
    accepted = advise_tools(*names)
    return tuple(names) if accepted is None else accepted


def _host_bash_instead() -> str:
    """What to do instead of a host_bash step: call it in the chat, when this caller may."""
    return _HOST_BASH_INSTEAD if "host_bash" in _offered("host_bash") else _HOST_BASH_UNAVAILABLE


# The chat funnel cuts a result at 2000 characters (ToolCaller max_result_chars), from the
# middle: a refusal that is longer loses exactly the commands it lists. Kept under it.
_REFUSAL_BUDGET = 1900
# Step numbers named in one sentence before "and N more": a plan with hundreds of network
# steps must not spend the budget on numbers.
_STEP_NUMBERS_SHOWN = 30
# The sandbox's own default is 30 s and its clamp 600 s (PythonSandboxTool._run_timeout); an
# install or a clone takes longer than 30 s, so the example asks for room per command.
_SANDBOX_SECONDS_BASE = 60
_SANDBOX_SECONDS_PER_COMMAND = 45
_SANDBOX_SECONDS_MAX = 600


def _step_numbers(steps: List[str]) -> str:
    """"1, 2, 3", or "1, 2, ... 30 and 970 more" past `_STEP_NUMBERS_SHOWN`."""
    shown = ", ".join(steps[:_STEP_NUMBERS_SHOWN])
    more = len(steps) - _STEP_NUMBERS_SHOWN
    return f"{shown} and {more} more" if more > 0 else shown


def _sandbox_timeout(count: int) -> int:
    return min(_SANDBOX_SECONDS_MAX, _SANDBOX_SECONDS_BASE + _SANDBOX_SECONDS_PER_COMMAND * count)


def _sandbox_code(commands: List[str]) -> str:
    """Runs EVERY command, never stopping at a failed one, and prints one line per command:
    OK or FAILED, the exit code, the command and the end of its stderr. So
    the model reports each command as it came back (a repeated clone fails from the second
    one on) instead of stopping at the first failure or calling all of them done.

    A command's output is decoded with ``errors='replace'`` (bytes that are not UTF-8 raised
    UnicodeDecodeError and ended the run), any other exception is a FAILED line of its own,
    and every line is flushed, so the lines already printed survive a timeout of the call.
    The command and the stderr tail are printed as repr(), so a newline in either cannot
    split one command's result over two lines ("a missing line" must mean a missing command)."""
    return ("import subprocess\n"
            "def run(c):\n"
            " try:\n"
            "  r = subprocess.run(c, shell=True, capture_output=True, text=True, errors='replace')\n"
            "  print('OK' if r.returncode == 0 else 'FAILED', r.returncode, repr(c),"
            " repr(r.stderr[-200:].strip()), flush=True)\n"
            " except Exception as e:\n"
            "  print('FAILED', '-', repr(c), repr(str(e)), flush=True)\n"
            + "".join(f"run({c!r})\n" for c in commands))


def _host_call(command: str) -> str:
    """One host_bash call as the model should write it: the command JSON-escaped, so a quote,
    a trailing backslash or a newline in it stays one valid argument."""
    return "`host_bash(command=" + json.dumps(command, ensure_ascii=False) + ")`"


def _network_instruction(commands: List[str], steps: List[str], offered: tuple, *,
                         listed: Optional[int] = None, host_listed: bool = True) -> str:
    """The one instruction for the bash steps that need the network.

    python_sandbox comes first: it has network and asks nothing, and most of these commands
    (an install, a clone to work on) need no result on the user's disk. host_bash is the way
    when the result must land there, and it asks the user per command. Only the tools in
    ``offered`` (what this caller may use, already advised) are named.

    Every command is spelled out, one ``run(...)`` per command in the sandbox code and one
    host_bash call per command, because a weak model copies the example it is shown and,
    shown only the first command, runs that one and reports all of them done. ``listed`` and
    ``host_listed`` shorten it only when the whole refusal would not fit the funnel's cut
    (`_preflight_steps` asks for the longest that fits): host_bash then refers to the sandbox
    code's order; past ``listed`` the steps are named by number; with ``listed=0`` no command
    text is shown at all (one command longer than the budget) and the commands are named by
    their step numbers only."""
    total = len(commands)
    listed = total if listed is None else max(0, min(listed, total))
    shown = commands[:listed]
    several = total > 1
    them = "them" if several else "it"
    timeout = _sandbox_timeout(total)
    rest = ""
    if 0 < listed < total:
        rest = (f" Then the commands of steps {_step_numbers(steps[listed:])} the same way, one "
                "per command: they are in your plan.")
    keeps = ("python_sandbox has network and asks nothing, but what it installs or downloads "
             "stays in the sandbox and never reaches the user's computer")
    if listed:
        sandbox_call = ("`python_sandbox(code=" + json.dumps(_sandbox_code(shown), ensure_ascii=False)
                        + f", timeout={timeout})`")
        sandbox = (f"{keeps}; one call runs {'all of them' if several else 'it'} and prints OK or "
                   f"FAILED per command, report each line as it came back (a missing line means that "
                   f"command did not finish): {sandbox_call}." + rest)
        host_calls = ", ".join(_host_call(c) for c in shown)
    else:
        sandbox = (f"{keeps}; one call runs all of them, with the commands of steps "
                   f"{_step_numbers(steps)} from your plan in a list, each run with "
                   "subprocess.run(c, shell=True, capture_output=True, text=True) and its result "
                   f"printed as OK or FAILED, timeout={timeout}.")
        host_calls = ""
    if "python_sandbox" in offered and "host_bash" in offered:
        if not listed:
            host = ("call host_bash instead, one call per command, with each command from your "
                    "plan; the user confirms each.")
        elif not several:
            host = f"call {_host_call(commands[0])} instead; the user confirms it."
        elif host_listed:
            host = ("call host_bash instead, one call per command and in this order; the user "
                    f"confirms each: {host_calls}." + rest)
        else:
            host = ("call host_bash instead, once per command in the order above, from "
                    f"{_host_call(commands[0])} on; the user confirms each.")
        return (f"Run {them} outside the workflow. {sandbox} If the result must land on the "
                f"user's computer, {host}")
    if "python_sandbox" in offered:
        return (f"Run {them} outside the workflow. {sandbox} Only if the user needs the result on "
                "their own computer, say that you cannot put it there from here.")
    if "host_bash" in offered:
        if not listed:
            return (f"Run {them} outside the workflow with host_bash, one call per command, with "
                    f"the commands of steps {_step_numbers(steps)} from your plan in this order; "
                    "the user confirms each.")
        if not several:
            return (f"Run it outside the workflow with host_bash; the user confirms it: "
                    f"{_host_call(commands[0])}.")
        return ("Run them outside the workflow with host_bash, one call per command and in this "
                f"order; the user confirms each: {host_calls}." + rest)
    return ("Neither python_sandbox nor host_bash is available to you here, so tell the user to "
            + ("run these commands themselves." if several else "run this command themselves."))


def _fitted_network_refusal(problems: List[str], commands: List[str], steps: List[str],
                            offered: tuple, others: bool) -> str:
    """The longest network refusal under `_REFUSAL_BUDGET`: every command in both tools, then
    host_bash pointing at the sandbox code's order with as many commands spelled out as fit
    (a binary search), then step numbers only. Every command with host_bash pointing at the
    sandbox code is tried before the search, because the length is not monotonic at the top:
    with all but one shown, the "Then the commands of steps ..." sentence makes the refusal
    LONGER than with all of them (measured on the live incident's plan: 19 shown did not fit
    where 20 did, and the search settled on 18)."""
    def render(listed: int, host_listed: bool) -> str:
        return _refusal(problems, _network_instruction(
            commands, steps, offered, listed=listed, host_listed=host_listed), others=others)

    for host_listed in (True, False):
        full = render(len(commands), host_listed)
        if len(full) <= _REFUSAL_BUDGET:
            return full
    best, low, high = render(0, False), 1, len(commands)
    while low <= high:
        mid = (low + high) // 2
        text = render(mid, False)
        if len(text) <= _REFUSAL_BUDGET:
            best, low = text, mid + 1
        else:
            high = mid - 1
    return best


def _refusal(problems: List[str], instruction: Optional[str] = None, *,
             others: bool = False) -> str:
    """The one in-turn refusal for a plan: nothing ran, one line per step, one instruction.

    With a network ``instruction`` the refusal ends with it, and "call create_agent_workflow
    again" follows only when ``others`` (other steps to fix) - a plan whose only problem is
    the network would be sent straight back into this refusal."""
    head = ("Error: nothing ran, the workflow plan has steps that cannot run:\n"
            + "\n".join(f"- {p}" for p in problems))
    if instruction is None:
        return head + "\nFix these steps and call create_agent_workflow again."
    return (head + "\n" + instruction
            + ("\nFix the other steps and call create_agent_workflow again." if others else ""))


def _preflight_steps(normalised: List[dict], tools: Dict[str, Any], *,
                     unknown: Optional[List[str]] = None) -> Optional[str]:
    """Check a plan before anything runs; the refusal for ALL broken steps, or None.

    A run_temp workflow usually runs in the background, so a step that cannot run used to
    fail there after the turn had ended, and the model learned of it a turn later and
    invented the results (live incident). The questions asked are the dispatcher's own: the
    step's args are built the way the engine builds them (`build_step_args`, with the
    `{placeholders}` left literal) and checked against the tool's schema the way dispatch
    checks them (`repair_tool_input`), so a step is refused here only for what would refuse
    it at run time anyway. Only a MISSING required field refuses: a placeholder can fail a
    type check that its value passes once resolved.

    Refused as well, because no repair makes them runnable: a tool that does not exist; a
    `bash` step that downloads (the workflow shell has no network, and the networked shell
    asks a person); a `host_bash` step, because this lane runs its steps without asking
    the user while host_bash in chat asks for every command; and a step whose output name the
    engine owns (`engine.ENGINE_OUTPUT_NAMES`), which the engine refuses as well.

    The way out a refusal names is a tool this caller may use, and it is handed to the turn
    (`advise_tools`): the network steps share one line and one instruction
    (`_network_instruction`), and a host_bash step points at host_bash in the chat only when
    the caller may run it. Called outside a tool call, every tool counts as offered.

    With an ``unknown`` list (the `create` action), a tool this registry lacks is appended
    there instead of refused: a saved workflow may run in another lane whose registry has it
    (the automation lane's channel tools). A temporary workflow runs here and now, so it
    refuses.

    NAMED BOUNDARY: a private helper, not on the facade. Measured: this module (its two
    actions) is the only planner that checks steps before they run; the facade's ToolCaller
    applies the same schema check at dispatch, so an embedder's own plan fails there with the
    same words. A second planner is the trigger for a public primitive.
    """
    from vaf.core.tool_dispatch import unknown_tool_hint
    from vaf.core.tool_input_repair import MISSING_REQUIRED, RETRY_SAME_CALL, repair_tool_input
    from vaf.tools.bash import command_needs_network
    from vaf.workflows.engine import build_step_args, reserved_output_problem

    problems: List[str] = []
    network: List[str] = []         # the commands of the bash steps that need the network
    network_steps: List[str] = []
    network_at = -1                 # where their one line goes: at the first such step
    for i, step in enumerate(normalised, 1):
        name = step.get("tool") or ""
        label = f"step {i} ({name})"
        _reserved = reserved_output_problem(step.get("output"))
        if _reserved:
            problems.append(f"{label}: {_reserved}")
            continue
        if name == "host_bash":
            problems.append(f"{label}: {_HOST_BASH_REASON} {_host_bash_instead()}")
            continue
        tool = tools.get(name)
        if tool is None and unknown is not None:
            unknown.append(label)
            continue
        if tool is None:
            hint = unknown_tool_hint(name, tools)
            problems.append(f"{label}: Unknown tool '{name}'." + (f" {hint}" if hint else ""))
            continue
        try:
            args = build_step_args(name, str(step.get("input") or ""), step.get("args"),
                                   lambda text: text)
        except Exception:
            continue  # the engine reports a step it cannot build on its own
        try:
            _, _, errors = repair_tool_input(getattr(tool, "parameters", None), dict(args),
                                             getattr(tool, "input_aliases", None))
        except Exception:
            errors = []
        # One line per field: jsonschema reports a missing field once per required entry it
        # checks, and the dispatcher's own "Retry the SAME call" sentence is dropped, because
        # the refusal ends with the one instruction that fits a plan (call the builder again).
        missing = list(dict.fromkeys(
            str(e).split(RETRY_SAME_CALL, 1)[0].strip()
            for e in errors if str(e).startswith(MISSING_REQUIRED)))
        if missing:
            problems.append(f"{label}: " + " ".join(missing))
            continue
        command = args.get("command") if name == "bash" else None
        if isinstance(command, str) and command_needs_network(command):
            # One line for all of them, and nothing repeated per step: a long plan's
            # refusal was cut in the middle by the funnel and lost its own instruction.
            if network_at < 0:
                network_at = len(problems)
            network.append(command)
            network_steps.append(str(i))
    if not problems and not network:
        return None
    if not network:
        return _refusal(problems)
    if len(network) == 1:
        line = (f"step {network_steps[0]} (bash): this command needs the network, and the "
                "workflow shell has none.")
    else:
        line = (f"steps {_step_numbers(network_steps)} (bash): these commands need the network, "
                "and the workflow shell has none.")
    others = bool(problems)
    problems.insert(network_at, line)
    offered = _offered("python_sandbox", "host_bash")     # asked and advised once
    return _fitted_network_refusal(problems, network, network_steps, offered, others)


def _agent_workflow_path(workflow_id: str) -> str:
    return os.path.join(os.path.expanduser("~/.vaf/workflows"), f"{workflow_id}.py")


def is_agent_created(workflow_id: str) -> bool:
    """Whether the saved workflow ``workflow_id`` was written by this builder (its first line
    carries the `created_by: agent` marker). False when there is no such file."""
    try:
        with open(_agent_workflow_path(workflow_id), "r", encoding="utf-8") as fh:
            return "created_by: agent" in fh.readline()
    except Exception:
        return False


def agent_workflow_refusal(workflow_id: str, template: Dict[str, Any]) -> Optional[str]:
    """The refusal for running a model-written saved workflow that has a host_bash step, or None.

    `create` refuses such a step, but a workflow saved before it did still holds one, and the
    workflow lanes run their steps without the confirmation gate. Checked when one is started
    (execute_workflow), with the same message the builder gives. A person's own workflow
    file (no marker) and the built-in templates are not touched."""
    if not is_agent_created(workflow_id):
        return None
    problems = [f"step {i} (host_bash): {_HOST_BASH_REASON}"
                for i, step in enumerate((template or {}).get("steps") or [], 1)
                if isinstance(step, dict) and str(step.get("tool") or "").strip() == "host_bash"]
    if not problems:
        return None
    return ("Error: nothing ran, the saved workflow '" + workflow_id + "' has steps that cannot "
            "run:\n" + "\n".join(f"- {p}" for p in problems)
            + f"\n{_host_bash_instead()}")


class AgentWorkflowBuilderTool(BaseTool):
    name = "create_agent_workflow"
    # An orchestrator: the engine inside already bounds every step, so bounding the whole
    # run again would double-bound it.
    self_supervised = True
    category    = "workflows"
    description = (
        "Plan and run multi-step workflows. Use action='run_temp' for any complex "
        "task that needs more than one tool — it runs immediately and leaves nothing on disk.\n\n"
        "WHEN TO USE WHICH ACTION:\n"
        "  run_temp — RIGHT NOW task, ad-hoc, no saving. This is the default choice for "
        "complex multi-step work (research → analyse → write report, etc.). "
        "The workflow runs immediately and is discarded when done. "
        "Needs 2+ steps that chain outputs — a single-step workflow is rejected; "
        "for a one-off single tool call, call that tool directly instead.\n"
        "  create   — Save a workflow PERMANENTLY so it can be re-used later via "
        "execute_workflow, @workflow_id in chat, or the WebUI Workflows tab. Admin-only. "
        "Only use this if the user explicitly wants a reusable workflow.\n"
        "  list     — Show workflows the agent has saved.\n"
        "  delete   — Remove a saved workflow.\n\n"
        "DO NOT use action='execute' — that does not exist. "
        "To run something NOW use action='run_temp'. "
        "To run a previously saved workflow use the separate execute_workflow tool.\n\n"
        "AUTOMATIC CLEANUP (run_temp only): throwaway scripts/scratch the run CREATES "
        "(.py, .sh, .js, .tmp, …) are deleted after completion (a file already in the project "
        "folder before the run is not deleted by the cleanup); the actual deliverable "
        "(documents, reports, data, images) is kept. To keep an extra file explicitly, pass "
        "keep_files=[\"/path/to/file\"]. Use write_file/document steps for the FINAL deliverable, "
        "and plain scripts for intermediate work.\n\n"
        "OUTPUT VALIDATION (run_temp AND create): on content/agent steps (document/research/coding/"
        "browser/librarian) set \"validate\": true to have the step's output LLM-checked against its "
        "goal and re-run up to 3x with a correction if it doesn't match, then accept and continue. "
        "ALWAYS flag the deliverable-producing steps - in create mode too, otherwise the saved "
        "workflow ships without any output checking. In run_temp, content/agent steps without "
        "validate flags get validation enabled AUTOMATICALLY; pass skip_validation=true to run "
        "without it on purpose.\n\n"
        "Each step needs an 'input' (supports {variable} substitution from prior steps) "
        "and a 'tool'. AVAILABLE SUB-AGENTS AND TOOLS FOR STEPS:\n"
        "  coding_agent    — Write/edit code, create HTML/CSS/JS, generate structured files, "
        "run analysis scripts. Default tool. Gets a shared project_path automatically.\n"
        "  research_agent  — Deep multi-source research (10+ sources), detailed reports, "
        "market analysis, technical deep-dives. Use for: patent research, market studies, "
        "competitive analysis. Args: topic (required), depth, language.\n"
        "  document_writer — Create simple structured documents (contracts, reports, letters) "
        "as .txt/.md/.docx ONLY; other extensions are rejected. Writes into the chat "
        "workspace. For raw files (html/svg/code) use write_file instead.\n"
        "  librarian_agent — File system operations: read/list/search files in directories. "
        "Use for: reading existing project files, finding documents.\n"
        "  web_search      — Quick web search (1-3 sources). Use for: news, facts, prices.\n"
        "  write_file      — Write raw content to a specific file path.\n"
        "  read_file       — Read a file. Use for: reading outputs from previous steps.\n"
        "  python_sandbox  — Execute Python code in the Docker sandbox. Use for: data "
        "processing, calculations, fetching/transforming data with pip tools "
        "(args.packages=['...']), and producing binary artifacts - declare them in "
        "args.export_files=['chart.png'] so they land in the chat workspace.\n\n"
        "RULE: Prefer research_agent for patent/market/technical research, "
        "coding_agent for file generation and analysis scripts. "
        "Do NOT use coding_agent for research that needs 10+ web sources.\n\n"
        "VARIABLE ANCHORING (automatic):\n"
        "The engine automatically prepends all original workflow variables as "
        "'IMMUTABLE DESIGN PILLARS' to every coding_agent/research_agent/document_writer/"
        "librarian_agent task. You MUST still reference variables in step inputs "
        "({patent_id}, {genre} etc.) — anchoring is a safety net, not a replacement.\n\n"
        "LIVING DOCUMENT PATTERN (recommended for 5+ step workflows):\n"
        "Instead of chaining {prev_step_output} through all steps (which causes drift), "
        "write a shared JSON file that all steps read and update:\n"
        "  Step 1: coding_agent → reads variables, writes /tmp/{workflow_id}/design.json\n"
        "  Step 2: coding_agent → reads design.json, adds its section, writes it back\n"
        "  Step N: coding_agent → always reads the FULL design.json (never loses Step 1 data)\n"
        "Use write_file + read_file tools for this. Avoid {prev_step_output} for large content.\n\n"
        "ASSERTIONS (output verification + selective retry):\n"
        "Each step can declare assertions to verify its output. On failure, only that step "
        "retries (with a correction hint) — not the whole workflow:\n"
        "  'assertions': [{'contains': '{patent_id}', 'error': 'Patent number missing from output'}]\n"
        "  'max_assertion_retries': 1  # default\n"
        "Supported operators: 'contains', 'not_contains'. Use {variable} in the expected value.\n\n"
        "CONSISTENCY REVIEW PATTERN (every 3-4 steps in complex workflows):\n"
        "Insert a check step that verifies original params are still honoured:\n"
        "  {'tool': 'coding_agent', 'description': 'Consistency Check',\n"
        "   'input': 'Check that {genre} and {core_mechanic} are correctly reflected in "
        "{prev_output}. Output ONLY \"OK\" if correct, else \"FAIL: [reason]\"',\n"
        "   'assertions': [{'not_contains': 'FAIL', 'error': 'Consistency check failed'}],\n"
        "   'on_failure': 'step_to_redo'}"
    )

    # ── Contract ──────────────────────────────────────────────────────────────
    # run_temp: available in any session — agent plans its own work.
    # create / delete: admin-only, enforced inside run() via _is_admin().
    permission_level  = "system"        # skip legacy confirmation gate
    side_effect_class = "reversible"    # files can be deleted; temp leaves nothing
    admin_only        = False           # run_temp works for all; create/delete checked internally
    channel_restrictions = ("channel",)

    # ── Examples ──────────────────────────────────────────────────────────────
    input_examples = [
        {
            "action": "run_temp",
            "name": "Research and summarize",
            "steps": [
                {"input": "Search for recent news about {topic}", "tool": "web_search",    "output": "news"},
                {"input": "Write a concise summary of:\n{news}",  "tool": "coding_agent",  "output": "summary"},
            ],
            "variables": {"topic": "quantum computing"},
        },
        {
            "action": "create",
            "workflow_id": "daily_brief",
            "name": "Daily Briefing",
            "description": "Searches news and writes a daily brief",
            "triggers": ["daily brief", "morning summary"],
            "steps": [
                {"input": "Search today's top tech news", "tool": "web_search",   "output": "news"},
                {"input": "Write a 3-paragraph brief:\n{news}", "tool": "coding_agent", "output": "brief"},
            ],
        },
        {"action": "list"},
    ]

    # ── Parameters ────────────────────────────────────────────────────────────
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["run_temp", "create", "delete", "list"],
                "description": (
                    "run_temp — Execute a temporary workflow plan immediately, then discard it.\n"
                    "create   — Save a reusable workflow to disk (admin-only).\n"
                    "delete   — Remove an agent-created workflow from disk (admin-only).\n"
                    "list     — Show all workflows the agent has created."
                ),
            },
            "workflow_id": {
                "type": "string",
                "description": (
                    "Lowercase snake_case identifier for the workflow. "
                    "Required for create and delete. Ignored for run_temp (auto-generated)."
                ),
            },
            "name": {
                "type": "string",
                "description": "Human-readable name (required for run_temp and create).",
            },
            "description": {
                "type": "string",
                "description": "What the workflow does. Used in create; optional for run_temp.",
            },
            "steps": {
                "type": "array",
                "description": (
                    "Ordered list of steps. Each step is an object with:\n"
                    "  input       — Prompt/instruction for this step. "
                    "Use {variable_name} for output from previous steps or initial variables; "
                    "{variable|fallback} supplies an inline default when the variable is unset "
                    "(saved workflows have no separate defaults block - use this).\n"
                    "  tool        — Tool to use (e.g. 'coding_agent', 'web_search', "
                    "'research_agent', 'python_sandbox', 'write_file'). Default: coding_agent.\n"
                    "  args        — Dict of tool parameters for MULTI-parameter tools, e.g. "
                    "python_sandbox: {\"code\": \"...\", \"packages\": [\"yt-dlp\"], "
                    "\"export_files\": [\"chart.png\"]} or write_file: {\"path\": \"out.md\", "
                    "\"content\": \"{summary}\"}. {variables} are substituted inside arg values too.\n"
                    "  output      — Variable name to store this step's result "
                    "(used in later steps as {output}). Default: step_N_output.\n"
                    "  description — Short label shown in progress output. Optional.\n"
                    "BRACE SAFETY for embedded code (python_sandbox args.code): every {...} block "
                    "in ANY step string goes through variable substitution - a block containing a "
                    "DOT is treated as a nested lookup and BREAKS the run. In embedded Python use "
                    "no f-strings and no dict/set literals whose braces contain dots; build strings "
                    "with + and use dict(...) constructors instead."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "input":       {"type": "string"},
                        "tool":        {"type": "string"},
                        "args":        {"type": "object", "description": "Tool parameters for multi-parameter tools ({variables} substituted inside values). See the steps description for examples."},
                        "output":      {"type": "string"},
                        "description": {"type": "string"},
                        "on_success":  {"type": "string", "description": "Jump to this step's output_name on success."},
                        "on_failure":  {"type": "string", "description": "Jump to this step's output_name on failure (suppresses abort)."},
                        "optional":    {"type": "boolean", "description": "Skip on failure instead of aborting."},
                        "assertions":  {
                            "type": "array",
                            "description": "Output checks — if any fail, the step retries with a correction hint.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "contains":     {"type": "string", "description": "Expected substring in output. Supports {variable}."},
                                    "not_contains": {"type": "string", "description": "Substring that must NOT appear in output."},
                                    "error":        {"type": "string", "description": "Message shown when this assertion fails."},
                                },
                            },
                        },
                        "max_assertion_retries": {"type": "integer", "description": "How many times to retry on assertion failure (default: 1)."},
                        "validate":    {"type": "boolean", "description": "Set true on content/agent steps (document/research/coding/browser/librarian) whose output must actually match the step's goal. The output is LLM-checked against the step description; on a mismatch the step is re-run with a correction hint up to 3 times, then the last version is accepted and the workflow continues."},
                    },
                    # NOTE deliberately NOT "required": ["input"]. The nested
                    # schema rejection killed the whole call pre-dispatch with
                    # "'input' is a required property" - and the live incident
                    # model, which had just done everything else right
                    # (action=run_temp, sensible steps), could not act on that
                    # message and regressed into planning spin until the loop
                    # guards ended the turn. Weak-model step shapes (tool named
                    # in 'action', instruction in 'description', args-only
                    # steps) are repaired in _repair_raw_step instead; a step
                    # with truly nothing usable still errors with a targeted
                    # message.
                },
            },
            "variables": {
                "type": "object",
                "description": (
                    "Initial variable values for run_temp "
                    "(e.g. {'topic': 'AI trends'}). These are available as {variable} "
                    "in the first step and passed through automatically."
                ),
                "additionalProperties": True,
            },
            "triggers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Trigger phrases for create mode (optional).",
            },
            "keep_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "For run_temp only: list of absolute file paths to preserve after the workflow "
                    "finishes. Throwaway scripts/scratch are auto-deleted; documents/reports/data are "
                    "kept by default. Use this only to force-keep something unusual "
                    "(e.g. keep_files=[\"/home/user/Documents/VAF_Projects/notes.py\"])."
                ),
            },
            "skip_validation": {
                "type": "boolean",
                "description": (
                    "For run_temp only: set true to run WITHOUT per-step output validation. When a "
                    "workflow has content/agent steps and none sets \"validate\": true, run_temp "
                    "enables validation on those steps automatically instead of asking."
                ),
            },
        },
        "required": ["action"],
    }

    # Injected by execute_tool() — gives access to the live tool registry
    # and current session context for admin checks.
    _agent: Optional[Any] = None

    # ─────────────────────────────────────────────────────────────────────────

    def run(self, **kwargs) -> str:                         # noqa: C901
        # Capture agent reference injected by execute_tool() via tool_args["_agent"].
        # The class attribute _agent defaults to None; kwargs is the only delivery
        # mechanism here since run() receives positional self + **kwargs (not self._agent).
        _injected = kwargs.get("_agent")
        if _injected is not None:
            self._agent = _injected

        action = (kwargs.get("action") or "").strip().lower()

        # Infer action from other fields when agent omits it
        if not action:
            if kwargs.get("is_temporary") or (kwargs.get("steps") and not kwargs.get("workflow_id")):
                action = "run_temp"
            elif kwargs.get("workflow_id") and kwargs.get("steps"):
                action = "create"
            elif kwargs.get("workflow_id") and not kwargs.get("steps"):
                action = "delete"

        if action == "run_temp":
            return self._run_temp(kwargs)
        if action == "create":
            return self._create_persistent(kwargs)
        if action == "list":
            return self._list_agent_workflows()
        if action == "delete":
            return self._delete_agent_workflow(kwargs)

        return (
            f"Error: unknown action '{action}'. "
            "Valid values: run_temp, create, list, delete."
        )

    # ─── run_temp ─────────────────────────────────────────────────────────────

    def _run_temp(self, kwargs: dict) -> str:
        """
        Build WorkflowStep objects from kwargs, run them synchronously via
        WorkflowEngine using the agent's live tool registry, and return the
        result.  Nothing is written to disk; WORKFLOW_TEMPLATES is untouched.

        When a WebUI session is active, emits workflow_start / workflow_update /
        workflow_output_stream WebSocket events so the VAFWorkflowRuntime panel
        opens and shows live step progress — identical to a persistent workflow.
        """
        import sys

        name      = (kwargs.get("name") or "Agent Temp Workflow").strip()
        desc      = (kwargs.get("description") or "").strip()
        raw_steps = kwargs.get("steps") or []
        variables = dict(kwargs.get("variables") or {})

        if not raw_steps:
            return (
                "Error: 'steps' is required for action='run_temp'. Provide the workflow steps "
                "(normally two or more — single-step workflows are rejected)."
            )

        # Validate and normalise steps
        normalised: List[dict] = []
        for i, s in enumerate(raw_steps):
            if not isinstance(s, dict):
                return f"Error: step {i + 1} must be an object with at least an 'input' field."
            # Weak-model field-shape repair first (tool <- action/agent_id,
            # input <- task/prompt/description/..., args-only steps).
            s = _repair_raw_step(s)
            tool_val = s.get("tool")
            raw_input = s.get("input") or ""
            if isinstance(raw_input, dict):
                raw_input = json.dumps(raw_input, ensure_ascii=False)
            raw_input = str(raw_input).strip()
            if not raw_input:
                return (
                    f"Error: step {i + 1} has no usable instruction. Give the step an "
                    f"'input' (the prompt/instruction for its tool), or 'args' with the "
                    f"tool's parameters."
                )
            tool_str = _resolve_step_tool(tool_val)
            step_dict: dict = {
                "input":       raw_input,
                "tool":        tool_str,
                "output":      s.get("output") or f"step_{i + 1}_output",
                "description": s.get("description") or s.get("name") or f"Step {i + 1}",
            }
            for _f in ("on_success", "on_failure", "optional", "condition",
                       "assertions", "max_assertion_retries", "validate", "args"):
                if _f in s:
                    step_dict[_f] = s[_f]
            normalised.append(step_dict)

        # ── Single-step guard ────────────────────────────────────────────────
        # A temp workflow with only ONE step has no orchestration benefit over
        # just calling that tool directly — there is nothing to chain. The one
        # legitimate single-step case is scheduling an automation (the built-in
        # "Create Scheduled Task" workflow), where create_automation is the whole
        # job. Reject everything else so the agent calls the tool directly.
        _SINGLE_STEP_OK = {"create_automation"}
        if len(normalised) == 1 and normalised[0]["tool"] not in _SINGLE_STEP_OK:
            only_tool = normalised[0]["tool"]
            return (
                f"Error: a run_temp workflow with only ONE step ('{only_tool}') is not allowed. "
                "A single step gains nothing from the workflow engine — there is no output to chain "
                f"into a next step. Just call the '{only_tool}' tool directly instead of wrapping it "
                "in a workflow. Use run_temp only for genuine multi-step work (2+ steps where one "
                "step's output feeds the next). The only valid single-step case is scheduling an "
                "automation via the 'create_automation' tool."
            )

        # The run's registry, collected before anything starts: the preflight checks every
        # step against it, and the engine (inline) runs on it.
        tools = self._collect_tools()
        _refused = _preflight_steps(normalised, tools)
        if _refused:
            self.log(f"[RUN_TEMP] refused before running: {_refused[:400]}")
            return _refused

        # Build WorkflowStep objects directly (no WORKFLOW_TEMPLATES mutation)
        try:
            from vaf.workflows.engine import WorkflowEngine, create_workflow, VALIDATABLE_TOOLS
        except ImportError as exc:
            return f"Error: could not import workflow engine: {exc}"

        # Build as a template dict so create_workflow() can normalise it
        template_dict = {
            "name":        name,
            "description": desc,
            "triggers":    [],
            "steps":       normalised,
        }
        steps = create_workflow(template_dict)

        # Observability: persist WHAT the model authored. During a live-incident
        # forensic the full run_temp step list existed nowhere (the timeline
        # truncates tool args) - whether a step referenced prior outputs via
        # {placeholders} had to be inferred from the coder's received task.
        self.log(f"[RUN_TEMP] name={name!r} steps={json.dumps(normalised, ensure_ascii=False)[:4000]}")

        # ── Validation auto-enable ────────────────────────────────────────────
        # Content/agent steps get per-step output validation by DEFAULT. This
        # used to be a bounce ("[VALIDATION CHECK] ... call run_temp again with
        # validate flags or skip_validation"), i.e. a conscious-decision prompt
        # - and it cost weak models the whole run twice in one live chat: the
        # model retried without the flags, got bounced again, and drifted into
        # doing every step manually while the authored workflow never ran. The
        # system can make this call itself: validation on deliverable steps is
        # exactly what the bounce text recommended, so set it and RUN.
        # skip_validation=true remains the explicit opt-out.
        _validatable_steps = [s for s in steps if s.tool in VALIDATABLE_TOOLS]
        _auto_validated = []
        if (
            _validatable_steps
            and not any(getattr(s, "validate", False) for s in _validatable_steps)
            and not kwargs.get("skip_validation")
        ):
            for s in _validatable_steps:
                s.validate = True
                _auto_validated.append(s.tool)
            self.log(f"[RUN_TEMP] auto-enabled output validation for steps: {sorted(set(_auto_validated))}")

        if not tools:
            return "Error: no tools available for workflow execution."

        # User isolation context (pass through from agent session)
        user_scope_id = None
        username      = "admin"
        agent = self._agent
        if agent is not None:
            user_scope_id = getattr(agent, "_current_user_scope_id", None)
            username      = getattr(agent, "_current_username", None) or "admin"

        # ── WebUI wiring ─────────────────────────────────────────────────────
        # Resolve the active session ID from the agent.  Both attribute names
        # are checked; if neither exists (CLI / test mode) session_id stays
        # None and every _push() call is a no-op.
        session_id = None
        if agent is not None:
            session_id = (
                getattr(agent, "current_session_id", None)
                or getattr(agent, "_session_id", None)
            )

        # ── In the background, when it can be ─────────────────────────────────
        # The same plan in a process of its own (vaf/workflows/background.py): this turn ends
        # at once and the chat hears the result when the run ends. The child gets the steps
        # exactly as normalised here, validation flags included, and runs the same checks and
        # the same cleanup. Only when every step's tool exists there - the child has the
        # workflow primitives, not this agent's registry - and otherwise inline, as before.
        from vaf.workflows import background as _bg
        _missing_bg = _bg.missing_tools(s.tool for s in steps)
        if (_bg.enabled(authorizer=getattr(agent, "_tool_authorizer", None))
                and session_id and not _missing_bg and len(steps) == len(normalised)):
            _plan = [dict(d, validate=True) if getattr(s, "validate", False) else dict(d)
                     for s, d in zip(steps, normalised)]
            try:
                _intent = agent._resolve_user_intent() if agent is not None and hasattr(agent, "_resolve_user_intent") else ""
            except Exception:
                _intent = ""
            _started = _bg.start_temp(
                name, _plan, variables, session_id=str(session_id), description=desc,
                keep_files=kwargs.get("keep_files") or [], user_intent=_intent or "",
                language=getattr(getattr(agent, "prompt_manager", None), "user_language", None))
            if _started is not None:
                self.log(f"[RUN_TEMP] background: {_started[:120]}")
                return _started
            self.log("[RUN_TEMP] background spawn failed, running inline")
        elif _missing_bg:
            self.log(f"[RUN_TEMP] inline: the background child has no {', '.join(_missing_bg)}")

        workflow_id = f"tmp-{uuid.uuid4().hex[:8]}"

        def _push(payload: dict) -> None:
            """Send a WebSocket event; silently swallows all errors."""
            if not session_id:
                return
            try:
                from vaf.core.web_interface import get_web_interface
                get_web_interface()._push_session_update(session_id, payload)
            except Exception:
                pass

        # Send workflow_start — this opens the VAFWorkflowRuntime panel
        ui_steps = [
            {
                "id":     f"step-{idx + 1}",
                "name":   s.description or s.tool,
                "type":   "tool",
                "status": "idle",
            }
            for idx, s in enumerate(steps)
        ]
        _push({
            "type":       "workflow_start",
            "workflowId": workflow_id,
            "name":       name,
            "steps":      ui_steps,
        })

        # ── Step-progress callback ────────────────────────────────────────────
        _STATUS = {"start": "running", "success": "success", "error": "failed", "skip": "skipped"}

        def _ws_callback(event: str, step, current: int, total: int) -> None:
            _push({
                "type":     "workflow_update",
                "stepId":   f"step-{current}",
                "status":   _STATUS.get(event, "running"),
                "progress": int((current / total) * 100),
            })
            if event == "start":
                label = step.description or step.tool
                _push({
                    "type":       "workflow_output_stream",
                    "workflowId": workflow_id,
                    "line":       f"\u2500\u2500\u2500 Step {current}/{total}: {label} [{step.tool}] \u2500\u2500\u2500",
                })
            elif event == "success":
                # Stream the step result so tools that don't write to stdout
                # (browser_agent, research_agent, etc.) still show output.
                _raw = str(getattr(step, "result", "") or "")
                _preview = _raw[:2000] + ("\n\u2026[gek\u00fcrzt]" if len(_raw) > 2000 else "")
                if _preview.strip():
                    for _line in _preview.splitlines():
                        _push({
                            "type":       "workflow_output_stream",
                            "workflowId": workflow_id,
                            "line":       _line,
                        })
                dur = getattr(step, "duration", 0)
                _push({
                    "type":       "workflow_output_stream",
                    "workflowId": workflow_id,
                    "line":       f"\u2713 Schritt {current}/{total} abgeschlossen ({dur:.1f}s)",
                })
            elif event == "error":
                err = getattr(step, "error", "") or ""
                _push({
                    "type":       "workflow_output_stream",
                    "workflowId": workflow_id,
                    "line":       f"\u2717 Schritt {current}/{total} fehlgeschlagen: {err[:300]}",
                })
            elif event == "skip":
                _push({
                    "type":       "workflow_output_stream",
                    "workflowId": workflow_id,
                    "line":       f"\u2014 Schritt {current}/{total} \u00fcbersprungen",
                })

        # ── Stdout / stderr wrapper ───────────────────────────────────────────
        def _send_wf_line(line: str) -> None:
            _push({"type": "workflow_output_stream", "workflowId": workflow_id, "line": line})

        # ── Execute ───────────────────────────────────────────────────────────
        engine = WorkflowEngine(
            tools         = tools,
            callback      = _ws_callback,
            user_scope_id = user_scope_id,
            username      = username,
            authorize     = getattr(agent, "_tool_authorizer", None),
        )
        engine._workflow_name = name   # used in debug logs
        # Run identity for the pause record (routing back to this session and this panel).
        # This lane has no saved template, hence the empty template id.
        engine._template_id = ""
        engine._session_id = session_id
        engine._ui_workflow_id = workflow_id

        # Wire per-step output validation (opt-in via step.validate). Needs the agent's LLM
        # backend; only available here in run_temp. Other workflow paths leave these unset.
        if agent is not None and hasattr(agent, "_validate_step_output"):
            try:
                engine._workflow_user_intent = agent._resolve_user_intent()
            except Exception:
                engine._workflow_user_intent = ""
            engine._validate_step = agent._validate_step_output

        # Stop wiring: let the Stop button abort the workflow — both between steps
        # (engine loop) and *during* a step (bounded-run inside the engine polls this).
        # IMPORTANT: the Stop button targets the *canonical* session id (what the WebSocket
        # sends / what get_current_session_id() returns, e.g. "green123456"). That can
        # differ from `agent.current_session_id` captured above, so we check should_stop for
        # ALL plausible session ids — otherwise Stop silently does nothing.
        _stop_sid = session_id
        def _check_stop() -> bool:
            try:
                from vaf.core.task_queue import TaskQueue
                from vaf.core.subagent_ipc import get_current_session_id
                tq = TaskQueue()
                candidates = set()
                if _stop_sid:
                    candidates.add(str(_stop_sid))
                # The resolver already answers "context first, process boundary second", so
                # a separate environment candidate would only ever ADD a foreign session to
                # a set that decides whether to abort this run.
                _cur = get_current_session_id()
                if _cur:
                    candidates.add(str(_cur))
                return any(tq.should_stop(s) for s in candidates)
            except Exception:
                return False

        _orig_stdout = sys.stdout
        # Capture ONLY stdout into the workflow terminal. That is where the intended workflow
        # output lives: the engine's Rich `UI.event(...)` step markers (the Console has no
        # explicit file → it writes to the current sys.stdout), and VAF_IN_WORKFLOW_TERMINAL's
        # simple_mode "[Coder] …" lines for any in-process step. The heavy agent steps
        # (coding/research/document/librarian/browser) run as child PROCESSES — their output is
        # drained separately and pushed as `subagent_output_stream`, not through here. We do NOT
        # redirect sys.stderr: Python logging / library chatter defaults to stderr and would
        # otherwise flood `workflow_output_stream` with noise that isn't workflow progress.
        _prev_wf_terminal = os.environ.get("VAF_IN_WORKFLOW_TERMINAL")
        _prev_tool_model = os.environ.get("VAF_TOOL_MODEL")
        from vaf.core.config import subagent_model_for as _sa_model_for
        # The calling agent's own config (an embedder's Agent(config=...) carries its
        # provider there), else the installation's.
        _agent_cfg = getattr(self._agent, "config", None)
        _subagent_model = _sa_model_for(config=_agent_cfg if isinstance(_agent_cfg, dict) else None)
        result = None
        _wf_exc = None
        try:
            # Shared, rate-capped mirror. interactive=False stops Rich from starting a
            # Live animation behind the Web UI: its redraws are what flooded the socket.
            # stdout ONLY here, deliberately - see the comment above about stderr.
            sys.stdout = MirroredStdout(sys.stdout,
                                        _send_wf_line if session_id else None,
                                        interactive=False)
            os.environ["VAF_IN_WORKFLOW_TERMINAL"] = "1"
            if _subagent_model:
                os.environ["VAF_TOOL_MODEL"] = _subagent_model
            result = engine.execute(
                steps, variables=variables, check_stop=_check_stop,
                wait_for_subagents=True,   # run sub-agents as killable child processes
            )
        except Exception as exc:
            _wf_exc = exc
        finally:
            try:
                sys.stdout.close_ticker()   # flush the tail before restoring
            except Exception:
                pass
            sys.stdout = _orig_stdout
            if _prev_wf_terminal is None:
                os.environ.pop("VAF_IN_WORKFLOW_TERMINAL", None)
            else:
                os.environ["VAF_IN_WORKFLOW_TERMINAL"] = _prev_wf_terminal
            if _prev_tool_model is None:
                os.environ.pop("VAF_TOOL_MODEL", None)
            else:
                os.environ["VAF_TOOL_MODEL"] = _prev_tool_model
            # workflow_start was sent at the top, so the WebUI must ALWAYS be told the run
            # ended — otherwise the chat "workflow running" indicator (and the runtime panel)
            # hang on "running" forever. Pushed after stdout is restored (not via the stream
            # writer). Paused is a valid non-final state (resolved on resume), so skip it.
            try:
                if _wf_exc is not None:
                    _push({"type": "workflow_done", "workflowId": workflow_id,
                           "success": False, "error": str(_wf_exc)})
                elif result is not None and not getattr(result, "paused", False):
                    _push({"type": "workflow_done", "workflowId": workflow_id,
                           "success": bool(result.success), "error": str(result.error or "")})
            except Exception:
                pass

        if _wf_exc is not None:
            return f"Error executing temporary workflow '{name}': {_wf_exc}"

        if result.paused:
            # Shared wording (vaf/workflows/engine.py) so every lane tells the model the same
            # thing about a pause. The old text here was too soft: a weak model read "paused"
            # as "stalled" and re-ran the work.
            from vaf.workflows.engine import paused_tool_message
            _done = sum(1 for s in (result.steps or [])
                        if getattr(getattr(s, "status", None), "value", "") == "success")
            return paused_tool_message(
                name, _done + 1, len(result.steps or []) or 1,
                getattr(result, "waiting_for_agent", "") or "a background helper",
                str(getattr(result, "waiting_for_task", "") or "?"),
            )

        # ── Intermediate file cleanup for run_temp ────────────────────────────
        # Throwaway scripts/scratch out, every potential deliverable kept: one implementation,
        # shared with a temporary workflow that runs in a process of its own.
        # The folder the ENGINE decided (result.project_path), never the outputs entry a step
        # can write, and only inside this caller's own projects folder.
        from vaf.workflows.engine import project_folder_note, remove_temp_intermediates
        remove_temp_intermediates(getattr(result, "project_path", None) or "",
                                  keep_files=kwargs.get("keep_files") or [],
                                  final_output=result.final_output,
                                  user_scope_id=user_scope_id,
                                  preexisting=getattr(result, "preexisting_files", ()))

        if result.success:
            # Per-step summary so a weird FINAL step can never hide the real
            # deliverables (see engine.summarize_run_steps for the incident).
            # Imperative lead: a weak model skims past a bare success banner
            # and redoes finished work (see workflow_executor's twin comment).
            from vaf.workflows.engine import summarize_run_steps
            _summary = summarize_run_steps(steps)
            return (
                f"Temporary workflow '{name}' completed.\n"
                f"THE WORK IS DONE. Do NOT redo any step, do NOT re-run searches, do NOT "
                f"rebuild files. Read the results below and present them to the user now, "
                f"including any file path shown.\n\n{result.final_output}"
                + (f"\n\nStep results:\n{_summary}" if _summary else "")
                + project_folder_note(result)
            )
        # Lead with "Error:" so the UI status heuristic flags this as a failure (red),
        # not a green success — the bubble status keys off the result prefix.
        return f"Error: Temporary workflow '{name}' failed: {result.error}"

    def _collect_tools(self) -> Dict[str, Any]:
        """The run's tool registry: the workflow primitives with the agent's live registry on
        top, the way execute_workflow builds it (vaf/workflows/tool_overlay.py). The main
        agent does not register the primitives it hides from chat (`bash`, `move_file`), so
        its registry alone left a run_temp step on them with "Tool not found"."""
        from vaf.workflows.tool_overlay import workflow_primitives
        tools = workflow_primitives()
        agent = self._agent
        if agent is not None and getattr(agent, "tools", None):
            tools.update(dict(agent.tools))
        return tools

    # ─── create (persistent) ──────────────────────────────────────────────────

    def _create_persistent(self, kwargs: dict) -> str:
        if not self._is_admin():
            return (
                "Error: Creating persistent workflows requires an admin session. "
                "For a one-shot plan, use action='run_temp' instead."
            )

        workflow_id = (kwargs.get("workflow_id") or "").strip()
        name        = (kwargs.get("name") or "").strip()
        description = (kwargs.get("description") or "").strip()
        triggers    = [str(t) for t in (kwargs.get("triggers") or []) if str(t).strip()]
        raw_steps   = kwargs.get("steps") or []

        if not re.match(r'^[a-z][a-z0-9_]*$', workflow_id):
            return (
                f"Error: workflow_id must be lowercase snake_case, got '{workflow_id}'. "
                "Example: 'daily_brief'."
            )
        if not name:
            return "Error: 'name' is required for action='create'."
        from vaf.workflows.templates import workflow_text_problem, write_workflow_file
        _text_problem = workflow_text_problem("'name'", name)
        if _text_problem:
            return f"Error: {_text_problem}"
        if not raw_steps:
            return "Error: 'steps' is required for action='create'. Provide at least one step."

        normalised = []
        for i, s in enumerate(raw_steps if isinstance(raw_steps, list) else []):
            if not isinstance(s, dict):
                return f"Error: step {i + 1} missing non-empty 'input' field."
            s = _repair_raw_step(s)  # same weak-model field-shape repair as run_temp
            if not str(s.get("input") or "").strip():
                return f"Error: step {i + 1} missing non-empty 'input' field."
            step_dict2: dict = {
                "input":       s["input"],
                "tool":        _resolve_step_tool(s.get("tool")),
                "output":      s.get("output") or f"step_{i + 1}_output",
                "description": s.get("description") or f"Step {i + 1}",
            }
            for _f in ("on_success", "on_failure", "optional", "condition",
                       "assertions", "max_assertion_retries", "validate", "args"):
                if _f in s:
                    step_dict2[_f] = s[_f]
            normalised.append(step_dict2)

        # The same check as run_temp, before anything is written: a saved plan that cannot
        # run fails each time it is started, where nobody can fix it.
        _tools = self._collect_tools()
        _unknown: List[str] = []
        _refused = _preflight_steps(normalised, _tools, unknown=_unknown)
        if _refused:
            return _refused

        wf_dict = {
            "name":        name,
            "description": description,
            "triggers":    triggers,
            "steps":       normalised,
        }

        user_dir = os.path.expanduser("~/.vaf/workflows")
        os.makedirs(user_dir, exist_ok=True)
        wf_path = os.path.join(user_dir, f"{workflow_id}.py")

        if os.path.exists(wf_path):
            return (
                f"Error: a workflow named '{workflow_id}' already exists. "
                "Use action='delete' first, or choose a different workflow_id."
            )

        # The ONLY header line is the marker is_agent_created reads; the name lives inside the
        # literal, never in a comment (a comment line can carry a source-encoding declaration).
        write_workflow_file(wf_path, wf_dict, ["created_by: agent"])

        try:
            from vaf.workflows.templates import reload_workflows, list_templates
            reload_workflows()
        except Exception as exc:
            return (
                f"Workflow '{workflow_id}' saved to disk but registry reload failed: {exc}. "
                "It will be available after the next server restart."
            )

        # Notify the WebUI: refresh list + play a save-preview animation
        try:
            import time as _time
            from vaf.core.web_interface import get_web_interface
            agent = self._agent
            session_id = None
            if agent is not None:
                session_id = (
                    getattr(agent, "current_session_id", None)
                    or getattr(agent, "_session_id", None)
                )
            if session_id:
                wi = get_web_interface()

                # 1. Open the Workflow Runtime panel showing all steps as idle
                ui_steps = [
                    {
                        "id":     f"step-{idx + 1}",
                        "name":   s.get("description") or s.get("tool") or f"Step {idx + 1}",
                        "type":   "tool",
                        "status": "idle",
                    }
                    for idx, s in enumerate(normalised)
                ]
                wi._push_session_update(session_id, {
                    "type":       "workflow_start",
                    "workflowId": workflow_id,
                    "name":       name,
                    "steps":      ui_steps,
                })

                # 2. Console line
                wi._push_session_update(session_id, {
                    "type":       "workflow_output_stream",
                    "workflowId": workflow_id,
                    "line":       f"✓ Saved: ~/.vaf/workflows/{workflow_id}.py",
                })

                # 3. Animate each step to success in sequence
                for idx in range(len(normalised)):
                    _time.sleep(0.28)
                    wi._push_session_update(session_id, {
                        "type":     "workflow_update",
                        "stepId":   f"step-{idx + 1}",
                        "status":   "success",
                        "progress": 100,
                    })

                # 4. Refresh workflow list (panel auto-closes after 2.5 s via store)
                wi._push_session_update(session_id, {
                    "type": "workflow_created",
                    "workflow_id": workflow_id,
                })
                wi._push_session_update(session_id, {
                    "type": "workflows_list",
                    "workflows": list_templates(),
                })
        except Exception:
            pass

        _note = ""
        if _unknown:
            _note = (f"\nNote: {', '.join(_unknown)} names a tool this chat does not have; that "
                     "step fails unless the workflow runs where the tool exists.")
        return (
            f"Workflow '{workflow_id}' saved successfully. "
            "It is now available via execute_workflow() and visible in the WebUI." + _note
        )

    # ─── list ─────────────────────────────────────────────────────────────────

    def _list_agent_workflows(self) -> str:
        user_dir = os.path.expanduser("~/.vaf/workflows")
        if not os.path.isdir(user_dir):
            return "You have not created any persistent workflows yet."

        found: List[str] = []
        for fname in sorted(os.listdir(user_dir)):
            if not fname.endswith(".py"):
                continue
            if is_agent_created(fname[:-3]):
                found.append(fname[:-3])   # strip .py

        if not found:
            return "You have not created any persistent workflows yet."

        lines = ["Agent-created persistent workflows:"]
        for wf_id in found:
            lines.append(f"  • {wf_id}")
        lines.append(
            "\nCall execute_workflow(workflow_id=...) to run one, "
            "or action='delete' to remove one."
        )
        return "\n".join(lines)

    # ─── delete ───────────────────────────────────────────────────────────────

    def _delete_agent_workflow(self, kwargs: dict) -> str:
        if not self._is_admin():
            return "Error: Deleting persistent workflows requires an admin session."

        workflow_id = (kwargs.get("workflow_id") or "").strip()
        if not workflow_id:
            return "Error: 'workflow_id' is required for action='delete'."

        user_dir = os.path.expanduser("~/.vaf/workflows")
        # The id becomes a file name: one entry, never a path (the web handlers do the same).
        from vaf.core.path_jail import PathEscape, safe_entry_name
        try:
            wf_file = safe_entry_name(f"{workflow_id}.py")
        except PathEscape:
            return f"Error: '{workflow_id}' is not a valid workflow id."
        wf_path  = os.path.join(user_dir, wf_file)

        if not os.path.exists(wf_path):
            return (
                f"Error: '{workflow_id}' not found in agent-created workflows. "
                "Use action='list' to see what you have created."
            )

        # Ownership check: only files with the agent marker may be deleted
        if not is_agent_created(workflow_id):
            return (
                f"Error: '{workflow_id}' was not created by the agent "
                "(missing 'created_by: agent' marker). Cannot delete."
            )

        os.remove(wf_path)

        try:
            from vaf.workflows.templates import reload_workflows
            reload_workflows()
        except Exception:
            pass

        return f"Workflow '{workflow_id}' deleted successfully."

    # ─── Private helpers ──────────────────────────────────────────────────────

    def _is_admin(self) -> bool:
        """True when the current agent session belongs to an admin user."""
        agent = self._agent
        if agent is None:
            return False
        try:
            from vaf.core.config import is_admin_identity
            return is_admin_identity(getattr(agent, "_current_user_role", None),
                                     getattr(agent, "_current_user_scope_id", None))
        except Exception:
            return False
