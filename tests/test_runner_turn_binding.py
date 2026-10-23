# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The runner's three lanes bind their turns through one primitive (vaf/core/turn_binding.py).

One agent per worker serves every chat and every account. MEASURED BEFORE THE FIX, through
the real runner and with only the provider faked: an agent room's turn for another account's
room carried the owner's chat history and five context markers, and the room's exchange stayed
in the owner chat's in-memory history, so that chat's next request carried it; the room turn's
tools resolved the last chat's session (its workspace, its sub-agent stamp); a user's chat had
its sub-agent results summarized as an admin; and the turn source was whatever task ran last.

The room tests replay the runner's room branch with its own producer: `collect_room_wake`,
`bind_turn(agent, _room_turn_binding(...))`, `chat_step`, `end_turn`.

MUTATIONS, each turns the named tests red:
- key the room conversation on the room id alone -> test_room_working_memory_is_per_room_and_account;
- leave the session context variable standing in an own conversation (no
  `set_current_session_id(None)`) -> test_room_tools_see_no_chat;
- keep `current_session_id` in an own conversation -> test_the_next_chat_turn_carries_no_room
  (the next load of that chat returns early on the room's history) and
  test_room_tools_see_no_chat (the dispatch falls back to the attribute);
- skip `init_chat` in an own conversation -> test_room_turn_carries_nothing_of_the_last_chat;
- drop `set_current_conversation_id` from `bind_turn`, or read the session alone in the
  working-memory tools -> test_room_working_memory_is_per_room_and_account (the note went to
  the store of "no session known", which every sessionless lane of every account reads);
- bind the room's source from the agent instead of "web" -> test_room_tools_see_no_chat;
- the drain's role always None or always "admin", or its source from the agent
  -> test_the_drain_runs_as_the_chats_owner;
- drop `allowed_tools` for a contact, or a `room_turn` key -> the two producer tests;
- remove `bind_turn` from any lane, or the loop's `end_turn`
  -> test_every_runner_lane_binds_before_its_turn;
- write `agent._unattended_turn = False` in the runner again
  -> test_binding_attributes_have_one_writer.
"""
import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.room import Room, derive_peer_id, participant_key
from vaf.core.identity_binding import Identity
from vaf.core.platform import Platform
from vaf.core.turn_binding import AGENT_STATE, bind_turn, end_turn

ROOT = Path(__file__).resolve().parents[1]

OWNER_SESSION = "red654321"
BOB_SESSION = "green123456"
CONTACT_SESSION = "whatsapp_4915100000001"
TELEGRAM_SESSION = "telegram_4242"

OWNER_SCOPE = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
BOB_SCOPE = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
CAROL_SCOPE = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"

OWNER = Identity(scope=OWNER_SCOPE, username="ownerq7", role="admin")
BOB = Identity(scope=BOB_SCOPE, username="bobq7", role="user")

OWNER_CHAT_MARKERS = ("OWNER7QHIST", "OWNER7QNOTE", "OWNER7QSUMMARY", "OWNER7QVAFMD")


def _context_runtime_state(summary):
    return {"timestamp": "2026-10-01T10:00:00", "schema_version": "1.0",
            "providers": {"context": {"version": "1.0", "captured_at": "2026-10-01T10:00:00",
                                      "state": {"intent": {"primary_goal": "", "sub_goals": [],
                                                           "constraints": [], "keywords": [],
                                                           "last_updated": "2026-10-01T10:00:00"},
                                                "state": {"files_created": [], "files_read": [],
                                                          "files_modified": [],
                                                          "errors_encountered": [],
                                                          "tools_used": [], "key_decisions": [],
                                                          "narrative_summary": summary,
                                                          "last_updated": "2026-10-01T10:00:00"},
                                                "max_tokens": 45000, "trigger_threshold": 0.85}}}}


def _seed(session_id, *, scope, username=None, messages=(), metadata=None, **fields):
    from vaf.core.session import Session, SessionManager
    session = Session(id=session_id, name=session_id)
    session.metadata["user_scope_id"] = scope
    if username:
        session.metadata["username"] = username
    session.metadata.update(metadata or {})
    for role, content in messages:
        session.add_message(role=role, content=content)
    for key, value in fields.items():
        setattr(session, key, value)
    SessionManager().save(session, sync_state=False)


class _Provider:
    """Records every request; `probe(agent)` is read during the main call."""

    def __init__(self, answer="Gern.", probe=None):
        self.answer, self.texts, self.probe, self.seen = answer, [], probe, []

    def __call__(self, messages=None, **kw):
        self.texts.append(json.dumps(messages or [], ensure_ascii=False, default=str))
        if kw.get("tools"):
            if self.probe:
                self.seen.append(self.probe())
            yield self.answer
            return
        yield "no"


def _task(session_id, identity, text, source="web", **meta):
    from vaf.core.task_queue import AgentTask
    metadata = {"user_scope_id": identity.scope, "username": identity.username,
                "role": identity.role, "enqueue_session_id": session_id,
                "task_class": "interactive", **meta}
    return AgentTask(session_id=session_id, input_text=text, source=source, metadata=metadata)


@pytest.fixture
def world(monkeypatch, tmp_path):
    import vaf.core.config as config_module
    import vaf.core.cross_chat as cross_chat
    import vaf.core.session as session_module
    import vaf.core.subagent_ipc as ipc
    from vaf.core.context import ContextManager
    from vaf.core.main_persistence import MainPersistenceManager

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
    monkeypatch.setattr(cross_chat, "_contact_endpoints", lambda *a, **k: set())
    monkeypatch.setattr(config_module, "get_local_admin_scope_id", lambda: OWNER_SCOPE)
    rooms = tmp_path / "rooms"
    rooms.mkdir()
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else rooms)

    # The owner's chat: its history, its saved summary, its working-memory note and the
    # VAF.md of its project folder.
    proj = tmp_path / "docs" / "VAF_Projects" / "ab12cd34" / "owner-proj"
    proj.mkdir(parents=True)
    (proj / "VAF.md").write_text("OWNER7QVAFMD project rules\n", encoding="utf-8")
    _seed(OWNER_SESSION, scope=OWNER_SCOPE, username="ownerq7",
          messages=[("user", "OWNER7QHIST Kuendige den Handyvertrag"), ("assistant", "Erledigt.")],
          runtime_state=_context_runtime_state("OWNER7QSUMMARY"), project_path=str(proj))
    MainPersistenceManager(str(tmp_path), session_id=OWNER_SESSION).update_working_memory(
        add_notes=["OWNER7QNOTE the contract ends in March"])
    _seed(BOB_SESSION, scope=BOB_SCOPE, username="bobq7",
          messages=[("user", "BOB7QHIST Wie ist das Wetter?"), ("assistant", "Sonnig.")])

    # Room A holds both accounts' agents; room B only the owner's. A guest speaks in both.
    room_a = Room.create(kind="round", owner_scope=OWNER_SCOPE, base=rooms, room_id="room-a",
                         multi_scope=True, tenants=[BOB_SCOPE])
    room_b = Room.create(kind="round", owner_scope=OWNER_SCOPE, base=rooms, room_id="room-b")
    for room, scopes in ((room_a, (OWNER_SCOPE, BOB_SCOPE)), (room_b, (OWNER_SCOPE,))):
        for scope in scopes:
            room.join(display=f"Agent-{scope[:4]}", scope_id=scope,
                      peer_id=derive_peer_id(participant_key("agent", scope), room.room_id))
    guests = {room.room_id: room.join(display="Codex", scope_id=None, peer_id=f"p-guest{i}")
              for i, room in enumerate((room_a, room_b))}

    def _make():
        from vaf.core.agent import Agent
        agent = Agent(register_signals=False, run_kind="chat",
                      config_overrides={"provider": "deepseek", "api_key_deepseek": "sk-test"})
        agent._current_chat_source = "web"
        monkeypatch.setattr(agent, "_try_workflow", lambda *args, **kw: None)
        monkeypatch.setattr(agent, "_room_unattended_report", lambda *a, **k: None)
        agent.init_chat()
        return agent

    def _say(room_id, text):
        Room.open(room_id, base=rooms).say(guests[room_id], text)

    yield SimpleNamespace(make=_make, tmp=tmp_path, rooms=rooms, say=_say)
    for var, token in reversed(tokens):
        var.reset(token)


def _queue_turn(agent, task, provider=None):
    """The queue lane's binding, then one turn, then the end of the task."""
    from vaf.core.headless_runner import _task_turn_binding
    from vaf.core.session import SessionManager
    provider = provider or _Provider()
    bind_turn(agent, _task_turn_binding(task, SessionManager()))
    agent.api_backend.chat_completion = provider
    try:
        agent.chat_step(user_input=task.input_text, raw_user_input=task.input_text,
                        stream_callback=lambda t: None)
    finally:
        end_turn(agent)
    return provider


def _room_turn(agent, scope, username, provider=None, during=None):
    """The runner's room branch, replayed with its own producer."""
    from vaf.core.headless_runner import _room_turn_binding
    provider = provider or _Provider()
    wake = agent.collect_room_wake(scopes=[scope])
    assert wake is not None, "nothing woke this account's agent"
    bind_turn(agent, _room_turn_binding(wake, Identity(scope=scope, username=username)))
    try:
        if during:
            during(agent)
        agent.api_backend.chat_completion = provider
        agent.chat_step(user_input=wake["prompt"], skip_input=False, disable_workflows=True,
                        disable_tools=False)
    finally:
        end_turn(agent)
        wake["advance"]()
    return provider


# ── AC 10: the room lane ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("last", ["owner", "bob"])
def test_room_turn_carries_nothing_of_the_last_chat(world, last):
    agent = world.make()
    if last == "owner":
        _queue_turn(agent, _task(OWNER_SESSION, OWNER, "Wann endet der Vertrag?"))
        assert "OWNER7QSUMMARY" in agent.context_manager.state.narrative_summary
        markers = OWNER_CHAT_MARKERS
    else:
        _queue_turn(agent, _task(BOB_SESSION, BOB, "Und morgen?"))
        markers = ("BOB7QHIST",)
    world.say("room-a", "GUEST7QASK Welche Farbe soll das Logo haben?")

    provider = _room_turn(agent, OWNER_SCOPE, "ownerq7")

    requests = "\n".join(provider.texts)
    assert "GUEST7QASK" in requests, "the probe does not see the room turn"
    leaked = [m for m in markers if m in requests]
    assert not leaked, f"the room turn carried the last chat: {leaked}"


def test_the_next_chat_turn_carries_no_room(world):
    agent = world.make()
    _queue_turn(agent, _task(BOB_SESSION, BOB, "Und morgen?"))
    world.say("room-a", "GUEST7QASK Welche Farbe soll das Logo haben?")
    _room_turn(agent, OWNER_SCOPE, "ownerq7", provider=_Provider("ROOM7QREPLY Blau."))

    provider = _queue_turn(agent, _task(BOB_SESSION, BOB, "Und uebermorgen?"))

    requests = "\n".join(provider.texts)
    assert "BOB7QHIST" in requests, "Bob's own history is not in his next turn"
    leaked = [m for m in ("YOU ARE IN AN AGENT ROOM", "GUEST7QASK", "ROOM7QREPLY")
              if m in requests]
    assert not leaked, f"Bob's chat carried the room turn: {leaked}"


def test_room_working_memory_is_per_room_and_account(world):
    from vaf.core.main_persistence import MainPersistenceManager
    agent = world.make()
    note = "ROOMA7QNOTE the vote closes on Friday"

    def _write(a):
        result = a.execute_tool("update_working_memory", {"add_notes": [note]})
        assert "updated" in result.lower(), result

    world.say("room-a", "GUEST7QASK first question")
    _room_turn(agent, OWNER_SCOPE, "ownerq7", during=_write)

    world.say("room-a", "GUEST7QASK second question")
    same = _room_turn(agent, OWNER_SCOPE, "ownerq7")
    assert note in "\n".join(same.texts), "the room's note is gone from the room's next turn"

    world.say("room-b", "GUEST7QASK in room b")
    other_room = _room_turn(agent, OWNER_SCOPE, "ownerq7")
    assert note not in "\n".join(other_room.texts), "another room read this room's note"

    other_account = _room_turn(agent, BOB_SCOPE, "bobq7")
    assert "GUEST7QASK second question" in "\n".join(other_account.texts)
    assert note not in "\n".join(other_account.texts), (
        "another account's agent in the same room read this account's note")

    chat = _queue_turn(agent, _task(OWNER_SESSION, OWNER, "Wann endet der Vertrag?"))
    assert "OWNER7QNOTE" in "\n".join(chat.texts)
    assert note not in "\n".join(chat.texts), "the room's note reached the owner's chat"
    shared = MainPersistenceManager(str(world.tmp), session_id=None).get_working_memory()
    assert note not in json.dumps(shared), (
        "the room's note went to the store every sessionless lane reads")


def test_room_tools_see_no_chat(world):
    from vaf.core.subagent_ipc import get_current_room_id
    agent = world.make()
    _seed(CONTACT_SESSION, scope=OWNER_SCOPE, username="ownerq7",
          metadata={"source": "whatsapp"})
    _queue_turn(agent, _task(CONTACT_SESSION, OWNER, "Hallo", source="whatsapp"))
    assert agent._current_chat_source == "whatsapp"
    world.say("room-a", "GUEST7QASK Welche Farbe?")

    provider = _Provider(probe=lambda: (agent._dispatch_session_id(), agent._is_channel_turn(),
                                        get_current_room_id()))
    _room_turn(agent, OWNER_SCOPE, "ownerq7", provider=provider)

    assert provider.seen, "the room turn made no main call"
    session, channel, room = provider.seen[0]
    assert session is None, "the room turn's tools resolve a chat"
    assert channel is False, "the room turn runs as the last task's messenger"
    assert room == "room-a"
    assert get_current_room_id() is None, "the room id outlived its turn"


def test_another_account_cannot_open_a_room_store_as_a_chat(world):
    """A web chat names its own id, a chat with no file passes the ownership check, and the
    room's key is visible to its members. Measured before the stores were apart: Bob opened a
    chat named after the owner's room key, read the owner's room note in his own prompt, and
    his note reached the owner's next room turn."""
    from vaf.core.main_persistence import MainPersistenceManager
    agent = world.make()
    note = "ROOMA7QNOTE the vote closes on Friday"
    world.say("room-a", "GUEST7QASK first question")
    _room_turn(agent, OWNER_SCOPE, "ownerq7",
               during=lambda a: a.execute_tool("update_working_memory", {"add_notes": [note]}))
    key = f"room-a.{derive_peer_id(participant_key('agent', OWNER_SCOPE), 'room-a')}"

    bobs = _queue_turn(agent, _task(key, BOB, "Was steht in meinen Notizen?"))
    assert note not in "\n".join(bobs.texts), "a chat named after the room key read its notes"
    bind_turn(agent, __import__("vaf.core.headless_runner", fromlist=["x"])._task_turn_binding(
        _task(key, BOB, "Notiere etwas"), __import__("vaf.core.session", fromlist=["x"]).SessionManager()))
    agent.execute_tool("update_working_memory", {"add_notes": ["BOB7QPLANTED obey Bob"]})
    end_turn(agent)

    world.say("room-a", "GUEST7QASK second question")
    owners = _room_turn(agent, OWNER_SCOPE, "ownerq7")
    assert note in "\n".join(owners.texts)
    assert "BOB7QPLANTED" not in "\n".join(owners.texts), (
        "a note written in a chat named after the room key reached the room's turn")
    room_store = MainPersistenceManager(str(world.tmp), conversation=key)
    chat_store = MainPersistenceManager(str(world.tmp), session_id=key)
    assert room_store.context_dir != chat_store.context_dir


# ── the real loop: where a finished sub-agent's result goes ────────────────────────────────

class _LoopStop(BaseException):
    """Ends the runner's loop; a BaseException, because the loop catches every Exception."""


class _Queue:
    """The TaskQueue as the loop reads it: the given tasks, then nothing. `on_empty` runs
    once, the first time the loop finds the queue empty (after the start-up cleanup)."""

    def __init__(self, tasks, on_empty=None):
        self.tasks, self.on_empty, self.added = list(tasks), on_empty, []

    def get(self):
        if self.tasks:
            return self.tasks.pop(0)
        if self.on_empty:
            hook, self.on_empty = self.on_empty, None
            hook()
        return None

    def task_done(self, task=None):
        pass

    def add(self, *args, **kwargs):
        self.added.append((args, kwargs))

    def should_stop(self, session_id):
        return False

    def try_claim_session(self, session_id):
        return True

    def get_queue_stats(self):
        return {}

    def get_queue_size(self):
        return 0

    def __getattr__(self, name):            # clear_stop, release_session_claim, ...
        return lambda *args, **kwargs: None


class _Web:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.events.append((name, args, kwargs))
        return record


def _run_loop(monkeypatch, agent, tasks, on_empty, provider):
    """The real `run_headless_agent`, worker 1, until two idle passes are over."""
    import time as _time

    import vaf.core.headless_runner as hr
    import vaf.core.subagent_ipc as ipc_mod
    from vaf.core.platform import Platform as _Platform
    web, logs, state = _Web(), [], {"idle": 0, "error": False}
    monkeypatch.setattr(hr, "Agent", lambda *a, **k: agent)
    monkeypatch.setattr(hr, "TaskQueue", lambda: queue)
    monkeypatch.setattr(hr, "get_web_interface", lambda: web)
    monkeypatch.setattr(hr, "_WORKER_THREADS", {})
    monkeypatch.setattr(hr, "append_domain_log_always", lambda domain, msg: logs.append(msg))
    # Never this machine's processes, profiler or chat files.
    monkeypatch.setattr(_Platform, "stop_webui_subagent_processes", staticmethod(lambda **k: 0))
    monkeypatch.setattr("vaf.core.memory_profiler.start_profiler", lambda *a, **k: None)
    monkeypatch.setattr("vaf.core.at_rest_migration.repair_context_copies_once", lambda: None)
    monkeypatch.setattr("vaf.memory.rag.turn_memory_context", lambda *a, **k: "")
    monkeypatch.setattr(ipc_mod, "_ipc_instance", None)
    monkeypatch.setattr(agent, "collect_room_wake", lambda **k: None)
    agent.api_backend.chat_completion = provider
    queue = _Queue(tasks, on_empty)

    def _sleep(seconds):
        if seconds >= 1:                     # the loop's error path
            state["error"] = True
            raise _LoopStop()
        if not queue.tasks and queue.on_empty is None:
            state["idle"] += 1
            if state["idle"] >= 2:
                raise _LoopStop()

    monkeypatch.setattr(hr, "time", SimpleNamespace(time=_time.time, sleep=_sleep))
    with pytest.raises(_LoopStop):
        hr.run_headless_agent(worker_id=1, total_workers=1)
    assert not state["error"], f"the loop raised: {logs[-5:]}"
    return SimpleNamespace(web=web, logs=logs)


def _subagent(session_id, *, room_id=None, description="build the page", result=None):
    """A sub-agent record created as the spawn does, in the starting turn's context (its
    session, its room): finished with `result`, or still running without one."""
    from vaf.core.subagent_ipc import (get_ipc, set_current_room_id,
                                       set_current_session_id)
    set_current_session_id(session_id)
    set_current_room_id(room_id)
    try:
        ipc = get_ipc()
        task_id = ipc.create_task("coding_agent", description, session_id=session_id)
        ipc.mark_task_running(task_id)
        if result is not None:
            ipc.complete_task(task_id, result)
    finally:
        set_current_room_id(None)
    return task_id


def _finished(session_id, text, *, room_id=None):
    return _subagent(session_id, room_id=room_id, result=text)


class _DrainProvider(_Provider):
    """Records each main call with whose turn it was and on which chat."""

    def __init__(self, agent):
        super().__init__(answer="Erledigt.", probe=lambda: (
            agent._current_user_scope_id, agent._current_user_role, agent.current_session_id))


def _stored(world, session_id):
    path = world.tmp / "sessions" / f"{session_id}.json"
    if not path.exists():
        return None
    from vaf.core.session import SessionManager
    return json.dumps(SessionManager()._read_session_file(path), ensure_ascii=False)


def test_a_result_that_names_no_chat_goes_to_no_chat(world, monkeypatch):
    """The order the room's coder was measured in: a room turn starts it (its record names the
    room and no session), Bob chats, the coder finishes, the drain runs. With the fallback to
    the worker's current chat, the room's result went into Bob's chat and was summarized as
    Bob; with a contact's turn last, it went to the contact."""
    from vaf.core.subagent_ipc import get_ipc
    agent = world.make()
    provider = _DrainProvider(agent)
    seeded = {}

    def _the_coder_finishes():
        seeded["room"] = _finished(None, "ROOM7QRESULT the logo page is ready", room_id="room-a")
        _subagent(None, room_id="room-a", description="ROOM7QRUNNING draw the banner")

    run = _run_loop(monkeypatch, agent, [_task(BOB_SESSION, BOB, "Und morgen?")],
                    _the_coder_finishes, provider)

    assert [s for s in provider.seen if s[2] != BOB_SESSION] == [], "a turn ran on another chat"
    assert len(provider.seen) == 1, "a summary turn ran for a result that names no chat"
    assert "ROOM7QRESULT" not in (_stored(world, BOB_SESSION) or ""), (
        "the room's result went into Bob's chat")
    assert not [e for e in run.web.events if "ROOM7QRESULT" in json.dumps(e, default=str)], (
        "the room's result was shown in a browser")
    assert not [e for e in run.web.events if "ROOM7QRUNNING" in json.dumps(e, default=str)], (
        "a running room task was shown in the window of the chat loaded last")
    assert seeded["room"] in [t.task_id for t in get_ipc().get_pending_results()], (
        "the result was consumed; it should stay pending")
    assert any("[DRAIN] no chat for this result" in line for line in run.logs)


def test_a_chat_that_is_gone_gets_no_summary_turn(world, monkeypatch):
    """Bob's chat is deleted while his sub-agent runs. Bound as nobody, the summary turn ran
    with no scope: an open file jail and no account allowlist, over a result Bob's account
    produced, and its reply recreated the chat with no owner."""
    agent = world.make()
    provider = _DrainProvider(agent)
    run = _run_loop(monkeypatch, agent, [_task(BOB_SESSION, BOB, "Und morgen?")],
                    lambda: _finished("web-bob-gone", "GONE7QRESULT done"), provider)

    assert len(provider.seen) == 1, "a summary turn ran for a chat that is gone"
    assert _stored(world, "web-bob-gone") is None, "the reply recreated the deleted chat"
    assert any("[DRAIN] no summary turn for web-bob-gone" in line for line in run.logs)


def test_a_result_is_summarized_in_its_own_chat_as_its_owner(world, monkeypatch):
    """The control: the drain is reached, and a result that names its chat is summarized there,
    bound as that chat's owner (no admin role for Bob), after a turn of another account."""
    agent = world.make()
    provider = _DrainProvider(agent)
    _run_loop(monkeypatch, agent, [_task(OWNER_SESSION, OWNER, "Wann endet der Vertrag?")],
              lambda: _finished(BOB_SESSION, "BOB7QRESULT the forecast is ready"), provider)

    assert provider.seen[0] == (OWNER_SCOPE, "admin", OWNER_SESSION)
    assert provider.seen[1:] == [(BOB_SCOPE, None, BOB_SESSION)], (
        "the summary did not run in Bob's chat as Bob")
    assert "Erledigt." in (_stored(world, BOB_SESSION) or "")


def test_no_other_lane_takes_a_result_that_names_no_chat(world, monkeypatch):
    """The order measured after the drain left such a result pending: the owner's room coder
    finishes, Bob chats, then Bob's scheduled automation runs (a fresh agent, no session
    declared, as `automation.py` builds it). It took every pending result into its own prompt
    and removed it from the queue. An in-lane turn takes its chat's results, or with no chat
    only the tasks it started itself."""
    from vaf.cli.cmd import run as cli_run
    from vaf.core.subagent_ipc import get_ipc, set_current_session_id
    agent = world.make()
    seeded = {}
    _run_loop(monkeypatch, agent, [_task(BOB_SESSION, BOB, "Und morgen?")],
              lambda: seeded.setdefault("room", _finished(
                  None, "ROOM7QRESULT the logo page is ready", room_id="room-a")),
              _DrainProvider(agent))

    automation = world.make()
    automation._background_run = True
    from vaf.core.turn_binding import TurnBinding
    bind_turn(automation, TurnBinding(identity=BOB, source="web", session_id=BOB_SESSION))
    # ...and then, as `automation.py` declares it, no chat at all.
    set_current_session_id(None)
    own = _finished(None, "OWN7QRESULT the report is written")
    automation._async_subagent_tasks[own] = {"agent_type": "coding_agent"}
    provider = _Provider()
    automation.api_backend.chat_completion = provider
    automation.chat_step(user_input="Fasse den Tag zusammen", stream_callback=lambda t: None)

    requests = "\n".join(provider.texts)
    assert "ROOM7QRESULT" not in requests, "an automation took the room's result into its prompt"
    pending = [t.task_id for t in get_ipc().get_pending_results()]
    assert seeded["room"] in pending, "the room's result left the queue"
    assert "OWN7QRESULT" in requests and own not in pending, (
        "the automation no longer gets the result of the task it started itself")

    # The terminal's own check asks the same question (it shares the service's queue).
    shown = cli_run._check_subagent_results(SimpleNamespace(console=SimpleNamespace(
        print=lambda *a, **k: None)), world.make())
    assert not [r for r in shown or [] if "ROOM7QRESULT" in str(r)]
    assert seeded["room"] in [t.task_id for t in get_ipc().get_pending_results()]


def test_the_summary_runs_on_the_results_chat_whatever_the_agent_holds(world, monkeypatch):
    """Something that runs while a result is processed (a resumed workflow) can move the agent
    to another chat; the summary turn still runs on the result's own chat, as its owner."""
    agent = world.make()
    provider = _DrainProvider(agent)
    real = agent._process_subagent_result

    def _moves_the_agent(task):
        out = real(task)
        agent.load_session_context(OWNER_SESSION)
        return out

    monkeypatch.setattr(agent, "_process_subagent_result", _moves_the_agent)
    _run_loop(monkeypatch, agent, [_task(OWNER_SESSION, OWNER, "Wann endet der Vertrag?")],
              lambda: _finished(BOB_SESSION, "BOB7QRESULT the forecast is ready"), provider)

    assert provider.seen[1:] == [(BOB_SCOPE, None, BOB_SESSION)], (
        "the summary ran on the chat the agent was moved to")


def test_an_open_rooms_working_memory_outlives_a_quiet_weekend(world):
    """The collector ages a room's store out only once its room is closed or gone: an open room
    that was quiet longer than `gc_max_age_hours` keeps its tasks and notes."""
    import os
    import time as _time
    from datetime import datetime, timedelta

    from vaf.core.garbage_collector import GarbageCollector
    from vaf.core.main_persistence import MainPersistenceManager

    def _store(room_id, scope=OWNER_SCOPE):
        peer = derive_peer_id(participant_key("agent", scope), room_id)
        return MainPersistenceManager(str(world.tmp), conversation=f"{room_id}.{peer}")

    closing = Room.create(kind="round", owner_scope=OWNER_SCOPE, base=world.rooms,
                          room_id="room-closing")
    host = closing.join(display="Agent-owner", scope_id=OWNER_SCOPE,
                        peer_id=derive_peer_id(participant_key("agent", OWNER_SCOPE),
                                               "room-closing"))
    closing.close(host)
    # A torn write leaves an empty manifest: the room is still there, only unreadable.
    torn = Room.create(kind="round", owner_scope=OWNER_SCOPE, base=world.rooms,
                       room_id="room-torn")
    torn.store.manifest_path.write_bytes(b"")
    stores = {"open": _store("room-a"), "closed": _store("room-closing"),
              "gone": _store("room-deleted"), "torn": _store("room-torn")}
    stale = _time.time() - 10 * 86400
    for store in stores.values():
        for f in store.context_dir.rglob("*"):
            if f.is_file():
                os.utime(f, (stale, stale))

    GarbageCollector.__new__(GarbageCollector)._clean_old_session_persistence(
        datetime.now() - timedelta(hours=48), {"deleted": 0, "freed_bytes": 0, "errors": 0})

    assert stores["open"].context_dir.exists(), "an open room lost its working memory"
    assert stores["torn"].context_dir.exists(), "an unreadable room lost its working memory"
    assert not stores["closed"].context_dir.exists(), "a closed room's store is never collected"
    assert not stores["gone"].context_dir.exists(), "a deleted room's store is never collected"


# ── AC 11: the drain runs as the chat's owner ──────────────────────────────────────────────

def test_the_drain_runs_as_the_chats_owner(world):
    from vaf.core.headless_runner import _drain_turn_binding
    from vaf.core.session import SessionManager
    from vaf.core.tool_dispatch import set_account_directory_resolver
    sm = SessionManager()
    _seed("web-carol-chat", scope=CAROL_SCOPE, username="carolq7")
    _seed(TELEGRAM_SESSION, scope=BOB_SCOPE, username="bobq7", metadata={"source": "telegram"})

    owner = _drain_turn_binding(sm, OWNER_SESSION)
    assert owner.identity == Identity(OWNER_SCOPE, "ownerq7", "admin")
    assert owner.synthetic is True and owner.session_id == OWNER_SESSION
    assert _drain_turn_binding(sm, BOB_SESSION).identity == Identity(BOB_SCOPE, "bobq7", None)

    row = {"username": "carolq7", "user_scope_id": CAROL_SCOPE, "role": "admin", "active": True}
    try:
        set_account_directory_resolver(lambda: [row])
        assert _drain_turn_binding(sm, "web-carol-chat").identity.role == "admin"
        row["active"] = False
        assert _drain_turn_binding(sm, "web-carol-chat").identity.role is None
        set_account_directory_resolver(lambda: [])
        assert _drain_turn_binding(sm, "web-carol-chat").identity.role is None
    finally:
        set_account_directory_resolver(None)

    assert _drain_turn_binding(sm, TELEGRAM_SESSION).source == "telegram"
    assert _drain_turn_binding(sm, BOB_SESSION).source == "web"

    # A chat that is gone or cannot be read has no owner to run as: bound as nobody, the turn
    # would run with no scope, the machine owner's freedoms.
    from vaf.core.turn_binding import TurnRefused
    with pytest.raises(TurnRefused):
        _drain_turn_binding(sm, "web-no-such-chat")
    (world.tmp / "sessions" / "web-broken-chat.json").write_bytes(b"\x00 not a chat")
    with pytest.raises(TurnRefused):
        _drain_turn_binding(sm, "web-broken-chat")
    # A readable chat that names no scope (a legacy chat) is bound as it is.
    _seed("web-legacy-chat", scope=None, username=None)
    assert _drain_turn_binding(sm, "web-legacy-chat").identity == Identity(None, None, None)


# ── AC 12: the producers ───────────────────────────────────────────────────────────────────

def test_the_task_binding_carries_the_task(world, monkeypatch):
    import vaf.core.contacts_store as contacts_store
    from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
    from vaf.core.headless_runner import _task_turn_binding
    from vaf.core.session import SessionManager
    asked = []
    grace = {"id": "c1", "name": "Grace", "preferred_language": "tr"}
    monkeypatch.setattr(contacts_store, "find_contact_by_channel",
                        lambda *a, **k: asked.append((a, k)) or grace)
    jid = "4915100000001@s.whatsapp.net"
    contact = _task(CONTACT_SESSION, OWNER, "Merhaba", source="whatsapp",
                    from_contact=True, whatsapp_chat_jid=jid)

    binding = _task_turn_binding(contact, SessionManager())

    assert binding.front_office is True
    assert binding.allowed_tools == frozenset(FRONT_OFFICE_ALLOWED_TOOLS)
    assert binding.front_office_contact == grace
    assert asked == [(("whatsapp", jid, "ownerq7"), {"user_scope_id": OWNER_SCOPE})]
    assert binding.source == "whatsapp" and binding.session_id == CONTACT_SESSION
    assert binding.identity == OWNER and binding.turn_id == contact.turn_id

    # The account check asks about the account the web enqueued the turn for.
    enqueued = _task(BOB_SESSION, Identity(None, None, None), "Hallo",
                     enqueue_user_scope_id=BOB_SCOPE)
    assert _task_turn_binding(enqueued, SessionManager()).account == BOB_SCOPE

    timer = _task(OWNER_SESSION, OWNER, "Erinnere mich an den Vertrag", timer=True)
    binding = _task_turn_binding(timer, SessionManager())
    assert binding.unattended is True and binding.is_human is False
    assert binding.wake == ("timer", "Erinnere mich an den Vertrag")
    assert binding.front_office is False and binding.allowed_tools is None
    person = _task(OWNER_SESSION, OWNER, "Hallo")
    binding = _task_turn_binding(person, SessionManager())
    assert binding.is_human is True and binding.unattended is False and binding.wake is None
    assert binding.project_dir.endswith("owner-proj")


def test_the_room_binding_hands_over_every_key():
    import re

    from vaf.core.headless_runner import _room_turn_binding
    agent_src = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    consumed = set(re.findall(r'room_turn\.get\("([a-z_]+)"\)', agent_src))
    consumed |= set(re.findall(r'room_turn\["([a-z_]+)"\]', agent_src))
    wake = {"room_id": "room-a", "peer_id": "p-0123456789", "mode": "assist", "query": "q",
            "from_user": True, "from_user_only": False, "from_authority": True,
            "scope": OWNER_SCOPE, "prompt": "..."}

    binding = _room_turn_binding(wake, OWNER)

    missing = (consumed | {"room_id", "mode"}) - set(binding.room_turn)
    assert not missing, f"the agent reads {sorted(missing)} off _room_turn and nobody hands it over"
    assert {k: binding.room_turn[k] for k in ("mode", "from_user", "from_authority")} == {
        "mode": "assist", "from_user": True, "from_authority": True}
    assert binding.conversation == "room-a.p-0123456789" and binding.room_id == "room-a"
    assert binding.source == "web" and binding.synthetic is True and binding.session_id is None
    for empty in ("", None):
        with pytest.raises(ValueError):
            _room_turn_binding({**wake, "peer_id": empty}, OWNER)


# ── AC 13: every lane binds before its turn ────────────────────────────────────────────────

def test_every_runner_lane_binds_before_its_turn():
    import vaf.core.headless_runner as hr
    loop = inspect.getsource(hr.run_headless_agent).replace("\r\n", "\n")

    queue = loop.split("task = tq.get()", 1)[1].split("_room_wake = agent.collect_room_wake", 1)[0]
    assert "_binding = _task_turn_binding(task, session_mgr)" in queue
    assert queue.index("bind_turn(agent, _binding)") < queue.index("agent.chat_step(")
    room = loop.split("_room_wake = agent.collect_room_wake", 1)[1].split("_pushed = False", 1)[0]
    assert room.index("bind_turn(agent, _room_turn_binding(") < room.index("agent.chat_step(")
    assert "end_turn(agent)" in room.split("finally:")[-1]
    drain = loop.split("_pushed = False", 1)[1]
    assert drain.index("bind_turn(agent, _drain_turn_binding(") < drain.index("agent.chat_step(")
    turn_and_after = drain.split("drain_summary_text = agent.chat_step(", 1)[1]
    assert turn_and_after.split("finally:", 1)[1].lstrip().startswith("end_turn(agent)")

    tree = ast.parse(loop)
    outer = [w.body[0] for w in ast.walk(tree) if isinstance(w, ast.While)
             and w.body and isinstance(w.body[0], ast.Try)]
    assert any("end_turn" in ast.dump(ast.Module(body=t.finalbody, type_ignores=[]))
               for t in outer), "the loop's iterations do not end their turn"
    for hand in ("bind_identity(", "reassert_identity(", "set_current_session_id(",
                 "set_current_turn_id(", "set_current_room_id(", "_unregister_session"):
        assert hand not in loop, f"the loop binds by hand again: {hand}"


# ── AC 14: one writer ──────────────────────────────────────────────────────────────────────

_BINDING = frozenset(n for n, kind in AGENT_STATE.items() if kind == "binding")
# Files that write a binding attribute on something other than the engine's own `self`.
ALLOWED_DIRECT_WRITERS = {
    # The construction-time lane default: the first prompt is built before any task binds
    # (tests/test_project_context_per_chat.py).
    "vaf/core/headless_runner.py": 1,
    # The facade's own `_allowed_tools`, a field of the facade object, handed to its engine
    # through `set_allowed_tools`.
    "vaf/framework.py": 2,
}
_PRIMITIVES = {"vaf/core/turn_binding.py", "vaf/core/identity_binding.py"}


def _binding_writes(path: Path) -> int:
    tree = ast.parse(path.read_bytes())
    engine_self = set()
    if path.as_posix().endswith("vaf/core/agent.py"):
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Agent")
        engine_self = {id(n) for n in ast.walk(cls)}
    count = 0
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, (ast.Assign, ast.Delete)):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            for t in (target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]):
                if not (isinstance(t, ast.Attribute) and t.attr in _BINDING):
                    continue
                if (id(t) in engine_self and isinstance(t.value, ast.Name)
                        and t.value.id == "self"):
                    continue
                count += 1
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("setattr", "delattr") and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in _BINDING):
            count += 1
    return count


def test_binding_attributes_have_one_writer():
    found = {}
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in _PRIMITIVES:
            continue
        count = _binding_writes(path)
        if count:
            found[rel] = count

    new = {f: n for f, n in found.items() if f not in ALLOWED_DIRECT_WRITERS}
    assert not new, (f"a turn's binding written by hand: {new}. Bind the turn through "
                     "vaf.core.turn_binding.bind_turn / end_turn")
    grown = {f: (found.get(f, 0), n) for f, n in ALLOWED_DIRECT_WRITERS.items()
             if found.get(f, 0) > n}
    assert not grown, f"an exempt file grew new direct writes (found, allowed): {grown}"
    shrunk = {f: (found.get(f, 0), n) for f, n in ALLOWED_DIRECT_WRITERS.items()
              if found.get(f, 0) < n}
    assert not shrunk, f"lower the baseline so it cannot creep back (found, allowed): {shrunk}"
