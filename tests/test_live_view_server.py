# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The live record where it is written and where it is read.

Written: a socket that moved from chat A to chat B receives nothing more of A (display
isolation), and every event A emits is still recorded for A, including the room lanes that
bypass `broadcast_to_session`. Read: `load_session` sends the transcript and then ONE
`live_replay`, only after the ownership gate; another account asking for the chat gets
neither; the connect-time auto-load sends the same pair marked `autoLoad`; a running turn's
transcript ends with its prompt as typed.
"""
import asyncio
import contextlib
import json
import threading
import time

import pytest

from vaf.core.live_view import LiveViewLedger
from vaf.core.task_queue import TaskQueue

BOB = "ab12cd34-0000-4000-8000-000000000001"
ALICE = "ab12cd34-0000-4000-8000-000000000002"
SID = "web-chat-0031"
OLDER = "web-chat-0030"


class _Sock:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


def _fresh_manager():
    from vaf.core.web_interface import WebInterfaceManager
    m = object.__new__(WebInterfaceManager)
    m.initialized = False
    m.__init__()
    return m


LIVE_TYPES = ("agent_message_update", "tool_update", "coder_state", "file_created",
              "subagent_update")


def _events_for(sid):
    return [
        {"type": "agent_message_update", "content": "streamed", "round": 1, "turnId": "t1"},
        {"type": "tool_update", "subType": "start", "toolId": "c1", "name": "read_file", "data": "{}"},
        {"type": "coder_state", "loop": 1},
        {"type": "file_created", "filePath": "/w/report.md", "title": "report.md", "turnId": "t1"},
        {"type": "subagent_update", "presence": "online", "agentName": "Coder"},
    ]


# ── AC1: recorded where it is delivered ──────────────────────────────────────────

def test_a_chat_left_behind_is_still_recorded_and_only_for_itself():
    """MUTATION: no record in broadcast_to_session - red."""
    m = _fresh_manager()
    sock = _Sock()
    m.active_connections.append(sock)
    m.subscribe_to_session(sock, "A")
    m.subscribe_to_session(sock, "B")          # the switch
    for ev in _events_for("A"):
        asyncio.run(m.broadcast_to_session("A", ev))
    assert sock.sent == [], "a chat left behind must not paint into the open one"
    recorded = [e["type"] for e in m.live_view.snapshot("A", active=True)["events"]]
    assert recorded == list(LIVE_TYPES)
    assert m.live_view.snapshot("B", active=True)["events"] == []
    # The terminal event takes the owner lane, and is recorded there too: the turn is over.
    asyncio.run(m.broadcast_to_session_and_owner("A", None, {"type": "message_complete",
                                                              "turnId": "t1"}))
    assert [e["type"] for e in m.live_view.snapshot("A", active=True)["events"]] == [
        "coder_state", "subagent_update"]


def test_the_room_lane_records_for_the_chat(monkeypatch):
    """The room reroute sends per user and never through broadcast_to_session.
    MUTATION: drop the record in either room branch of _push_session_update - red."""
    m = _fresh_manager()
    monkeypatch.setattr(m, "_http_fallback_push", lambda data: None)
    monkeypatch.setattr(m, "_get_dispatch_loop", lambda: None)

    class _Agent:
        _room_turn = {"room_id": "room-1"}
        _current_user_scope_id = BOB
    m.agent_instance = _Agent()
    m._push_session_update("A", {"type": "tool_update", "subType": "start", "toolId": "r1"})
    m.agent_instance = None
    monkeypatch.setattr(m, "room_route_for_session", lambda sid: ("room-1", BOB))
    m._push_session_update("A", {"type": "coder_state", "loop": 3})
    events = m.live_view.snapshot("A", active=True)["events"]
    assert [(e["type"], e.get("roomId")) for e in events] == [("tool_update", "room-1"),
                                                             ("coder_state", "room-1")]


def test_the_stream_endpoint_room_branches_record(monkeypatch):
    """MUTATION: drop the record in a room branch of /api/subagent/stream - red."""
    import vaf.core.web_server as server
    m = _fresh_manager()

    async def _to_user(scope, data):
        return None
    monkeypatch.setattr(m, "broadcast_to_user", _to_user)
    monkeypatch.setattr(m, "room_audience", lambda rid: BOB)
    monkeypatch.setattr(m, "room_route_for_session", lambda sid: ("room-2", BOB))
    monkeypatch.setattr(server, "manager", m)
    asyncio.run(server.receive_subagent_stream(server.SubAgentStreamUpdate(
        type="subagent_update", sessionId="A", roomId="room-1", status="one"), _caller={}))
    asyncio.run(server.receive_subagent_stream(server.SubAgentStreamUpdate(
        type="subagent_output_stream", sessionId="A", line="two"), _caller={}))
    events = m.live_view.snapshot("A", active=True)["events"]
    assert [e["type"] for e in events] == ["subagent_update", "subagent_output_stream"]


def test_record_failure_never_costs_the_delivery(monkeypatch):
    m = _fresh_manager()
    sock = _Sock()
    m.active_connections.append(sock)
    m.subscribe_to_session(sock, "A")

    def _boom(*a, **k):
        raise RuntimeError("record broke")
    monkeypatch.setattr(m.live_view, "record", _boom)
    asyncio.run(m.broadcast_to_session("A", {"type": "coder_state"}))
    assert [e["type"] for e in sock.sent] == ["coder_state"]


# ── AC12, server half: the preview place ─────────────────────────────────────────

def test_preview_place_on_the_upload_socket_is_recorded_and_cleared(monkeypatch):
    import vaf.core.web_server as server
    m = _fresh_manager()
    monkeypatch.setattr(server, "manager", m)
    got = []

    async def _send(payload):
        got.append(payload)
    notify = server._recording_notify(_send, "A")
    asyncio.run(notify({"type": "document_preview_wait", "name": "a.docx", "position": 3}))
    assert [e["position"] for e in m.live_view.snapshot("A", active=False)["events"]] == [3]
    asyncio.run(notify({"type": "document_preview_wait", "name": "a.docx", "position": 0}))
    assert m.live_view.snapshot("A", active=False)["events"] == []
    assert [g["position"] for g in got] == [3, 0]       # the socket still gets both


# ── AC4, AC5: read after the gate, through the real /ws handler ──────────────────

@pytest.fixture(autouse=True)
def _fresh_queue():
    TaskQueue._instance = None  # type: ignore[attr-defined]
    tq = TaskQueue()
    tq._legacy_mode = True  # type: ignore[attr-defined]
    yield tq
    TaskQueue._instance = None  # type: ignore[attr-defined]


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Bob's two chats on disk (SID the newest), a fresh live record, the real handler."""
    import vaf.auth.crypto as crypto
    import vaf.auth.permissions as permissions
    import vaf.core.session as session_mod
    import vaf.core.task_queue as tq_mod
    import vaf.core.web_server as server

    sessions_dir = tmp_path / "ws-sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_mod, "default_sessions_dir", lambda: sessions_dir)
    mgr = session_mod.SessionManager(storage_dir=str(sessions_dir))
    monkeypatch.setattr(server, "session_mgr", mgr)
    for sid in (OLDER, SID):
        s = mgr.new(name="Bobs Chat", user_scope_id=BOB)
        s.id = sid
        s.messages = [session_mod.Message(role="user", content="earlier question"),
                      session_mod.Message(role="assistant", content="earlier answer")]
        mgr.save(s)
        time.sleep(0.01)                      # SID is the newest, the one auto-loaded

    async def _unknown(scope):
        return ("unknown", "")
    monkeypatch.setattr(permissions, "account_standing_async", _unknown)
    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    monkeypatch.setattr(tq_mod, "enqueue_system_command", lambda command, **k: None)
    monkeypatch.setattr(server.manager, "live_view", LiveViewLedger())
    return server


