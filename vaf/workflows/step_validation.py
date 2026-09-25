# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Does one workflow step's OUTPUT fulfil the step's GOAL? The per-step validator.

One implementation for every runner. It lived on the chat agent (`_validate_step_output`),
which made it reachable only from a run inside a chat turn: a workflow running in a process
of its own (`vaf workflow run`, vaf/workflows/background.py) has no agent object, so it would
have run the same plan without the checks a temporary workflow turns on by default. What
differs between the runners is only how a model is asked, so that is the one argument.

`ask(messages, max_tokens) -> str` returns the model's text and may raise; the chat agent
passes its own backend (`Agent._run_validation_llm`), a runner without an agent passes
`ask_via_complete` (the `complete()` primitive).
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

Ask = Callable[[List[dict], int], str]


def validate_step_output(goal: str, result: str, tool: str, user_intent: str = "", *,
                         ask: Ask) -> Tuple[bool, Optional[str]]:
    """(fulfilled, retry_hint) for one step.

    The actual content is judged against the goal: there is NO lenient "report saved ->
    accept" fast path (that one would wave through an empty or wrong document just because
    the tool reported success). Any failure to decide - an empty goal or result, a backend
    error, no decisive answer after three tries - accepts, so a flaky validator can never
    break a workflow. Coding output with an explicit completion signal is trusted (the local
    model tends to false-negative on perfectly valid code).
    """
    result = (result or "").strip()
    goal = (goal or "").strip()
    if not result or not goal:
        return True, None
    if tool == "coding_agent" and "[vaf_coding_agent_status: complete]" in result.lower():
        return True, None

    prompt = (
        "You are a strict validator for ONE step of a multi-step workflow.\n"
        "Judge ONLY whether the STEP OUTPUT actually fulfils the STEP GOAL - by its CONTENT, "
        "not by whether a tool merely reported success.\n\n"
        f"STEP GOAL: {goal[:600]}\n"
        f"OVERALL USER INTENT: {(user_intent or '')[:400]}\n"
        f"STEP OUTPUT: {result[:1200]}\n\n"
        "Reply with EXACTLY one of:\n"
        "- </true> if the output fulfils the goal\n"
        "- </false> if it does NOT (empty, wrong content, missing the requested data, off-topic)\n\n"
        "If </false>, add on the next line: RETRY: [one concrete instruction to fix it]"
    )
    stricter_prompt = (
        "Reply with EXACTLY </true> or </false>. Nothing else.\n"
        f"GOAL: {goal[:300]}\n"
        f"OUTPUT: {result[:500]}\n"
        "Does the output fulfil the goal? </true> or </false>"
    )
    for attempt in range(3):
        try:
            content = ask([{"role": "user", "content": stricter_prompt if attempt > 0 else prompt}], 150)
        except Exception:
            return True, None  # backend error -> never block the workflow
        resp = (content or "").strip().lower()
        if "</true>" in resp:
            return True, None
        if "</false>" in resp:
            retry_hint = None
            for line in (content or "").splitlines():
                if "retry:" in line.lower():
                    retry_hint = line.split(":", 1)[-1].strip()
                    break
            return False, (retry_hint or f"The output did not fulfil the goal: {goal[:200]}")
    # No decisive answer after retries -> accept (don't burn workflow retries on indecision).
    return True, None


def ask_via_complete(messages: List[dict], max_tokens: int) -> str:
    """`ask` for a runner with no agent: one `complete()` call, bounded, deterministic.
    `complete()` never raises and answers None on a failure, which reads as "no decisive
    answer" and so, after the three tries, as accepted."""
    from vaf.core.completion import complete
    return complete(messages, max_tokens=max_tokens, temperature=0, timeout=30,
                    caller="workflow:validate") or ""


def validator_for_runner():
    """The engine's `_validate_step` hook for a runner with no agent."""
    def _validate(goal: str, result: str, tool: str, user_intent: str = ""):
        return validate_step_output(goal, result, tool, user_intent, ask=ask_via_complete)
    return _validate
