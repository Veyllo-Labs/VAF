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

A request of the app's own agent (``agent_ask``, protocol section 10) is a turn of the same
chat with two differences. Its text is another agent's (vaf/core/agent_origin.py: marked,
fenced, the tools that need the person off), so it is not stored as the person's message.
And it is answered on the DEVICE that asked, never the account: ``delta`` and ``done`` carry
the request's own id and go to that device only, with no ``read`` and no ``typing``, the
stored answer is put into that device's outbox only, and only that device is notified. The
device is addressed by its id at every send, so after a drop the answer reaches the session
it connected again with (the app sends the same id again and gets ``ack``). A device holds
at most ``AGENT_ASK_OPEN_MAX`` requests queued or running; one that ends without a reply (a
stop, a cancel, a removal) is answered with ``done`` and its place freed by the sweep. A
request whose turn parked a call for the user's confirmation stays open, with its place,
until the card's outcome answers it (vaf/core/app_link/confirmations.py).

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
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

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
# Keyed by (account scope, reply id): a request's reply id is the id its device chose, so
# two accounts may use the same one.
_replies: Dict[Tuple[str, str], "Reply"] = {}
_asks: Dict[str, Dict[str, "OpenAsk"]] = {}  # device_id -> ask id -> the open request
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
    # A request of the app's agent: answered on the device that asked, with its id.
    ask: bool = False
    device_id: str = ""
    # The card a request waits on (confirmations): its turn ended, its answer has not.
    awaiting: str = ""
    # An answer the request already has while a further card waits (a confirmed turn's): it
    # leads the final `done`, so the app shows every outcome of the request.
    carried: str = ""


@dataclass
class OpenAsk:
    chat_sid: str
    scope: str = ""
    started: float = field(default_factory=time.time)
    # Set once the turn is in the queue: until then the sweep cannot tell it from a dropped one.
    queued: bool = False


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


def _rkey(scope: Any, reply_id: Any) -> Tuple[str, str]:
    return (str(scope or "").strip(), str(reply_id or ""))


def _device_of(scope: str, device_id: str) -> bool:
    """Whether the device row exists and belongs to the account ``scope``."""
    row = devices.get(device_id) if device_id else None
    return bool(row) and str(row.get("user_scope_id") or "") == str(scope)


def _to_reply(reply: "Reply", obj: Dict[str, Any]) -> List[str]:
    """A reply's message: to the device that asked for an agent's request (by its id, so a
    device that reconnected gets it on its new session; nobody when none is named or the
    device is not the account's), to every device of the account otherwise."""
    if reply.ask:
        if not _device_of(reply.scope, reply.device_id):
            return []
        return [reply.device_id] if _send(reply.device_id, obj) else []
    return _to_account(reply.scope, obj)


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
    # `agent_ask` is always taken (protocol section 10: no switch); the protocol's reserved
    # switch message and refusal code for it are never sent. `confirm`: this VAF sends
    # confirmation cards (section 12) and takes `confirm_answer`.
    session.send_json({"t": "welcome", "v": pr.PROTOCOL_VERSION,
                       "chat": devices.chat_session_id(scope), "account": row.get("username", ""),
                       "agent": agent or "VAF", "server_time": int(time.time()),
                       "agent_ask": True, "confirm": True})
    _came_online(did)
    replay(session)
    from vaf.core.app_link import confirmations
    confirmations.resend(session)


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
    if t in ("msg", "ack", "agent_ask", "confirm_answer"):
        _touch_now_and_then(did, session)
    if t == "msg":
        _on_msg(session, row, message, files)
    elif t == "agent_ask":
        _on_agent_ask(session, row, message)
    elif t == "confirm_answer":
        from vaf.core.app_link import confirmations
        confirmations.on_answer(session, row, message)
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
        if message.get("code") == "unknown_type" and message.get("ref") == "confirm":
            # An app without cards: the type, not a card id, comes back, so every card of
            # this connection is answered as refused (confirmations.refused_by_app).
            from vaf.core.app_link import confirmations
            confirmations.refused_by_app(session)


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


def _take_ask_slot(device_id: str, ask_id: str, chat_sid: str, scope: str = "") -> bool:
    """Hold one of the device's places for open requests, or False when all are taken or
    this id holds one already (two copies of one request arriving at once queue once)."""
    from vaf.core.app_link.session import AGENT_ASK_OPEN_MAX
    with _lock:
        held = _asks.setdefault(device_id, {})
        if ask_id in held or len(held) >= AGENT_ASK_OPEN_MAX:
            return False
        held[ask_id] = OpenAsk(chat_sid=chat_sid, scope=scope)
        return True


