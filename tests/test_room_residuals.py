# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Agent rooms and the Veyllo App room: the line id a page names is kept by one post only,
the web room lane runs off the event loop with a write rate per account, labels keep their
numbers when a member joins or renames into a collision, a refused turn answers the Veyllo
App, and a zero-width space after a name ends the mention.

The fuzz tests carry a fixed seed and a small draw count; the environment variables
`VAF_ROOM_FUZZ_ROUNDS`, `VAF_ROOM_FUZZ_LINES` and `VAF_ROOM_FUZZ_ROOMS` run the larger counts outside
the suite.

MUTATIONS, each turns the named test red:
- no claim of the page's id in `post`, or a claim released before the store
  -> test_two_posts_of_one_page_id_at_once_keep_it_once (both holds);
- the claim not released on a refusal -> test_a_refused_post_releases_its_claim;
- `_send_room_transcript` building inline, or on `asyncio.to_thread`
  -> test_the_transcript_is_built_on_the_room_lane; the command or `open_room` store work
  inline -> test_a_say_and_an_open_run_on_the_room_lane;
- no take for `room_react`, or one window key for every account
  -> test_the_sixty_first_write_in_a_minute_is_refused;
- labels sorted by peer again, `named_at` written on every join, a missing `named_at` read as
  now -> the label tests below;
- no `finish_reply` for a refused Veyllo App turn, or one for every channel
  -> the refused-turn tests below;
- U+200B not a separator, a separator without the union, U+2060 treated like U+200B
  -> the mention tests below.
"""
import asyncio
import os
import random
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List

import pytest

from app_link_client import paired_device
from app_link_env import A1, SCOPE_A, SCOPE_B, _ask, _task_of, _two_devices  # noqa: F401
from app_link_env import app_link_home  # noqa: F401  (fixture)
from test_app_link_room import _capable, _queued, room_home  # noqa: F401  (fixture)
from vaf.core.a2a import store as store_mod
from vaf.core.a2a.room import (MENTION_MAX_CHARS, Room, derive_peer_id, display_name,
                               lookup_key, mention_candidates, name_key, participant_key,
                               resolve_mention)
from vaf.core.app_link import bridge, devices
from vaf.core.app_link import room as app_room

ZWSP = "\u200b"
WJ = "\u2060"


# ── item 2: a page's line id, claimed for the length of the post ─────────────────────────

@pytest.mark.parametrize("hold", ["check", "store"])
def test_two_posts_of_one_page_id_at_once_keep_it_once(room_home, monkeypatch, hold):
    """Two posts of one account with the same page id at the same moment: one keeps the id,
    the other gets the server's. The first post is held inside its id check (with the answer
    it already read) or inside its store, until the second post has returned; without the
    claim, the second post also reads "not in use" and both keep the id."""
    _capable()
    page_id = str(uuid.uuid4())
    go = threading.Event()
    seen: List[str] = []
    if hold == "check":
        real = app_room._line_id_in_use

        def held(scope, owner, line_id):
            answer = real(scope, owner, line_id)
            seen.append(line_id)
            if len(seen) == 1:
                go.wait(10)
            return answer
        monkeypatch.setattr(app_room, "_line_id_in_use", held)
        texts = ("@VAF eins", "@VAF zwei")
    else:
        real_append = app_room._append

        def held(*args, **kwargs):
            seen.append("append")
            if len(seen) == 1:
                go.wait(10)
            return real_append(*args, **kwargs)
        monkeypatch.setattr(app_room, "_append", held)
        texts = ("eins", "zwei")
    out: Dict[str, Any] = {}
    first = threading.Thread(target=lambda: out.setdefault(
        "first", app_room.post(SCOPE_A, "alice", texts[0], client_id=page_id)))
    first.start()
    deadline = time.monotonic() + 10
    while not seen and time.monotonic() < deadline:
        time.sleep(0.005)
    assert seen, "the first post never reached the held step"
    try:
        out["second"] = app_room.post(SCOPE_A, "alice", texts[1], client_id=page_id)
    finally:
        go.set()
        first.join(10)
    ids = [out["first"]["id"], out["second"]["id"]]
    assert ids[0] != ids[1], "two lines kept the page's id"
    assert ids.count(page_id) == 1
    assert app_room._claimed == {}


def test_a_refused_post_releases_its_claim(room_home, monkeypatch):
    """A post refused after it claimed the id (the chat could not be written) holds nothing
    afterwards, and the same id is the line's on the next post."""
    _capable()
    page_id = str(uuid.uuid4())
    real_append = app_room._append
    monkeypatch.setattr(app_room, "_append", lambda *a, **k: False)
    with pytest.raises(app_room.RoomRefused):
        app_room.post(SCOPE_A, "alice", "hallo", client_id=page_id)
    assert app_room._claimed == {}
    monkeypatch.setattr(app_room, "_append", real_append)
    assert app_room.post(SCOPE_A, "alice", "hallo", client_id=page_id)["id"] == page_id


