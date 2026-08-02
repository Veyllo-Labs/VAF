# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The agent bridge: the classic turn machinery driven through duck surfaces.

The load-bearing claim of the full-screen lane is that there is NO second turn
implementation: the REAL `_process_agent_message` (think-state machine, web
mirroring, session persistence) runs against `_StreamSurface`, and the summary
turn runs with the classic flag set. These tests therefore drive the real
imported functions with a FakeAgent and assert what arrives on the events
contract - if the classic function changes shape, they fail here first, which
is exactly the early warning the port needs.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from vaf.cli.tui_app.agent_bridge import AgentBridge, _inline_attachments


def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class Recorder:
    """The events contract, verbatim - a missing method here means the bridge
    grew a callback the app does not implement yet."""

    def __init__(self):
        self.calls = []

    def _rec(self, name, *args):
        self.calls.append((name, args))

    def turn_started(self, text):            self._rec("turn_started", text)
    def agent_message_start(self):           self._rec("agent_message_start")
    def agent_chunk(self, text):             self._rec("agent_chunk", text)
    def agent_think(self, text):             self._rec("agent_think", text)
    def agent_message_done(self):            self._rec("agent_message_done")
    def turn_finished(self, tools_ran):      self._rec("turn_finished", tools_ran)
    def event_note(self, t, m, s):           self._rec("event_note", t, m, s)
    def system_note(self, text):             self._rec("system_note", text)
    def renderable(self, obj):               self._rec("renderable", obj)
    def tool_start(self, tool, preview):     self._rec("tool_start", tool, preview)
    def tool_end(self, tool, ok, duration):  self._rec("tool_end", tool, ok, duration)
    def gate_required(self, tool, reason):   self._rec("gate_required", tool, reason)
    def gate_decision(self, decision):       self._rec("gate_decision", decision)
    def presence(self, state, detail=""):    self._rec("presence", state)
    def context(self, used, total):          self._rec("context", used, total)

    def names(self):
        return [c[0] for c in self.calls]

    def texts(self, name):
        return "".join(str(a[0]) for n, a in self.calls if n == name and a)


class FakeSession:
    def __init__(self):
        self.id = "sess-tui-test"
        self.messages = []

    def add_message(self, role, content, **kwargs):
        self.messages.append((role, content))


class FakeAgent:
    def __init__(self, script="Hello world"):
        self.script = script
        self.history = []
        self.current_session_id = "sess-tui-test"
        self.seen_inputs = []
        self.seen_kwargs = []
        self.LANGUAGE_NAMES_NATIVE = {"en": "English", "de": "Deutsch"}

    def _detect_user_language(self, text):
        return "en"

    def chat_step(self, user_input, stream_callback=None, **kwargs):
        self.seen_inputs.append(user_input)
        self.seen_kwargs.append(kwargs)
        if callable(self.script):
            return self.script(stream_callback)
        if isinstance(self.script, list):             # tags arrive as whole tokens,
            if stream_callback:                       # the way the engine emits them
                for chunk in self.script:
                    stream_callback(chunk)
            return "".join(self.script)
        if stream_callback:
            for i in range(0, len(self.script), 7):   # chunked like a real stream
                stream_callback(self.script[i:i + 7])
        return self.script

    def get_token_usage(self):
        return 10, 100

    def set_event_sink(self, sink):
        self.sink = sink

    def shutdown(self):
        pass


class FakeWeb:
    """Everything `_process_agent_message` mirrors to, as no-ops, plus the gate
    contract the responder test drives."""

    def __init__(self, resolve_after=0):
        self.resolved = []
        self._failures_left = resolve_after

    def update_status(self, *a, **k): pass
    def log(self, *a, **k): pass
    def emit_agent_message(self, *a, **k): pass
    def emit_message_complete(self, *a, **k): pass
    def emit_stats(self, *a, **k): pass
    def push_update(self, *a, **k): pass
    def register_agent(self, *a, **k): pass

    def resolve_gate(self, session_id, decision):
        if self._failures_left > 0:
            self._failures_left -= 1
            return False
        self.resolved.append((session_id, decision))
        return True


