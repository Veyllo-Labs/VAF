# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The workflow web ticker must survive a Rich Live animation storm.

Live incident 2026-07-16: the research agent's Live animation streamed
hundreds of ANSI lines per second into the Web UI (one HTTP POST + one
WebSocket event + one React render each) until the tray browser froze and
the WebSocket dropped. _WebTickerFilter enforces ticker semantics at the
emit site; this pins its contract.
"""
import vaf.cli.cmd.workflow as wf


class _FakeTime:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def test_animation_storm_is_capped_stripped_and_deduped(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(wf, "time", fake)
    sent = []
    f = wf._WebTickerFilter(sent.append)

    # 300 distinct ANSI-colored lines inside ONE rate window.
    for i in range(300):
        f.feed(f"\x1b[1;38;2;0;212;255mprogress frame {i}\x1b[0m\r\n")

    assert len(sent) == f.MAX_LINES_PER_WINDOW  # hard cap held
    assert all("\x1b" not in s and "\r" not in s for s in sent)  # web-safe
    assert sent[0] == "progress frame 0"

    # Next window: the skipped volume is surfaced once, then flow resumes.
    fake.now += 1.0
    f.feed("real content after the storm\n")
    assert sent[-2] == f"[... {300 - f.MAX_LINES_PER_WINDOW} lines skipped]"
    assert sent[-1] == "real content after the storm"


def test_control_frames_and_duplicate_redraws_are_dropped(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(wf, "time", fake)
    sent = []
    f = wf._WebTickerFilter(sent.append)

    # Pure cursor-control / clear-line frames collapse to nothing.
    f.feed("\x1b[2K\x1b[1A\n\x1b[0m   \n")
    # A Live panel redraws the same visible line over and over.
    for _ in range(20):
        f.feed("\x1b[36mSection 1/2:\x1b[0m \x1b[37mResearch\x1b[0m\n")

    assert sent == ["Section 1/2: Research"]


def test_partial_lines_are_buffered_until_newline(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(wf, "time", fake)
    sent = []
    f = wf._WebTickerFilter(sent.append)

    f.feed("chunk one, ")
    f.feed("chunk two")
    assert sent == []
    f.feed(" - done\n")
    assert sent == ["chunk one, chunk two - done"]


def test_osc_title_sequences_are_stripped(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(wf, "time", fake)
    sent = []
    f = wf._WebTickerFilter(sent.append)
    f.feed("\x1b]0;window title\x07visible text\n")
    assert sent == ["visible text"]


def test_execute_workflow_blocks_a_duplicate_live_run(monkeypatch):
    """Live incident 2026-07-16: after empty-response snapshot resets the
    model re-called execute_workflow while the first run was still live -
    two concurrent research workflows on one GPU. Session-scoped IPC is the
    truth; a duplicate must be refused with an honest status. The guard now
    runs on SubAgentIPC.has_live_task (shared with the async terminal lane;
    full concurrency/self-registration coverage lives in
    test_workflow_duplicate_guard.py)."""
    import types

    import vaf.core.subagent_ipc as ipc_mod
    from vaf.tools.workflow_executor import ExecuteWorkflowTool

    seen = {}

    class _FakeIpc:
        def has_live_task(self, agent_type, session_id, **kw):
            seen["args"] = (agent_type, session_id)
            return agent_type == "workflow:research_and_document"

    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIpc())
    _agent = types.SimpleNamespace(current_session_id="sess-dup-test", tools={})
    result = ExecuteWorkflowTool().run("research_and_document", {"topic": "x"}, _agent=_agent)
    assert "ALREADY RUNNING" in result
    # The guard must check THIS session, not the module-global fallback.
    assert seen["args"] == ("workflow:research_and_document", "sess-dup-test")

    # A different workflow id is NOT blocked by the guard (it proceeds into
    # normal resolution; unknown id yields the not-found message).
    result2 = ExecuteWorkflowTool().run("some_other_wf_xyz", {}, _agent=_agent)
    assert "ALREADY RUNNING" not in result2
    assert "not found" in result2


def test_bounded_run_sets_cancel_event_for_the_abandoned_worker():
    """Stop semantics: run_bounded cannot kill a thread, it abandons it. The
    thread-local cancel event lets the worker exit at its next checkpoint
    instead of crawling on as a zombie (live incident 2026-07-16: web_search
    kept calling the local LLM 42s after the stop). Unlike the shared
    should_stop flag, the event cannot be cleared by the main loop."""
    import threading
    import time as _time

    from vaf.core.bounded_run import STOPPED_PREFIX, cancel_requested, run_bounded

    worker_exited = threading.Event()
    saw_cancel = {}

    def _looping_tool():
        for _ in range(200):  # ~10s worst case; exits at the first checkpoint
            if cancel_requested():
                saw_cancel["yes"] = True
                worker_exited.set()
                return "aborted-early"
            _time.sleep(0.05)
        worker_exited.set()
        return "ran-to-completion"

    result = run_bounded(
        _looping_tool, timeout=30, stop_check=lambda: True, poll=0.05, label="test"
    )
    assert isinstance(result, str) and result.startswith(STOPPED_PREFIX)
    assert worker_exited.wait(timeout=5.0), "worker never exited - zombie"
    assert saw_cancel.get("yes") is True

    # Outside a bounded worker the helper is inert.
    assert cancel_requested() is False


def test_tool_result_is_error_recognizes_all_failure_prefixes():
    """Live incident: a failed write_file rendered '-> OK: Tool Error'
    because context.py's detector missed the 'Tool Error:' prefix, and the
    local model reported the (non-existent) file as created. One shared
    detector now backs the retry guard, the summarizer and the tool_end flag."""
    from vaf.core.context import tool_result_is_error, summarize_tool_turn

    fail = [
        "Tool Error: invalid arguments for 'write_file': 'path' is a required property",
        "Security Error: Tool 'x' requires an admin session.",
        "[PLAN REQUIRED] set your approach first",
        "Error: Unknown tool 'foo'",
        "❌ something broke",
        "Traceback (most recent call last):\n  File ...",
        "Exception: boom",
    ]
    for f in fail:
        assert tool_result_is_error(f), f

    ok = [
        "Saved: /tmp/report.html",
        "No errors found in the document.",
        "Message sent to the user via Telegram.",
        "### Web Search Results ...",
    ]
    for o in ok:
        assert not tool_result_is_error(o), o

    # End to end: the per-turn summary must label the failed write FAILED, not OK.
    msgs = [
        {"role": "assistant", "tool_calls": [{"function": {"name": "write_file"}}]},
        {"role": "tool", "name": "write_file",
         "content": "Tool Error: invalid arguments for 'write_file': 'path' is a required property"},
    ]
    summary = summarize_tool_turn(msgs)
    assert summary and "FAILED" in summary and "→ OK" not in summary


def test_execute_workflow_redirects_tool_name_confusion(monkeypatch):
    """Live incident: a weak local model called execute_workflow with
    workflow_id="create_agent_workflow" (the builder TOOL's own name, not a
    saved template) and got a plain not-found listing that did not explain
    the actual mistake. The error now detects a live tool-name collision and
    redirects to the right tool."""
    import vaf.core.subagent_ipc as ipc_mod
    from vaf.tools.workflow_executor import ExecuteWorkflowTool

    class _FakeIpc:
        def get_active_tasks_for_current_session(self):
            return []

    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIpc())

    class _FakeAgent:
        tools = {"create_agent_workflow": object(), "web_search": object()}

    result = ExecuteWorkflowTool().run(
        "create_agent_workflow", {}, _agent=_FakeAgent()
    )
    assert "is the name of a TOOL, not a saved workflow" in result
    assert "call the 'create_agent_workflow' tool directly" in result

    # A genuinely unknown id (no tool-name collision) keeps the plain listing.
    result2 = ExecuteWorkflowTool().run("totally_made_up_xyz", {}, _agent=_FakeAgent())
    assert "not a saved workflow" not in result2
    assert "not found" in result2

    # No _agent kwarg (defensive path) must not crash - falls back to listing.
    result3 = ExecuteWorkflowTool().run("create_agent_workflow", {})
    assert "not found" in result3 or "not a saved workflow" in result3