def test_page_ids_posted_from_four_threads_are_never_kept_twice(room_home, monkeypatch):
    """Concurrency fuzz: four threads post at once with page ids drawn from three, to both
    addressees. Oracle: no two answers of a round carry one id, and no id is stored or queued
    twice. 20 rounds in the suite (seed 308); `VAF_ROOM_FUZZ_ROUNDS` runs more."""
    from vaf.core.app_link import session as link_session
    monkeypatch.setattr(link_session, "MSG_PER_MINUTE", 10 ** 9)
    _capable()
    rng = random.Random(308)
    pool = [str(uuid.uuid4()) for _ in range(3)]
    returned: List[str] = []
    for _round in range(int(os.environ.get("VAF_ROOM_FUZZ_ROUNDS", "20"))):
        plan = [(rng.choice(pool), rng.choice(("@VAF frage", "zeile"))) for _ in range(4)]
        start = threading.Barrier(4)
        answers: List[str] = []
        failures: List[BaseException] = []

        def worker(page_id, text):
            start.wait(10)
            try:
                answers.append(app_room.post(SCOPE_A, "alice", text, client_id=page_id)["id"])
            except BaseException as exc:  # noqa: BLE001 - reported below
                failures.append(exc)

        threads = [threading.Thread(target=worker, args=step) for step in plan]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert not failures, failures
        assert len(set(answers)) == len(answers) == 4, (plan, answers)
        returned.extend(answers)
    assert len(set(returned)) == len(returned)
    queued = [str((t.metadata or {}).get("channel_message_id") or "") for t in _queued()]
    stored = [line["id"] for line in app_room.transcript(SCOPE_A)]
    assert len(set(queued)) == len(queued)
    assert len(set(stored)) == len(stored)
    assert app_room._claimed == {}


# ── item 3: the web room lane, off the loop and bounded per account ──────────────────────

class _RecordingSocket:
    def __init__(self):
        self.sent: List[dict] = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _record_threads(monkeypatch, owner, name: str) -> List[str]:
    names: List[str] = []
    real = getattr(owner, name)

    def recorded(*args, **kwargs):
        names.append(threading.current_thread().name)
        return real(*args, **kwargs)
    monkeypatch.setattr(owner, name, recorded)
    return names


def test_the_transcript_is_built_on_the_room_lane(tmp_path, monkeypatch):
    """The transcript and the read mark run on `vaf-room-lane` threads: not on the loop, and
    not on the default executor the uploads and the voice feed share."""
    from vaf.core.web_server import _send_room_transcript
    room = Room.create(kind="round", owner_scope=SCOPE_A, base=tmp_path, room_id="room-t")
    me = room.join(display="Alice", scope_id=SCOPE_A,
                   peer_id=derive_peer_id(participant_key("cli", SCOPE_A), "room-t"))
    room.say(me, "hallo")
    built = _record_threads(monkeypatch, Room, "transcript")
    marked = _record_threads(monkeypatch, Room, "mark_read")
    socket = _RecordingSocket()
    asyncio.run(_send_room_transcript(socket, Room.open("room-t", base=tmp_path), SCOPE_A))
    assert socket.sent and socket.sent[0]["type"] == "room_transcript"
    assert built and all(n.startswith("vaf-room-lane") for n in built), built
    assert marked and all(n.startswith("vaf-room-lane") for n in marked), marked


def test_a_viewer_no_longer_in_the_room_is_sent_nothing(tmp_path, monkeypatch):
    """Membership is decided again where the transcript is read: a member removed while the
    build waited for a thread is sent nothing, and an account that is only invited gets the
    door, never a line. MUTATION: drop the check in `_build` - red."""
    from vaf.core.session import _room_rows
    from vaf.core.web_server import _send_room_transcript
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope=SCOPE_A, base=tmp_path, room_id="room-gone",
                       multi_scope=True)
    host = room.join(display="Alice", scope_id=SCOPE_A,
                     peer_id=derive_peer_id(participant_key("cli", SCOPE_A), "room-gone"))
    room.say(host, "the secret plan")
    room.invite_account(host, SCOPE_B, display="bob")
    gone = room.accept_invitation(SCOPE_B, display="Bob")
    socket = _RecordingSocket()
    asyncio.run(_send_room_transcript(socket, Room.open("room-gone", base=tmp_path), SCOPE_B))
    assert socket.sent and socket.sent[0]["messages"], "a member is sent the room"
    room.kick(host, gone.peer_id)
    socket = _RecordingSocket()
    asyncio.run(_send_room_transcript(socket, Room.open("room-gone", base=tmp_path), SCOPE_B))
    assert socket.sent == [], "a removed member was sent the room"

    third = "0a0b0c0d-0000-4000-8000-0000000000c3"
    room.invite_account(host, third, display="carol")
    assert any(r.get("invited") for r in _room_rows(third))
    socket = _RecordingSocket()
    asyncio.run(_send_room_transcript(socket, Room.open("room-gone", base=tmp_path), third))
    [door] = socket.sent
    assert door["messages"] == [] and door["room"]["invited"]
    assert "secret plan" not in str(door)


SECRET = "f308-test-secret-of-at-least-thirty-two-bytes"


@pytest.fixture
def ws_world(tmp_path, monkeypatch):
    """The real /ws handler from localhost, two accounts signed in with tokens, each a member
    of a room of its own (`room-a`, `room-b`)."""
    import jwt
    import vaf.auth.crypto as crypto
    import vaf.core.web_server as web_server
    from starlette.testclient import TestClient
    from vaf.core.config import Config

    rooms = tmp_path / "rooms"
    rooms.mkdir()
    monkeypatch.setattr(store_mod, "rooms_root", lambda base=None: Path(base) if base else rooms)
    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: SECRET)

    async def stands(*_a, **_k):
        return True
    monkeypatch.setattr(web_server, "token_account_stands", stands)
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: False if key == "local_network_enabled"
        else real_get(key, default)))
    monkeypatch.setenv("VAF_WEBUI_ACTIVE", "0")
    web_server._ROOM_WRITES.clear()
    made = {}
    for scope, room_id, name in ((SCOPE_A, "room-a", "Alice"), (SCOPE_B, "room-b", "Bob")):
        room = Room.create(kind="round", owner_scope=scope, room_id=room_id)
        room.join(display=name, scope_id=scope,
                  peer_id=derive_peer_id(participant_key("cli", scope), room_id))
        made[scope] = room
    client = TestClient(web_server.app, client=("127.0.0.1", 40000))

    def connect(scope):
        token = jwt.encode({"sub": scope, "user_scope_id": scope, "username": "u",
                            "role": "user", "type": "access"}, SECRET, algorithm="HS256")
        return client.websocket_connect(f"/ws?token={token}")

    yield SimpleNamespace(connect=connect, rooms=made, server=web_server)
    web_server._ROOM_WRITES.clear()


