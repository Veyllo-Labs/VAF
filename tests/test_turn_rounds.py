# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every generation round of a turn has its own place: in the engine's events, in
`last_turn.rounds`, in the web chat lane's stream, in the stored answer and in a reloaded chat.

A turn is a loop of generations, and a guard can send a reply back for another one (the
model-health re-run, a result-grounding correction). The web lane streamed all of a turn into
one buffer, so a re-run's reasoning grew the first round's thinking block and the first
round's text, written before the forced tool call, was shown below the tool card. The engine
now marks each generation (`round_start`, `TurnStatus.rounds`) and the hosts lay a turn out
per round.

On the real `Agent` with only the provider faked (the local server's HTTP post, the API
lane's `chat_completion`), and through the real queue runner with the provider faked.

MUTATIONS, each turns the named test red:
- remove the `round_start` emit, or move it below the provider request
  -> test_round_start_per_generation_in_order;
- set `answer=True` in `_model_health_rerun`, or drop the tool-id assignment
  -> test_rounds_record_tools_and_answers;
- mark the grounding-flagged round as an answer -> test_rounds_grounding_correction;
- mark only the last answer round -> test_rounds_auto_continue_marks_both_answers;
- `compare=True` on `TurnStatus.rounds` -> test_rounds_do_not_change_equality;
- call `clear()` without the kept text, or drop the containment filter
  -> test_stored_buffer_unchanged;
- send no round with the retraction -> test_retraction_names_its_round;
- skip the segment switch on `round_start`, put `keep` into the segment instead of `parts`,
  or drop the unthrottled flush -> test_chat_turn_stream_segments;
- store the rounds for a single round -> test_rounds_metadata_only_for_multi_round;
- compute the rounds before the draft branch -> test_draft_turn_stores_no_rounds;
- leave the sink installed after the turn -> test_runner_streams_and_stores_a_turn_per_round;
- overwrite the stored content with the last segment -> test_runner_stores_a_corrected_turn_whole;
- emit all tool entries after the last round, keep the stored tool-call entry, or give the
  rounds equal timestamps -> test_projection_interleaves_rounds_and_tools;
- remove the AGENT_STATE entry or the facade export -> test_facade_exports_turn_round,
  tests/test_turn_binding.py.
"""
import contextvars
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import vaf
from vaf.core.platform import Platform
from vaf.core.session import TurnRound, TurnStatus

SESSION = "green123456"
ASK = "Bitte installiere mir die zehn Pakete aus der Liste"
SAY = "Ich installiere jetzt die zehn Pakete mit python_sandbox und melde mich gleich."
DONE = "Alle zehn Pakete sind installiert."
TASK = "python_sandbox"

# What the web lane's buffer holds after the model-health turn below, recorded on the code
# before rounds existed (the clear followed by a re-streamed kept answer). The stored answer
# must stay byte-identical.
STORED_BEFORE = (SAY + "\n\n<think>Reasoning C.</think>\n\n" + DONE + "\n")


class _Install(vaf.BaseTool):
    """A stand-in for VAF's own python_sandbox (the forced set takes only VAF's own tools)."""
    __module__ = "vaf.tools.python_sandbox"
    name = TASK
    description = "Install Python packages."
    permission_level = "write"
    side_effect_class = "reversible"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return "10/10 installed"


class _Buffer:
    """The web lane's stream callback shape: callable, with clear(keep). Chunks and the sink's
    events go into one shared log, so their order can be read."""

    def __init__(self, log):
        self.log = log
        self.chunks = []

    def __call__(self, text):
        self.chunks.append(str(text))
        self.log.append(("chunk", str(text)))

    def clear(self, keep=""):
        self.chunks = [keep] if keep else []

    @property
    def text(self):
        return "".join(self.chunks)


class _Stream:
    status_code = 200
    text = ""

    def __init__(self, deltas):
        self._lines = [b"data: " + json.dumps({"choices": [{"delta": d}]}).encode() for d in deltas]
        self._lines.append(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
        self._lines.append(b"data: [DONE]")

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        pass


class _Refused:
    status_code = 500
    text = ""

    def json(self):
        return {}


@pytest.fixture
def make(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    for var in ("VAF_IN_SUBAGENT_TERMINAL", "VAF_THINKING_MODE", "VAF_IN_AUTOMATION"):
        monkeypatch.delenv(var, raising=False)
    from vaf.core import model_health as mh
    from vaf.core.agent import Agent

    def _make(lane="server"):
        a = Agent(register_signals=False, run_kind="chat",
                  config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
        if lane == "server":
            a.api_backend = None
            a.use_server = True
        monkeypatch.setattr(a, "_try_workflow", lambda *x, **k: None)
        monkeypatch.setattr(a, "_route_tools", lambda text: [TASK])
        monkeypatch.setattr(a, "get_token_usage", lambda **k: (1000, 32768))
        monkeypatch.setattr(a, "manage_context", lambda *x, **k: None)
        monkeypatch.setattr(a, "analyze_intent", lambda *x, **k: 0.7)
        a.tools[TASK] = _Install()
        a.retractions = []
        monkeypatch.setattr(a, "_clear_last_assistant_ui", lambda sid: a.retractions.append(sid))
        monkeypatch.setattr(a, "_detect_ungrounded_result_claim", lambda text, res: (False, None))
        a.current_session_id = SESSION
        a._bind_session_persistence(SESSION)
        mh.reset_metrics()
        return a
    return _make


def _health_turn(agent, monkeypatch, callback, sink=None):
    """The model-health turn on the local server lane: round 1 announces and calls nothing,
    the re-run (round 2) is forced to call the tool, round 3 answers. Each round streams its
    own reasoning first."""
    from vaf.core import model_health as mh
    agent.main_persistence.save_model_health(mh.to_payload(mh.State(5.0, "de")))
    script = [
        [{"reasoning_content": "Reasoning A."}, {"content": SAY}],
        [{"reasoning_content": "Reasoning B."},
         {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": TASK, "arguments": "{}"}}]}],
        [{"reasoning_content": "Reasoning C."}, {"content": DONE}],
    ]
    payloads = []

    def post(url, json=None, **kw):
        if isinstance(json, dict) and json.get("stream") and "messages" in json:
            payloads.append(json)
            return _Stream(script[min(len(payloads), len(script)) - 1])
        return _Refused()
    monkeypatch.setattr(requests, "post", post)
    agent.set_event_sink(sink)
    try:
        agent.chat_step(user_input=ASK, raw_user_input=ASK, stream_callback=callback)
    finally:
        agent.set_event_sink(None)
    assert len(payloads) == 3, "the scripted turn did not run three generations"
    assert agent.tools[TASK].calls, "the forced call ran"


# ── the engine ────────────────────────────────────────────────────────────────────────────

def test_round_start_per_generation_in_order(make, monkeypatch):
    agent = make()
    log = []
    buf = _Buffer(log)
    _health_turn(agent, monkeypatch, buf, sink=lambda evt: log.append(("event", evt)))

    starts = [i for i, (kind, v) in enumerate(log) if kind == "event" and v.get("type") == "round_start"]
    assert [log[i][1]["round"] for i in starts] == [1, 2, 3]
    s1, s2, s3 = starts
    assert s1 < min(i for i, (kind, _) in enumerate(log) if kind == "chunk"), (
        "a generation streamed before its round_start")

    def where(text):
        return next(i for i, (kind, v) in enumerate(log) if kind == "chunk" and text in v)
    assert s1 < where(SAY) < s2, "the first round's text is not between round 1 and 2"
    assert s2 < where("Reasoning B.") < s3, "the re-run's reasoning is not in round 2"
    assert s3 < where("Reasoning C.") and s3 < where(DONE)


def test_rounds_record_tools_and_answers(make, monkeypatch):
    agent = make()
    _health_turn(agent, monkeypatch, _Buffer([]))
    assert agent.last_turn.rounds == (TurnRound(1, (), False), TurnRound(2, ("c1",), False),
                                      TurnRound(3, (), True))
    assert agent.last_turn == TurnStatus()


def _api_turn(agent, script, text=ASK):
    """The API lane: each main call answers from `script` (a text, or a tool call tuple)."""
    calls = []

    def model(messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        calls.append(kw)
        if len(calls) > 8:
            raise AssertionError("runaway turn")
        step = script.pop(0) if script else "Fertig."
        if isinstance(step, str):
            yield step
            return
        yield "Einen Moment."
        yield json.dumps({"tool_calls": [{"index": 0, "id": f"c{len(calls)}", "type": "function",
                                          "function": {"name": step[0], "arguments": "{}"}}]})
        yield json.dumps({"finish_reason": "tool_calls"})
    agent.api_backend.chat_completion = model
    agent.chat_step(user_input=text, raw_user_input=text, stream_callback=_Buffer([]))
    return calls


def test_rounds_grounding_correction(make, monkeypatch):
    """The flagged reply is kept and sent back: its round is not an answer, the correction's
    round is."""
    agent = make("api")
    verdicts = iter([(True, "Die Datei wurde gespeichert."), (False, None)])
    monkeypatch.setattr(agent, "_detect_ungrounded_result_claim",
                        lambda text, res: next(verdicts, (False, None)))
    _api_turn(agent, ["Die Datei wurde gespeichert, alles erledigt.",
                      "Korrektur: Es wurde nichts gespeichert, ich habe kein Werkzeug benutzt."])
    assert [(r.round, r.answer) for r in agent.last_turn.rounds] == [(1, False), (2, True)]


def test_rounds_auto_continue_marks_both_answers(make, monkeypatch):
    """A validated answer followed by the pending-task auto-continue: both answer rounds are
    answers, the tool round between them is not."""
    agent = make("api")
    steps = iter([(0, "Pakete installieren", 0, 1)])
    monkeypatch.setattr(agent.main_persistence, "_current_step", lambda tasks: next(steps, None))
    monkeypatch.setattr(agent, "_reply_needs_user", lambda text: False)
    _api_turn(agent, ["Hier ist die Liste der zehn Pakete: numpy, pandas und acht weitere.",
                      (TASK,), "Alle zehn Pakete sind jetzt installiert."])
    assert [(r.round, r.tool_call_ids, r.answer) for r in agent.last_turn.rounds] == [
        (1, (), True), (2, ("c2",), False), (3, (), True)]


def test_rounds_do_not_change_equality():
    """`last_turn == TurnStatus()` keeps meaning "answered, nothing failed"."""
    assert TurnStatus(rounds=(TurnRound(1, (), True),)) == TurnStatus()
    assert TurnStatus(failed=True, rounds=(TurnRound(1),)) != TurnStatus()
    # Coerced at the boundary, like the other two fields.
    status = TurnStatus(rounds=[{"round": "2", "tool_call_ids": ["c1"], "answer": 1}, (3, "c2")])
    assert status.rounds == (TurnRound(2, ("c1",), True), TurnRound(3, ("c2",), False))
    with pytest.raises(AttributeError):
        status.rounds[0].round = 4  # noqa: B018


def test_stored_buffer_unchanged(make, monkeypatch):
    """The web lane's buffer, which is the stored answer, is byte-identical to what it was
    before rounds existed: the clear is handed the kept answers instead of a re-streamed chunk."""
    agent = make()
    buf = _Buffer([])
    _health_turn(agent, monkeypatch, buf)
    assert buf.text == STORED_BEFORE


class _ZeroArgBuffer(_Buffer):
    """A stream callback written for the older hook: `clear()` takes nothing."""

    def clear(self):
        self.chunks = []


def test_a_zero_argument_clear_gets_the_old_buffer(make, monkeypatch):
    """An embedder's callback on `CoreAgent.chat_step` whose `clear` takes no argument is
    still cleared, and the kept answer comes back as a chunk: its buffer is the same value
    the web lane's has. MUTATION: call `clear(keep)` unconditionally in
    `_clear_stream_buffer` - the TypeError is swallowed and the announcement stays."""
    agent = make()
    buf = _ZeroArgBuffer([])
    _health_turn(agent, monkeypatch, buf)
    assert buf.text == STORED_BEFORE


def test_retraction_names_its_round(make, monkeypatch):
    """The empty-response retry takes back its own round's bubble and nothing else."""
    from vaf.core.agent import Agent
    from vaf.core.web_interface import WebInterfaceManager
    agent = make("api")
    sent = []
    web = SimpleNamespace(emit_clear_last_assistant=lambda sid, round=None: sent.append((sid, round)))
    monkeypatch.setattr("vaf.core.web_interface.get_web_interface", lambda: web)
    agent._turn_rounds = [{"round": 1, "tool_call_ids": ("c1",), "answer": False},
                          {"round": 2, "tool_call_ids": (), "answer": False}]
    Agent._clear_last_assistant_ui(agent, SESSION)
    assert sent == [(SESSION, 2)]

    pushed = []
    fake = SimpleNamespace(_push_session_update=lambda sid, payload: pushed.append(payload))
    WebInterfaceManager.emit_clear_last_assistant(fake, SESSION, round=2)
    WebInterfaceManager.emit_clear_last_assistant(fake, SESSION)
    assert pushed == [{"type": "clear_last_assistant", "round": 2}, {"type": "clear_last_assistant"}]