@contextlib.contextmanager
def _connect(server, scope, name):
    import jwt
    from starlette.testclient import TestClient
    token = jwt.encode({"sub": name, "user_scope_id": scope, "username": name, "role": "user"},
                       "s" * 32, algorithm="HS256")
    with TestClient(server.app, client=("127.0.0.1", 40002)).websocket_connect(
            f"/ws?token={token}") as ws:
        yield ws


def _send(ws, frame):
    ws.send_text(json.dumps(frame))


def _until(ws, kind, timeout=10.0):
    seen = []

    def read():
        try:
            while True:
                msg = ws.receive_json()
                seen.append(msg)
                if msg.get("type") == kind:
                    return
        except Exception:
            return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    assert seen and seen[-1].get("type") == kind, f"no {kind}: {[m.get('type') for m in seen]}"
    return seen


def _busy(tq, sid):
    """A turn of `sid` taken by a worker: the queue says the chat is active."""
    task = tq.add(session_id=sid, input_text="running", source="web",
                  metadata={"enqueue_user_scope_id": BOB})
    assert tq.get(timeout=0.0, worker_id="w1") is not None
    return task


def _record(server, sid):
    for ev in _events_for(sid):
        server.manager.live_view.record(sid, dict(ev, sessionId=sid))


def test_load_session_sends_history_then_one_replay(world, _fresh_queue):
    """MUTATION: send the replay before the history_update - red."""
    server = world
    task = _busy(_fresh_queue, OLDER)
    server.manager.live_view.set_prompt(OLDER, task.turn_id,
                                        {"role": "user", "content": "what now?",
                                         "timestamp": "2999-01-01T00:00:00"})
    _record(server, OLDER)
    with _connect(server, BOB, "bob") as ws:
        _until(ws, "history_update")              # the auto-load of the newest chat
        _send(ws, {"type": "load_session", "id": OLDER})
        frames = _until(ws, "live_replay")
        mine = [f for f in frames if f.get("sessionId") == OLDER
                and f.get("type") in ("history_update", "live_replay")]
        assert [f["type"] for f in mine] == ["history_update", "live_replay"]
        history, replay = mine
        assert history["live"] is True and history["isActive"] is True
        assert history["messages"][-1] == {"role": "user", "content": "what now?",
                                           "timestamp": "2999-01-01T00:00:00"}
        assert [e["type"] for e in replay["events"]] == list(LIVE_TYPES)
        assert all(e["sessionId"] == OLDER for e in replay["events"])


