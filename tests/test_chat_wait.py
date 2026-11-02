# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A queued web chat turn tells its chat where it stands (`chat_wait`).

Driven through the real `/ws` handler as a signed-in account, with the real TaskQueue: a task
held in flight by one worker makes the next turn wait, a second `get()` takes it. What has to
hold: the place reaches the chat's own subscribers (never the room reroute of
`_push_session_update`), after a short grace so a turn taken at once shows nothing, with the
session bound when the turn was queued; a reload shows the place again (`history_update`
carries `chatWait`); the page forwards every field; and the old every-two-seconds
`queue_stats` broadcast to every connection is gone.
"""
import contextlib
import json
import re
import threading
import time
from pathlib import Path

import pytest

from vaf.core.task_queue import TaskQueue

ROOT = Path(__file__).resolve().parents[1]
WEB_SERVER = ROOT / "vaf" / "core" / "web_server.py"
PAGE = ROOT / "web" / "app" / "page.tsx"
SCOPE = "ab12cd34-0000-4000-8000-000000000001"
OTHER = "ab12cd34-0000-4000-8000-000000000002"
SID = "web-chat-0021"
SID2 = "web-chat-0022"


@pytest.fixture(autouse=True)
def _fresh_queue():
    TaskQueue._instance = None  # type: ignore[attr-defined]
    tq = TaskQueue()
    tq._legacy_mode = True  # type: ignore[attr-defined]
    yield tq
    TaskQueue._instance = None  # type: ignore[attr-defined]


@contextlib.contextmanager
def _bob(monkeypatch, tmp_path, *, exchange=False):
    """The real WebSocket handler, signed in as Bob, with two chats of his on disk."""
    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    import vaf.auth.permissions as permissions
    import vaf.core.session as session_mod
    import vaf.core.task_queue as tq_mod
    import vaf.core.web_server as real_server

    sessions_dir = tmp_path / "ws-sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_mod, "default_sessions_dir", lambda: sessions_dir)
    mgr = session_mod.SessionManager(storage_dir=str(sessions_dir))
    monkeypatch.setattr(real_server, "session_mgr", mgr)
    for sid in (SID, SID2):
        s = mgr.new(name="Bobs Chat", user_scope_id=SCOPE)
        s.id = sid
        if exchange:
            s.messages = [session_mod.Message(role="user", content="question"),
                          session_mod.Message(role="assistant", content="answer")]
        mgr.save(s)

    async def _unknown(scope):
        return ("unknown", "")
    monkeypatch.setattr(permissions, "account_standing_async", _unknown)
    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    # A chat switch queues LOAD_SESSION for the runner; nothing runs it here.
    monkeypatch.setattr(tq_mod, "enqueue_system_command", lambda command, **k: None)
    token = jwt.encode({"sub": "2", "user_scope_id": SCOPE, "username": "bob", "role": "user"},
                       "s" * 32, algorithm="HS256")
    with TestClient(real_server.app, client=("127.0.0.1", 40001)).websocket_connect(
            f"/ws?token={token}") as ws:
        yield ws, real_server


def _send(ws, frame):
    ws.send_text(json.dumps(frame))


def _until(ws, kind, timeout=10.0):
    """Frames up to and including the first of `kind` (a thread, so a missing frame is a
    red test and not a hang)."""
    seen = []

    def read():
        try:
            while True:
                msg = ws.receive_json()
                seen.append(msg)
                if msg.get("type") == kind:
                    return
        except Exception:
            return   # the socket closed under a reader that timed out

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    assert seen and seen[-1].get("type") == kind, f"no {kind}: {[m.get('type') for m in seen]}"
    return seen


def _busy(tq):
    tq.add(session_id="busy-chat", input_text="busy", source="web",
           metadata={"enqueue_user_scope_id": OTHER})
    assert tq.get(timeout=0.0, worker_id="busy") is not None


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _take(tq, sid):
    task = tq.get(timeout=2.0, worker_id="w")
    assert task is not None and task.session_id == sid
    return task


# ── the event ────────────────────────────────────────────────────────────────────

def test_a_queued_web_turn_reports_its_place(monkeypatch, tmp_path, _fresh_queue):
    """MUTATION: no on_wait at the chat enqueue - no chat_wait, red."""
    tq = _fresh_queue
    _busy(tq)
    with _bob(monkeypatch, tmp_path) as (ws, _server):
        _send(ws, {"type": "chat", "content": "hello", "sessionId": SID})
        first = _until(ws, "chat_wait")[-1]
        assert first["sessionId"] == SID and first["position"] == 1
        assert first["eta_ms"] is None and isinstance(first["waited_ms"], int)
        _take(tq, SID)
        assert _until(ws, "chat_wait")[-1]["position"] == 0


def test_the_wait_never_follows_a_room_turn(monkeypatch, tmp_path, _fresh_queue):
    """With worker 1's agent in a room turn of another account, the place still reaches the
    chat's own subscriber and nothing goes per user.
    MUTATION: send through _push_session_update - it goes to the room owner, red."""
    tq = _fresh_queue
    _busy(tq)
    with _bob(monkeypatch, tmp_path) as (ws, server):
        agent = type("RoomAgent", (), {})()
        agent._room_turn = {"room_id": "room-1"}
        agent._current_user_scope_id = OTHER
        monkeypatch.setattr(server.manager, "agent_instance", agent, raising=False)
        per_user = []
        real_to_user = server.manager.broadcast_to_user

        async def spy(user_id, message):
            per_user.append((user_id, message.get("type")))
            return await real_to_user(user_id, message)

        monkeypatch.setattr(server.manager, "broadcast_to_user", spy)
        _send(ws, {"type": "chat", "content": "hello", "sessionId": SID})
        assert _until(ws, "chat_wait")[-1]["position"] == 1
        assert not [p for p in per_user if p[1] == "chat_wait"]


def test_the_session_is_bound_at_enqueue(monkeypatch, tmp_path, _fresh_queue):
    """The same socket sends into a second chat before the first turn is taken: the first
    turn's place still names the first chat.
    MUTATION: a sender closure reading the WS loop's session_id - both name SID2, red."""
    tq = _fresh_queue
    _busy(tq)
    with _bob(monkeypatch, tmp_path) as (ws, server):
        sent = []
        real_to_session = server.manager.broadcast_to_session

        async def spy(session_id, message):
            if message.get("type") == "chat_wait":
                sent.append((session_id, message.get("sessionId"), message.get("position")))
            return await real_to_session(session_id, message)

        monkeypatch.setattr(server.manager, "broadcast_to_session", spy)
        _send(ws, {"type": "chat", "content": "first", "sessionId": SID})
        _send(ws, {"type": "chat", "content": "second", "sessionId": SID2})
        assert _wait_for(lambda: len(sent) >= 2)
        assert sorted(sent) == [(SID, SID, 1), (SID2, SID2, 2)]