def _release_ask(device_id: str, ask_id: str) -> bool:
    """Free a request's place. Whether it was held."""
    with _lock:
        held = _asks.get(device_id)
        if not held or held.pop(ask_id, None) is None:
            return False
        if not held:
            del _asks[device_id]
        return True


def _id_taken(row: Dict[str, Any], ask_id: str) -> bool:
    """Whether a request id already names a reply of the account, running, open on any of its
    devices or stored: an answer stored under it would replace that one."""
    scope = str(row["user_scope_id"])
    with _lock:
        if _rkey(scope, ask_id) in _replies:
            return True
        open_ids = {aid for did, asks in _asks.items()
                    for aid, ask in asks.items() if ask.scope == scope}
        if ask_id in open_ids:
            return True
    try:
        from vaf.core import channel_message_store as store
        return store.get_message(row.get("username") or "", devices.chat_key(row["user_scope_id"]),
                                 ask_id, direction="out", user_scope_id=row["user_scope_id"],
                                 channel=CHANNEL) is not None
    except Exception:
        return False


def _on_agent_ask(session, row, message) -> None:
    """A task the app's own agent gives VAF: one turn of the account's chat, marked as an
    agent's request (``app_agent_ask``), answered on this device (module docstring)."""
    from vaf.core.app_link import settings
    scope = row["user_scope_id"]
    username = row.get("username") or ""
    did = row["device_id"]
    key = devices.chat_key(scope)
    chat_sid = f"{CHANNEL}_{key}"
    aid = message["id"]
    chat = message.get("chat") or ""
    if chat and chat not in (chat_sid, key):
        session.send_json({"t": "error", "code": "wrong_chat", "ref": aid})
        return
    # Seen, queued or running already: the app sends the same id again after a drop.
    if _seen_before(did, aid) or _id_taken(row, aid):
        session.send_json({"t": "ack", "id": aid})
        return
    if not _take_ask_slot(did, aid, chat_sid, scope):
        with _lock:
            again = aid in _asks.get(did, {})
        # The same request arriving twice at once: the copy that holds the place is the one.
        session.send_json({"t": "ack", "id": aid} if again
                          else {"t": "error", "code": "rate_limited", "ref": aid})
        return
    if not _rate_ok(did):
        _release_ask(did, aid)
        session.send_json({"t": "error", "code": "rate_limited", "ref": aid})
        return
    from vaf.core.channel_ingress_policy import evaluate_ingress
    from vaf.core.config import Config
    allowed, _reason = evaluate_ingress(CHANNEL, Config.get("channel_ingress_policy"),
                                        explicit_match=True)
    if not allowed or not settings().get("enabled", True):
        _release_ask(did, aid)
        session.send_json({"t": "error", "code": "refused", "ref": aid})
        return
    # Not stored as a message of the person (no inbox entry, no unread count, no hit of the
    # read tools): the chat's history holds it, marked as the app agent's.
    try:
        from vaf.core.task_queue import TaskQueue
        TaskQueue().add(session_id=chat_sid, input_text=message["text"], source=CHANNEL,
                        metadata={"user_scope_id": scope, "username": username,
                                  "origin_channel": CHANNEL, "task_class": "interactive",
                                  "app_device_id": did, "app_agent_ask": True,
                                  "app_ask_id": aid})
    except Exception:
        _release_ask(did, aid)
        raise
    with _lock:
        held = _asks.get(did, {}).get(aid)
        if held is not None:
            held.queued = True
    _remember(did, aid)
    session.send_json({"t": "ack", "id": aid})


def _drop_queued_asks(chat_sid: str, accept: Callable[[Dict[str, Any]], bool]) -> int:
    """Take the queued requests ``accept(metadata)`` names out of the queue, free their places
    and answer each with ``done`` (no text) on its device. How many were dropped."""
    from vaf.core.task_queue import TaskQueue
    dropped: Dict[tuple, None] = {}

    def _where(task) -> bool:
        meta = task.metadata or {}
        if not meta.get("app_agent_ask") or not accept(meta):
            return False
        dropped[(str(meta.get("app_device_id") or ""), str(meta.get("app_ask_id") or ""))] = None
        return True

    TaskQueue().drop_queued_tasks_for_session(chat_sid, where=_where)
    for did, aid in dropped:
        _release_ask(did, aid)
        if did and aid:
            _send(did, {"t": "done", "id": aid})
    return len(dropped)