def _answer(socket, payload, kinds=("room_transcript", "error")) -> dict:
    """Send one command and read up to its answer (a room view or an error)."""
    socket.send_json(payload)
    while True:
        frame = socket.receive_json()
        if frame.get("type") in kinds:
            return frame


def _frames(room_id: str) -> int:
    return len(Room.open(room_id).store.frames())


def test_a_say_and_an_open_run_on_the_room_lane(ws_world, monkeypatch):
    """Through the real socket handler: a line's store work and an open's membership read run
    on `vaf-room-lane` threads."""
    import vaf.core.session as session_module
    ingested = _record_threads(monkeypatch, Room, "ingest")
    rows = _record_threads(monkeypatch, session_module, "_room_rows")
    with ws_world.connect(SCOPE_A) as socket:
        assert _answer(socket, {"type": "room_say", "room_id": "room-a",
                                "text": "hallo"})["type"] == "room_transcript"
        assert ingested and all(n.startswith("vaf-room-lane") for n in ingested), ingested
        rows.clear()
        assert _answer(socket, {"type": "open_room", "room_id": "room-a"})["type"] \
            == "room_transcript"
    assert rows and all(n.startswith("vaf-room-lane") for n in rows), rows


def test_the_sixty_first_write_in_a_minute_is_refused(ws_world):
    """60 lines, reactions and ballots of one account in a minute go through; the 61st is
    answered with the sentence and writes nothing, a reaction counts in the same window, and
    another account in the same second is not touched. The first 57 are charged directly, so
    the limit tested is the real one at the cost of three lines."""
    from vaf.core.web_server import ROOM_WRITES_PER_MINUTE, ROOM_WRITES_REFUSED
    assert ROOM_WRITES_PER_MINUTE == 60
    window = ws_world.server._ROOM_WRITES
    asker = Room.open("room-a").identity_for(participant_key("cli", SCOPE_A))
    vote = Room.open("room-a").open_vote(asker, "Deploy today?", options=["yes", "no"])
    for _ in range(57):
        assert window.take(SCOPE_A, limit=ROOM_WRITES_PER_MINUTE)
    with ws_world.connect(SCOPE_A) as socket:
        for number in range(3):
            sent_id = str(uuid.uuid4())
            frame = _answer(socket, {"type": "room_say", "room_id": "room-a",
                                     "text": f"zeile {number}", "client_id": sent_id})
            assert frame["type"] == "room_transcript", frame
            assert frame["ack"] == sent_id, "the answer does not name the write it answers"
        before = _frames("room-a")

        def refusal(write_id):
            return {"type": "error", "code": "room_rate", "message": ROOM_WRITES_REFUSED,
                    "client_id": write_id}

        said, reacted_id, voted_id = (str(uuid.uuid4()) for _ in range(3))
        refused = _answer(socket, {"type": "room_say", "room_id": "room-a", "text": "zu viel",
                                   "client_id": said})
        assert refused == refusal(said), "the refusal does not name the refused write"
        target = Room.open("room-a").transcript()[-1]["id"]
        reacted = _answer(socket, {"type": "room_react", "room_id": "room-a",
                                   "reply_to": target, "emoji": "+1", "client_id": reacted_id})
        assert reacted == refusal(reacted_id)
        voted = _answer(socket, {"type": "cast_room_vote", "room_id": "room-a",
                                 "vote_id": vote.id, "choice": "yes", "client_id": voted_id})
        assert voted == refusal(voted_id), "a ballot is a write of the same window"
        # An id that is no UUID is not echoed: the frame names nothing then.
        odd = _answer(socket, {"type": "room_say", "room_id": "room-a", "text": "x",
                               "client_id": "<b>not an id</b>"})
        assert odd == refusal("")
        assert _frames("room-a") == before, "a refused write was stored"
    with ws_world.connect(SCOPE_B) as other:
        assert _answer(other, {"type": "room_say", "room_id": "room-b",
                               "text": "hallo"})["type"] == "room_transcript"