# ── the chat lane's stream ────────────────────────────────────────────────────────────────

def test_chat_turn_stream_segments(make, monkeypatch):
    """The real engine's event and chunk order through the chat lane's stream: one segment
    per round, every emit tagged, each closed round sent once more in full, and the stored
    copy unchanged. The clock stands still, so only the very first chunk passes the throttle
    and everything else of a round arrives through the unthrottled flush."""
    from vaf.core.headless_runner import _ChatTurnStream, _strip_tool_calls_json
    emits = []
    stream = _ChatTurnStream(SESSION, emit=lambda text, n, answer: emits.append((text, n, answer)),
                             clock=lambda: 1000.0)
    agent = make()
    _health_turn(agent, monkeypatch, stream, sink=stream.on_event)

    marks = {1: ("Reasoning A.", SAY), 2: ("Reasoning B.",), 3: ("Reasoning C.", DONE)}
    for text, n, _ in emits:
        assert n in marks, f"an emit without its round: {text!r}"
        foreign = [m for k, ms in marks.items() if k != n for m in ms if m in text]
        assert not foreign, f"round {n}'s bubble carries another round's text: {foreign}"
    live = [(text, n) for text, n, answer in emits if answer is None]
    assert ("<think>Reasoning A.</think>\n\n" + SAY + "\n", 1) in live, "round 1 never went out whole"
    assert any(n == 2 and "Reasoning B." in text for text, n in live), "round 2 never went out"

    emits.clear()
    assert stream.emit_rounds(agent.last_turn.rounds, clean=_strip_tool_calls_json) == 3
    assert [(n, answer) for _, n, answer in emits] == [(1, False), (2, False), (3, True)]
    assert "".join(stream.parts) == STORED_BEFORE


