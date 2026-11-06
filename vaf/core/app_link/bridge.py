# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The link manager: the live device sessions, the feed of their messages into the agent,
the replies back, and the per-device outbox.

In: a ``msg`` of a device that stands becomes a turn in ``TaskQueue`` IN THIS PROCESS, with
the scope and username of the device row (never from a field of the message), source
``veyllo_app``, in the account's one chat (``veyllo_app_<key>``). No HTTP request and no
socket to this machine is made on the way.

Out: a running turn of the chat is streamed as ``delta`` increments of its cleaned visible
text and always ends with ``done`` carrying the final text (the app replaces the stream with
it; nothing is erased on VAF's side). A message VAF sends by itself (``send_to_user``, the
send tool) is a ``msg``. Every finished reply and every ``msg`` is stored ONCE in the
channel message store and its id is put into the outbox of EVERY device of the account; an
``ack`` takes it out of that device's outbox only. After ``welcome`` a device gets its due
entries in order, as ``msg`` with the stored text (streamed deltas are never replayed).
Every send asks whether the device still stands, so a device removed mid-turn receives
nothing more of it.

NAMED BOUNDARY: a file sent while a device is away reaches it as its text line only; the
file itself goes to the devices connected at the time (the outbox keeps ids, not bytes).
"""
from __future__ import annotations

import logging
import mimetypes
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from vaf.core.app_link import CHANNEL, devices
from vaf.core.app_link import protocol as pr

logger = logging.getLogger(__name__)

SWEEP_SECONDS = 5
DUPLICATE_WINDOW_SECONDS = 24 * 3600
# A reply no path ended (the runner closes every turn; this is the last net) is forgotten
# after this long.
REPLY_MAX_SECONDS = 24 * 3600
EXPIRY_EVERY_SECONDS = 3600
# How often a device that stays connected refreshes its last use (an idle expiry counts
# from it), at most: once per hour, not a sealed-file write per message.
TOUCH_EVERY_SECONDS = 3600

_lock = threading.RLock()
_sessions: Dict[str, Any] = {}          # device_id -> its authenticated link session
_all: set = set()                        # every session alive, handshakes included
_replies: Dict[str, "Reply"] = {}
_seen_ids: Dict[str, Dict[str, float]] = {}
_msg_times: Dict[str, Deque[float]] = {}
_touched: Dict[str, float] = {}
_offline_hooks: List[Callable[[Dict[str, Any]], None]] = []
_online_hooks: List[Callable[[str], None]] = []
_state = {"expired_at": 0.0}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


@dataclass
class Reply:
    id: str
    scope: str
    username: str
    chat_key: str
    app_message_id: str = ""
    rounds: Dict[int, str] = field(default_factory=dict)
    started: float = field(default_factory=time.time)
    sent: str = ""
    seq: int = 0
    streaming: bool = True


def _clean(text: str) -> str:
    from vaf.core.headless_runner import _prepare_channel_outbound
    return _prepare_channel_outbound(text)


# ── sessions ─────────────────────────────────────────────────────────────────

def new_session(transport: str, send_bytes: Callable[[bytes], None], *, connection: str,
                peer: str, peer_label: str = "", relay_cid: Optional[str] = None):
    """A new link session for a carrier, or None when a limit refuses it (sessions per
    minute per carrier connection and per peer, sessions alive at once)."""
    from vaf.core.app_link.session import LIMITER, LinkSession
    if not LIMITER.admit(connection, peer):
        try:
            from vaf.core.security_events import log_security_event
            log_security_event("app_pairing_refused", channel=CHANNEL,
                               detail=f"session rate limit, {transport}")
        except Exception:
            pass
        return None

    def _closed(session) -> None:
        LIMITER.release()
        with _lock:
            _all.discard(session)
            if session.device and _sessions.get(session.device["device_id"]) is session:
                del _sessions[session.device["device_id"]]

    session = LinkSession(transport, send_bytes, peer_label or peer, on_close=_closed,
                          relay_cid=relay_cid)
    with _lock:
        _all.add(session)
    return session


def session_for(device_id: str):
    with _lock:
        session = _sessions.get(str(device_id))
    return session if session is not None and not session.closed else None


def connected_devices(user_scope_id: Any) -> List[Dict[str, Any]]:
    """The account's devices with a live session that still stand, newest use first."""
    scope = str(user_scope_id or "").strip()
    rows = []
    for row in devices.for_scope(scope):
        if session_for(row["device_id"]) is not None and devices.row_stands(row):
            rows.append(row)
    return sorted(rows, key=lambda r: -float(r.get("last_seen_at") or 0))