def test_regenerate_reports_its_place(monkeypatch, tmp_path, _fresh_queue):
    """MUTATION: omit on_wait at the regenerate enqueue - no chat_wait, red."""
    tq = _fresh_queue
    _busy(tq)
    with _bob(monkeypatch, tmp_path, exchange=True) as (ws, _server):
        _send(ws, {"type": "regenerate_last_reply", "sessionId": SID})
        first = _until(ws, "chat_wait")[-1]
        assert first["sessionId"] == SID and first["position"] == 1
        _take(tq, SID)
        assert _until(ws, "chat_wait")[-1]["position"] == 0


def test_a_turn_taken_at_once_sends_nothing(monkeypatch, tmp_path, _fresh_queue):
    """A worker takes the turn within CHAT_WAIT_GRACE_S: no chat_wait at all, neither the
    place nor the start. Counted at the send (`broadcast_to_session`), not by reading the
    socket, which cannot prove that something did NOT arrive.
    MUTATION: grace_s 0 for chat - "place 1" flashes, red."""
    tq = _fresh_queue
    with _bob(monkeypatch, tmp_path) as (ws, server):
        sent = []
        real_to_session = server.manager.broadcast_to_session

        async def spy(session_id, message):
            if message.get("type") == "chat_wait":
                sent.append(message.get("position"))
            return await real_to_session(session_id, message)

        monkeypatch.setattr(server.manager, "broadcast_to_session", spy)
        _send(ws, {"type": "chat", "content": "hello", "sessionId": SID})
        assert _wait_for(lambda: tq.get_queue_size() == 1, timeout=5.0)
        _take(tq, SID)
        time.sleep(server.CHAT_WAIT_GRACE_S + 0.5)
        assert sent == []


def test_a_stop_within_the_grace_sends_nothing(monkeypatch, tmp_path, _fresh_queue):
    """Stop drops the queued turn before the grace is over: the held-back place is never
    sent (the queue drops a turn silently, so the sender asks whether the chat still waits).
    MUTATION: drop the still_waiting check - "place 1" arrives after the Stop, red."""
    tq = _fresh_queue
    _busy(tq)
    with _bob(monkeypatch, tmp_path) as (ws, server):
        sent = []
        real_to_session = server.manager.broadcast_to_session

        async def spy(session_id, message):
            if message.get("type") == "chat_wait":
                sent.append(message.get("position"))
            return await real_to_session(session_id, message)

        monkeypatch.setattr(server.manager, "broadcast_to_session", spy)
        _send(ws, {"type": "chat", "content": "hello", "sessionId": SID})
        assert _wait_for(lambda: tq.has_queued(SID), timeout=5.0)
        assert tq.drop_queued_tasks_for_session(SID) == 1
        time.sleep(server.CHAT_WAIT_GRACE_S + 0.5)
        assert sent == []