def test_every_room_store_path_of_the_socket_runs_on_the_room_lane(ws_world, monkeypatch):
    """Typing, the task record and the invitation commands read or write a room's store; each
    runs on `vaf-room-lane` threads, never on the loop. MUTATION: call the store work of one
    of them inline again - red."""
    import vaf.core.session as session_module
    host_scope = SCOPE_A
    shared = Room.create(kind="round", owner_scope=host_scope, room_id="room-shared",
                         multi_scope=True)
    host = shared.join(display="Alice", scope_id=host_scope,
                       peer_id=derive_peer_id(participant_key("cli", host_scope), "room-shared"))
    revoked = shared.invite_account(host, SCOPE_B, display="bob")
    member_reads = _record_threads(monkeypatch, session_module, "member_room_ids")
    tasks = _record_threads(monkeypatch, Room, "tasks")
    revokes = _record_threads(monkeypatch, Room, "revoke_invitation")
    listings = _record_threads(monkeypatch, Room, "invitations")
    accepts = _record_threads(monkeypatch, Room, "accept_invitation")
    declines = _record_threads(monkeypatch, Room, "decline_invitation")
    standings = _record_threads(monkeypatch, Room, "invitation_for")
    import vaf.core.tool_dispatch as tool_dispatch
    monkeypatch.setattr(tool_dispatch, "resolve_account_directory", lambda: [
        {"username": "bob", "user_scope_id": SCOPE_B, "active": True}])
    with ws_world.connect(host_scope) as socket:
        socket.send_json({"type": "room_typing", "room_id": "room-a"})
        # One socket's commands run in order: the next answer comes after the typing.
        assert _answer(socket, {"type": "open_room", "room_id": "room-a"})["type"] \
            == "room_transcript"
        assert member_reads, "the typing never asked whether the person is in the room"
        assert ws_world.server._ROOM_KEYS_TYPING.get("room-a"), "the keypress was not kept"
        assert _answer(socket, {"type": "room_task_history", "room_id": "room-a"},
                       kinds=("room_task_history", "error"))["type"] == "room_task_history"
        assert _answer(socket, {"type": "room_invitation_text", "room_id": "room-shared",
                                "invitation_id": "none"})["type"] == "error"
        assert _answer(socket, {"type": "revoke_room_invite", "room_id": "room-shared",
                                "invitation_id": revoked["id"]})["type"] == "room_transcript"
        # Recorded around this command alone: the sidebar listing reads invitations on the
        # loop too (a named boundary of its own).
        standings.clear()
        listed = _answer(socket, {"type": "room_invite_candidates", "room_id": "room-shared"},
                         kinds=("room_invite_candidates", "error"))
        assert [a["username"] for a in listed["accounts"]] == ["bob"]
        assert standings and all(n.startswith("vaf-room-lane") for n in standings), standings
    shared.invite_account(host, SCOPE_B, display="bob")
    with ws_world.connect(SCOPE_B) as guest:
        # A decline answers with the sidebar only; the next command's answer comes after it.
        guest.send_json({"type": "decline_room_invite", "room_id": "room-shared"})
        assert _answer(guest, {"type": "open_room", "room_id": "room-shared"})["type"] \
            == "error"
    shared.invite_account(host, SCOPE_B, display="bob")
    with ws_world.connect(SCOPE_B) as guest:
        assert _answer(guest, {"type": "accept_room_invite",
                               "room_id": "room-shared"})["type"] == "room_transcript"
    for name, seen in (("typing", member_reads), ("history", tasks), ("revoke", revokes),
                       ("invitations", listings), ("accept", accepts), ("decline", declines)):
        assert seen and all(n.startswith("vaf-room-lane") for n in seen), (name, seen)


@pytest.mark.parametrize("value, want", [
    ("8B0B1D7E-1C2A-4D3B-9E4F-5A6B7C8D9E0F", "8b0b1d7e-1c2a-4d3b-9e4f-5a6b7c8d9e0f"),
    (" 8b0b1d7e-1c2a-4d3b-9e4f-5a6b7c8d9e0f ", "8b0b1d7e-1c2a-4d3b-9e4f-5a6b7c8d9e0f"),
    # A JSON number whose text is 32 digits would read as a UUID if it were turned into text.
    (12345678901234567890123456789012, ""),
    (["8b0b1d7e-1c2a-4d3b-9e4f-5a6b7c8d9e0f"], ""),
    ({"id": "8b0b1d7e-1c2a-4d3b-9e4f-5a6b7c8d9e0f"}, ""),
    (True, ""), (None, ""),
    (" " * 40 + "8b0b1d7e-1c2a-4d3b-9e4f-5a6b7c8d9e0f", ""),   # longer than 64 characters
    ("not an id", ""),
])
def test_a_write_id_is_read_only_from_a_short_string(value, want):
    """The id a page names a write with is read only from a string of at most 64 characters;
    any other JSON value names nothing and is never turned into text. MUTATION: drop the type
    or the length check - red."""
    assert app_room._client_line_id(value) == want


def test_the_sidebar_and_the_workspace_resolver_run_on_the_room_lane(ws_world, monkeypatch):
    """The chat list opens every room on this machine (`_room_rows`) and the page asks for it
    after every room write: the list on connect, `get_sessions` and the broadcast after a room
    command are built on `vaf-room-lane` threads, and so is the workspace routes' resolver.
    MUTATION: build one of them inline again - red."""
    import vaf.core.web_server as web_server
    lists = _record_threads(monkeypatch, web_server.session_mgr, "list_ui")
    with ws_world.connect(SCOPE_A) as socket:
        _answer(socket, {"type": "get_sessions"}, kinds=("session_list",))
        assert _answer(socket, {"type": "rename_room", "room_id": "room-a",
                                "title": "Renamed"})["type"] == "room_transcript"
        # The rename's broadcast goes out before the next command is answered.
        _answer(socket, {"type": "open_room", "room_id": "room-a"})
    assert len(lists) >= 3, lists
    assert all(n.startswith("vaf-room-lane") for n in lists), lists

    resolved = []
    monkeypatch.setattr(web_server, "_resolve_session_workspace",
                        lambda *a, **k: resolved.append(threading.current_thread().name) or "")
    assert asyncio.run(web_server._workspace_root("room-a", None, create=True)) == ""
    assert resolved and resolved[0].startswith("vaf-room-lane"), resolved
    source = Path(web_server.__file__).read_text(encoding="utf-8")
    assert source.count("root = await _workspace_root(") == 4
    assert "root = _resolve_session_workspace(" not in source


