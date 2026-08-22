# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The coder's deterministic gates: verify-before-done, read-before-edit,
search-before-build - the /regeln rules as code instead of prompt prose.

The prompt has stated these rules all along ("run_tests must be green before
task_done") and the measured reality is that models ignore stated rules under
pressure; the linter gate exists as code for exactly that reason. Each gate is
a module-level pure function (the _verify_task_goal pattern), so the decision
logic is pinned here without constructing a coder, and the wiring - dispatch
hooks, ContextState fields, the Guards feed to the web UI - is pinned
statically so it cannot drift one file at a time.
"""
import os
from pathlib import Path

from vaf.tools.coder import (
    ContextState,
    _active_lint_failure_files,
    _lint_feedback_message,
    _project_has_test_infra,
    _sibling_file_note,
    _unread_edit_reason,
    _unverified_done_reason,
)

_REPO = Path(__file__).resolve().parents[1]


# ── verify-before-done ────────────────────────────────────────────────────────

def test_done_blocks_when_writes_follow_the_last_green_verify():
    reason = _unverified_done_reason(
        last_write_seq=7, last_green_verify_seq=3,
        web_written=True, has_test_infra=False, prior_blocks=0)
    assert reason and "UNVERIFIED" in reason
    assert "render_check" in reason and "run_tests" not in reason


def test_done_passes_when_the_verify_came_after_the_write():
    assert _unverified_done_reason(3, 7, True, True, 0) is None


def test_done_passes_when_nothing_was_written():
    assert _unverified_done_reason(0, 0, False, True, 0) is None


def test_done_passes_when_no_verify_lane_exists():
    # A project with no tests and no web pages has nothing that can turn
    # green; gating it would be a dead loop by construction.
    assert _unverified_done_reason(7, 0, False, False, 0) is None


def test_gate_offers_only_the_lanes_that_apply():
    both = _unverified_done_reason(7, 0, True, True, 0)
    assert "render_check" in both and "TESTS PASSED" in both
    tests_only = _unverified_done_reason(7, 0, False, True, 0)
    assert "run_tests" in tests_only and "render_check" not in tests_only


def test_gate_stands_down_after_two_blocks():
    # An unusable verify lane (sandbox down, browser busy) must degrade to a
    # warning, never to a dead loop.
    assert _unverified_done_reason(7, 0, True, True, 1) is not None
    assert _unverified_done_reason(7, 0, True, True, 2) is None


def test_context_state_clone_carries_the_gate_fields():
    # The rollback path: a clone that loses these silently disarms the gate.
    s = ContextState(context_manager=None, history=[], phase="task_0",
                     last_write_seq=5, last_green_verify_seq=2,
                     web_written=True, verify_gate_blocks=1)
    c = s.clone()
    assert (c.last_write_seq, c.last_green_verify_seq, c.web_written,
            c.verify_gate_blocks) == (5, 2, True, 1)


def test_test_infra_detection(tmp_path):
    assert _project_has_test_infra(str(tmp_path)) is False
    (tmp_path / "tests").mkdir()
    assert _project_has_test_infra(str(tmp_path)) is True


def test_test_infra_detects_a_package_json_test_script(tmp_path):
    (tmp_path / "package.json").write_bytes(b'{"scripts": {"test": "vitest"}}')
    assert _project_has_test_infra(str(tmp_path)) is True
    (tmp_path / "package.json").write_bytes(b'{"scripts": {}}')
    assert _project_has_test_infra(str(tmp_path)) is False


# ── read-before-edit ──────────────────────────────────────────────────────────

def test_editing_an_unread_existing_file_is_refused(tmp_path):
    f = tmp_path / "app.py"
    f.write_bytes(b"x = 1\n")
    reason = _unread_edit_reason(str(f), set())
    assert reason and "read_file" in reason


def test_editing_a_read_file_is_allowed(tmp_path):
    f = tmp_path / "app.py"
    f.write_bytes(b"x = 1\n")
    assert _unread_edit_reason(str(f), {os.path.realpath(str(f))}) is None


def test_editing_a_missing_file_is_left_to_the_tools_own_error(tmp_path):
    # The gate must not shadow edit_file's clearer missing-file message.
    assert _unread_edit_reason(str(tmp_path / "ghost.py"), set()) is None


# ── search-before-build ───────────────────────────────────────────────────────

def test_new_file_with_a_same_stemmed_sibling_gets_the_sibling_named(tmp_path):
    (tmp_path / "utils.py").write_bytes(b"")
    note = _sibling_file_note(str(tmp_path / "Utils_new.js"), str(tmp_path))
    # 'utilsnew' != 'utils' - different stem, no note; exact-stem match notes.
    assert note is None
    note = _sibling_file_note(str(tmp_path / "UTILS.js"), str(tmp_path))
    assert note and "utils.py" in note and "EXTEND" in note


def test_sibling_note_never_fires_without_a_match(tmp_path):
    (tmp_path / "app.py").write_bytes(b"")
    assert _sibling_file_note(str(tmp_path / "server.py"), str(tmp_path)) is None


def test_sibling_note_ignores_noise_directories(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "index.js").write_bytes(b"")
    assert _sibling_file_note(str(tmp_path / "index.html"), str(tmp_path)) is None


# ── immediate lint feedback ───────────────────────────────────────────────────

def _lint_msg(verdict: str, fname: str) -> dict:
    head = ("❌ LINTER CHECK FAILED" if verdict == "fail" else "✅ LINTER CHECK PASSED")
    return {"role": "system", "content": f"{head}\nFile: {fname}\nStatus: whatever\n"}


def test_a_fixed_file_stops_blocking_the_linter_gate():
    # The sequence per-write linting made routine: broken write (FAILED),
    # corrected write (PASSED). The old any-FAILED-in-window scan kept
    # blocking task_done until the stale message scrolled out.
    history = [_lint_msg("fail", "app.py"), {"role": "tool", "content": "x"},
               _lint_msg("pass", "app.py")]
    assert _active_lint_failure_files(history) == []


def test_a_still_broken_file_keeps_blocking():
    history = [_lint_msg("pass", "app.py"), _lint_msg("fail", "app.py")]
    assert _active_lint_failure_files(history) == ["app.py"]


def test_lint_verdicts_are_tracked_per_file():
    history = [_lint_msg("fail", "a.py"), _lint_msg("fail", "b.py"),
               _lint_msg("pass", "a.py")]
    assert _active_lint_failure_files(history) == ["b.py"]


def test_non_lint_messages_are_ignored():
    history = [{"role": "system", "content": "CONTEXT RESET"},
               {"role": "user", "content": "❌ LINTER CHECK FAILED (quoted by user)"}]
    assert _active_lint_failure_files(history) == []


class _StubLinter:
    def __init__(self, answer):
        self.answer = answer

    def run(self, path):
        return self.answer


def test_lint_feedback_carries_the_gate_markers(tmp_path):
    msg, failed = _lint_feedback_message(str(tmp_path / "x.py"),
                                         {"linter": _StubLinter("E999 SyntaxError")})
    assert failed and "❌ LINTER CHECK FAILED" in msg["content"]
    assert "File: x.py" in msg["content"]
    msg, failed = _lint_feedback_message(str(tmp_path / "x.py"),
                                         {"linter": _StubLinter("✓ clean")})
    assert not failed and "✅ LINTER CHECK PASSED" in msg["content"]


def test_lint_feedback_stays_silent_where_it_has_nothing_to_say(tmp_path):
    # Unsupported types and a missing linter must produce no verdict at all -
    # a fake PASSED for an unlintable file would defeat the gate.
    assert _lint_feedback_message(str(tmp_path / "x.html"),
                                  {"linter": _StubLinter("[INFO] no linter for html")}) == (None, False)
    assert _lint_feedback_message(str(tmp_path / "x.py"), {}) == (None, False)


# ── wiring, pinned statically so it cannot drift one file at a time ───────────

def _coder_src() -> str:
    return (_REPO / "vaf" / "tools" / "coder.py").read_bytes().decode("utf-8")


def test_the_gates_are_wired_into_the_dispatch():
    src = _coder_src()
    # verify-before-done sits in the task_done chain and counts its blocks
    assert "_unverified_done_reason(" in src.split("def _unverified_done_reason", 1)[1]
    assert "current_state.verify_gate_blocks += 1" in src
    # read-before-edit guards edit_file with the answer-then-continue shape
    assert "_unread_edit_reason(" in src.split("def _unread_edit_reason", 1)[1]
    # the sibling note rides the adjacency-safe post-tool lane
    assert src.count("_sibling_file_note(") >= 2  # definition + call site


def test_the_result_append_tracks_writes_and_green_verifies():
    src = _coder_src()
    assert "current_state.last_write_seq = loop.guard_seq" in src
    assert 'result_str.lstrip().startswith("TESTS PASSED")' in src
    assert '"Page errors: none" in result_str' in src


def test_immediate_lint_covers_both_write_lanes_and_feeds_the_gate():
    src = _coder_src()
    # write_file handler and the edit_file lane both consume the shared
    # verdict builder; the task_done gate decides on the latest-per-file scan.
    assert src.count("_lint_feedback_message(") >= 3  # definition + 2 lanes
    assert 'if fn_name == "edit_file" and _gpath:' in src
    assert "_active_lint_failure_files(current_state.history)" in src


def test_the_guards_feed_reaches_the_web_ui_end_to_end():
    # Rule 2 (the twice-bitten trap): the payload field, the field-by-field
    # forwarding in page.tsx, and the window's tab are three registries; one
    # missing and the feed silently never renders.
    src = _coder_src()
    assert '"guards": list(_guard_events)' in src
    page = (_REPO / "web" / "app" / "page.tsx").read_bytes().decode("utf-8")
    assert "guards: Array.isArray(data.guards)" in page
    win = (_REPO / "web" / "components" / "SubAgentWindow.tsx").read_bytes().decode("utf-8")
    assert "'guards'" in win and "activeConsoleTab === 'guards'" in win


def test_the_lifecycle_stepper_reaches_the_ui_with_all_four_anchors():
    # The Tasks section shows WHAT is being built; the stepper shows WHERE the
    # run is. Document and commit were invisible as steps before this.
    src = _coder_src()
    assert '"phases": [{"name": p, "status": _phases[p]} for p in _PHASE_ORDER]' in src
    assert '_set_phase("build")' in src
    assert '_set_phase("document")' in src
    assert '_set_phase("commit")' in src
    assert '_phases["commit"] = "done"' in src
    page = (_REPO / "web" / "app" / "page.tsx").read_bytes().decode("utf-8")
    assert "phases: Array.isArray(data.phases)" in page
    win = (_REPO / "web" / "components" / "SubAgentWindow.tsx").read_bytes().decode("utf-8")
    assert "coder.phases" in win


def test_the_verify_verbs_show_in_the_live_action():
    # "Checking" was invisible in the header: run_tests and render_check set
    # no action, so file-less verify stretches read as frozen.
    src = _coder_src()
    assert 'tui.set_action("🧪 Running tests...")' in src
    assert 'tui.set_action("🖥️ Render check...")' in src


def test_every_named_loop_intervention_reports_to_the_feed():
    # The user-facing promise: gates AND the existing loop machinery are
    # visible in the Guards tab, not only in the terminal stream.
    src = _coder_src()
    for label in (
        "task_done blocked: linter errors",
        "task_done blocked: unverified changes",
        "task_done blocked: tasks remaining",
        "task_done blocked: no files for task",
        "edit blocked: file not read",
        "edit blocked: no plan yet",
        "write blocked: no plan yet",
        "write blocked: meta file",
        "similar file exists",
        "stuck: goal verified, auto-completed",
        "stuck: retry with fresh context",
        "context reset: empty loop",
        "context reset: thinking loop",
        "inactivity auto-complete blocked",
        "verify gate stood down",
        "lint failed after write",
    ):
        assert f'"{label}"' in src, f"guard event missing: {label}"