@pytest.fixture()
def quiet_run_module(monkeypatch, tmp_path):
    """Point the classic function's side channels at stubs: web mirror, the
    stream-debug log, the session save, speech, and the real IPC drain."""
    import vaf.cli.cmd.run as run_mod
    import vaf.core.session as session_mod
    import vaf.core.speech as speech_mod

    web = FakeWeb()
    monkeypatch.setattr(run_mod, "get_web_interface", lambda: web)
    monkeypatch.setattr(run_mod, "get_dated_log_path",
                        lambda name, ext: tmp_path / f"{name}.{ext}")

    saved = []

    class _StubMgr:
        def __init__(self, *a, **k): pass
        def save(self, session, **k): saved.append(session)
        def list(self, limit=50, **k): return []

    monkeypatch.setattr(session_mod, "SessionManager", _StubMgr)
    monkeypatch.setattr(speech_mod, "get_speech_manager",
                        lambda: SimpleNamespace(stop=lambda: None))
    monkeypatch.setattr(run_mod, "_check_subagent_results", lambda tui, agent: [])
    return SimpleNamespace(web=web, saved=saved)


def _make_bridge(agent, events):
    return AgentBridge(agent, FakeSession(), None, events)


# ── the real _process_agent_message through _StreamSurface ──────────────────────────

def test_think_and_answer_split_through_the_real_classic_function(quiet_run_module):
    """The whole routing contract is one word: the classic think machine prints
    style="dim" for think text and "bold ..." for answer text."""
    events = Recorder()
    bridge = _make_bridge(FakeAgent(
        ["<think>", "pondering deeply", "</think>", "The answer."]), events)

    bridge.submit_turn("hi")
    assert _wait_for(lambda: "turn_finished" in events.names())

    assert "pondering deeply" in events.texts("agent_think")
    assert "The answer." in events.texts("agent_chunk")
    assert "pondering" not in events.texts("agent_chunk")
    bridge.shutdown()


def test_turn_lifecycle_order_and_session_persistence(quiet_run_module):
    events = Recorder()
    agent = FakeAgent("Plain reply")
    bridge = _make_bridge(agent, events)

    bridge.submit_turn("hello there")
    assert _wait_for(lambda: "turn_finished" in events.names())

    names = events.names()
    assert names.index("turn_started") < names.index("agent_message_start")
    assert names.index("agent_message_start") < names.index("agent_message_done")
    assert names.index("agent_message_done") <= names.index("turn_finished")

    # Caller-side contract: the USER message is the bridge's job, the assistant
    # message is `_process_agent_message`'s (with think tags preserved as XML).
    assert ("user", "hello there") in bridge.session.messages
    assert any(r == "assistant" and "Plain reply" in c
               for r, c in bridge.session.messages)
    assert ("context", (10, 100)) in events.calls
    bridge.shutdown()


def test_async_ack_reply_reaches_the_transcript(quiet_run_module):
    """[ASYNC_ACK] bypasses streaming; the classic function prints it directly,
    so it must arrive as a chunk, not vanish."""
    events = Recorder()
    bridge = _make_bridge(FakeAgent(lambda cb: "[ASYNC_ACK] Task started."), events)

    bridge.submit_turn("go")
    assert _wait_for(lambda: "turn_finished" in events.names())

    assert "Task started." in events.texts("agent_chunk")
    bridge.shutdown()


def test_a_crashing_turn_becomes_an_error_note_not_a_dead_lane(quiet_run_module):
    events = Recorder()

    def _boom(cb):
        raise RuntimeError("backend fell over")

    bridge = _make_bridge(FakeAgent(_boom), events)
    bridge.submit_turn("hi")
    assert _wait_for(lambda: "turn_finished" in events.names())

    assert any(n == "event_note" and a[0] == "Error" and "backend fell over" in a[1]
               for n, a in events.calls)
    assert not bridge.busy

    # The lane survives: a second turn still runs.
    bridge.agent.script = "recovered"
    bridge.submit_turn("again")
    assert _wait_for(lambda: "recovered" in events.texts("agent_chunk"))
    bridge.shutdown()