def _on_cancel(row: Dict[str, Any], ref: str) -> None:
    """Stop a running reply of the account (an agent's request only from the device that
    asked), or drop a queued request of this device. Another device cannot touch a request."""
    scope = row["user_scope_id"]
    did = row["device_id"]
    chat_sid = devices.chat_session_id(scope)
    # A request whose card waits: no turn runs, so nothing is stopped (a stop would hit
    # whatever else the chat runs); the card is settled instead.
    from vaf.core.app_link import confirmations
    if confirmations.cancel(row, ref):
        return
    with _lock:
        running = [r for r in _replies.values() if r.scope == scope
                   and ref in (r.id, r.app_message_id)
                   and (not r.ask or r.device_id == did)]
        queued_mine = ref in _asks.get(did, {})
    if running:
        from vaf.core import revocation
        revocation.stop_session(chat_sid, include_subagents=False)
        return
    if queued_mine:
        _drop_queued_asks(chat_sid, lambda meta: meta.get("app_ask_id") == ref
                          and meta.get("app_device_id") == did)


# ── the turn's reply ─────────────────────────────────────────────────────────

def _reply_for(meta: Dict[str, Any], scope: str) -> Reply:
    """The reply of a turn: an agent's request answers under its own id on the device that
    asked; every other turn under a new id, to the account."""
    username = str(meta.get("username") or "")
    key = devices.chat_key(scope)
    if meta.get("app_agent_ask"):
        return Reply(id=str(meta.get("app_ask_id") or uuid.uuid4()), scope=scope,
                     username=username, chat_key=key, ask=True,
                     device_id=str(meta.get("app_device_id") or ""))
    return Reply(id=str(uuid.uuid4()), scope=scope, username=username, chat_key=key,
                 app_message_id=str(meta.get("app_message_id") or ""))


def turn_started(meta: Dict[str, Any]) -> Optional[str]:
    """A queued turn of the account's chat starts: ``read`` for the message that started it
    and ``typing on``. Mints the reply id and remembers it in ``meta`` (``app_reply_id``).
    An agent's request answers under its own id and gets neither: the asking agent waits for
    its ``done``."""
    scope = str(meta.get("user_scope_id") or "").strip()
    if not scope:
        return None
    if meta.get("app_agent_ask") and meta.get("app_confirm_id"):
        # The turn a confirmation woke answers the request that waited on it: under the same
        # reply, which streams nothing more (its `done` carries the answer the app shows in
        # place of what it streamed before the card).
        with _lock:
            waiting = _replies.get(_rkey(scope, meta.get("app_ask_id")))
            if waiting is not None and waiting.ask and waiting.awaiting \
                    and waiting.awaiting == meta.get("app_confirm_id") \
                    and waiting.device_id == str(meta.get("app_device_id") or ""):
                waiting.awaiting = ""
                waiting.streaming = False
                meta["app_reply_id"] = waiting.id
                return waiting.id
    reply = _reply_for(meta, scope)
    with _lock:
        _replies[_rkey(scope, reply.id)] = reply
    meta["app_reply_id"] = reply.id
    if reply.ask:
        return reply.id
    key = reply.chat_key
    chat = f"{CHANNEL}_{key}"
    if reply.app_message_id:
        _to_account(scope, {"t": "read", "id": reply.app_message_id})
    _to_account(scope, {"t": "typing", "chat": chat, "on": True})
    return reply.id


def stream_round(reply_id: Optional[str], text: str, number: int, *, scope: Any = "") -> None:
    """One round's full text so far: the increment of the cleaned accumulated text goes out
    as a ``delta``. A text that no longer extends what was sent (a clear, a retraction)
    stops the deltas of this reply; ``done`` still carries the final text."""
    with _lock:
        reply = _replies.get(_rkey(scope, reply_id))
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
    _to_reply(reply, {"t": "delta", "id": reply.id, "seq": reply.seq, "text": increment})


def standing_devices(user_scope_id: Any) -> List[Dict[str, Any]]:
    """The account's devices that may still receive (removed, switched-off and long-unused
    ones excluded), connected or not."""
    scope = str(user_scope_id or "").strip()
    return [r for r in devices.for_scope(scope) if devices.row_stands(r)] if scope else []