def test_rounds_metadata_only_for_multi_round():
    from vaf.core.headless_runner import _ChatTurnStream, _turn_rounds_meta
    stream = _ChatTurnStream(SESSION, emit=lambda *a: None)
    stream("<think>A</think>\n\nSay.\n\n\n\nmore")
    stream.on_event({"type": "round_start", "round": 2})
    stream("<think>B</think>")
    stream.on_event({"type": "round_start", "round": 3})
    stream.clear("Say.\n\n")
    stream("Done.")
    rounds = (TurnRound(1, (), False), TurnRound(2, ("c1",), False), TurnRound(3, (), True))
    assert _turn_rounds_meta(rounds, stream) == [
        {"round": 1, "text": "<think>A</think>\n\nSay.\n\nmore", "tool_call_ids": [], "answer": False},
        {"round": 2, "text": "<think>B</think>", "tool_call_ids": ["c1"], "answer": False},
        {"round": 3, "text": "Done.", "tool_call_ids": [], "answer": True},
    ]
    one = _ChatTurnStream(SESSION, emit=lambda *a: None)
    one("Just the answer.")
    assert _turn_rounds_meta((TurnRound(1, (), True),), one) is None
    # A round that streamed nothing and called nothing has no entry, so it cannot make a
    # one-round turn look like two.
    assert _turn_rounds_meta((TurnRound(1, (), False), TurnRound(2, (), True)), one) is None


