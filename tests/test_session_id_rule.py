# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One rule for what may name a session, and a working-memory store keyed by the exact id.

The defect this file pins: the working-memory folder of a chat was derived by a sanitiser
that trimmed punctuation and rewrote characters, so "red654321." and "red654321" shared one
folder. The ownership gate reads the session by its EXACT id, so a second account could
start the variant, pass the gate as its owner, and read and write the first account's goal,
plan, tasks and notes. Closed from both sides, because each side alone leaves a hole:

- the store name is injective (`MainPersistenceManager._safe_session_dir`): a plain id is
  its own folder, every other id gets "_" plus a hash. A variant planted BEFORE the fix
  still passes the gate as its holder's chat, and now reaches only its own store;
- the session store refuses an id that is not one plain entry (`check_session_id`), so
  "../escape" never reads or writes outside it, and a non-admin may start only ids the
  server hands out (`session_access_allowed`), so "telegram_<id>" or another account's
  default chat cannot be claimed before its first message.
"""
import os
import random
import re
from pathlib import Path

import pytest

import vaf.core.config as cfg
from vaf.core.main_persistence import MainPersistenceManager
from vaf.core.session import (
    SESSION_COLOURS,
    InvalidSessionId,
    Session,
    SessionManager,
    _generate_session_id,
    check_session_id,
    default_session_id,
    session_access_allowed,
)

OWNER = "aaaa1111-2222-3333-4444-555555555555"
BOB = "bbbb1111-2222-3333-4444-555555555555"
ADMIN = "cccc1111-2222-3333-4444-555555555555"

_REPO = Path(__file__).resolve().parents[1]
_HEX_NAME = re.compile(r"_[0-9a-f]{40}")

# Every form the server mints (vaf/core/session.py, web_server.py, the channel bridges,
# messaging_connections.whatsapp_session_id, the thinking lane, the task queue).
SERVER_FORMS = [
    "red654321", "green123456", "yellow012345",       # <colour><6 digits>
    "ab12cd34",                                       # legacy uuid prefix
    "web-default-aaaa1111", "web-default-default",
    "telegram_123456789", "telegram_-1001234567890",
    "discord_123456789012345678",
    "whatsapp_alice_4915112345678", "whatsapp_alice_self", "whatsapp_alice_unknown",
    "whatsapp_anna m_4915112345678", "whatsapp_Jörg_4915112345678",
    "whatsapp_" + "\U0001f600" * 50 + "_" + "9" * 20,  # the longest: 230 bytes
    "thinking_default_20261028", "thinking_aaaa1111-2222-3333-4444-5555_0123abcd",
    "system",
]


def _old_safe_session_dir(session_id: str) -> str:
    """The mapping before this change, verbatim: the reference for "kept unchanged"."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id).strip())
    safe = re.sub(r"\.+", ".", safe)
    safe = safe.strip("._-") or "default"
    return safe[:128]


def _store(base, session_id):
    return MainPersistenceManager(str(base), session_id=session_id)