def test_the_page_says_a_refused_room_write_and_keeps_its_text():
    """The page names every room write it sends (`client_id`) and settles each by that name:
    the answer that carries it as `ack` ends its pending copy, never a line of the same text;
    a `room_rate` refusal naming it settles that write alone (`afterRefusal`) and gives a
    refused LINE back to an empty box (`boxAfterRefusal`, run in node in
    test_app_link_room_web.py). The agent room shows the catalogue's notice in the amber status
    style; another room on screen drops it. MUTATION: reconcile by text again, drop the
    branch, restore without the helper, send a write without its id, or keep the notice
    across rooms - red."""
    page = (Path(__file__).resolve().parents[1] / "web" / "app" / "page.tsx").read_text(
        encoding="utf-8")
    branch = page.split("else if (data.type === 'error') {", 1)[1].split(
        "// The server refused the chat a reconnect asked for again", 1)[0]
    assert "if (data.code === 'room_rate') {" in branch
    assert "const settled = afterRefusal(liveWrites(roomWritesInFlightRef.current, Date.now()), cid);" \
        in branch
    assert "setPendingRoomSays(prev => prev.filter(p => p.cid !== refused.cid));" in branch
    assert "setInput(prev => boxAfterRefusal(prev, refused, openRoom));" in branch
    assert "const openRoom = roomViewRef.current?.room.roomId ?? null;" in branch
    assert "const rid = refusalNotice(refused, openRoom);" in branch
    assert "setRoomRateNotice(rid);" in branch
    assert "p.roomId !== rid" not in branch, "a refusal drops every line of the room again"
    notice = page.split("{roomView && !appRoomWords && roomRateNotice === roomView.room.roomId && (",
                        1)[1][:700]
    assert 'role="status"' in notice and "{tMain('appRoomRateLimited')}" in notice
    assert "border-amber-200 bg-amber-50 text-amber-800" in notice
    transcript = page.split("else if (data.type === 'room_transcript') {", 1)[1][:4000]
    assert "setPendingRoomSays(prev => liveWrites(afterAnswer(prev, ack), Date.now())" in transcript
    assert "afterAnswer(roomWritesInFlightRef.current, ack), Date.now());" in transcript
    assert "texts.has(" not in transcript, "a pending line is settled by its text again"
    writer = page.split("const sendRoomWrite = (", 1)[1].split("\n    };", 1)[0]
    assert "const cid = newLineId();" in writer
    assert "roomWritesInFlightRef.current = [...liveWrites(roomWritesInFlightRef.current, now)," \
        in writer
    assert ".slice(" not in writer, "records of open writes are dropped by count again"
    # Pending copies expire on a timer of their own, also when no transcript arrives.
    timer = page.split("const wait = nextExpiryIn(pendingRoomSays, Date.now());", 1)
    assert len(timer) == 2, "pending lines wait for a transcript to expire"
    assert "setTimeout(() => setPendingRoomSays(prev => liveWrites(prev, Date.now())), wait + 50);" \
        in timer[1][:400]
    assert "return () => clearTimeout(timer);" in timer[1][:500]
    assert "}, [pendingRoomSays]);" in timer[1][:600]
    assert "sock.send(JSON.stringify({ ...payload, room_id: roomId, client_id: cid }));" in writer
    assert "setRoomRateNotice(null);" in writer
    for kind, command in (("say", "room_say"), ("react", "room_react"), ("vote", "cast_room_vote")):
        assert f"sendRoomWrite('{kind}', " in page and f"{{ type: '{command}', " in page, kind
        assert f"type: '{command}', room_id" not in page, f"{command} is sent without its id"
    assert ("setRoomRateNotice(prev => (prev && prev !== roomView?.room.roomId ? null : prev));\n"
            "    }, [roomView?.room.roomId]);") in page, "another room shows this room's notice"


# ── item 4: labels keep their numbers when a member joins or renames into a collision ────