def is_connected(device_id: str) -> bool:
    return session_for(device_id) is not None


def close_device(device_id: str, reason: str = "removed") -> None:
    with _lock:
        session = _sessions.pop(str(device_id), None)
    if session is not None:
        session.close(reason, bye=True)


def add_offline_hook(hook: Callable[[Dict[str, Any]], None]) -> None:
    """``hook(device_row)`` runs when something lands in the outbox of a device that is not
    connected (the relay connector schedules its notify)."""
    with _lock:
        if hook not in _offline_hooks:
            _offline_hooks.append(hook)


def add_online_hook(hook: Callable[[str], None]) -> None:
    with _lock:
        if hook not in _online_hooks:
            _online_hooks.append(hook)


def remove_hooks(hook) -> None:
    with _lock:
        if hook in _offline_hooks:
            _offline_hooks.remove(hook)
        if hook in _online_hooks:
            _online_hooks.remove(hook)


def _went_offline(row: Dict[str, Any]) -> None:
    with _lock:
        hooks = list(_offline_hooks)
    for hook in hooks:
        try:
            hook(row)
        except Exception:
            pass


def _came_online(device_id: str) -> None:
    with _lock:
        hooks = list(_online_hooks)
    for hook in hooks:
        try:
            hook(device_id)
        except Exception:
            pass


def _send(device_id: str, obj: Dict[str, Any]) -> bool:
    """Send to one device, only while it stands."""
    session = session_for(device_id)
    if session is None:
        return False
    if not devices.stands(device_id):
        close_device(device_id)
        return False
    return session.send_json(obj)


def _to_account(scope: str, obj: Dict[str, Any]) -> List[str]:
    sent = []
    for row in devices.for_scope(scope):
        if _send(row["device_id"], obj):
            sent.append(row["device_id"])
    return sent


# ── hello and what follows ───────────────────────────────────────────────────

def on_hello(session, hello: Dict[str, Any]) -> None:
    row = session.device
    did = row["device_id"]
    with _lock:
        previous = _sessions.get(did)
        _sessions[did] = session
    if previous is not None and previous is not session:
        previous.close("replaced", bye=True)
    # The relay address is learned from the claim; a device paired before the relay sent it is
    # given the first cid its certificate authenticated on. Identity never comes from it.
    devices.touch(did, session.transport,
                  cid=session.relay_cid if session.transport == "relay" and not row.get("relay_cid")
                  else None)
    with _lock:
        _touched[did] = time.monotonic()
    if hello.get("caps") is not None:
        devices.set_caps(did, hello["caps"])
    scope = row["user_scope_id"]
    from vaf.core.app_link import agent_name
    agent = agent_name(row.get("username") or "")
    session.send_json({"t": "welcome", "v": pr.PROTOCOL_VERSION,
                       "chat": devices.chat_session_id(scope), "account": row.get("username", ""),
                       "agent": agent or "VAF", "server_time": int(time.time())})
    _came_online(did)
    replay(session)


def _dropped_notice(row: Dict[str, Any], count: int) -> str:
    from vaf.core import vocab
    lang = vocab.resolve_user_language(row.get("user_scope_id"), row.get("username"))
    return vocab.pick("app_outbox_dropped", lang, row.get("user_scope_id"), count=count) or \
        f"{count} older messages are only in VAF's chat history."