def test_at_file_references_are_inlined_before_the_agent_sees_them(quiet_run_module, tmp_path):
    note = tmp_path / "note.txt"
    note.write_text("the secret ingredient")

    events = Recorder()
    agent = FakeAgent("ok")
    bridge = _make_bridge(agent, events)
    bridge.submit_turn(f"summarize @{note}")
    assert _wait_for(lambda: "turn_finished" in events.names())

    # Classic wrapper, byte-faithful: full user-typed path, classic delimiters.
    assert any("the secret ingredient" in i and f"--- FILE: {note} ---" in i
               for i in agent.seen_inputs)
    bridge.shutdown()


def test_failed_attachment_reports_and_keeps_the_token(quiet_run_module):
    """Classic behavior: a failed read is a VISIBLE error and the literal
    @token stays in the message - never a silent drop."""
    events = Recorder()
    agent = FakeAgent("ok")
    bridge = _make_bridge(agent, events)
    bridge.submit_turn("see @/nonexistent/nope.txt")
    assert _wait_for(lambda: "turn_finished" in events.names())

    assert any(n == "event_note" and a[0] == "Error" and "Failed to attach" in a[1]
               for n, a in events.calls)
    assert any("@/nonexistent/nope.txt" in i for i in agent.seen_inputs)
    bridge.shutdown()


def test_inline_attachments_leaves_nonfiles_alone():
    assert _inline_attachments("ping @nonexistent/path.txt") == "ping @nonexistent/path.txt"
    assert _inline_attachments("no at-sign here") == "no at-sign here"


def test_tool_turns_end_in_success_then_settle_to_idle(quiet_run_module):
    """The success ring must actually render: the lane's final idle is deferred
    past the avatar's one-shot window instead of overwriting success in the
    same tick."""
    events = Recorder()
    agent = FakeAgent("placeholder")
    bridge = _make_bridge(agent, events)

    def _script(cb):
        bridge.on_sink_event({"type": "tool_start", "tool": "x", "args": {}})
        bridge.on_sink_event({"type": "tool_end", "tool": "x", "ok": True})
        cb("ok")
        return "ok"

    agent.script = _script
    bridge.submit_turn("go")
    assert _wait_for(lambda: "turn_finished" in events.names())
    time.sleep(0.3)

    presences = [a[0] for n, a in events.calls if n == "presence"]
    assert presences[-1] == "success", presences
    assert _wait_for(
        lambda: [a[0] for n, a in events.calls if n == "presence"][-1] == "idle",
        timeout=4)
    bridge.shutdown()


def test_first_answer_chunk_flips_presence_to_talking(quiet_run_module):
    events = Recorder()
    bridge = _make_bridge(FakeAgent("A reply."), events)
    bridge.submit_turn("hi")
    assert _wait_for(lambda: "turn_finished" in events.names())

    presences = [a[0] for n, a in events.calls if n == "presence"]
    assert "talking" in presences
    bridge.shutdown()


# ── the drain summary turn ──────────────────────────────────────────────────────────

def test_drain_summary_runs_with_the_classic_flags(quiet_run_module, monkeypatch):
    """Found results trigger the summary turn EXACTLY like run.py:1264-1307:
    workflows disabled, tools enabled, results embedded in the instruction."""
    import vaf.cli.cmd.run as run_mod
    monkeypatch.setattr(run_mod, "_check_subagent_results",
                        lambda tui, agent: ["research finding one"])

    events = Recorder()
    agent = FakeAgent("Summary: done.")
    bridge = _make_bridge(agent, events)

    bridge.drain_tick()
    assert _wait_for(lambda: "Summary: done." in events.texts("agent_chunk"))

    instruction = agent.seen_inputs[-1]
    kwargs = agent.seen_kwargs[-1]
    assert "research finding one" in instruction
    assert kwargs.get("disable_workflows") is True
    assert kwargs.get("disable_tools") is False
    assert ("context", (10, 100)) in events.calls
    bridge.shutdown()