def _legacy_labels(members: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """`Room.labels` as it was before members carried `named_at` (numbered by handle)."""
    names = {peer: display_name(rec.get("display"), peer) for peer, rec in members.items()}
    groups: Dict[str, List[str]] = {}
    for peer in sorted(names):
        groups.setdefault(name_key(names[peer]), []).append(peer)
    taken = set()
    for name in names.values():
        taken.add(lookup_key(name))
        taken.add(name_key(name))
    out = {peer: name for peer, name in names.items() if len(groups[name_key(name)]) < 2}
    for key in sorted(groups):
        if len(groups[key]) < 2:
            continue
        number = 0
        for peer in groups[key]:
            while True:
                number += 1
                label = f"{names[peer]}{number}"
                if lookup_key(label) not in taken and name_key(label) not in taken:
                    break
            taken.add(lookup_key(label))
            taken.add(name_key(label))
            out[peer] = label
    return out


def _table_room(table: Dict[str, Dict[str, Any]]):
    """`Room.labels` and `Room.name_lookup` over a member table in memory (every member a
    peer)."""
    view = SimpleNamespace()
    view.store = SimpleNamespace(members=lambda: table)
    view.labels = lambda: Room.labels(view)
    view.members = lambda: {p: {"display": r.get("display") or p} for p, r in table.items()}
    view.roles = lambda: {p: "peer" for p in table}
    return view


def test_a_room_written_before_keeps_its_labels(tmp_path):
    room = Room.create(kind="round", owner_scope="s", base=tmp_path, room_id="room-legacy")
    for peer, name in (("p-d", "Bob"), ("p-b", "Bob"), ("p-a", "bob."), ("p-c", "Alice")):
        room.store.put_member(peer, {"display": name, "lease": time.time()})
    members = room.store.members()
    assert all("named_at" not in record for record in members.values())
    assert room.labels() == _legacy_labels(members)
    assert room.labels()["p-a"] == "bob.1"


def test_joining_or_renaming_into_a_collision_takes_the_next_number(tmp_path):
    room = Room.create(kind="round", owner_scope="s", base=tmp_path, room_id="room-numbers")
    room.join(display="Bob", scope_id="s", peer_id="p-b")
    room.join(display="Bob", scope_id="s", peer_id="p-d")
    alice = room.join(display="Alice", scope_id="s", peer_id="p-c")
    assert room.labels() == {"p-b": "Bob1", "p-d": "Bob2", "p-c": "Alice"}
    # A third "Bob" whose handle sorts first: the newcomer is Bob3, the others keep theirs.
    room.join(display="Bob", scope_id="s", peer_id="p-a")
    assert room.labels() == {"p-b": "Bob1", "p-d": "Bob2", "p-a": "Bob3", "p-c": "Alice"}
    # Alice renames to "Bob": she is Bob4, nobody else moves.
    room.introduce(alice, display="Bob")
    assert room.labels() == {"p-b": "Bob1", "p-d": "Bob2", "p-a": "Bob3", "p-c": "Bob4"}
    for peer, label in room.labels().items():
        assert room.peers_named(label) == [peer]


def test_a_rename_a_mention_cannot_tell_apart_keeps_the_number(tmp_path):
    """"Bob" renaming to "bob" (or "Bob.") stays in the same collision: it keeps its place
    there, and nobody else's number moves."""
    room = Room.create(kind="round", owner_scope="s", base=tmp_path, room_id="room-case")
    first = room.join(display="Bob", scope_id="s", peer_id="p-b")
    room.join(display="Bob", scope_id="s", peer_id="p-a")
    assert room.labels() == {"p-b": "Bob1", "p-a": "Bob2"}
    room.introduce(first, display="bob")
    assert room.labels() == {"p-b": "bob1", "p-a": "Bob2"}
    assert room.peers_named("Bob2") == ["p-a"]
    room.introduce(first, display="Bob.")
    assert room.labels()["p-a"] == "Bob2"


def test_a_rejoin_under_the_same_name_keeps_when_it_was_taken(tmp_path):
    room = Room.create(kind="round", owner_scope="s", base=tmp_path, room_id="room-rejoin")
    room.join(display="Bob", scope_id="s", peer_id="p-b")
    first = room.store.member("p-b")["named_at"]
    room.join(display="Bob", scope_id="s", peer_id="p-d")
    room.join(display="Bob", scope_id="s", peer_id="p-b")
    assert room.store.member("p-b")["named_at"] == first
    assert room.labels() == {"p-b": "Bob1", "p-d": "Bob2"}
    # A re-join under another name is a new name.
    room.join(display="Rob", scope_id="s", peer_id="p-b")
    assert room.store.member("p-b")["named_at"] > first
    # A member written before the field, joining again under its name, stays without it.
    room.store.put_member("p-x", {"display": "Zed", "lease": time.time()})
    room.join(display="Zed", scope_id="s", peer_id="p-x")
    assert "named_at" not in room.store.member("p-x")


_LABEL_POOL = ["Bob", "bob", "Bob.", "Bob1", "@Bob", "Bob2", "Straße", "STRASSE", "Mia",
               "Mia's", "Ab", "Ab1", "Codex", "codex?"]


def test_labels_fuzz():
    """300 rooms of 2-12 members (seed 308); `VAF_ROOM_FUZZ_ROOMS` runs more. Oracle: (a) with no
    `named_at` the labels are the legacy ones; (b) every label names exactly one member;
    (c) a newcomer with a later `named_at`, whose name is no current label (also not folded),
    leaves every numbered label as it was."""
    rng = random.Random(308)
    for _ in range(int(os.environ.get("VAF_ROOM_FUZZ_ROOMS", "300"))):
        size = rng.randint(2, 12)
        table: Dict[str, Dict[str, Any]] = {}
        for index in range(size):
            record: Dict[str, Any] = {"display": rng.choice(_LABEL_POOL)}
            table[f"p-{rng.randrange(10 ** 6):06d}-{index}"] = record
        bare = {peer: {"display": rec["display"]} for peer, rec in table.items()}
        assert Room.labels(_table_room(bare)) == _legacy_labels(bare)
        for record in table.values():
            if rng.random() < 0.7:
                record["named_at"] = rng.uniform(1, 1000)
        view = _table_room(table)
        labels = view.labels()
        lookup = Room.name_lookup(view)
        for peer, label in labels.items():
            assert lookup(label) == [peer], (label, labels)
        newcomer = rng.choice(_LABEL_POOL)
        if any(lookup_key(newcomer) == lookup_key(label) or name_key(newcomer) == name_key(label)
               for label in labels.values()):
            continue
        grown = dict(table)
        # A handle that sorts before every other: numbered by handle, it would take the
        # first number and move the others.
        grown["a-new"] = {"display": newcomer, "named_at": 5000.0}
        after = Room.labels(_table_room(grown))
        for peer, label in labels.items():
            if label != display_name(table[peer]["display"], peer):
                assert after[peer] == label, (newcomer, labels, after)


# ── item 6: a refused Veyllo App turn answers the device ─────────────────────────────────

# The admin's reason, with a provider's error text in it: it stays on VAF.
REFUSAL = "The API provider x is not set up (RuntimeError: key 1234 rejected). Check its key."


def test_a_refused_line_to_vafs_agent_answers_the_device(room_home):
    """The device gets the short fixed sentence; the detailed reason (a profile, a command, a
    provider's error text) stays in the room. MUTATION: send `why` to the device - red."""
    import vaf.core.headless_runner as hr
    from vaf.core.session import get_manager
    client, _pipe, _row = _capable()
    away, _key, _der = paired_device(SCOPE_A, model="Old phone")
    app_room.post(SCOPE_A, "alice", "@VAF hi")
    [task] = _queued()
    hr.refuse_turn_without_backend(task, REFUSAL, get_manager())
    client.pump()
    [done] = client.of("done")
    assert done["final_text"] == hr.APP_NO_MODEL_REPLY
    assert "RuntimeError" not in done["final_text"] and "1234" not in done["final_text"]
    assert app_room.transcript(SCOPE_A)[-1]["text"] == REFUSAL, "the room lost the reason"
    assert done["id"] in [e["id"] for e in devices.get(away["device_id"])["outbox"]], \
        "the absent device does not get the reason after its next welcome"
    assert devices.held_in_outbox(SCOPE_A, done["id"])


def test_a_refused_request_answers_the_asker_and_frees_its_place(app_link_home, room_home):
    import vaf.core.headless_runner as hr
    from vaf.core.session import get_manager
    world = _two_devices()
    world.client.send(_ask(world.row))
    task = _task_of(A1)
    hr.refuse_turn_without_backend(task, REFUSAL, get_manager())
    world.client.pump()
    world.other_client.pump()
    assert world.client.of("done") == [{"t": "done", "id": A1,
                                         "final_text": hr.APP_NO_MODEL_REPLY}]
    assert not world.other_client.of("done"), "another device got an answer it never asked for"
    assert A1 not in bridge._asks.get(world.row["device_id"], {})


def test_a_refused_messenger_turn_sends_the_device_nothing(room_home):
    import vaf.core.headless_runner as hr
    from vaf.core.app_link import CHANNEL
    from vaf.core.channels import CHAT_CHANNELS
    from vaf.core.session import get_manager
    from vaf.core.task_queue import AgentTask
    client, _pipe, _row = _capable()
    other = next(c for c in CHAT_CHANNELS if c != CHANNEL)
    task = AgentTask(session_id="telegram_123", input_text="Hallo?", source=other,
                     metadata={"user_scope_id": SCOPE_A, "username": "alice"})
    hr.refuse_turn_without_backend(task, REFUSAL, get_manager())
    client.pump()
    assert not client.of("done") and not client.of("msg")


# ── item 7: a zero-width space ends the mention ──────────────────────────────────────────

def _old_candidates(text: Any) -> List[str]:
    """`mention_candidates` as it was before U+200B ended a mention."""
    from vaf.core.a2a.room import _shorter_end
    stripped = str(text or "").lstrip()
    if not stripped.startswith("@"):
        return []
    parts = stripped[1:].split(None, 1)
    word = parts[0] if parts else ""
    out: List[str] = []
    end = len(word)
    while end > 0:
        if end <= MENTION_MAX_CHARS:
            out.append(word[:end])
        shorter = _shorter_end(word, end)
        if shorter == end:
            break
        end = shorter
    return out


def _old_resolve(text: Any, known: Callable[[str], Iterable[Any]]) -> Any:
    found: List[Any] = []
    for candidate in _old_candidates(text):
        for hit in known(candidate) or ():
            if hit and hit not in found:
                found.append(hit)
    return found[0] if len(found) == 1 else None


def _members_room(tmp_path, names, room_id):
    room = Room.create(kind="round", owner_scope="s", base=tmp_path, room_id=room_id)
    for index, name in enumerate(names):
        room.join(display=name, scope_id="s", peer_id=f"p-{index}")
    return room


@pytest.mark.parametrize("line", [f"@Mia{ZWSP} hi", f"@Mia{ZWSP}hi", f"@{ZWSP}Mia hi",
                                  f"{ZWSP}@Mia hi", f"{ZWSP} @ {ZWSP}Mia. hi"])
def test_a_zero_width_space_ends_the_mention(tmp_path, room_home, monkeypatch, line):
    from vaf.core import app_link
    room = _members_room(tmp_path, ["Mia", "Bob"], "room-zw")
    assert room.address_from_mention(line) == {"peer": "p-0"}
    monkeypatch.setattr(app_link, "_agent_name_resolver", lambda username: "Mia")
    assert app_room.addressee(line, "alice") == app_room.VAF_AGENT


def test_a_joiner_continues_the_word(tmp_path, room_home, monkeypatch):
    from vaf.core import app_link
    room = _members_room(tmp_path, ["Mia", "Bob"], "room-wj")
    assert room.address_from_mention(f"@Mia{WJ} hi") is None
    monkeypatch.setattr(app_link, "_agent_name_resolver", lambda username: "Mia")
    assert app_room.addressee(f"@Mia{WJ} hi", "alice") == app_room.APP_AGENT


def test_a_name_holding_a_zero_width_space_beside_its_cut_goes_to_the_room(tmp_path):
    room = _members_room(tmp_path, ["Mia", f"Mia{ZWSP}X"], "room-zw2")
    assert room.address_from_mention(f"@Mia{ZWSP}X hi") is None
    assert room.address_from_mention("@Mia hi") == {"peer": "p-0"}


@pytest.mark.parametrize("line", ["@" + ZWSP * 65535 + " hi", "@" + "." * 65534 + ZWSP + " hi"])
def test_a_long_run_of_zero_width_spaces_costs_one_pass(tmp_path, line):
    import tracemalloc
    room = _members_room(tmp_path, ["Bob"], "room-zw3")
    candidates = mention_candidates(line)
    assert len(candidates) <= 2 * MENTION_MAX_CHARS
    assert all(len(c) <= MENTION_MAX_CHARS for c in candidates)
    tracemalloc.start()
    started = time.perf_counter()
    assert room.address_from_mention(line) is None
    elapsed = time.perf_counter() - started
    _now, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert elapsed < 5.0 and peak < 20_000_000, (elapsed, peak)


_ALPHABET = (list("abMiBo19") + [".", ",", "?", "!", "'", "@", " ", "\xa0", "\u3000", ZWSP,
                                  WJ, "\ufeff", "\u200d", "\u0301", "\u4e2d"])


def _draw(rng: random.Random, low: int, high: int) -> str:
    return "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(low, high)))


def _table_lookup(names: List[str]) -> Callable[[str], List[str]]:
    index: Dict[str, List[str]] = {}
    for number, name in enumerate(names):
        index.setdefault(lookup_key(name), []).append(f"p{number}")

    def lookup(candidate: str) -> List[str]:
        key = lookup_key(candidate)
        return list(index.get(key, [])) if key else []
    return lookup


def _lead(text: str) -> str:
    """The line with the U+200B before the "@", and between the "@" and the word, removed."""
    at = 0
    while at < len(text) and (text[at].isspace() or text[at] == ZWSP):
        at += 1
    if not text.startswith("@", at):
        return text
    after = at + 1
    while after < len(text) and (text[after].isspace() or text[after] == ZWSP):
        after += 1
    head = "".join(c for c in text[:at] if c != ZWSP)
    gap = "".join(c for c in text[at + 1:after] if c != ZWSP)
    return head + "@" + gap + text[after:]


def test_mentions_fuzz():
    """3,000 lines (seed 308); `VAF_ROOM_FUZZ_LINES` runs more. Oracle: (a) a line without U+200B
    resolves as before; (b) with one, the result is the old result of the line with its
    leading U+200B removed, of the line with every U+200B a space, or nobody, and nobody when
    those two name different members."""
    rng = random.Random(308)
    checked_zw = 0
    for _ in range(int(os.environ.get("VAF_ROOM_FUZZ_LINES", "3000"))):
        names = [_draw(rng, 1, 6) for _ in range(rng.randint(1, 6))]
        if rng.random() < 0.5:
            names.append(rng.choice(["Mia", "Bob", "Mi"]))
        text = rng.choice(["@", "@", " @", ZWSP + "@", ""]) + _draw(rng, 0, 12)
        # Members named by the line itself, so the two readings it allows name somebody: the
        # word as it stands and the word cut at its U+200B.
        for reading in (_lead(text), text.replace(ZWSP, " ")):
            forms = _old_candidates(reading)
            if forms and rng.random() < 0.4:
                names.append(rng.choice(forms))
        lookup = _table_lookup(names)
        got = resolve_mention(text, lookup)
        if ZWSP not in text:
            assert got == _old_resolve(text, lookup), (text, names)
            continue
        checked_zw += 1
        lead = _old_resolve(_lead(text), lookup)
        spaced = _old_resolve(text.replace(ZWSP, " "), lookup)
        assert got in (lead, spaced, None), (text, names, got, lead, spaced)
        if lead is not None and spaced is not None and lead != spaced:
            assert got is None, (text, names)
    assert checked_zw > 0


def test_unicode_tables_agree_on_the_gap():
    """The gap the parse skips is Python's whitespace plus U+200B: NBSP and U+3000 already
    were whitespace, the joiners are not."""
    assert "\xa0".isspace() and "\u3000".isspace()
    assert not ZWSP.isspace() and not WJ.isspace() and not "\ufeff".isspace()
    assert unicodedata.category(ZWSP) == "Cf"


# ── the docs say what the code does ──────────────────────────────────────────────────────

def _doc(path: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return " ".join((root / path).read_text(encoding="utf-8").split())


@pytest.mark.parametrize("path, sentences, gone", [
    ("docs/integrations/VEYLLO_APP.md", [
        "show any other value as from an unknown party and answer nothing.",
        "the account's app chat answers its turns one after another in the order they "
        "arrived, so place it by time.",
        "the Veyllo App gets a short fixed sentence as the agent's reply",
        "The detailed reason (a profile, a command, a provider's error text) stays in the room "
        "and the log.",
        "A zero-width space (U+200B), which some keyboards put after a name they completed, "
        "counts as a space"], []),
    ("docs/EMBEDDING.md", [
        "`join` and `introduce` cut a display name to 80 characters",
        "given in the order the members took the name",
        "An async server runs them on threads of its own, not on its event loop, and bounds "
        "what one person may write with `vaf.RateWindow`",
        "## Bounding what one person may send: `vaf.RateWindow`",
        "- `vaf.RateWindow` - the per-key sliding window",
        "one `now` far ahead lets every key's earlier charges expire, and a clock that stepped "
        "back holds every key's charges until it catches up"], []),
    ("docs/agents/A2A_PROTOCOL.md", [
        "The members of a collision are numbered in the order they took the name, then by "
        "handle",
        "A leave renumbers nothing: the member's record stays in the store.",
        "A zero-width space (U+200B, which some keyboards put after a name they completed) "
        "counts as a space",
        "One account writes at most 60 lines, reactions and ballots a minute"],
     ["update_card", "not across a join, a leave or a rename"]),
    ("docs/web-ui/WEBUI_WEBSOCKET_FLOW.md", [
        "run on a pool of their own (`vaf-room-lane`, four threads)",
        "`room_task_history`, the invitation commands and the answer at the door",
        "so it runs on the room lane's threads and only the map write stays on the loop.",
        "the room shows `appRoomRateLimited` above the box",
        "\"Too many room messages in a minute. Wait a moment and write again.\"",
        "the page settles a write only by that id, never by matching text",
        "it opens every room on this machine to find the account's rows"],
     ["never touches the room store", "the page ignores `error` frames",
      "every room of the account"]),
    ("docs/web-ui/WEB_UI.md", ["**A follow yields to the person.**",
                               "`appRoomRateLimited` until the next write",
                               "it is never settled by text",
                               "also when no transcript arrives any more (a dropped socket)",
                               "a refusal that arrives after a switch puts nothing into the "
                               "other room's box"], ["compared TRIMMED"]),
])
def test_the_docs_say_what_the_rooms_do(path, sentences, gone):
    """MUTATION: delete one of these sentences from its doc - red."""
    text = _doc(path)
    for sentence in sentences:
        assert sentence in text, (path, sentence)
    for stale in gone:
        assert stale not in text, (path, stale)
