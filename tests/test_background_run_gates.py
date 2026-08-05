# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A background pass reconfigures itself, never the process it shares.

MEASURED (2026-08-04). The rule was already written twice in `vaf/core/agent.py`,
with a live incident behind it: run kind is INSTANCE truth, because the environment
is shared across threads and a concurrent background run makes every other agent in
the process look like one. It was stated and then not applied to the thinking gates -
which are the ones read LIVE, per turn, on a human's in-flight turn.

WHAT A WAITING USER GOT while a background pass ran, all of it silent:

- a tool budget of 15 instead of 75, and a soft cap of 12 instead of 50
- a read cap that blocks the third call to any read tool
- the no-progress guard disabled
- NO empty-response retry - and the comment on that gate records that without it
  "the turn never closes and the Web UI hangs forever on a loading thinking block
  (observed)"
- a context clear at 2 empty retries instead of 8
- their own update_intent tool answering with a refusal

THE SECOND HALF is an identity, not a mode. `thinking_note_add` declared no
identity_kwargs while its schema told the model the field was "injected by the
framework", so nothing injected it and the tool fell back to a process-global set by
whichever thinking run was in flight. It is registered on EVERY agent, so a tenant's
chat turn wrote its note into another tenant's bucket - and those notes are read back
into that tenant's next thinking prompt under a header telling the model to follow
them carefully. A cross-tenant write that becomes a cross-tenant instruction.

WHAT IS DELIBERATELY NOT PINNED HERE: `_emit_to_web_ui()` still reads the
environment. It is a module function with no agent to ask, and its eleven call sites
would each have to be handed one. Its hazard is already written at the one call site
that refused to use it. That is a named boundary, not an oversight.
"""
import types

import pytest

from vaf.core.agent import Agent


def _agent(run_kind):
    """A stand-in carrying the REAL predicate, not a re-implementation of it."""
    ns = types.SimpleNamespace(_run_kind=run_kind, _thinking_read_counts={},
                               _nonprogress_streak=0)
    ns._is_thinking_run = types.MethodType(Agent._is_thinking_run, ns)
    return ns


# ── the run kind is the instance, not the process ────────────────────────────

def test_a_chat_agent_is_not_a_thinking_run_because_the_environment_says_so(monkeypatch) -> None:
    """The whole defect in one assertion: a background pass sets that variable for
    its whole duration, and every other agent in the process used to believe it."""
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    assert _agent("chat")._is_thinking_run() is False


def test_a_thinking_agent_is_one_without_any_environment(monkeypatch) -> None:
    monkeypatch.delenv("VAF_THINKING_MODE", raising=False)
    assert _agent("thinking")._is_thinking_run() is True


def test_an_agent_that_never_declared_one_is_not_a_thinking_run() -> None:
    """Defensive: agents built before the kwarg existed, and duck-typed stand-ins."""
    assert Agent._is_thinking_run(types.SimpleNamespace()) is False


# ── the gates a waiting human used to lose ───────────────────────────────────

def test_the_read_cap_does_not_reach_a_concurrent_chat_turn(monkeypatch) -> None:
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    chat = _agent("chat")
    for _ in range(6):
        assert Agent._thinking_read_cap_step(chat, "memory_search") is None


def test_the_read_cap_still_applies_to_the_background_pass_itself(monkeypatch) -> None:
    monkeypatch.delenv("VAF_THINKING_MODE", raising=False)
    bg = _agent("thinking")
    assert Agent._thinking_read_cap_step(bg, "memory_search") is None
    assert Agent._thinking_read_cap_step(bg, "memory_search") is None
    assert Agent._thinking_read_cap_step(bg, "memory_search") is not None


def test_the_no_progress_guard_stays_armed_for_a_chat_turn(monkeypatch) -> None:
    """Disabled for a background pass on purpose - the environment carried that
    decision to everyone else."""
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    chat = _agent("chat")
    nudged = False
    for _ in range(12):
        msg, force = Agent._nonprogress_step(chat, "list_automations")
        nudged = nudged or bool(msg) or bool(force)
    assert nudged, "a chat turn lost its runaway guard to a concurrent background pass"


# ── the identity half ────────────────────────────────────────────────────────

def test_the_note_tool_declares_the_identity_its_schema_promises() -> None:
    """Its parameter description says "injected by the framework". Without the
    declaration the dispatcher injects nothing, and the tool read a process-global
    belonging to whichever background run was in flight."""
    from vaf.tools.thinking_note_add import ThinkingNoteAddTool

    assert "user_scope_id" in (ThinkingNoteAddTool.identity_kwargs or ())
    assert "user_scope_id" in ThinkingNoteAddTool.parameters["properties"]


def test_the_note_tool_has_no_environment_fallback() -> None:
    """A fallback here is not a safety net: it is the wrong tenant's bucket."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "vaf" / "tools"
           / "thinking_note_add.py").read_text(encoding="utf-8")
    assert "VAF_THINKING_SCOPE_ID" not in src


def test_the_note_goes_to_the_caller_not_the_running_background_scope(monkeypatch) -> None:
    """End to end through the tool: the injected scope wins, and the environment
    of a concurrent background run is not consulted at all."""
    monkeypatch.setenv("VAF_THINKING_SCOPE_ID", "ab12cd34-0000-4000-8000-000000000001")
    from vaf.tools.thinking_note_add import ThinkingNoteAddTool

    seen = []
    import vaf.core.thinking_notes as notes

    monkeypatch.setattr(notes, "add_note", lambda scope, note: seen.append(scope))
    ThinkingNoteAddTool().run(note="remember this",
                              user_scope_id="ab12cd34-0000-4000-8000-000000000002")
    assert seen and seen[0].endswith("0002"), (
        f"the note landed in {seen}, which is the scope of whichever background run "
        "was in flight rather than the caller's"
    )


# ── the flag that outlived every run ─────────────────────────────────────────

def test_the_automation_flag_is_restored_not_left_behind() -> None:
    """It was set unconditionally and never put back, so after the first automation
    the process stopped prompting a human for the rest of its life - while the
    comment at the cleanup already claimed the prior value was kept."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "vaf" / "core"
           / "automation.py").read_text(encoding="utf-8")
    assert "_prior_noninteractive = os.environ.get(\"VAF_NONINTERACTIVE\")" in src
    assert "os.environ[\"VAF_NONINTERACTIVE\"] = _prior_noninteractive" in src