def _random_ids(n=3000, seed=7):
    rng = random.Random(seed)
    alphabet = "ab12_.- äZ9"
    out = set()
    while len(out) < n:
        out.add("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12))))
    return sorted(i for i in out if i.strip())


# ── the working-memory store: one folder per id ────────────────────────────────────────────

@pytest.mark.parametrize("variant", [
    "red654321.", "red654321_", "-red654321", "red654321ä", " red654321",
    ".red654321", "red654321 ",
])
def test_a_variant_of_a_chat_id_gets_its_own_store(tmp_path, variant):
    assert _store(tmp_path, variant).context_dir != _store(tmp_path, "red654321").context_dir


def test_two_accounts_whatsapp_chats_with_one_contact_get_two_stores(tmp_path):
    """Two accounts named "anna m" and "anna_m" writing to the same contact: the server
    mints both ids, and the old mapping put them into one folder."""
    a = _store(tmp_path, "whatsapp_anna m_4915112345678")
    b = _store(tmp_path, "whatsapp_anna_m_4915112345678")
    assert a.context_dir != b.context_dir


def test_no_two_ids_share_a_store_name():
    ids = _random_ids() + SERVER_FORMS
    names = {MainPersistenceManager._safe_session_dir(i) for i in ids}
    assert len(names) == len(set(ids)), "two different ids map onto one working-memory folder"


def test_a_plain_id_keeps_its_folder_and_every_other_id_is_hashed():
    """Every id the old mapping left unchanged is still its own folder name, so existing
    chats and agent rooms find their stores where they were; everything else is "_" plus
    40 hex digits."""
    for sid in _random_ids() + SERVER_FORMS:
        name = MainPersistenceManager._safe_session_dir(sid)
        if _old_safe_session_dir(sid) == sid:
            assert name == sid, f"{sid!r} moved to {name!r}"
        else:
            assert _HEX_NAME.fullmatch(name), f"{sid!r} -> {name!r}"
    kept = [s for s in SERVER_FORMS if _old_safe_session_dir(s) == s]
    assert len(kept) == len(SERVER_FORMS) - 3, "a server-minted plain id is no longer kept"


def test_an_old_store_is_read_in_place(tmp_path):
    folder = tmp_path / ".vaf" / "main" / "sessions" / "telegram_123456789"
    folder.mkdir(parents=True)
    (folder / "working_memory.json").write_bytes(
        b'{"notes": ["OLD-LAYOUT-NOTE"], "plan": [], "tasks": []}')
    mpm = _store(tmp_path, "telegram_123456789")
    assert Path(mpm.context_dir) == folder
    assert "OLD-LAYOUT-NOTE" in str(mpm.get_working_memory()["notes"])


def test_a_hashed_name_is_never_a_plain_id():
    """A hashed name, given back as an id, must not land in its own folder: otherwise the
    chat named "_<hex>" would share the store of the id that hashed to it."""
    for sid in ["red654321.", " red654321", "whatsapp_anna m_4915112345678", "a..b"]:
        name = MainPersistenceManager._safe_session_dir(sid)
        assert MainPersistenceManager._safe_session_dir(name) != name


def test_the_store_keeps_the_id_exactly_as_given(tmp_path):
    assert _store(tmp_path, " red654321").session_id == " red654321"
    assert _store(tmp_path, " red654321").context_dir != _store(tmp_path, "red654321").context_dir
    assert _store(tmp_path, "   ").session_id is None
    assert Path(_store(tmp_path, "   ").context_dir) == tmp_path / ".vaf" / "main"


# ── an agent room's conversation store keeps its key as its folder name ───────────────────

def test_a_room_conversation_store_is_named_by_its_key_and_the_collector_reads_it(
        tmp_path, monkeypatch):
    """A room's working memory lives under `.vaf/main/conversations/<room>.p-<hex>/`, and the
    collector reads that folder name back as the conversation key to ask whether the room is
    still open. A key the mapping rewrote would name no room, and the open room's store would
    be swept after 48 hours."""
    import time as _time
    from datetime import datetime, timedelta

    import vaf.core.a2a.store as store_mod
    from vaf.core.a2a.room import Room, conversation_key, derive_peer_id
    from vaf.core.a2a.store import new_room_id
    from vaf.core.garbage_collector import GarbageCollector

    for _ in range(200):
        rid = new_room_id()
        key = conversation_key(rid, derive_peer_id(f"agent:{OWNER}", rid))
        assert MainPersistenceManager._safe_session_dir(key) == key

    rooms = tmp_path / "rooms"
    rooms.mkdir()
    monkeypatch.setattr(store_mod, "rooms_root", lambda base=None: Path(base) if base else rooms)
    monkeypatch.chdir(tmp_path)
    open_id, gone_id = new_room_id(), new_room_id()
    Room.create(kind="round", owner_scope=OWNER, base=rooms, room_id=open_id)
    stores = {
        rid: MainPersistenceManager(str(tmp_path), conversation=conversation_key(
            rid, derive_peer_id(f"agent:{OWNER}", rid)))
        for rid in (open_id, gone_id)
    }
    stale = _time.time() - 10 * 86400
    for mpm in stores.values():
        assert Path(mpm.context_dir).name == mpm.conversation
        for f in Path(mpm.context_dir).rglob("*"):
            if f.is_file():
                os.utime(f, (stale, stale))

    GarbageCollector.__new__(GarbageCollector)._clean_old_session_persistence(
        datetime.now() - timedelta(hours=48), {"deleted": 0, "freed_bytes": 0, "errors": 0})

    assert Path(stores[open_id].context_dir).exists(), "an open room lost its working memory"
    assert not Path(stores[gone_id].context_dir).exists(), "the control store was not swept"


# ── the structural rule ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "", " red654321", "red654321 ",
    "../escape", "a/b", "a\\b", ".", "..",
    ".hidden", "x\x00y", "D:escape",
    "x" * 241,
    None, 123,
])
def test_check_session_id_refuses(bad):
    with pytest.raises(InvalidSessionId):
        check_session_id(bad)