def test_the_forget_refresh_stays_with_the_chat(monkeypatch, tmp_path):
    """The transcript a forget refresh carries goes to the chat's subscribers only, also while
    worker 1's agent runs a room turn of another account (the reroute of
    _push_session_update).
    MUTATION: send it through _bridge_or_push again - it goes to the room owner, red."""
    import asyncio

    import vaf.core.session as session_mod
    import vaf.core.web_server as server

    mgr = session_mod.SessionManager(storage_dir=str(tmp_path))
    s = mgr.new(name="Bobs Chat", user_scope_id=SCOPE)
    s.id = SID
    mgr.save(s)
    monkeypatch.setattr(server, "session_mgr", mgr)
    agent = type("RoomAgent", (), {})()
    agent._room_turn = {"room_id": "room-1"}
    agent._current_user_scope_id = OTHER
    monkeypatch.setattr(server.manager, "agent_instance", agent, raising=False)
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    to_session, to_user = [], []

    async def session_spy(session_id, message):
        to_session.append((session_id, message.get("type")))

    async def user_spy(user_id, message):
        to_user.append((user_id, message.get("type")))

    monkeypatch.setattr(server.manager, "broadcast_to_session", session_spy)
    monkeypatch.setattr(server.manager, "broadcast_to_user", user_spy)
    monkeypatch.setattr(server.manager, "_get_dispatch_loop", lambda: loop)
    try:
        worker = threading.Thread(target=server._refresh_after_forget,
                                  kwargs={"session_id": SID, "transcript_scrubbed": True})
        worker.start()
        worker.join(5)
        assert _wait_for(lambda: len(to_session) >= 2, timeout=5.0)
        time.sleep(0.2)
        assert to_session == [(SID, "context_checkpoint"), (SID, "history_update")]
        assert to_user == []
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(5)
        loop.close()


def test_the_speech_sender_is_unchanged():
    """The speech callers pass none of the new knobs: the first place goes at once, a change
    within a second is throttled, the grant always goes.
    MUTATION: change the default throttle - the second place goes too, red."""
    import asyncio

    import vaf.core.web_server as server
    from vaf.core.fair_queue import WaitInfo

    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    got = []

    class _Socket:
        async def send_json(self, payload):
            got.append(payload)

    try:
        on_wait = server._queue_wait_sender(_Socket(), loop, "stt_wait")
        on_wait(WaitInfo("stt", 3, None, 0.0))
        on_wait(WaitInfo("stt", 2, None, 0.1))
        on_wait(WaitInfo("stt", 0, None, 0.2))
        assert _wait_for(lambda: len(got) >= 2, timeout=5.0)
        time.sleep(0.2)
        assert [(m["type"], m["position"]) for m in got] == [("stt_wait", 3), ("stt_wait", 0)]
        assert set(got[0]) == {"type", "position", "eta_ms", "waited_ms"}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(5)
        loop.close()


# ── the place on a reload or a chat switch ───────────────────────────────────────

def test_history_update_carries_the_place(monkeypatch, tmp_path, _fresh_queue):
    """MUTATION: build the load_session history_update by hand again (no chatWait) - red."""
    tq = _fresh_queue
    _busy(tq)
    tq.add(session_id="other-1", input_text="x", source="web",
           metadata={"enqueue_user_scope_id": OTHER})
    tq.add(session_id=SID, input_text="waiting", source="web",
           metadata={"enqueue_user_scope_id": SCOPE})
    with _bob(monkeypatch, tmp_path) as (ws, server):
        assert server._session_activity(SID)["chatWait"] == {"position": 2, "eta_ms": None,
                                                             "more": False}
        assert server._session_activity(SID2)["chatWait"] is None
        _send(ws, {"type": "load_session", "id": SID})
        update = _until(ws, "history_update")[-1]
        while update.get("sessionId") != SID:   # the connect sends the newest chat first
            update = _until(ws, "history_update")[-1]
        assert update["chatWait"] == {"position": 2, "eta_ms": None, "more": False}
        assert update["isActive"] is True


def test_one_activity_helper():
    """The history_update activity fields come from ONE helper; only the regenerate repaint
    keeps its literal (its task is queued right after and reports by chat_wait).
    MUTATION: build one of the three by hand again - red."""
    src = WEB_SERVER.read_bytes().decode()
    assert src.count("**_session_activity(") == 3
    helper = src[src.index("def _session_activity("):src.index("def _session_status_label(")]
    # Two in the whole file: the helper's own and the regenerate repaint's literal.
    assert len(re.findall(r'"isActive":\s', src)) == 2
    assert len(re.findall(r'"isActive":\s', helper)) == 1
    assert len(re.findall(r'"isActive":\s*True,\s*\n\s*"currentStatus":\s*"thinking"', src)) == 1