def replay(session) -> int:
    """The device's due outbox, in order, as ``msg``; first a notice when entries fell out."""
    from vaf.core import channel_message_store as store
    row = session.device
    did = row["device_id"]
    scope = row["user_scope_id"]
    key = devices.chat_key(scope)
    chat = devices.chat_session_id(scope)
    entries, dropped = devices.outbox_due(did)
    sent = 0
    if dropped:
        if session.send_json({"t": "msg", "id": str(uuid.uuid4()), "chat": chat,
                              "text": _dropped_notice(row, dropped)}):
            devices.take_dropped(did)
    for entry in entries:
        message = store.get_message(row.get("username") or "", key, entry["id"], direction="out",
                                    user_scope_id=scope, channel=CHANNEL)
        if message is None:
            devices.outbox_ack(did, entry["id"])
            continue
        if session.send_json({"t": "msg", "id": entry["id"], "chat": chat,
                              "text": message.get("body") or ""}):
            sent += 1
    return sent


def on_message(session, message: Dict[str, Any], files: List[Dict[str, Any]]) -> None:
    row = session.device
    did = row["device_id"]
    if not devices.stands(did):
        close_device(did)
        session.close("removed", bye=True)
        return
    t = message["t"]
    if t in ("msg", "ack"):
        _touch_now_and_then(did, session)
    if t == "msg":
        _on_msg(session, row, message, files)
    elif t == "ack":
        devices.outbox_ack(did, message["id"])
    elif t == "read":
        try:
            from vaf.core.channel_message_store import mark_seen
            mark_seen(row.get("username") or "", CHANNEL, devices.chat_key(row["user_scope_id"]),
                      row["user_scope_id"])
        except Exception:
            pass
    elif t == "caps":
        devices.set_caps(did, message["caps"])
    elif t == "cancel":
        _on_cancel(row, message["id"])
    elif t == "task_result":
        try:
            from vaf.core.app_link import phone_tasks
            phone_tasks.on_result(did, message, files)
        except ImportError:
            pass
    elif t == "error":
        logger.info("app link: device %s reported %s", did[:8], message.get("code"))


def _touch_now_and_then(device_id: str, session) -> None:
    """A device in use for weeks without reconnecting still counts as used."""
    now = time.monotonic()
    with _lock:
        last = _touched.get(device_id)
        if last is not None and now - last < TOUCH_EVERY_SECONDS:
            return
        _touched[device_id] = now
    devices.touch(device_id, session.transport)


def _rate_ok(device_id: str) -> bool:
    from vaf.core.app_link.session import MSG_PER_MINUTE
    now = time.monotonic()
    with _lock:
        times = _msg_times.setdefault(device_id, deque())
        while times and now - times[0] > 60:
            times.popleft()
        if len(times) >= MSG_PER_MINUTE:
            return False
        times.append(now)
        return True


def _seen_before(device_id: str, message_id: str) -> bool:
    """Whether this message id was queued already (within 24 hours)."""
    now = time.time()
    with _lock:
        seen = _seen_ids.setdefault(device_id, {})
        for key in [k for k, ts in seen.items() if now - ts > DUPLICATE_WINDOW_SECONDS]:
            del seen[key]
        return message_id in seen


def _remember(device_id: str, message_id: str) -> None:
    """Marked only once the turn is queued: a message refused or failed on the way may be
    sent again with the same id."""
    with _lock:
        _seen_ids.setdefault(device_id, {})[message_id] = time.time()