@pytest.mark.parametrize("sid", SERVER_FORMS)
def test_check_session_id_accepts_every_server_form(sid):
    assert check_session_id(sid) == sid


def test_every_server_form_round_trips_through_the_store(tmp_path):
    sm = SessionManager(storage_dir=str(tmp_path / "sessions"))
    for sid in SERVER_FORMS:
        sm.save(Session(id=sid, name=sid))
        assert sm.load(sid, repoint=False).id == sid


# ── the session store never leaves its folder ──────────────────────────────────────────────

@pytest.fixture
def outside(tmp_path):
    """A store, and session files planted just outside it where "../escape" points."""
    storage = tmp_path / "sessions"
    sm = SessionManager(storage_dir=str(storage))
    beside = Session(id="escape", name="beside the store")
    beside.metadata["user_scope_id"] = OWNER
    SessionManager(storage_dir=str(tmp_path)).save(beside)
    archived = Session(id="escape", name="beside the archive")
    archived.metadata["user_scope_id"] = OWNER
    archive_root = storage / "archive"
    SessionManager(storage_dir=str(archive_root)).save(archived)
    # The account's own archive folder exists (it has archived a chat before); without it
    # "<archive>/<scope>/../escape.json" does not resolve on POSIX and proves nothing.
    sm.archive_dir(OWNER).mkdir(parents=True, exist_ok=True)
    return sm, tmp_path / "escape.json", archive_root / "escape.json"


def test_save_refuses_an_id_that_is_not_one_entry(outside, tmp_path):
    sm, _beside, _archived = outside
    for compress in (False, True):
        with pytest.raises(InvalidSessionId):
            sm.save(Session(id="../planted", name="x"), compress=compress)
    assert not (tmp_path / "planted.json").exists()
    assert not (tmp_path / "planted.json.gz").exists()


def test_load_does_not_read_beside_the_store(outside):
    sm, beside, _archived = outside
    assert beside.exists()
    with pytest.raises(FileNotFoundError):
        sm.load("../escape")


def test_delete_archive_and_delete_archived_leave_outside_files_alone(outside):
    sm, beside, archived = outside
    assert sm.delete("../escape") is False
    assert sm.archive("../escape", user_scope_id=OWNER) is False
    assert sm.delete_archived("../escape", user_scope_id=OWNER) is False
    assert beside.exists(), "a file beside the store was removed or moved"
    assert archived.exists(), "a file beside the archive was removed"


# ── the gate ───────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def gate_store(tmp_path):
    sm = SessionManager(storage_dir=str(tmp_path / "sessions"))
    owned = Session(id="red654321", name="owner's chat")
    owned.metadata["user_scope_id"] = OWNER
    sm.save(owned)
    return sm


@pytest.mark.parametrize("bad", ["../escape", "a/b", "D:escape", " red654321", ".hidden"])
@pytest.mark.parametrize("is_admin", [False, True])
def test_the_gate_refuses_a_structurally_invalid_id_for_everyone(gate_store, bad, is_admin):
    scope = ADMIN if is_admin else BOB
    assert session_access_allowed(gate_store, bad, user_scope_id=scope, is_admin=is_admin,
                                  allow_missing=True) == (False, None)


@pytest.mark.parametrize("sid,ok", [
    ("green123456", True),
    (default_session_id(BOB), True),
    ("red654321.", False),
    ("telegram_123456789", False),
    (default_session_id(OWNER), False),
    ("system", False),
    ("never-used", False),
    ("green123456" + "7", False),
    ("pink123456", False),
])
def test_a_non_admin_may_start_only_ids_the_server_hands_out(gate_store, sid, ok):
    allowed, _ = session_access_allowed(gate_store, sid, user_scope_id=BOB, is_admin=False,
                                        allow_missing=True)
    assert allowed is ok
    # Without allow_missing nothing missing passes.
    assert session_access_allowed(gate_store, sid, user_scope_id=BOB, is_admin=False)[0] is False