# ── the page ─────────────────────────────────────────────────────────────────────

def _branch(src, marker, length=None):
    """The source from `marker` up to the next handler branch (or `length` characters)."""
    start = src.index(marker)
    if length is not None:
        return src[start:start + length]
    end = src.find("else if (data.type ===", start + len(marker))
    return src[start:end if end != -1 else len(src)]


def test_page_forwards_and_shows_the_place():
    """Every field is read explicitly (the handler rebuilds objects field by field), the
    three texts sit in the loading bubble's status slot, and every end of the wait clears it.
    MUTATIONS: drop the eta_ms forward; drop the clear in generation_stopped - red."""
    src = PAGE.read_bytes().decode()
    wait = _branch(src, "else if (data.type === 'chat_wait')")
    for read in ("data.sessionId", "data.position", "data.eta_ms", "data.more"):
        assert read in wait, read
    history = _branch(src, "else if (data.type === 'history_update')")
    assert "data.chatWait.position" in history and "data.chatWait.eta_ms" in history
    assert "data.chatWait.more" in history
    for key in ("'chatWaiting'", "'chatWaitingMinutes'", "'chatWaitingNoEta'",
                "'chatWaitingMany'"):
        assert key in src, key
    bubble = _branch(src, "{chatWaitLine ? (", 400)
    assert 'role="status"' in bubble and 'aria-live="polite"' in bubble
    for kind in ("generation_stopped", "message_complete", "agent_message_update"):
        body = _branch(src, f"else if (data.type === '{kind}')")
        assert "clearChatWait(" in body, kind


# ── the counted places ───────────────────────────────────────────────────────────

# Each locale's way of saying "at least": the reported place PLACES_COUNTED + 1 means that
# place OR LATER, so exactly PLACES_COUNTED turns may be ahead and "more than" would be false.
AT_LEAST = {"en": "at least", "de": "mindestens", "ja": "以上", "ko": "이상",
            "th": "อย่างน้อย", "tr": "en az", "zh": "至少"}


@pytest.mark.parametrize("ahead", [19, 20, 21])
def test_the_line_beyond_the_counted_places_is_true(ahead, _fresh_queue):
    """19, 20 and 21 turns ahead: 19 is counted exactly; 20 and 21 both arrive as
    PLACES_COUNTED + 1 with `more`, and the page says "at least {position - 1} ahead",
    which holds for both. MUTATION: the old "more than {count}" wording - red at 20 ahead."""
    import vaf.core.web_server as server
    from vaf.core.task_queue import PLACES_COUNTED

    tq = _fresh_queue
    _busy(tq)
    for i in range(ahead):
        tq.add(session_id=f"ahead-{i}", input_text="x", source="web",
               metadata={"enqueue_user_scope_id": OTHER})
    tq.add(session_id=SID, input_text="mine", source="web",
           metadata={"enqueue_user_scope_id": OTHER})
    wait = server._session_chat_wait(SID)
    if ahead < PLACES_COUNTED:
        assert wait == {"position": ahead + 1, "eta_ms": None, "more": False}
        return
    assert wait == {"position": PLACES_COUNTED + 1, "eta_ms": None, "more": True}
    count = wait["position"] - 1          # what page.tsx passes as {count}
    assert count <= ahead, "the line claims more turns ahead than there are"
    src = PAGE.read_bytes().decode()
    assert "tMain('chatWaitingMany', { count: chatWait.position - 1 })" in src
    for locale, marker in AT_LEAST.items():
        catalog = json.loads((ROOT / "web" / "messages" / f"{locale}.json").read_bytes())
        line = catalog["main"]["chatWaitingMany"]
        assert "{count}" in line and marker in line, (locale, line)


# ── the ghost ────────────────────────────────────────────────────────────────────

def test_no_queue_broadcast():
    """The whole-queue counts went to every connection of every account every two seconds,
    and nothing read them. MUTATION: restore the push - red."""
    for path in (ROOT / "vaf").rglob("*.py"):
        text = path.read_bytes().decode("utf-8", errors="replace")
        assert '"queue_stats"' not in text and "'queue_stats'" not in text, path
    flow = (ROOT / "docs" / "web-ui" / "WEBUI_WEBSOCKET_FLOW.md").read_bytes().decode()
    assert "`queue_stats`" not in flow
    runner = (ROOT / "vaf" / "core" / "headless_runner.py").read_bytes().decode()
    assert "[METRICS]" in runner