def _store_out(scope: str, username: str, message_id: str, text: str, *, document: bool = False,
               device_id: Optional[str] = None) -> None:
    """Stored once; an outbox entry for every standing device, or for ``device_id`` only."""
    from vaf.core.channel_message_store import append_message
    append_message(username=username, chat_id=devices.chat_key(scope), body=text, direction="out",
                   message_id=message_id, content_type="document" if document else "text",
                   channel=CHANNEL, user_scope_id=scope)
    now = time.time()
    for row in standing_devices(scope):
        if device_id is None or row["device_id"] == device_id:
            devices.outbox_add(row["device_id"], message_id, ts=now)


def finish_reply(meta: Dict[str, Any], final_text: str) -> bool:
    """The turn ended: ``done`` with the final text to every connected device of the account
    and the reply stored once with an outbox entry per device (an absent device gets it as
    one ``msg`` after its next ``welcome``). An agent's request: all of that for the device
    that asked only, under the request's id, and its place freed."""
    scope = str(meta.get("user_scope_id") or "").strip()
    if not scope:
        return False
    with _lock:
        reply = _replies.pop(_rkey(scope, meta.get("app_reply_id")), None)
    if reply is None:
        reply = _reply_for(meta, scope)
    meta.pop("app_reply_id", None)
    text = "\n\n".join(t for t in (reply.carried.strip(), str(final_text or "").strip()) if t)
    chat = f"{CHANNEL}_{reply.chat_key}"
    if text:
        _store_out(scope, reply.username, reply.id, text,
                   device_id=reply.device_id if reply.ask else None)
    done = {"t": "done", "id": reply.id}
    if text:
        done["final_text"] = text
    reached = set(_to_reply(reply, done))
    if not reply.ask:
        _to_account(scope, {"t": "typing", "chat": chat, "on": False})
    if text:
        for row in devices.for_scope(scope):
            if reply.ask and row["device_id"] != reply.device_id:
                continue
            if row["device_id"] not in reached:
                _went_offline(row)
    if reply.ask:
        _release_ask(reply.device_id, reply.id)
        # Answered once: a later text of the same turn (an error after the answer) goes
        # nowhere rather than to every device.
        meta["app_ask_closed"] = True
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
    when there is one, else a message. An agent's request is always answered as its reply,
    on the device that asked (a turn that failed before its reply started included), and
    never twice."""
    with _lock:
        running = _rkey(meta.get("user_scope_id"), meta.get("app_reply_id")) in _replies
    if running:
        return finish_reply(meta, text)
    if meta.get("app_agent_ask"):
        if meta.get("app_ask_closed"):
            return False
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
        for key in [k for k, r in _replies.items() if wall_now - r.started > REPLY_MAX_SECONDS]:
            del _replies[key]
    try:
        from vaf.core.app_link import confirmations
        confirmations.settle(wall_now)
    except Exception:
        logger.debug("app link: settling confirmations failed", exc_info=True)
    settle_asks(wall_now)
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


def settle_asks(now: Optional[float] = None) -> int:
    """Requests of the app's agent that ended without a reply: no longer queued, taken by a
    worker or replying (the web UI's stop, a removal, a turn that never started), or open
    longer than ``REPLY_MAX_SECONDS``. Each is answered with ``done`` on its device and its
    place freed. How many."""
    from vaf.core.task_queue import TaskQueue
    wall = time.time() if now is None else now
    with _lock:
        held = [(did, aid, ask) for did, asks in _asks.items() for aid, ask in asks.items()]
        replying = set(_replies)
    ended = 0
    for did, aid, ask in held:
        if wall - ask.started <= REPLY_MAX_SECONDS:
            if _rkey(ask.scope, aid) in replying or not ask.queued:
                continue
            if TaskQueue().has_queued(ask.chat_sid, in_flight=True, where=lambda t, d=did, a=aid: (
                    (t.metadata or {}).get("app_ask_id") == a
                    and (t.metadata or {}).get("app_device_id") == d)):
                continue
        if _release_ask(did, aid):
            ended += 1
            _send(did, {"t": "done", "id": aid})
    return ended


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
        _asks.clear()
        _seen_ids.clear()
        _msg_times.clear()
        _touched.clear()
        _state["expired_at"] = 0.0
    from vaf.core.app_link import confirmations
    confirmations.reset()


devices.add_removed_listener(close_device)
