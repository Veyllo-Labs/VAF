# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The paired Veyllo App devices: one sealed file, ``~/.vaf/app_link/devices.json``.

A row binds a device's public key (SHA-256 of its SubjectPublicKeyInfo) and certificate
serial to ONE account. A link session takes its account from the row its certificate names
and from nothing else: no message field, no relay address, no client address. A removed
row ends access at once, even while the certificate is still valid.

Also here, because they belong to the devices: one chat per account (``chats``, a random
key minted once, so no scope id ever goes to the phone; the session id is
``veyllo_app_<key>``), the per-device outbox of ids still to be delivered (the text stays
in the channel message store, the one copy), the relay members still to delete
(``member_tombstones``), and the records of the files held for a device that was away
(``spool``: name, type, size and hash per message id; the bytes are in spool.py's store).

Under a thread lock plus a file lock, so the CLI's ``vaf devices remove`` and the running
VAF never write over each other; ``generation()`` lets the running VAF see a change made by
another process within one sweep.
"""
from __future__ import annotations

import contextlib
import logging
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from vaf.core.app_link import CHANNEL

logger = logging.getLogger(__name__)

DEVICES_FORMAT = "applink-devices-1-06c31c"
OUTBOX_MAX = 200
OUTBOX_MAX_AGE_SECONDS = 7 * 86400
_LOCK_TIMEOUT_S = 10

_lock = threading.RLock()
_removed_listeners: List[Callable[[str], None]] = []


def _path() -> Path:
    from vaf.core.platform import Platform
    return Platform.vaf_dir() / "app_link" / "devices.json"


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    from vaf.core.secure_store import _get_filelock_cls
    with _lock:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        cls = _get_filelock_cls()
        lock = cls(str(path) + ".lock", timeout=_LOCK_TIMEOUT_S) if cls is not None else None
        if lock is not None:
            lock.acquire()
        try:
            yield
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass


def _empty() -> Dict[str, Any]:
    return {"format": DEVICES_FORMAT, "devices": [], "chats": {}, "member_tombstones": [],
            "spool": {}}


def _load() -> Dict[str, Any]:
    from vaf.core import data_files
    try:
        state = data_files.read_json(_path(), None)
    except Exception as exc:
        logger.warning("The Veyllo App device list cannot be read: %s", exc)
        state = None
    if not isinstance(state, dict):
        return _empty()
    # A file without the tag is read as legacy; a foreign tag vouches for no device.
    if state.get("format") not in (None, DEVICES_FORMAT):
        logger.warning("The Veyllo App device list has an unknown format; no device is trusted.")
        return _empty()
    out = _empty()
    out["devices"] = [d for d in state.get("devices") or [] if isinstance(d, dict)
                      and d.get("device_id") and d.get("user_scope_id")]
    out["chats"] = {str(k): str(v) for k, v in (state.get("chats") or {}).items()}
    out["member_tombstones"] = [str(m) for m in state.get("member_tombstones") or [] if m]
    # Additive key: a file written before it has none, and a loader before it drops it (the
    # files then replay as their text line, as they always did).
    spool = state.get("spool")
    out["spool"] = {str(k): dict(v) for k, v in spool.items()
                    if isinstance(v, dict)} if isinstance(spool, dict) else {}
    return out


def _save(state: Dict[str, Any]) -> None:
    from vaf.core import data_files
    state["format"] = DEVICES_FORMAT
    data_files.write_json_atomic(_path(), state)


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    return dict(row, caps=list(row.get("caps") or []), outbox=[dict(e) for e in row.get("outbox") or []])


def _scope(value: Any) -> str:
    return str(value or "").strip()


# ── reading ──────────────────────────────────────────────────────────────────

def all_devices() -> List[Dict[str, Any]]:
    with _lock:
        return [_public(d) for d in _load()["devices"]]


def get(device_id: str) -> Optional[Dict[str, Any]]:
    did = str(device_id or "")
    return next((d for d in all_devices() if d["device_id"] == did), None)


def by_spki(spki_sha256: str) -> Optional[Dict[str, Any]]:
    key = str(spki_sha256 or "").lower()
    return next((d for d in all_devices() if str(d.get("spki_sha256") or "").lower() == key), None)


def for_scope(user_scope_id: Any) -> List[Dict[str, Any]]:
    scope = _scope(user_scope_id)
    return [d for d in all_devices() if _scope(d.get("user_scope_id")) == scope]


def resolve(device_ref: str, user_scope_id: Any = None) -> Optional[Dict[str, Any]]:
    """A device by its id or a unique id prefix (at least 6 characters), optionally only
    among one account's devices."""
    ref = str(device_ref or "").strip().lower()
    rows = for_scope(user_scope_id) if user_scope_id is not None else all_devices()
    exact = [d for d in rows if d["device_id"] == ref]
    if exact:
        return exact[0]
    if len(ref) < 6:
        return None
    hits = [d for d in rows if d["device_id"].startswith(ref)]
    return hits[0] if len(hits) == 1 else None


def chat_key(user_scope_id: Any) -> str:
    """The account's chat key, minted once."""
    scope = _scope(user_scope_id)
    if not scope:
        raise ValueError("a chat needs the account's scope")
    with _locked():
        state = _load()
        key = state["chats"].get(scope)
        if not key:
            key = secrets.token_hex(6)
            state["chats"][scope] = key
            _save(state)
        return key


def chat_session_id(user_scope_id: Any) -> str:
    return f"{CHANNEL}_{chat_key(user_scope_id)}"


ASK_SESSION_SUFFIX = "_agent"


def ask_session_id(user_scope_id: Any) -> str:
    """The session the app agent's requests (``agent_ask``) run in, beside the account's chat
    and never inside it: a request left in the person's chat read as an open question of the
    person, and a later turn of the person answered it instead of them."""
    return chat_session_id(user_scope_id) + ASK_SESSION_SUFFIX


def scope_for_chat(chat: str) -> Optional[str]:
    """The account whose chat this is (a session id or a bare key), or None."""
    key = str(chat or "")
    if key.startswith(f"{CHANNEL}_"):
        key = key[len(CHANNEL) + 1:]
    with _lock:
        for scope, value in _load()["chats"].items():
            if value == key:
                return scope
    return None


def generation() -> Tuple[int, int]:
    """Changes when the file changes, also through another process."""
    try:
        st = _path().stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def tombstones() -> List[str]:
    with _lock:
        return list(_load()["member_tombstones"])


# ── whether a device may act ─────────────────────────────────────────────────

def idle_days() -> int:
    from vaf.core.app_link import settings
    try:
        return max(1, int(settings().get("device_idle_days") or 90))
    except (TypeError, ValueError):
        return 90


def row_stands(row: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
    """A row that still opens the link: its account stands, the account's Veyllo App lane is
    on, and it was used within ``device_idle_days``."""
    if not row:
        return False
    scope = _scope(row.get("user_scope_id"))
    from vaf.core import revocation
    if revocation.is_revoked(scope) or not revocation.account_stands(scope):
        return False
    from vaf.core.messaging_connections import channel_enabled_for_scope
    if not channel_enabled_for_scope(CHANNEL, scope):
        return False
    now = time.time() if now is None else now
    last = float(row.get("last_seen_at") or row.get("created_at") or 0)
    return now - last <= idle_days() * 86400


def stands(device_id: str) -> bool:
    return row_stands(get(device_id))


# ── writing ──────────────────────────────────────────────────────────────────

def add_device(*, user_scope_id: Any, username: str, spki_sha256: str, cert_serial: str,
               model: str, platform: str, app_version: str, device_id: Optional[str] = None,
               transport: Optional[str] = None, relay_member: Optional[str] = None,
               relay_cid: Optional[str] = None) -> Dict[str, Any]:
    scope = _scope(user_scope_id)
    if not scope or not spki_sha256:
        raise ValueError("a device needs an account and a key")
    now = time.time()
    row = {"device_id": str(device_id or uuid.uuid4()), "user_scope_id": scope,
           "username": str(username or ""), "spki_sha256": str(spki_sha256).lower(),
           "cert_serial": str(cert_serial), "model": str(model or ""),
           "platform": str(platform or ""), "app_version": str(app_version or ""), "caps": [],
           "created_at": now, "last_seen_at": now, "last_transport": transport,
           "relay_member": relay_member or None, "relay_cid": relay_cid or None,
           "outbox": [], "outbox_dropped": 0, "state": "active"}
    with _locked():
        state = _load()
        if any(d.get("spki_sha256") == row["spki_sha256"] for d in state["devices"]):
            raise ValueError("this device key is already paired")
        if not state["chats"].get(scope):
            state["chats"][scope] = secrets.token_hex(6)
        state["devices"].append(row)
        _save(state)
    return _public(row)


def _update(device_id: str, change: Callable[[Dict[str, Any]], Any]) -> Any:
    with _locked():
        state = _load()
        row = next((d for d in state["devices"] if d["device_id"] == str(device_id)), None)
        if row is None:
            return None
        result = change(row)
        _save(state)
        return result


def touch(device_id: str, transport: str, cid: Optional[str] = None) -> None:
    def _change(row):
        row["last_seen_at"] = time.time()
        row["last_transport"] = transport
        if cid:
            row["relay_cid"] = cid
    _update(device_id, _change)


def set_relay(device_id: str, *, member: Optional[str], cid: Optional[str]) -> None:
    def _change(row):
        row["relay_member"] = member or None
        row["relay_cid"] = cid or None
    _update(device_id, _change)


def set_caps(device_id: str, caps: List[str]) -> None:
    """The FULL list the app reported, replacing the stored one (protocol section 9)."""
    def _change(row):
        row["caps"] = [str(c) for c in caps]
    _update(device_id, _change)


def _outbox_append(row: Dict[str, Any], message_id: str, ts: Optional[float]) -> None:
    box = [e for e in row.get("outbox") or [] if e.get("id") != message_id]
    box.append({"id": str(message_id), "ts": float(ts if ts is not None else time.time())})
    overflow = len(box) - OUTBOX_MAX
    if overflow > 0:
        box = box[overflow:]
        row["outbox_dropped"] = int(row.get("outbox_dropped") or 0) + overflow
    row["outbox"] = box


def outbox_add(device_id: str, message_id: str, ts: Optional[float] = None) -> None:
    _update(device_id, lambda row: _outbox_append(row, message_id, ts))


def outbox_publish(user_scope_id: Any, device_ids: List[str], message_id: str, *,
                   ts: Optional[float] = None, spool: Optional[Dict[str, Any]] = None,
                   keep: int = 0) -> List[str]:
    """A stored message's outbox entry for each of ``device_ids`` and, for a message carrying
    a file, that file's record (``spool``), in ONE write of the device list. A replay reading
    the list in between would otherwise see the entry without its file and send the text
    line only, and a ``collect`` would see the file without its entries and drop it. The
    account's oldest file records beyond ``keep`` go; their ids are returned so their bytes
    go too. A target that no longer stands (``row_stands``: its account revoked or gone, the
    account's Veyllo App lane switched off, unused too long) takes no entry, with or without
    a file, the same rule the caller used to pick its targets; it is asked again here, so
    that holds whatever ran between the caller's choice and this write. It is asked right
    before the lock and not under it: it reads
    the account directory, a store query, which the device-list lock must not wait on. The
    window that remains is the lock acquisition itself; a row removed in it is caught under
    the lock. When no device row takes an entry, nothing is written: the file is not
    recorded, so it cannot push out another message's pending file, and its own id is
    returned so its staged bytes go."""
    wanted = {str(d) for d in device_ids}
    ids = {row["device_id"] for row in all_devices()
           if row["device_id"] in wanted and row_stands(row)} if wanted else set()
    with _locked():
        state = _load()
        rows = [row for row in state["devices"] if row["device_id"] in ids]
        if not rows:
            return [str(message_id)] if spool else []
        for row in rows:
            _outbox_append(row, message_id, ts)
        evicted = _spool_record(state, user_scope_id, message_id, spool, keep) if spool else []
        _save(state)
        return evicted


def outbox_ack(device_id: str, message_id: str) -> bool:
    def _change(row):
        box = row.get("outbox") or []
        kept = [e for e in box if e.get("id") != str(message_id)]
        row["outbox"] = kept
        return len(kept) != len(box)
    return bool(_update(device_id, _change))


def outbox_due(device_id: str, now: Optional[float] = None) -> Tuple[List[Dict[str, Any]], int]:
    """(entries to send in order, how many fell out since the last replay). Entries older
    than seven days fall out here and are counted with the overflow."""
    now = time.time() if now is None else now

    def _change(row):
        box = row.get("outbox") or []
        fresh = [e for e in box if now - float(e.get("ts") or 0) <= OUTBOX_MAX_AGE_SECONDS]
        row["outbox_dropped"] = int(row.get("outbox_dropped") or 0) + len(box) - len(fresh)
        row["outbox"] = fresh
        return [dict(e) for e in fresh], int(row["outbox_dropped"])
    result = _update(device_id, _change)
    return result if result is not None else ([], 0)


def outbox_file_sent(device_id: str, message_id: str) -> int:
    """Count one more replay of an outbox entry's file to this device; the new count (0 when
    the entry is gone)."""
    def _change(row):
        for entry in row.get("outbox") or []:
            if entry.get("id") == str(message_id):
                entry["sends"] = int(entry.get("sends") or 0) + 1
                return entry["sends"]
        return 0
    return int(_update(device_id, _change) or 0)


# ── the records of spooled files (the bytes: spool.py) ────────────────────────

def _spool_record(state: Dict[str, Any], user_scope_id: Any, message_id: str,
                  meta: Dict[str, Any], keep: int) -> List[str]:
    """Record a spooled file of the account in ``state``. The account's oldest records beyond
    ``keep`` go; their ids are returned. Another account's records are untouched."""
    scope = _scope(user_scope_id)
    spool = state["spool"]
    spool[str(message_id)] = dict(meta, scope=scope)
    mine = sorted((float(v.get("ts") or 0), k) for k, v in spool.items()
                  if _scope(v.get("scope")) == scope)
    evicted = [k for _ts, k in mine[:max(0, len(mine) - max(0, int(keep)))]]
    for key in evicted:
        spool.pop(key, None)
    return evicted


def spool_get(message_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        record = _load()["spool"].get(str(message_id))
    return dict(record) if record is not None else None


def spool_ids() -> List[str]:
    with _lock:
        return list(_load()["spool"])


def spool_drop(ids: List[str]) -> None:
    ids = [str(i) for i in ids]
    if not ids:
        return
    with _locked():
        state = _load()
        if not any(i in state["spool"] for i in ids):
            return
        for key in ids:
            state["spool"].pop(key, None)
        _save(state)


def spool_unreferenced(now: Optional[float] = None) -> List[str]:
    """The spooled files no device's outbox holds any more (every device acked it, a device
    was removed, its entry fell out), or older than the outbox keeps anything."""
    now = time.time() if now is None else now
    with _lock:
        state = _load()
    held = {str(e.get("id")) for d in state["devices"] for e in d.get("outbox") or []}
    return [k for k, v in state["spool"].items()
            if k not in held or now - float(v.get("ts") or 0) > OUTBOX_MAX_AGE_SECONDS]


def take_dropped(device_id: str) -> int:
    """The count of dropped entries, reset (the notice about them was sent)."""
    def _change(row):
        n = int(row.get("outbox_dropped") or 0)
        row["outbox_dropped"] = 0
        return n
    return int(_update(device_id, _change) or 0)


def drop_tombstone(member: str) -> None:
    with _locked():
        state = _load()
        state["member_tombstones"] = [m for m in state["member_tombstones"] if m != member]
        _save(state)


def clear_relay_state() -> int:
    """Forget every relay member and address and the tombstones (the relay instance was
    dissolved); the devices stay for the direct path."""
    with _locked():
        state = _load()
        n = 0
        for row in state["devices"]:
            if row.get("relay_member") or row.get("relay_cid"):
                n += 1
            row["relay_member"] = None
            row["relay_cid"] = None
        state["member_tombstones"] = []
        _save(state)
        return n


def add_removed_listener(listener: Callable[[str], None]) -> None:
    """``listener(device_id)`` runs after a device was removed (the link manager closes its
    session at once). Must not raise."""
    with _lock:
        if listener not in _removed_listeners:
            _removed_listeners.append(listener)


def remove_removed_listener(listener: Callable[[str], None]) -> None:
    with _lock:
        if listener in _removed_listeners:
            _removed_listeners.remove(listener)


def _after_removal(rows: List[Dict[str, Any]], by: str, reason: str) -> None:
    for row in rows:
        try:
            from vaf.core.security_events import log_security_event
            log_security_event("channel_unpaired", channel=CHANNEL, username=str(by or ""),
                               path=row["device_id"], detail=f"{row.get('model') or 'device'}, {reason}")
        except Exception:
            pass
        try:
            from vaf.core.task_queue import TaskQueue
            sid = f"{CHANNEL}_{row.get('chat_key')}" if row.get("chat_key") else None
            if sid:
                did = row["device_id"]
                for one in (sid, sid + ASK_SESSION_SUFFIX):
                    TaskQueue().drop_queued_tasks_for_session(
                        one, where=lambda t, did=did: (t.metadata or {}).get("app_device_id") == did)
        except Exception:
            pass
        with _lock:
            listeners = list(_removed_listeners)
        for listener in listeners:
            try:
                listener(row["device_id"])
            except Exception:
                pass


def _remove_where(predicate: Callable[[Dict[str, Any]], bool]) -> List[Dict[str, Any]]:
    with _locked():
        state = _load()
        gone = [d for d in state["devices"] if predicate(d)]
        if not gone:
            return []
        state["devices"] = [d for d in state["devices"] if not predicate(d)]
        for row in gone:
            row["chat_key"] = state["chats"].get(_scope(row.get("user_scope_id")))
            member = row.get("relay_member")
            if member and member not in state["member_tombstones"]:
                state["member_tombstones"].append(member)
        _save(state)
        return gone


def remove_device(device_id: str, *, by: str, reason: str) -> bool:
    """Remove one device in ONE step: the row goes, its relay member is tombstoned for
    deletion, ``channel_unpaired`` is logged, its queued turns are dropped (the account's
    other devices keep theirs) and its live session is closed. A turn already running is
    not killed; its reply never reaches the removed device (every send asks ``stands``)."""
    did = str(device_id or "")
    gone = _remove_where(lambda d: d["device_id"] == did)
    _after_removal(gone, by, reason)
    return bool(gone)


def remove_account_devices(user_scope_id: Any, *, by: str, reason: str) -> int:
    scope = _scope(user_scope_id)
    if not scope:
        return 0
    gone = _remove_where(lambda d: _scope(d.get("user_scope_id")) == scope)
    _after_removal(gone, by, reason)
    return len(gone)


def expire_idle(now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    limit = idle_days() * 86400
    gone = _remove_where(
        lambda d: now - float(d.get("last_seen_at") or d.get("created_at") or 0) > limit)
    _after_removal(gone, "", f"unused for {idle_days()} days")
    return len(gone)


def _on_account_stopped(user_scope_id: str) -> None:
    """Revocation listener: a deactivated or deleted account loses every device; a narrowing
    of its rights (work stopped, account not revoked) keeps them."""
    from vaf.core import revocation
    scope = _scope(user_scope_id)
    if not scope:
        return
    if revocation.is_revoked(scope) or not revocation.account_stands(scope):
        remove_account_devices(scope, by="", reason="account deactivated or deleted")


def register_revocation_listener() -> None:
    from vaf.core import revocation
    revocation.add_revocation_listener(_on_account_stopped)


register_revocation_listener()
