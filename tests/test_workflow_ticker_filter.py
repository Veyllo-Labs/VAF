# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The web ticker must survive a Rich Live animation storm - in EVERY lane that mirrors
output into the browser.

Live incident 2026-07-16: the research agent's Live animation streamed hundreds of ANSI
lines per second into the Web UI (one HTTP POST, one WebSocket event and one React render
each) until the tray browser froze and the WebSocket dropped. A hardened filter was written
then - and applied to one of four copies.

Live incident 2026-07-20: the in-chat workflow lane, one of the three unhardened copies,
pushed 48,359 frames in 181 seconds (mean 267/s, bursts of 12-18 lines every ~70 ms, which
is refresh_per_second=15). The browser socket died mid-run, and every event that would have
advanced or closed the Workflow Runtime panel was then broadcast to zero subscribers.

So the filter now lives in exactly one place (vaf/core/web_ticker.py) and this pins both its
contract and the fact that no lane may reimplement it.
"""
import ast
from pathlib import Path

from vaf.core.web_ticker import MirroredStdout, WebTicker

_REPO = Path(__file__).resolve().parents[1]

# Every module that mirrors a stream into the browser. Frozen: a new lane must use the
# shared ticker, not grow a fourth private copy.
_MIRROR_LANES = (
    "vaf/cli/cmd/workflow.py",
    "vaf/tools/workflow_executor.py",
    "vaf/tools/agent_workflow_builder.py",
)


def _source(rel: str) -> str:
    # read_bytes().decode("utf-8"): many first-party files are not cp1252-decodable, so a
    # bare read_text() passes on Linux and fails on the Windows CI runner only.
    return (_REPO / rel).read_bytes().decode("utf-8")


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_animation_storm_is_capped_stripped_and_deduped():
    clock = _FakeClock()
    sent = []
    f = WebTicker(sent.append, clock=clock)

    # 300 distinct ANSI-colored lines inside ONE rate window.
    for i in range(300):
        f.feed(f"\x1b[1;38;2;0;212;255mprogress frame {i}\x1b[0m\r\n")

    assert len(sent) == f.MAX_LINES_PER_WINDOW  # hard cap held
    assert all("\x1b" not in s and "\r" not in s for s in sent)  # web-safe
    assert sent[0] == "progress frame 0"

    # Next window: the skipped volume is surfaced once, then flow resumes.
    clock.now += 1.0
    f.feed("real content after the storm\n")
    assert sent[-2] == f"[... {300 - f.MAX_LINES_PER_WINDOW} lines skipped]"
    assert sent[-1] == "real content after the storm"


def test_control_frames_and_duplicate_redraws_are_dropped():
    sent = []
    f = WebTicker(sent.append, clock=_FakeClock())

    # Pure cursor-control / clear-line frames collapse to nothing.
    f.feed("\x1b[2K\x1b[1A\n\x1b[0m   \n")
    # A Live panel redraws the same visible line over and over.
    for _ in range(20):
        f.feed("\x1b[36mSection 1/2:\x1b[0m \x1b[37mResearch\x1b[0m\n")

    assert sent == ["Section 1/2: Research"]


def test_partial_lines_are_buffered_until_newline():
    sent = []
    f = WebTicker(sent.append, clock=_FakeClock())

    f.feed("chunk one, ")
    f.feed("chunk two")
    assert sent == []
    f.feed(" - done\n")
    assert sent == ["chunk one, chunk two - done"]


def test_osc_title_sequences_are_stripped():
    sent = []
    f = WebTicker(sent.append, clock=_FakeClock())
    f.feed("\x1b]0;window title\x07visible text\n")
    assert sent == ["visible text"]


def test_close_flushes_the_tail_and_the_pending_notice():
    """The old writers reached into a private buffer and only ever flushed stdout, so the
    last partial line (and any pending suppression notice) was silently dropped."""
    clock = _FakeClock()
    sent = []
    f = WebTicker(sent.append, clock=clock)
    for i in range(100):
        f.feed(f"line {i}\n")
    f.feed("a final line with no newline")
    f.close()
    # The notice first, then the tail - and the tail must survive even though the rate
    # window was already full when the run ended.
    assert "lines skipped" in sent[-2]
    assert sent[-1] == "a final line with no newline"


def test_run_ceiling_stops_an_endless_stream_once_and_says_so():
    """A per-window cap alone still allows an unbounded total. The browser keeps only a few
    hundred lines anyway, so an endless stream buys nothing and costs a socket."""
    clock = _FakeClock()
    sent = []
    f = WebTicker(sent.append, clock=clock, max_lines_per_run=20)
    for i in range(500):
        clock.now += 1.0          # a fresh rate window every line
        f.feed(f"line {i}\n")
    assert len(sent) == 21, "20 lines plus exactly one notice"
    assert "output limit" in sent[-1]


def test_activity_fires_even_for_lines_the_cap_drops():
    """The silence watchdog hangs off on_activity. If it were wired to the SEND path
    instead, rate-capping a chatty step would read as a hang and abort a healthy run."""
    clock = _FakeClock()
    sent, beats = [], []
    f = WebTicker(sent.append, on_activity=lambda: beats.append(1), clock=clock)
    for i in range(300):
        f.feed(f"frame {i}\n")
    assert len(sent) == f.MAX_LINES_PER_WINDOW
    assert len(beats) == 300, "activity must reflect the process, not the throttle"


def test_bounded_dedup_window_does_not_grow_without_limit():
    """The sub-agent stream lane deduped against an unbounded set - a slow memory leak in a
    long run."""
    f = WebTicker(lambda _l: None, dedup_window=8, clock=_FakeClock())
    for i in range(1000):
        f.feed(f"line {i}\n")
    assert len(f._recent) <= 8


def test_mirror_reports_the_interactivity_it_was_given():
    """isatty() must NOT be a constant. False stops Rich's Live at the source for lanes
    mirrored into the browser; the separate-terminal lane keeps the real stream's value,
    because that window exists to be watched and would otherwise lose its TUI and colour."""
    class _RealTty:
        def isatty(self):
            return True

        def write(self, _d):
            pass

        def flush(self):
            pass

    assert MirroredStdout(_RealTty(), None, interactive=False).isatty() is False
    assert MirroredStdout(_RealTty(), None).isatty() is True


def test_the_mirror_actually_switches_rich_off():
    """The load-bearing claim of the whole fix: Rich gates Live on isatty() AND on
    Console.is_terminal, and vaf/cli/tui.py builds its Console without an explicit file, so
    the Console resolves lazily from sys.stdout. Reporting isatty()=False must therefore flip
    BOTH terms - which is what stops the 15 fps animation at the source instead of filtering
    its frames afterwards. Verified against the installed rich, not assumed."""
    import sys

    from vaf.cli.tui import UI

    class _RealTty:
        def isatty(self):
            return True

        def write(self, _d):
            pass

        def flush(self):
            pass

    orig = sys.stdout
    try:
        sys.stdout = _RealTty()
        assert UI.console.is_terminal is True, "baseline: a real TTY reads as a terminal"
        sys.stdout = MirroredStdout(_RealTty(), None, interactive=False)
        assert UI.console.is_terminal is False, "the mirror must switch Rich off"
        sys.stdout = MirroredStdout(_RealTty(), None)          # separate-terminal lane
        assert UI.console.is_terminal is True, "the visible terminal keeps its TUI"
    finally:
        sys.stdout = orig


def test_the_visible_terminal_lane_keeps_its_tty():
    """Regression guard for a trap found in review: VAF_SESSION_ID is exported process-wide
    on every tool call, so the separate-terminal lane installs its mirror even in pure CLI
    mode. A constant isatty()=False there would strip the colour and the live TUI out of a
    window whose entire purpose is to be watched, and would also skip the auto-close
    countdown, which reads sys.stdout.isatty()."""
    src = _source("vaf/cli/cmd/workflow.py")
    assert "MirroredStdout(sys.stdout, send_web_line)" in src
    assert "interactive=False" not in src


def test_a_quiet_heavy_step_is_not_killed_as_stuck():
    """Turning the animation off removes the only thing that kept the 60 s silence watchdog
    fed during a long research step - those redraws WERE the heartbeat. Without a per-tool
    budget a healthy quiet step would now be aborted as 'stuck', which is the same fabricated
    failure this change set exists to remove. Heavy steps therefore fall back on the engine's
    own worst-case cap for that tool, so the two watchdogs agree."""
    from vaf.workflows.engine import SPAWNABLE_STEP_TOOLS, _workflow_step_timeout

    src = _source("vaf/tools/workflow_executor.py")
    assert "_silence_budget" in src, "the fixed 60 s timeout must be per-tool now"
    assert "_budget = _silence_budget()" in src

    assert "_TIMEOUT_HEAVY_FLOOR = 300.0" in src, "heavy steps need a silence floor"
    for tool in SPAWNABLE_STEP_TOOLS:
        budget = max(_workflow_step_timeout(tool), 300.0)
        assert budget >= 300.0, f"{tool} must get far more than the old 60 s of silence"
    # A cheap tool keeps its short leash - it has no reason to go quiet for minutes.
    assert _workflow_step_timeout("write_file") <= 600


def test_no_lane_reimplements_the_ticker():
    """Rule 2: the hardened filter existed for four days short of a year in ONE of four
    copies. This is the guard that stops a fifth."""
    offenders = []
    for rel in _MIRROR_LANES:
        src = _source(rel)
        if "from vaf.core.web_ticker import" not in src:
            offenders.append(f"{rel}: does not use the shared ticker")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ClassDef) and (
                "StreamWriter" in node.name or "TickerFilter" in node.name
            ):
                offenders.append(f"{rel}:{node.lineno}: private mirror class {node.name}")
    assert not offenders, "\n".join(offenders)


def test_only_the_shared_module_owns_the_ansi_regex():
    """A second ANSI regex is a second place to get the escaping subtly wrong."""
    owners = []
    for path in sorted((_REPO / "vaf").rglob("*.py")):
        if "\\x1b(?:\\[" in path.read_bytes().decode("utf-8"):
            owners.append(path.relative_to(_REPO).as_posix())
    assert owners == ["vaf/core/web_ticker.py"], owners


def test_the_shared_ticker_stays_import_safe():
    """It is imported from vaf.cli.*, vaf.tools.* and vaf.core.platform, so a vaf-internal
    import here could create a cycle."""
    tree = ast.parse(_source("vaf/core/web_ticker.py"))
    vaf_imports = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("vaf")
    ]
    assert not vaf_imports, "the shared ticker must stay stdlib-only"


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