def test_draft_turn_stores_no_rounds():
    """A turn that ends at a draft (or a delegation, a system note) shows one message and
    keeps today's stored shape: the rounds are taken only on the two branches that emitted the
    turn per round, a stop the engine reported and the normal answer. Source-level, because
    that branch structure is the property."""
    from vaf.core import headless_runner as hr
    loop = inspect.getsource(hr.run_headless_agent).replace("\r\n", "\n")
    reset = loop.index("_rounds_meta = None")
    draft = loop.index("elif _turn_ends_at_draft:")
    stopped = loop.index("if _agent_stopped_internally:")
    taken = [i for i in range(len(loop)) if loop.startswith("_rounds_meta = _turn_rounds_meta(", i)]
    assert len(taken) == 2
    assert reset < loop.index('if response_text.startswith("[ASYNC_ACK]"):') < draft < stopped < taken[0]
    assert taken[0] < loop.index("else:", stopped) < taken[1] < loop.index("# Emit message_complete event", stopped)


# ── the runner, end to end ────────────────────────────────────────────────────────────────

class _LoopStop(BaseException):
    """Ends the runner's loop; a BaseException, because the loop catches every Exception."""


class _Queue:
    def __init__(self, tasks, stop=None):
        self.tasks = list(tasks)
        self.stop = stop or (lambda: False)

    def get(self):
        return self.tasks.pop(0) if self.tasks else None

    def should_stop(self, session_id):
        return bool(self.stop())

    def try_claim_session(self, session_id):
        return True

    def get_queue_stats(self):
        return {}

    def get_queue_size(self):
        return 0

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Web:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.events.append((name, args, kwargs))
        return record