def save_files(scope: str, username: str, chat_sid: str, files: List[Dict[str, Any]],
               *, origin: str = CHANNEL) -> Optional[List[str]]:
    """Check every file (threat database) and save them into the chat's workspace. The
    workspace paths, or None when one file is refused (then none is saved)."""
    from vaf.core import threat_db
    from vaf.core.path_jail import PathEscape, safe_entry_name
    for item in files:
        verdict = threat_db.inspect_upload(item["data"], filename=item["name"], origin=origin,
                                           username=username)
        if verdict.blocked:
            return None
    from vaf.core.session import get_session_workspace_dir
    folder = get_session_workspace_dir(chat_sid, create=True, user_scope_id=scope)
    if folder is None:
        return None
    folder = Path(folder) / "from_phone"
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for item in files:
        try:
            name = safe_entry_name(item["name"])
        except PathEscape:
            name = f"file-{uuid.uuid4().hex[:8]}"
        target = folder / name
        if target.exists():
            target = folder / f"{target.stem}-{uuid.uuid4().hex[:6]}{target.suffix}"
        target.write_bytes(item["data"])
        saved.append(str(target))
    return saved


def _on_msg(session, row, message, files) -> None:
    from vaf.core.app_link import settings
    scope = row["user_scope_id"]
    username = row.get("username") or ""
    did = row["device_id"]
    key = devices.chat_key(scope)
    chat_sid = f"{CHANNEL}_{key}"
    mid = message["id"]
    if message["chat"] not in (chat_sid, key):
        session.send_json({"t": "error", "code": "wrong_chat", "ref": mid})
        return
    if _seen_before(did, mid):
        session.send_json({"t": "ack", "id": mid})
        return
    if not _rate_ok(did):
        session.send_json({"t": "error", "code": "rate_limited", "ref": mid})
        return
    from vaf.core.channel_ingress_policy import evaluate_ingress
    from vaf.core.config import Config
    allowed, _reason = evaluate_ingress(CHANNEL, Config.get("channel_ingress_policy"),
                                        explicit_match=True)
    if not allowed or not settings().get("enabled", True):
        session.send_json({"t": "error", "code": "refused", "ref": mid})
        return
    text = message.get("text") or ""
    if files:
        saved = save_files(scope, username, chat_sid, files)
        if saved is None:
            for item in message.get("attachments") or []:
                session.send_json({"t": "error", "code": "xfer_abort", "ref": pr.b64url(item["xfer"])})
            return
        lines = [f"[File: {f['name']}, saved as {path}]" for f, path in zip(files, saved)]
        text = (text + "\n" if text else "") + "\n".join(lines)
    try:
        from vaf.core.channel_message_store import append_message
        append_message(username=username, chat_id=key, body=text, direction="in", message_id=mid,
                       content_type="document" if files else "text", channel=CHANNEL,
                       user_scope_id=scope)
    except Exception:
        logger.debug("app link: storing an inbound message failed", exc_info=True)
    from vaf.core.task_queue import TaskQueue
    TaskQueue().add(session_id=chat_sid, input_text=text, source=CHANNEL,
                    metadata={"user_scope_id": scope, "username": username,
                              "origin_channel": CHANNEL, "task_class": "interactive",
                              "app_device_id": did, "app_message_id": mid,
                              "channel_message_id": mid})
    _remember(did, mid)
    session.send_json({"t": "ack", "id": mid})


def _on_cancel(row: Dict[str, Any], ref: str) -> None:
    scope = row["user_scope_id"]
    with _lock:
        running = [r for r in _replies.values() if r.scope == scope
                   and ref in (r.id, r.app_message_id)]
    if not running:
        return
    from vaf.core import revocation
    revocation.stop_session(devices.chat_session_id(scope), include_subagents=False)


# ── the turn's reply ─────────────────────────────────────────────────────────