@pytest.mark.parametrize("sid", ["red654321.", "telegram_123456789", "never-used",
                                 default_session_id(OWNER), "green123456"])
def test_the_admin_keeps_every_missing_valid_id(gate_store, sid):
    assert session_access_allowed(gate_store, sid, user_scope_id=ADMIN, is_admin=True)[0] is True


def test_an_existing_chat_answers_by_ownership_as_before(gate_store):
    assert session_access_allowed(gate_store, "red654321", user_scope_id=OWNER,
                                  is_admin=False)[0] is True
    assert session_access_allowed(gate_store, "red654321", user_scope_id=BOB,
                                  is_admin=False, allow_missing=True)[0] is False
    assert session_access_allowed(gate_store, "red654321", user_scope_id=ADMIN,
                                  is_admin=True)[0] is True


# ── the reported scenario, end to end through the WebSocket gate ──────────────────────────

class _Connection:
    def __init__(self, scope, role):
        self._scope, self._role = scope, role

    def get_connection_user(self, _ws):
        return self._scope

    def get_connection_user_role(self, _ws):
        return self._role


def test_bob_cannot_reach_the_owners_working_memory_through_a_variant_id(tmp_path, monkeypatch):
    import vaf.core.web_server as ws

    sm = SessionManager(storage_dir=str(tmp_path / "sessions"))
    owned = Session(id="red654321", name="owner's chat")
    owned.metadata["user_scope_id"] = OWNER
    sm.save(owned)
    monkeypatch.setattr(ws, "session_mgr", sm)
    monkeypatch.setattr(ws, "manager", _Connection(BOB, "user"))
    monkeypatch.setattr(cfg, "get_local_admin_scope_id", lambda: ADMIN)
    _store(tmp_path, "red654321").update_working_memory(add_notes=["OWNER-SECRET-NOTE"])

    # Today: Bob cannot start the variant at all.
    assert ws._ws_session_owner_ok(None, "red654321.", allow_missing=True)[0] is False

    # A variant Bob claimed before the fix is his chat and passes the gate ...
    planted = Session(id="red654321.", name="claimed")
    planted.metadata["user_scope_id"] = BOB
    sm.save(planted)
    assert ws._ws_session_owner_ok(None, "red654321.", allow_missing=True)[0] is True
    # ... and reaches only its own working memory, in both directions.
    bobs = _store(tmp_path, "red654321.")
    assert "OWNER-SECRET-NOTE" not in str(bobs.get_working_memory())
    bobs.update_working_memory(add_notes=["BOB-PLANTED"])
    assert "BOB-PLANTED" not in str(_store(tmp_path, "red654321").get_working_memory())
    assert "OWNER-SECRET-NOTE" in str(_store(tmp_path, "red654321").get_working_memory())


# ── one default-chat recipe, one colour list ───────────────────────────────────────────────

def test_the_web_server_has_no_hand_copy_of_the_default_chat_id():
    text = (_REPO / "vaf" / "core" / "web_server.py").read_bytes().decode("utf-8")
    assert 'f"web-default-' not in text, (
        "vaf/core/web_server.py builds the default chat id by hand again; "
        "call vaf.core.session.default_session_id")


def test_the_minted_colours_are_the_hygiene_guards_colours():
    text = (_REPO / "tests" / "test_public_repo_hygiene.py").read_bytes().decode("utf-8")
    m = re.search(r'_SESSION_ID_RE = re\.compile\(r"\(\?:([a-z|]+)\)', text)
    assert m, "the session-id pattern of tests/test_public_repo_hygiene.py was not found"
    assert set(SESSION_COLOURS) == set(m.group(1).split("|"))


def test_every_minted_id_may_be_started_by_its_account(tmp_path, monkeypatch):
    import vaf.core.session as session_mod
    monkeypatch.setattr(session_mod, "default_sessions_dir", lambda: tmp_path)
    sm = SessionManager(storage_dir=str(tmp_path))
    for _ in range(50):
        sid = _generate_session_id()
        assert check_session_id(sid) == sid
        assert session_access_allowed(sm, sid, user_scope_id=BOB, is_admin=False,
                                      allow_missing=True)[0] is True, sid