def test_drain_tick_never_interleaves_with_a_running_turn(quiet_run_module):
    """Rule 4.3 territory: the drain must not run while a turn holds the lane."""
    events = Recorder()
    bridge = _make_bridge(FakeAgent("x"), events)
    submitted = []
    bridge._submit = lambda fn: submitted.append(fn)

    bridge._busy = True
    bridge.drain_tick()
    assert submitted == []

    bridge._busy = False
    bridge.drain_tick()
    assert len(submitted) == 1


# ── the engine event sink ───────────────────────────────────────────────────────────

def test_sink_events_dispatch_to_ui_events(quiet_run_module):
    events = Recorder()
    bridge = _make_bridge(FakeAgent(), events)

    bridge.on_sink_event({"type": "tool_start", "tool": "web_search",
                          "args": {"query": "x" * 50}})
    bridge.on_sink_event({"type": "tool_end", "tool": "web_search",
                          "ok": True, "duration_ms": 1500})
    bridge.on_sink_event({"type": "gate_required", "tool": "run_command",
                          "reason": "shell access"})
    bridge.on_sink_event({"type": "gate_decision", "decision": "allow_once"})
    bridge.on_sink_event({"type": "llm_start"})                 # enrichment only
    bridge.on_sink_event({"type": "no_such_event", "x": object()})  # never raises

    assert ("tool_start", ("web_search", "query=" + "x" * 24)) in events.calls
    assert ("tool_end", ("web_search", True, "1.5s")) in events.calls
    assert ("gate_required", ("run_command", "shell access")) in events.calls
    assert ("gate_decision", ("allow_once",)) in events.calls
    assert bridge._tools_ran is True


def test_console_sink_events_become_event_notes(quiet_run_module):
    events = Recorder()
    bridge = _make_bridge(FakeAgent(), events)
    bridge.on_console_event("Router", "3 tools selected", "info")
    assert ("event_note", ("Router", "3 tools selected", "info")) in events.calls


# ── the gate responder ──────────────────────────────────────────────────────────────

def test_gate_answer_resolves_through_the_web_contract_with_retry(quiet_run_module):
    """The responder must survive the race where the engine has not yet
    registered the gate: first attempts return False, then it lands."""
    web = FakeWeb(resolve_after=2)
    events = Recorder()
    bridge = AgentBridge(FakeAgent(), FakeSession(), None, events,
                         web_interface_getter=lambda: web)

    bridge.answer_gate("always")
    assert _wait_for(lambda: web.resolved)
    assert web.resolved == [("sess-tui-test", "allow_always")]


def test_gate_words_map_to_the_engine_decisions(quiet_run_module):
    assert AgentBridge.DECISIONS == {"once": "allow_once",
                                     "always": "allow_always",
                                     "cancel": "cancel"}
    assert AgentBridge.DECISIONS.get("garbage", "cancel") == "cancel"


# ── chrome data ─────────────────────────────────────────────────────────────────────