def turn_started(meta: Dict[str, Any]) -> Optional[str]:
    """A queued turn of the account's chat starts: ``read`` for the message that started it
    and ``typing on``. Mints the reply id and remembers it in ``meta`` (``app_reply_id``)."""
    scope = str(meta.get("user_scope_id") or "").strip()
    if not scope:
        return None
    key = devices.chat_key(scope)
    reply = Reply(id=str(uuid.uuid4()), scope=scope, username=str(meta.get("username") or ""),
                  chat_key=key, app_message_id=str(meta.get("app_message_id") or ""))
    with _lock:
        _replies[reply.id] = reply
    meta["app_reply_id"] = reply.id
    chat = f"{CHANNEL}_{key}"
    if reply.app_message_id:
        _to_account(scope, {"t": "read", "id": reply.app_message_id})
    _to_account(scope, {"t": "typing", "chat": chat, "on": True})
    return reply.id


def stream_round(reply_id: Optional[str], text: str, number: int) -> None:
    """One round's full text so far: the increment of the cleaned accumulated text goes out
    as a ``delta``. A text that no longer extends what was sent (a clear, a retraction)
    stops the deltas of this reply; ``done`` still carries the final text."""
    with _lock:
        reply = _replies.get(str(reply_id or ""))
    if reply is None or not reply.streaming:
        return
    reply.rounds[int(number)] = str(text or "")
    full = _clean("\n\n".join(reply.rounds[k] for k in sorted(reply.rounds) if reply.rounds[k].strip()))
    if not full.startswith(reply.sent):
        reply.streaming = False
        return
    increment = full[len(reply.sent):]
    if not increment:
        return
    reply.sent = full
    reply.seq += 1
    _to_account(reply.scope, {"t": "delta", "id": reply.id, "seq": reply.seq, "text": increment})


def standing_devices(user_scope_id: Any) -> List[Dict[str, Any]]:
    """The account's devices that may still receive (removed, switched-off and long-unused
    ones excluded), connected or not."""
    scope = str(user_scope_id or "").strip()
    return [r for r in devices.for_scope(scope) if devices.row_stands(r)] if scope else []


def _store_out(scope: str, username: str, message_id: str, text: str, *, document: bool = False) -> None:
    from vaf.core.channel_message_store import append_message
    append_message(username=username, chat_id=devices.chat_key(scope), body=text, direction="out",
                   message_id=message_id, content_type="document" if document else "text",
                   channel=CHANNEL, user_scope_id=scope)
    now = time.time()
    for row in standing_devices(scope):
        devices.outbox_add(row["device_id"], message_id, ts=now)


def finish_reply(meta: Dict[str, Any], final_text: str) -> bool:
    """The turn ended: ``done`` with the final text to every connected device of the account
    and the reply stored once with an outbox entry per device (an absent device gets it as
    one ``msg`` after its next ``welcome``)."""
    scope = str(meta.get("user_scope_id") or "").strip()
    if not scope:
        return False
    with _lock:
        reply = _replies.pop(str(meta.get("app_reply_id") or ""), None)
    if reply is None:
        reply = Reply(id=str(uuid.uuid4()), scope=scope, username=str(meta.get("username") or ""),
                      chat_key=devices.chat_key(scope))
    meta.pop("app_reply_id", None)
    text = str(final_text or "").strip()
    chat = f"{CHANNEL}_{reply.chat_key}"
    if text:
        _store_out(scope, reply.username, reply.id, text)
    done = {"t": "done", "id": reply.id}
    if text:
        done["final_text"] = text
    reached = set(_to_account(scope, done))
    _to_account(scope, {"t": "typing", "chat": chat, "on": False})
    if text:
        for row in devices.for_scope(scope):
            if row["device_id"] not in reached:
                _went_offline(row)
    return True


