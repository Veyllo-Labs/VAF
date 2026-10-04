# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The code audit rounds inside the coding agent's loop (vaf/tools/coder.py): once the
tasks are done the run commits, reviews its own change and works the verified findings as
one more task, until a round is clean, incomplete, finds the same as the round before, or
the limit is reached.

The decisions are module-level pure functions (the _verify_task_goal pattern) and pinned
here without constructing a coder; the wiring - every all-done exit point, the phase
stepper, the summary line, the allowlist - is pinned statically, like the other gates in
test_coder_gates.py, so it cannot drift one site at a time.
"""
import re
from pathlib import Path

from vaf.core.coder_tools import CODER_ALLOWED_TOOLS
from vaf.tools.coder import (TaskManager, _audit_attempted, _audit_round_outcome,
                             _audit_round_refusal)

_REPO = Path(__file__).resolve().parents[1]


def _coder_src() -> str:
    return (_REPO / "vaf" / "tools" / "coder.py").read_bytes().decode("utf-8")


# ── before a round ────────────────────────────────────────────────────────────

def test_a_round_may_run_until_the_limit():
    """MUTATION: `>` instead of `>=` - the 51st round runs."""
    allowed = dict(enabled=True, content_only=False, max_rounds=50)
    assert _audit_round_refusal(rounds_done=0, **allowed) == ""
    assert _audit_round_refusal(rounds_done=49, **allowed) == ""
    assert "limit of 50" in _audit_round_refusal(rounds_done=50, **allowed)


def test_switched_off_or_content_only_never_audits():
    assert "switched off" in _audit_round_refusal(enabled=False, content_only=False,
                                                  rounds_done=0, max_rounds=50)
    assert "content-only" in _audit_round_refusal(enabled=True, content_only=True,
                                                  rounds_done=0, max_rounds=50)


# ── after a round ─────────────────────────────────────────────────────────────

def test_findings_are_fixed_and_a_clean_complete_round_ends_the_loop():
    assert _audit_round_outcome("complete", open_count=1, fresh_count=1) == "fix"
    assert _audit_round_outcome("complete", open_count=0, fresh_count=0) == "clean"


def test_each_finding_gets_one_fix_attempt():
    """Measured on a live loop: every fix writes new code, the next review finds something
    new in it, and the run oscillated (fix, revert, fix) until it was stopped by hand. A
    round whose findings were all handed to a fix task before ends the loop; a round with
    anything new goes on. MUTATION: fix whatever is open."""
    assert _audit_round_outcome("complete", open_count=3, fresh_count=0) == "no_progress"
    assert _audit_round_outcome("complete", open_count=3, fresh_count=1) == "fix"


class _F:
    def __init__(self, id, file, start, end):
        self.id, self.file, self.start_line, self.end_line = id, file, start, end


def test_a_finding_on_lines_already_worked_on_counts_as_attempted():
    """The same bug comes back with another title, another quoted line, another category:
    measured, three unchanged bugs read as new for four rounds when only the id was compared.
    MUTATION: compare ids only."""
    attempted = [{"id": "a1", "file": "inventory.py", "start": 4, "end": 5}]
    assert _audit_attempted(_F("zz", "inventory.py", 5, 5), attempted)       # reworded
    assert _audit_attempted(_F("a1", "other.py", 40, 40), attempted)         # same id
    assert not _audit_attempted(_F("zz", "inventory.py", 30, 31), attempted)  # elsewhere
    assert not _audit_attempted(_F("zz", "other.py", 4, 5), attempted)        # other file


def test_an_incomplete_review_is_never_clean_but_its_proven_findings_are_fixed():
    """MUTATION: read status "incomplete" with nothing found as "clean"."""
    assert _audit_round_outcome("incomplete", open_count=0, fresh_count=0) == "incomplete"
    assert _audit_round_outcome("failed", open_count=0, fresh_count=0) == "incomplete"
    assert _audit_round_outcome("incomplete", open_count=1, fresh_count=1) == "fix"


def test_a_round_that_ended_the_loop_is_not_repeated_at_the_next_exit_point():
    """One run end passes through two all-done exit points (the task_done branch, then the
    loop's own check); measured, the audit ran a second, identical round there.
    MUTATION: drop the finished flag."""
    src = _coder_src()
    assert 'if _audit_state["finished"]:' in src
    assert 'if outcome != "fix":\n                _audit_state["finished"] = True' in src


# ── the findings become a task ────────────────────────────────────────────────

def test_an_appended_task_becomes_current_after_a_finished_plan(tmp_path):
    """MUTATION: append without moving the cursor - the run reads as done and ends."""
    tm = TaskManager(str(tmp_path))
    tm.set_todos(["build it"])
    tm.complete_current_task("built")
    assert tm.is_all_done()
    idx = tm.append_task("Fix the code audit findings (round 1)", description="2 finding(s)")
    assert idx == 1 and tm.current_task_idx == 1
    assert not tm.is_all_done()
    assert tm.get_current_task() == "Fix the code audit findings (round 1)"
    # Persisted like every other task: a resumed run sees it.
    assert TaskManager(str(tmp_path)).todos[1]["task"] == "Fix the code audit findings (round 1)"


# ── wiring ────────────────────────────────────────────────────────────────────

def test_every_all_done_exit_point_offers_the_audit_round():
    """Six places end a run when every task is done; each must ask _next_round (final
    retry first, then the audit). MUTATION: one site keeps calling the retry alone."""
    src = _coder_src()
    assert src.count("if _next_round():") + src.count("_round = _next_round()") == 6
    assert not re.search(r"if _maybe_start_final_retry\(\):\s*\n\s*(?:#.*\n\s*)*continue", src)
    body = src[src.index("def _next_round() -> str:"):]
    body = body[:body.index("\n\n")]
    assert body.index("_maybe_start_final_retry()") < body.index("_maybe_start_audit_round()")


def test_a_fix_task_carries_only_what_no_fix_task_saw_and_keeps_the_task_in_force():
    src = _coder_src()
    assert 'to_fix = [f for f in found if not _audit_attempted(f, _audit_state["attempted"])]' in src
    assert '_dc.replace(report, findings=to_fix).fix_prompt(max_chars=20_000)' in src
    assert "original task explicitly asked for stays in force" in src


def test_the_audit_shows_as_a_phase_and_in_the_summary():
    src = _coder_src()
    assert '("plan", "build", "audit", "document", "commit")' in src
    assert '_set_phase("audit")' in src
    # An audit that never ran is not drawn as a passed step.
    assert '_phases["audit"] = "skipped"' in src and 'if _phases[p] != "skipped":' in src
    assert 'git_line += f"**🔎 {_audit_state[\'note\']}**\\n"' in src


def test_the_coder_may_call_code_audit_and_it_is_advertised_in_task_context():
    assert "code_audit" in CODER_ALLOWED_TOOLS
    src = _coder_src()
    assert 'self.local_tools["code_audit"] = _CoderAuditTool()' in src
    assert '"name": "code_audit"' in src


def test_the_coders_audit_tool_is_not_discoverable_as_a_tool_class():
    """Tool discovery instantiates every BaseTool subclass in vaf/tools/*.py - a module-level
    one in coder.py would register a second `code_audit` for the main agent."""
    import inspect

    import vaf.tools.coder as coder
    from vaf.tools.base import BaseTool
    names = {obj().name for _, obj in inspect.getmembers(coder)
             if inspect.isclass(obj) and issubclass(obj, BaseTool) and obj is not BaseTool}
    assert "code_audit" not in names