def test_tasks_snapshot_builds_the_three_marker_kinds(quiet_run_module, monkeypatch):
    from datetime import datetime, timedelta
    import vaf.core.subagent_ipc as ipc_mod

    t0 = (datetime.now() - timedelta(seconds=90)).isoformat()
    fake_ipc = SimpleNamespace(
        get_active_tasks_for_current_session=lambda: [
            SimpleNamespace(task_id="aaaabbbbcccc", agent_type="librarian_agent",
                            created_at=t0),
            SimpleNamespace(task_id="ddddeeeeffff", agent_type="workflow:deep_research",
                            created_at=t0),
        ],
        get_paused_workflows_for_session=lambda sid: [
            SimpleNamespace(waiting_for_task_id="11112222333344",
                            workflow_name="deep_research", created_at=t0),
        ],
    )
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: fake_ipc)
    monkeypatch.setattr(ipc_mod, "get_current_session_id", lambda: "sess-tui-test")

    bridge = _make_bridge(FakeAgent(), Recorder())
    rows = bridge.tasks_snapshot()

    assert ("[>]", "librarian_agent", "aaaabbbb", "1m 30s") in rows
    assert ("[>>]", "deep_research", "ddddeeee", "1m 30s") in rows
    assert ("[||]", "deep_research", "11112222", "1m 30s") in rows


def test_quitting_mid_turn_never_blocks_process_exit():
    """The freeze this pins: a ThreadPoolExecutor lane is joined by an atexit
    hook, so quitting during a blocked turn (a gate wait, a slow request)
    froze the closed terminal until the turn finished - up to 300 s. With the
    daemon lane, the interpreter must exit promptly even while a turn sleeps.
    Runs as a subprocess because the defect IS interpreter-exit behavior."""
    import subprocess
    import sys
    import time as _time

    probe = r'''
import sys, time, tempfile
from pathlib import Path
from types import SimpleNamespace
import vaf.cli.cmd.run as rm
import vaf.core.session as sm
import vaf.core.speech as sp

tmp = Path(tempfile.mkdtemp())
class W:
    def resolve_gate(self, sid, d): return True
    def __getattr__(self, n): return lambda *a, **k: None
rm.get_web_interface = lambda: W()
rm.get_dated_log_path = lambda name, ext: tmp / f"{name}.{ext}"
rm._check_subagent_results = lambda t, a: []
sm.SessionManager = type("M", (), {"__init__": lambda s, *a, **k: None,
                                   "save": lambda s, x, **k: None,
                                   "list": lambda s, **k: []})
sp.get_speech_manager = lambda: SimpleNamespace(stop=lambda: None)

from vaf.cli.tui_app.agent_bridge import AgentBridge
class A:
    LANGUAGE_NAMES_NATIVE = {}
    history = []
    current_session_id = "s"
    def _detect_user_language(self, t): return "en"
    def chat_step(self, *a, **k): time.sleep(60); return ""
    def get_token_usage(self): return 0, 1
    def set_event_sink(self, s): pass
    def shutdown(self): pass
names = ("turn_started","agent_message_start","agent_chunk","agent_think",
         "agent_message_done","turn_finished","event_note","system_note",
         "renderable","tool_start","tool_end","gate_required","gate_decision",
         "presence","context")
ev = SimpleNamespace(**{n: (lambda *a, **k: None) for n in names})
sess = SimpleNamespace(id="s", messages=[], add_message=lambda *a, **k: None)
mgr = SimpleNamespace(save=lambda s, **k: None)
b = AgentBridge(A(), sess, mgr, ev, web_interface_getter=lambda: W())
b.submit_turn("x")
time.sleep(0.7)                 # the turn is now blocked inside chat_step
b.shutdown()
print("MAIN-DONE", flush=True)
'''
    start = _time.monotonic()
    result = subprocess.run([sys.executable, "-c", probe],
                            capture_output=True, text=True, timeout=30)
    elapsed = _time.monotonic() - start
    assert result.returncode == 0, result.stderr
    assert "MAIN-DONE" in result.stdout
    assert elapsed < 25, f"interpreter exit took {elapsed:.1f}s - the lane blocked it"


def test_shutdown_saves_the_session_and_detaches_the_sink(quiet_run_module):
    events = Recorder()
    agent = FakeAgent()
    saved = []
    mgr = SimpleNamespace(save=lambda s, **k: saved.append(s.id),
                          list=lambda **k: [])
    bridge = AgentBridge(agent, FakeSession(), mgr, events)
    bridge.shutdown()

    assert saved == ["sess-tui-test"]
    assert agent.sink is None