def deliver(user_scope_id: Any, username: Optional[str], text: str,
            file_path: Optional[str] = None) -> bool:
    """A message VAF sends by itself (``send_to_user``, the send tool). Stored once with an
    outbox entry per device; sent to the connected devices now. True when it is stored and
    the account has a device that may still receive (an absent one gets it after its next
    ``welcome``); False when none may, so the caller falls back to the web UI. A file larger
    than the protocol's 25 MiB is refused (``file_fits``)."""
    scope = str(user_scope_id or "").strip()
    rows = standing_devices(scope)
    if not rows:
        return False
    text = str(text or "").strip()
    attach = file_path if file_path and os.path.isfile(file_path) else None
    if attach and not file_fits(attach):
        return False
    if not text and not attach:
        return False
    mid = str(uuid.uuid4())
    body = text
    if attach:
        body = (text + "\n" if text else "") + f"[File: {os.path.basename(attach)}]"
    _store_out(scope, str(username or rows[0].get("username") or ""), mid, body, document=bool(attach))
    chat = devices.chat_session_id(scope)
    message: Dict[str, Any] = {"t": "msg", "id": mid, "chat": chat, "text": text}
    xfer = None
    if attach:
        import hashlib
        digest = hashlib.sha256()
        with open(attach, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        xfer = os.urandom(pr.XFER_ID_BYTES)
        message["attachments"] = [{"xfer": pr.b64url(xfer), "name": os.path.basename(attach)[:255],
                                   "mime": mimetypes.guess_type(attach)[0] or "application/octet-stream",
                                   "size": os.path.getsize(attach), "sha256": digest.hexdigest()}]
    for row in rows:
        did = row["device_id"]
        if _send(did, message):
            if xfer is not None:
                session = session_for(did)
                if session is not None:
                    session.send_file(xfer, attach)
        else:
            _went_offline(row)
    return True


def file_fits(path: str) -> bool:
    """Whether a file can travel as one attachment (1 byte to 25 MiB)."""
    try:
        return 1 <= os.path.getsize(path) <= pr.MAX_FILE_BYTES
    except OSError:
        return False


def send_text(meta: Dict[str, Any], text: str) -> bool:
    """The plain-text answer of ``channel_reply.reply_to_chat``: the end of the running reply
    when there is one, else a message."""
    with _lock:
        running = str(meta.get("app_reply_id") or "") in _replies
    if running:
        return finish_reply(meta, text)
    return deliver(meta.get("user_scope_id"), meta.get("username"), text)


# ── the sweep ────────────────────────────────────────────────────────────────

def sweep(now: Optional[float] = None) -> None:
    """Every few seconds: sessions of devices that no longer stand are closed (a removal by
    another process, ``vaf devices remove`` without a running VAF, is seen here), handshakes and transfers that
    ran out of time end, a confirmation not given in time ends its pairing, and idle
    devices expire."""
    with _lock:
        live = list(_sessions)
        everything = list(_all)
    for did in live:
        if not devices.stands(did):
            close_device(did)
    for session in everything:
        session.check_timeouts()
    wall_now = time.time()
    with _lock:
        for rid in [r.id for r in _replies.values() if wall_now - r.started > REPLY_MAX_SECONDS]:
            del _replies[rid]
    try:
        from vaf.core.app_link import pairing
        pairing.sweep()
    except Exception:
        pass
    wall = time.time() if now is None else now
    if wall - _state["expired_at"] > EXPIRY_EVERY_SECONDS:
        _state["expired_at"] = wall
        try:
            devices.expire_idle(wall)
        except Exception:
            logger.debug("app link: expiring idle devices failed", exc_info=True)


def _run() -> None:
    while not _stop.wait(SWEEP_SECONDS):
        try:
            sweep()
        except Exception:
            logger.debug("app link: sweep failed", exc_info=True)


def start() -> None:
    """Start the sweep, once per process."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_run, name="vaf-app-link-sweep", daemon=True)
        _thread.start()


def stop() -> None:
    _stop.set()
    with _lock:
        sessions = list(_all)
    for session in sessions:
        session.close("shutdown", bye=True)


def reset() -> None:
    """Forget every session and reply (tests)."""
    from vaf.core.app_link.session import LIMITER
    LIMITER.reset()
    with _lock:
        _sessions.clear()
        _all.clear()
        _replies.clear()
        _seen_ids.clear()
        _msg_times.clear()
        _touched.clear()
        _state["expired_at"] = 0.0


devices.add_removed_listener(close_device)