def test_a_finished_turn_is_not_replayed_and_not_live(world):
    """The queue says idle: the turn's events and its prompt stay out; the worker view stays."""
    server = world
    server.manager.live_view.set_prompt(OLDER, "t1", {"role": "user", "content": "q"})
    _record(server, OLDER)
    with _connect(server, BOB, "bob") as ws:
        _until(ws, "history_update")
        _send(ws, {"type": "load_session", "id": OLDER})
        frames = _until(ws, "live_replay")
        history = [f for f in frames if f.get("type") == "history_update"
                   and f.get("sessionId") == OLDER][-1]
        assert history["live"] is False and history["isActive"] is False
        assert history["messages"][-1]["content"] == "earlier answer"
        assert [e["type"] for e in frames[-1]["events"]] == ["coder_state", "subagent_update"]


def test_another_account_gets_neither_history_nor_replay(world, _fresh_queue):
    """Rule 4.4: Alice asks for Bob's running chat by id.
    MUTATION: send the replay before the ownership gate - red."""
    server = world
    _busy(_fresh_queue, OLDER)
    server.manager.live_view.set_prompt(OLDER, "t1", {"role": "user", "content": "bob's secret"})
    _record(server, OLDER)
    with _connect(server, ALICE, "alice") as ws:
        first = _until(ws, "history_update")       # her own (new) chat
        assert all(f.get("sessionId") != OLDER for f in first)
        _send(ws, {"type": "load_session", "id": OLDER})
        _send(ws, {"type": "get_sessions"})
        frames = _until(ws, "session_list")
        assert {"type": "error", "message": "Access denied"} in frames
        leaked = [f for f in frames if f.get("type") in ("history_update", "live_replay")]
        assert leaked == [], leaked
        assert "bob's secret" not in json.dumps(frames)


def test_the_auto_load_sends_the_pair_marked(world, _fresh_queue):
    """MUTATION: no autoLoad on the connect-time history_update - red; no replay there - red."""
    server = world
    _busy(_fresh_queue, SID)
    _record(server, SID)
    with _connect(server, BOB, "bob") as ws:
        frames = _until(ws, "live_replay")
        pair = [f for f in frames if f.get("type") in ("history_update", "live_replay")]
        assert [(f["type"], f["sessionId"]) for f in pair] == [("history_update", SID),
                                                               ("live_replay", SID)]
        assert pair[0]["autoLoad"] is True and pair[0]["live"] is True


def test_no_replay_frame_without_events(world):
    server = world
    with _connect(server, BOB, "bob") as ws:
        _send(ws, {"type": "load_session", "id": OLDER})
        _send(ws, {"type": "get_sessions"})
        frames = _until(ws, "session_list")
        frames += _until(ws, "session_list")
        assert not [f for f in frames if f.get("type") == "live_replay"]


def test_a_running_turns_prompt_is_kept_as_typed(world, _fresh_queue):
    """The chat frame's text as typed, never the editor block the server prepends for the model.
    MUTATION: record the prompt from the editor-prefixed content - red."""
    server = world
    with _connect(server, BOB, "bob") as ws:
        _until(ws, "history_update")
        _send(ws, {"type": "chat", "content": "fix the intro", "sessionId": OLDER,
                   "editorDocument": {"name": "Draft", "content": "Once upon a time"}})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _fresh_queue.is_busy_for_session(OLDER):
            time.sleep(0.02)
        assert _fresh_queue.is_busy_for_session(OLDER)
        _send(ws, {"type": "load_session", "id": OLDER})
        frames = _until(ws, "history_update")
        while frames[-1].get("sessionId") != OLDER:
            frames = _until(ws, "history_update")
        history = frames[-1]
        prompt = history["messages"][-1]
        assert prompt["role"] == "user" and prompt["content"] == "fix the intro"
        assert history["live"] is True


def test_deleting_or_hiding_a_chat_forgets_its_record():
    """Source pin: the record goes with the chat."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_bytes().decode()
    delete = src[src.index('elif type == "delete_session":'):src.index('elif type == "hide_session":')]
    hide = src[src.index('elif type == "hide_session":'):src.index('elif type == "new_session":')]
    assert "manager.live_view.forget(sid)" in delete
    assert "manager.live_view.forget(sid)" in hide
    forget = src[src.index("def _refresh_after_forget("):src.index("def _send_to_chat_viewers(")]
    assert "drop_prompt(sid)" in forget