@pytest.fixture
def runner_world(monkeypatch, tmp_path):
    import vaf.core.config as config_module
    import vaf.core.session as session_module
    import vaf.core.subagent_ipc as ipc
    from vaf.core.context import ContextManager

    monkeypatch.delenv("VAF_SESSION_ID", raising=False)
    tokens = [(var, var.set(ipc._UNSET)) for var in
              (ipc._session_ctx, ipc._turn_ctx, ipc._room_ctx, ipc._conversation_ctx)]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    monkeypatch.setattr(Platform, "documents_dir", staticmethod(lambda: tmp_path / "docs"))
    monkeypatch.setattr(config_module.Config, "APP_DIR", tmp_path / "app", raising=False)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(session_module, "default_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_module, "_manager", None)
    monkeypatch.setattr(ContextManager, "ARCHIVE_DIR", tmp_path / "context_archive")
    scope = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
    monkeypatch.setattr(config_module, "get_local_admin_scope_id", lambda: scope)
    session = session_module.Session(id=SESSION, name=SESSION)
    session.metadata["user_scope_id"] = scope
    session.metadata["username"] = "bobq7"
    session_module.SessionManager().save(session, sync_state=False)
    yield SimpleNamespace(tmp=tmp_path, sessions=sessions, scope=scope)
    for var, token in reversed(tokens):
        var.reset(token)


def _run_one_turn(monkeypatch, world, text, provider, keep_grounding=False, stop=None):
    import time as _time

    import vaf.core.headless_runner as hr
    import vaf.core.subagent_ipc as ipc_mod
    from vaf.core.agent import Agent
    from vaf.core.task_queue import AgentTask
    agent = Agent(register_signals=False, run_kind="chat",
                  config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    agent._current_chat_source = "web"
    monkeypatch.setattr(agent, "_try_workflow", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_route_tools", lambda text: [TASK])
    if not keep_grounding:
        monkeypatch.setattr(agent, "_detect_ungrounded_result_claim", lambda text, res: (False, None))
    monkeypatch.setattr(agent, "collect_room_wake", lambda **k: None)
    agent.tools[TASK] = _Install()
    agent.init_chat()
    sinks = []
    real_set = agent.set_event_sink
    monkeypatch.setattr(agent, "set_event_sink", lambda s: (sinks.append(s), real_set(s))[1])
    agent.api_backend.chat_completion = provider

    task = AgentTask(session_id=SESSION, input_text=text, source="web", metadata={
        "user_scope_id": world.scope, "username": "bobq7", "role": "user",
        "enqueue_session_id": SESSION, "task_class": "interactive"})
    queue = _Queue([task], stop)
    web, logs, state = _Web(), [], {"idle": 0, "error": False}
    monkeypatch.setattr(hr, "Agent", lambda *a, **k: agent)
    monkeypatch.setattr(hr, "TaskQueue", lambda: queue)
    monkeypatch.setattr(hr, "get_web_interface", lambda: web)
    monkeypatch.setattr(hr, "_WORKER_THREADS", {})
    monkeypatch.setattr(hr, "append_domain_log_always", lambda domain, msg: logs.append(msg))
    monkeypatch.setattr(Platform, "stop_webui_subagent_processes", staticmethod(lambda **k: 0))
    monkeypatch.setattr("vaf.core.memory_profiler.start_profiler", lambda *a, **k: None)
    monkeypatch.setattr("vaf.core.at_rest_migration.repair_context_copies_once", lambda: None)
    monkeypatch.setattr("vaf.memory.rag.turn_memory_context", lambda *a, **k: "")
    monkeypatch.setattr(ipc_mod, "_ipc_instance", None)

    def _sleep(seconds):
        if seconds >= 1:
            state["error"] = True
            raise _LoopStop()
        if not queue.tasks:
            state["idle"] += 1
            if state["idle"] >= 2:
                raise _LoopStop()

    monkeypatch.setattr(hr, "time", SimpleNamespace(time=_time.time, sleep=_sleep))
    with pytest.raises(_LoopStop):
        contextvars.copy_context().run(hr.run_headless_agent, worker_id=1, total_workers=1)
    assert not state["error"], f"the loop raised: {logs[-5:]}"
    from vaf.core.session import SessionManager
    stored = SessionManager()._read_session_file(world.sessions / f"{SESSION}.json")
    return SimpleNamespace(web=web, stored=stored, sinks=sinks, agent=agent)


def test_runner_streams_and_stores_a_turn_per_round(runner_world, monkeypatch):
    """A tool round, then the answer: the live updates are tagged per round and never mix
    two rounds; the stored answer keeps the content it always had and adds the rounds; the
    turn's sink is taken off again; the projection of the stored chat shows the round-1 text
    above the tool card."""
    from vaf.core.web_server import _history_projection
    from vaf.core.session import SessionManager
    calls = []

    def provider(messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        calls.append(kw)
        if len(calls) == 1:
            yield "Ich schaue nach."
            yield json.dumps({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                              "function": {"name": TASK, "arguments": "{}"}}]})
            yield json.dumps({"finish_reason": "tool_calls"})
            return
        yield "Fertig: alle zehn Pakete sind installiert."

    run = _run_one_turn(monkeypatch, runner_world, ASK, provider)
    assert len(calls) == 2

    updates = [kw for name, _, kw in run.web.events if name == "emit_agent_message"]
    assert updates and all(isinstance(u.get("round"), int) for u in updates), updates
    assert not [u for u in updates if "Ich schaue nach." in u["content"] and "Fertig" in u["content"]]
    final = [(u["round"], u["answer"]) for u in updates if u.get("answer") is not None]
    assert final == [(1, False), (2, True)]
    assert run.sinks and run.sinks[-1] is None, "the turn's sink stayed on the agent"
    assert run.agent._event_sink is None

    answer = [m for m in run.stored["messages"] if m["role"] == "assistant"][-1]
    assert answer["content"] == "Fertig: alle zehn Pakete sind installiert."
    assert answer["metadata"]["rounds"] == [
        {"round": 1, "text": "Ich schaue nach.", "tool_call_ids": ["c1"], "answer": False},
        {"round": 2, "text": "Fertig: alle zehn Pakete sind installiert.", "tool_call_ids": [],
         "answer": True},
    ]
    shown = _history_projection(SessionManager().load(SESSION), SESSION)
    turn = shown[next(i for i, e in enumerate(shown) if e["role"] == "user" and e["content"] == ASK):]
    assert [(e["role"], e.get("round"), e.get("toolId")) for e in turn] == [
        ("user", None, None), ("assistant", 1, None), ("tool", None, "c1"), ("assistant", 2, None)]


def test_runner_stores_a_corrected_turn_whole(runner_world, monkeypatch):
    """A reply the result-grounding guard kept and sent back, then the correction: no tool
    round, so no clear. The stored content stays the whole stream, both rounds, as before;
    the rounds say which one is the answer."""
    flagged = "Die Datei liegt jetzt im Projektordner und ist gespeichert."
    fixed = "Korrektur: gespeichert wurde nichts, es lief kein Werkzeug."
    verdicts = iter([(True, "Die Datei ist gespeichert.")])

    def provider(messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        provider.n = getattr(provider, "n", 0) + 1
        yield flagged if provider.n == 1 else fixed

    from vaf.core.agent import Agent
    monkeypatch.setattr(Agent, "_detect_ungrounded_result_claim",
                        lambda self, text, res: next(verdicts, (False, None)))
    run = _run_one_turn(monkeypatch, runner_world, "Speichere bitte die Datei", provider,
                        keep_grounding=True)
    answer = [m for m in run.stored["messages"] if m["role"] == "assistant"][-1]
    assert flagged in answer["content"] and fixed in answer["content"]
    assert answer["content"].index(flagged) < answer["content"].index(fixed)
    assert [(r["round"], r["answer"]) for r in answer["metadata"]["rounds"]] == [(1, False), (2, True)]
    assert answer["metadata"]["rounds"][0]["text"] == flagged


def test_stop_marks_only_its_round():
    """MUTATION: put the whole stored copy into the notice - red."""
    from vaf.core.headless_runner import _ChatTurnStream
    emits = []
    stream = _ChatTurnStream(SESSION, emit=lambda text, n, answer: emits.append((text, n, answer)))
    stream("Erster Teil.")
    stream.on_event({"type": "round_start", "round": 2})
    stream("Zweiter Teil.")
    emits.clear()
    stream.emit_stopped("*[Generation stopped]*")
    assert emits == [("Zweiter Teil.\n\n*[Generation stopped]*", 2, None)]
    # What is stored afterwards holds the notice too, in the same round.
    assert stream.segment_text(2) == "Zweiter Teil.\n\n*[Generation stopped]*"
    assert stream.segment_text(1) == "Erster Teil."
    assert "".join(stream.parts).endswith("Zweiter Teil.\n\n*[Generation stopped]*")
    empty = _ChatTurnStream(SESSION, emit=lambda *a: None)
    empty.emit_stopped("*[Generation stopped]*")
    assert empty.segment_text(1) == "*[Generation stopped]*" and empty.parts == ["*[Generation stopped]*"]


def test_a_stop_reaches_only_the_stream_of_its_own_chat():
    """MUTATION: drop the session comparison in `_stop_stream_for` - red. The stop handler
    finds the stream as a loop local, which can outlive its task."""
    from vaf.core.headless_runner import _ChatTurnStream, _stop_stream_for
    other = _ChatTurnStream("red654321", emit=lambda *a: None)
    mine = _ChatTurnStream(SESSION, emit=lambda *a: None)
    assert _stop_stream_for(other, SESSION) is None
    assert _stop_stream_for(mine, SESSION) is mine
    assert _stop_stream_for(lambda t: None, SESSION) is None and _stop_stream_for(None, SESSION) is None
    loop = inspect.getsource(__import__("vaf.core.headless_runner", fromlist=["x"]).run_headless_agent)
    handler = loop.split("except _StopGenerationRequested:", 1)[1].split("except Exception as e:", 1)[0]
    assert '_stop_stream_for(locals().get("webui_stream_callback"),' in handler
    assert "isinstance(_stopped_stream, _ChatTurnStream)" not in handler


def _stop_events(run):
    clears = [e for e in run.web.events if e[0] == "emit_clear_last_assistant"]
    updates = [kw for name, _, kw in run.web.events if name == "emit_agent_message"]
    return clears, updates


def test_runner_stop_from_the_stream_stays_in_its_round(runner_world, monkeypatch):
    """Stop pressed while round 2 streams (the stream raises): the notice goes into round 2's
    bubble, tagged, and round 1's bubble keeps its text alone. MUTATION: restore the untagged
    clear plus the joined copy in the `_StopGenerationRequested` handler - red."""
    flagged = "Die Datei liegt jetzt im Projektordner und ist gespeichert."
    state = {"stop": False}

    def provider(messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        provider.n = getattr(provider, "n", 0) + 1
        if provider.n == 1:
            yield flagged
            return
        state["stop"] = True
        yield "Korrektur: gespeichert wurde nichts."

    from vaf.core.agent import Agent
    verdicts = iter([(True, "Die Datei ist gespeichert.")])
    monkeypatch.setattr(Agent, "_detect_ungrounded_result_claim",
                        lambda self, text, res: next(verdicts, (False, None)))
    run = _run_one_turn(monkeypatch, runner_world, "Speichere bitte die Datei", provider,
                        keep_grounding=True, stop=lambda: state["stop"])
    clears, updates = _stop_events(run)
    assert clears == [], "a stop cleared a bubble by position"
    assert updates[-1]["round"] == 2 and updates[-1]["content"] == "*[Generation stopped]*"
    assert not [u for u in updates if flagged in u["content"] and "stopped" in u["content"]]
    # Named boundary (WEBUI_WEBSOCKET_FLOW.md): a stop raised while the stream runs leaves
    # the turn's save behind, so nothing of the turn is stored, as before rounds existed.
    assert not [m for m in run.stored["messages"] if "Speichere bitte" in str(m.get("content"))
                or flagged in str(m.get("content"))]


def test_runner_engine_stop_stays_in_its_round_and_is_stored_per_round(runner_world, monkeypatch):
    """Stop seen by the engine at the top of round 3, after two tool rounds: the notice goes
    into round 2's bubble, and the stored answer carries both rounds. MUTATION: restore the
    untagged clear plus the joined copy in the `_agent_stopped_internally` branch - red."""
    from vaf.core import task_queue as tq_mod
    state = {"stop": False}
    monkeypatch.setattr(tq_mod.TaskQueue, "should_stop", lambda self, sid: state["stop"])
    monkeypatch.setattr(tq_mod.TaskQueue, "clear_stop", lambda self, sid: None)

    def provider(messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        provider.n = getattr(provider, "n", 0) + 1
        yield ("Erster Schritt." if provider.n == 1 else "Zweiter Schritt.")
        yield json.dumps({"tool_calls": [{"index": 0, "id": f"c{provider.n}", "type": "function",
                                          "function": {"name": TASK, "arguments": "{}"}}]})
        yield json.dumps({"finish_reason": "tool_calls"})
        if provider.n == 2:
            state["stop"] = True

    run = _run_one_turn(monkeypatch, runner_world, ASK, provider)
    clears, updates = _stop_events(run)
    assert clears == []
    assert (updates[-1]["round"], updates[-1]["content"]) == (2, "Zweiter Schritt.\n\n*[Generation stopped]*")
    answer = [m for m in run.stored["messages"] if m["role"] == "assistant"][-1]
    assert [(r["round"], r["text"], r["tool_call_ids"]) for r in answer["metadata"]["rounds"]] == [
        (1, "Erster Schritt.", ["c1"]), (2, "Zweiter Schritt.\n\n*[Generation stopped]*", ["c2"])]
    # The stored text and the reloaded round hold the notice the live bubble showed.
    assert answer["content"] == "Zweiter Schritt.\n\n*[Generation stopped]*"


# ── the projection ────────────────────────────────────────────────────────────────────────

def _session(messages):
    from vaf.core.session import Message
    return SimpleNamespace(messages=[Message(**m) for m in messages])


_TURN = [
    {"role": "user", "content": ASK, "timestamp": "2026-11-03T10:00:00.500000"},
    {"role": "assistant", "content": "", "timestamp": "2026-11-03T10:00:01",
     "tool_calls": [{"id": "c1", "type": "function", "function": {"name": TASK, "arguments": "{}"}}]},
    {"role": "tool", "content": "10/10 installed", "tool_call_id": "c1", "name": TASK,
     "timestamp": "2026-11-03T10:00:01"},
    {"role": "assistant", "content": SAY + "\n\n<think>Reasoning C.</think>\n\n" + DONE,
     "timestamp": "2026-11-03T10:00:02",
     "metadata": {"rounds": [
         {"round": 1, "text": "<think>Reasoning A.</think>\n\n" + SAY, "tool_call_ids": [], "answer": False},
         {"round": 2, "text": "<think>Reasoning B.</think>", "tool_call_ids": ["c1"], "answer": False},
         {"round": 3, "text": "<think>Reasoning C.</think>\n\n" + DONE, "tool_call_ids": [], "answer": True},
     ]}},
]


def test_projection_interleaves_rounds_and_tools():
    from vaf.core.web_server import _history_projection
    out = _history_projection(_session(_TURN), SESSION)
    assert [(e["role"], e.get("round"), e.get("roundAnswer"), e.get("toolId")) for e in out] == [
        ("user", None, None, None),
        ("assistant", 1, False, None),
        ("assistant", 2, False, None),
        ("tool", None, None, "c1"),
        ("assistant", 3, True, None),
    ]
    assert SAY in out[1]["content"] and "Reasoning B." in out[2]["content"]
    stamps = [e["timestamp"] for e in out if e.get("round")]
    assert len(set(stamps)) == 3 and stamps[0] == "2026-11-03T10:00:02"
    assert stamps[2] == "2026-11-03T10:00:02.002000"


def test_projection_without_a_stored_tool_keeps_the_round():
    """A round whose call is not in the store (the turn-end squash keeps a summary) is shown
    without its card; a tool no round names stays where it was stored."""
    from vaf.core.web_server import _history_projection
    turn = [dict(m) for m in _TURN]
    turn[2] = {"role": "tool", "content": "other", "tool_call_id": "x9", "name": "web_search",
               "timestamp": "2026-11-03T10:00:01"}
    out = _history_projection(_session(turn), SESSION)
    assert [(e["role"], e.get("round"), e.get("toolId")) for e in out] == [
        ("user", None, None), ("tool", None, "x9"),
        ("assistant", 1, None), ("assistant", 2, None), ("assistant", 3, None)]


def test_projection_survives_malformed_rounds():
    """The rounds are read back from a file: a number no engine writes, ids of the wrong
    shape or a non-dict entry must not cost the chat its history. MUTATION: drop
    `OverflowError` from `_round_timestamp` - red on the direct call; drop the range check
    in `_round_number` - red (10**15 ms overflows the date); read `tool_call_ids` without
    the shape check - red (an int is not iterable)."""
    from vaf.core.web_server import _history_projection, _round_timestamp
    assert _round_timestamp("2026-11-03T10:00:02", 10**15) == "2026-11-03T10:00:02"
    turn = [dict(m) for m in _TURN]
    turn[3] = dict(turn[3], metadata={"rounds": [
        {"round": 10**15, "text": "Huge.", "tool_call_ids": 7, "answer": True},
        "not a round",
        {"round": float("inf"), "text": "Infinite.", "tool_call_ids": ["c1"]},
        {"round": "x", "text": "Words.", "tool_call_ids": None},
    ]})
    out = _history_projection(_session(turn), SESSION)
    rounds = [e for e in out if "round" in e]
    assert [e["content"] for e in rounds] == ["Huge.", "Infinite.", "Words."]
    assert all(e["round"] == 0 and e["timestamp"] == "2026-11-03T10:00:02" for e in rounds)
    assert [e.get("toolId") for e in out if e["role"] == "tool"] == ["c1"]


def test_projection_falls_back_to_the_stored_order(monkeypatch):
    """MUTATION: call `_expand_turn_rounds` without the guard - red."""
    from vaf.core import web_server as ws

    def boom(out, aux):
        raise RuntimeError("unexpected shape")
    monkeypatch.setattr(ws, "_expand_turn_rounds", boom)
    out = ws._history_projection(_session(_TURN), SESSION)
    assert [e["role"] for e in out] == ["user", "assistant", "tool", "assistant"]


def test_projection_legacy_unchanged():
    """A chat stored before rounds existed projects exactly as before."""
    from vaf.core import web_server as ws
    legacy = [dict(m) for m in _TURN]
    legacy[3] = {k: v for k, v in legacy[3].items() if k != "metadata"}
    out = ws._history_projection(_session(legacy), SESSION)
    assert [e["role"] for e in out] == ["user", "assistant", "tool", "assistant"]
    assert all("round" not in e and "roundAnswer" not in e for e in out)
    assert out[3]["content"] == legacy[3]["content"] and out[1]["content"] == ""
    assert ws._expand_turn_rounds(out, [{} for _ in out]) is out


# ── the registry copies ───────────────────────────────────────────────────────────────────

def test_facade_exports_turn_round():
    from vaf.core.turn_binding import AGENT_STATE, _STEP
    assert vaf.TurnRound is TurnRound
    assert "TurnRound" in vaf.__all__
    assert AGENT_STATE["_turn_rounds"] == _STEP


def test_the_docs_name_the_event_and_the_field():
    root = Path(__file__).resolve().parents[1]
    obs = (root / "docs" / "OBSERVABILITY.md").read_text(encoding="utf-8")
    assert "| `round_start` |" in obs and "last_turn.rounds" in obs
    assert "`None` in web sessions" not in obs
    embedding = (root / "docs" / "EMBEDDING.md").read_text(encoding="utf-8")
    assert "vaf.TurnRound" in embedding and "round_start" in embedding
