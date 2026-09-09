# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one list of conversations across every channel: the inbox.

Every place that shows "who wrote, when, and does somebody wait for me" reads the rows built
here: the agent's `inbox` tool, `vaf inbox list`, the `/api/inbox` routes behind the
Posteingang window, and the per-channel windows' own lists. The predicates are pure functions
over the store's grouped overview, so the four surfaces cannot disagree about what "unread",
"waits for you" or "done" means, and a test can pin each rule without a database.

Five sources, one row shape: the messenger chats of the channel message store (WhatsApp,
Telegram, Discord, groups included), mail threads (v2 store), and A2A rooms. The person's
own state per messenger chat lives in `channel_message_store.chat_marks`; mail keeps IMAP's
Seen flag as its read marker and rooms their cursor, both take the done mark from the same
table. Listing is store-only and never waits on a bridge: a GET must answer at once, and a
chat the store never saw has nothing to say about unread or waiting anyway.

Facade: CONSIDERED AND LEFT OFF. Every consumer is first-party (a tool, a command, a route,
a window); the public facade is a versioned promise, and the first embedder who asks for a
cross-channel inbox is the measurement that earns an export.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from vaf.core.channel_message_store import OWNER_SENDER

CHANNELS: Tuple[str, ...] = ("whatsapp", "telegram", "discord", "mail", "room")
MESSENGERS: Tuple[str, ...] = ("whatsapp", "telegram", "discord")
VIEWS: Tuple[str, ...] = ("all", "waits", "unread", "agent")

WAITS_UNANSWERED = "unanswered"
WAITS_OWNER_ASKED = "owner_asked"
WAITS_INVITATION = "invitation"

_CHANNEL_NAMES = {"whatsapp": "WhatsApp", "telegram": "Telegram", "discord": "Discord", "mail": "Mail", "room": "Room"}


# -- pure rules --------------------------------------------------------------------------------

def reply_window_until(agent_ts: Optional[float], in_within_ts: Optional[float],
                       window_seconds: float) -> Optional[float]:
    """The bridge's reply-window rule, on the overview's two inputs: the agent's own message
    opens the window, and a reply the contact sent inside it extends it. No agent message,
    no window; the person's own sends never open one (they are not `agent_ts`)."""
    if not window_seconds or window_seconds <= 0 or agent_ts is None:
        return None
    until = float(agent_ts) + float(window_seconds)
    if in_within_ts is not None and float(in_within_ts) > float(agent_ts):
        until = max(until, float(in_within_ts) + float(window_seconds))
    return until


def is_group(channel: str, chat_id: str) -> bool:
    cid = str(chat_id or "")
    if channel == "whatsapp":
        return cid.endswith("@g.us")
    if channel == "telegram":
        return cid.startswith("-")
    return channel == "room"


def chat_state(row: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
    """The person's view of one messenger chat, from one overview row.

    unread: inbound rows after the seen marker (the overview counted them).
    answered_by_agent: the newest row is the agent's own send.
    done: marked done and nothing newer arrived, or the newest row is the person's own reply.
    waits: not done, and either the agent asked the person about this chat and neither the
    person nor the agent has written since, or the newest row is the other side's and nobody
    answered. The agent's reply lifts "waits" and does not close the row: the person may still
    want to see what was said in their name."""
    last_ts = float(row.get("last_ts") or 0.0)
    last_direction = row.get("last_direction") or ""
    last_sender = row.get("last_sender") or ""
    done_ts = row.get("done_ts")
    owner_asked_ts = row.get("owner_asked_ts")
    last_agent_ts = row.get("last_agent_ts")
    last_owner_ts = row.get("last_owner_ts")
    newest_is_owner = last_direction == "out" and last_sender == OWNER_SENDER
    newest_is_agent = last_direction == "out" and last_sender != OWNER_SENDER
    done = newest_is_owner or (done_ts is not None and float(done_ts) >= last_ts)
    floor = max(float(done_ts or 0.0), float(last_owner_ts or 0.0), float(last_agent_ts or 0.0))
    owner_asked_pending = owner_asked_ts is not None and float(owner_asked_ts) > floor
    waits_reason = ""
    if not done:
        if owner_asked_pending:
            waits_reason = WAITS_OWNER_ASKED
        elif last_direction == "in":
            waits_reason = WAITS_UNANSWERED
    preview_from = "you" if newest_is_owner else ("agent" if newest_is_agent else "them")
    return {
        "unread": int(row.get("unread") or 0),
        "waits": bool(waits_reason),
        "waits_reason": waits_reason,
        "answered_by_agent": newest_is_agent,
        "done": done,
        "preview_from": preview_from,
    }


def chat_mode(channel: str, chat_id: str, *, owners: Set[str], contacts: Set[str], relays: Set[str],
              reply_window_until_ts: Optional[float], now: float, needs_assign: bool = False) -> str:
    """Which lane answers in this chat, the words the channel windows already use."""
    if channel == "discord":
        return "admin"
    if needs_assign:
        return "needs_assign"
    cid = str(chat_id or "")
    if cid in owners:
        return "owner"
    if channel == "telegram" and cid in relays:
        return "relay"
    if cid in contacts:
        return "contact"
    if channel == "whatsapp" and reply_window_until_ts is not None and reply_window_until_ts > now:
        return "conversation"
    return "readonly"


def mail_thread_state(thread: Dict[str, Any], mark: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A mail thread's state: unread is IMAP's count, the last word was ours when the newest
    message sits in the Sent folder, and the thread waits when it is not done, the last word
    was the correspondent's and nobody marked it answered."""
    newest_in_sent = str(thread.get("newest_special_use") or "").lower() == "\\sent"
    answered = bool(thread.get("newest_answered_at")) or int(thread.get("answered") or 0) > 0
    last_ts = float(thread.get("last_date_ts") or 0.0)
    done_ts = (mark or {}).get("done_ts")
    done = newest_in_sent or (done_ts is not None and float(done_ts) >= last_ts)
    waits = (not done) and (not answered)
    return {
        "unread": int(thread.get("unread_count") or 0),
        "waits": waits,
        "waits_reason": WAITS_UNANSWERED if waits else "",
        "answered_by_agent": answered,
        "done": done,
        "preview_from": "you" if newest_in_sent else "them",
    }


def room_state(room: Dict[str, Any], mark: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A room's state: unread is the person's own reading position; an invitation waits for
    an answer; a room with unread frames waits until the person looked, or marked it done."""
    last_ts = float(room.get("last_ts") or 0.0)
    done_ts = (mark or {}).get("done_ts")
    done = done_ts is not None and float(done_ts) >= last_ts and not room.get("invited")
    unread = int(room.get("unread") or 0)
    if room.get("invited"):
        reason = WAITS_INVITATION
    elif not done and unread > 0:
        reason = WAITS_UNANSWERED
    else:
        reason = ""
    last = room.get("last") or {}
    return {
        "unread": unread,
        "waits": bool(reason),
        "waits_reason": reason,
        "answered_by_agent": False,
        "done": done,
        "preview_from": "you" if last.get("mine") else str(last.get("sender") or "them"),
    }


# -- identity ---------------------------------------------------------------------------------

def _row_username(channel: str, username: Optional[str]) -> str:
    from vaf.core.contacts_store import message_channel_username
    return message_channel_username(channel, username)


def _local_admin(username: Optional[str], user_scope_id: Optional[str]) -> bool:
    from vaf.core.contacts_store import is_local_admin_caller
    return is_local_admin_caller(username, user_scope_id)


def _display_name(channel: str, chat_id: str, chat_name: str, username: Optional[str],
                  user_scope_id: Optional[str]) -> str:
    name = (chat_name or "").strip()
    if name:
        return name
    if channel == "whatsapp" and str(chat_id).startswith("+"):
        try:
            from vaf.core.contacts_store import get_contact_name_by_phone
            found = get_contact_name_by_phone(chat_id, username, user_scope_id=user_scope_id)
            if found and found.strip():
                return found.strip()
        except Exception:
            pass
    return str(chat_id or "")


def _session_id(channel: str, chat_id: str, username: Optional[str]) -> str:
    if channel == "whatsapp":
        from vaf.core.messaging_connections import whatsapp_session_id
        return whatsapp_session_id(username, chat_id, fallback="")
    return f"{channel}_{chat_id}"


def _lid_needs_assign(chat_id: str) -> bool:
    cid = str(chat_id or "")
    if "@lid" not in cid:
        return False
    try:
        from vaf.core.config import Config
        wc = Config.get("whatsapp_config") or {}
        mapping = dict((wc.get("lid_to_e164") or {}) if isinstance(wc, dict) else {})
    except Exception:
        mapping = {}
    return not (mapping.get(cid) or "").strip()


# -- lanes ------------------------------------------------------------------------------------

def _messenger_rows(username: Optional[str], user_scope_id: Optional[str], channels: Iterable[str],
                    *, now: float) -> List[Dict[str, Any]]:
    from vaf.core.channel_message_store import chat_overview, store_exists
    from vaf.core.messaging_connections import owner_endpoints, reply_window_hours
    rows: List[Dict[str, Any]] = []
    window = reply_window_hours() * 3600.0
    for channel in channels:
        if channel not in MESSENGERS:
            continue
        if channel == "discord" and not _local_admin(username, user_scope_id):
            continue
        row_user = _row_username(channel, username)
        scope = user_scope_id if channel != "discord" else None
        if not store_exists(row_user, scope):
            continue
        try:
            overview = chat_overview(row_user, user_scope_id=scope, channel=channel, limit=500,
                                     reply_window_seconds=window if channel == "whatsapp" else None)
        except Exception:
            continue
        owners = owner_endpoints(channel, username, user_scope_id)
        relays = owner_endpoints(channel, username, user_scope_id, relay=True) if channel == "telegram" else set()
        try:
            from vaf.core.contacts_store import front_office_endpoints
            contacts = set(front_office_endpoints(username, user_scope_id, channel) or ())
        except Exception:
            contacts = set()
        for o in overview:
            chat_id = str(o.get("chat_id") or "")
            state = chat_state(o, now=now)
            until = reply_window_until(o.get("last_agent_ts"), o.get("last_in_within_ts"), window) \
                if channel == "whatsapp" else None
            needs_assign = channel == "whatsapp" and _lid_needs_assign(chat_id)
            mode = chat_mode(channel, chat_id, owners=owners, contacts=contacts, relays=relays,
                             reply_window_until_ts=until, now=now, needs_assign=needs_assign)
            rows.append({
                "key": f"{channel}:{chat_id}",
                "channel": channel,
                "id": chat_id,
                "name": _display_name(channel, chat_id, o.get("chat_name") or "", username, user_scope_id),
                "preview": o.get("last_body") or "",
                "preview_from": state["preview_from"],
                "last_ts": float(o.get("last_ts") or 0.0),
                "message_count": int(o.get("message_count") or 0),
                "unread": state["unread"],
                "waits": state["waits"],
                "waits_reason": state["waits_reason"],
                "answered_by_agent": state["answered_by_agent"],
                "done": state["done"],
                "is_group": is_group(channel, chat_id),
                "mode": mode,
                "reply_window_until": until,
                "can_compose": channel == "whatsapp" and not needs_assign and mode == "readonly",
                "session_id": _session_id(channel, chat_id, username),
                "jump": {"channel": channel, "chat_id": chat_id},
            })
    return rows


def _mail_rows(username: Optional[str], user_scope_id: Optional[str], *, limit: int) -> List[Dict[str, Any]]:
    from vaf.tools.mail_utils import mail_v2_active
    if not mail_v2_active(username or "", user_scope_id) or not user_scope_id:
        return []
    from vaf.mail.store import MailStore
    if not MailStore.exists(user_scope_id):
        return []
    from vaf.core.channel_message_store import chat_marks
    from vaf.mail.service import MailService
    svc = MailService(user_scope_id)
    marks = chat_marks(username or "", user_scope_id, channel="mail")
    rows: List[Dict[str, Any]] = []
    for t in svc.list_threads(limit=min(max(int(limit), 1), 200)):
        thread_id = str(t.get("thread_id"))
        state = mail_thread_state(t, marks.get(("mail", thread_id)))
        rows.append({
            "key": f"mail:{thread_id}",
            "channel": "mail",
            "id": thread_id,
            "name": (t.get("from_addr") or t.get("subject") or "").strip() or thread_id,
            "subject": t.get("subject") or "",
            "preview": (t.get("snippet") or t.get("subject") or "")[:160],
            "preview_from": state["preview_from"],
            "last_ts": float(t.get("last_date_ts") or 0.0),
            "message_count": int(t.get("message_count") or 0),
            "unread": state["unread"],
            "waits": state["waits"],
            "waits_reason": state["waits_reason"],
            "answered_by_agent": state["answered_by_agent"],
            "done": state["done"],
            "is_group": False,
            "mode": "mail",
            "reply_window_until": None,
            "can_compose": False,
            "session_id": "",
            "jump": {"channel": "mail", "thread_id": thread_id, "account_id": t.get("acct"),
                     "folder": t.get("newest_folder"), "message_id": t.get("newest_message_id"),
                     "message_pk": t.get("newest_pk")},
        })
    return rows


def _room_lane(username: Optional[str], user_scope_id: Optional[str]) -> List[Dict[str, Any]]:
    from vaf.core.channel_message_store import chat_marks
    from vaf.core.session import _room_rows
    marks = chat_marks(username or "", user_scope_id, channel="room")
    rows: List[Dict[str, Any]] = []
    for r in _room_rows(user_scope_id):
        room_id = str(r.get("room_id") or "")
        state = room_state(r, marks.get(("room", room_id)))
        last = r.get("last") or {}
        rows.append({
            "key": f"room:{room_id}",
            "channel": "room",
            "id": room_id,
            "name": r.get("name") or room_id,
            "preview": str(last.get("text") or ""),
            "preview_from": state["preview_from"],
            "last_ts": float(r.get("last_ts") or 0.0),
            "message_count": int(r.get("message_count") or 0),
            "unread": state["unread"],
            "waits": state["waits"],
            "waits_reason": state["waits_reason"],
            "answered_by_agent": False,
            "done": state["done"],
            "is_group": True,
            "mode": "room",
            "reply_window_until": None,
            "can_compose": False,
            "session_id": "",
            "members": int(r.get("members") or 0),
            "invited": bool(r.get("invited")),
            "jump": {"channel": "room", "room_id": room_id},
        })
    return rows


# -- the list ---------------------------------------------------------------------------------

def search_hits(username: Optional[str], user_scope_id: Optional[str], query: str,
                channels: Iterable[str]) -> Set[str]:
    """Row keys whose stored messages match the query, in every lane the caller asked for."""
    q = (query or "").strip()
    if not q:
        return set()
    hits: Set[str] = set()
    wanted = set(channels)
    if wanted & set(MESSENGERS):
        from vaf.core.channel_message_store import search_messages, store_exists
        for channel in MESSENGERS:
            if channel not in wanted:
                continue
            if channel == "discord" and not _local_admin(username, user_scope_id):
                continue
            row_user = _row_username(channel, username)
            scope = user_scope_id if channel != "discord" else None
            if not store_exists(row_user, scope):
                continue
            try:
                for m in search_messages(row_user, q, limit=100, user_scope_id=scope, channel=channel):
                    hits.add(f"{channel}:{m.get('chat_id')}")
            except Exception:
                continue
    if "mail" in wanted and user_scope_id:
        try:
            from vaf.tools.mail_utils import mail_v2_active
            from vaf.mail.store import MailStore
            if mail_v2_active(username or "", user_scope_id) and MailStore.exists(user_scope_id):
                from vaf.mail.service import MailService
                for m in MailService(user_scope_id).search(q, limit=100):
                    if m.get("thread_id") is not None:
                        hits.add(f"mail:{m['thread_id']}")
        except Exception:
            pass
    return hits


def list_conversations(username: Optional[str], user_scope_id: Optional[str], *,
                       channels: Optional[Iterable[str]] = None, view: str = "all",
                       include_groups: bool = True, include_done: bool = False, query: str = "",
                       limit: int = 200, now: Optional[float] = None) -> Dict[str, Any]:
    """Every conversation of this person, newest first, with the counts the rail shows.

    `channels` narrows the lanes (default all five); `view` is one of VIEWS; the group and
    done toggles apply before the counts, the view after them, so the rail's numbers describe
    what the toggles allow. `query` keeps rows whose name or preview contain it, or whose
    stored messages match (`search_hits`)."""
    now = float(now if now is not None else time.time())
    wanted = tuple(c for c in (channels or CHANNELS) if c in CHANNELS) or CHANNELS
    view = view if view in VIEWS else "all"
    rows: List[Dict[str, Any]] = []
    rows.extend(_messenger_rows(username, user_scope_id, wanted, now=now))
    if "mail" in wanted:
        try:
            rows.extend(_mail_rows(username, user_scope_id, limit=max(int(limit), 50)))
        except Exception:
            pass
    if "room" in wanted:
        try:
            rows.extend(_room_lane(username, user_scope_id))
        except Exception:
            pass
    if not include_groups:
        rows = [r for r in rows if not r["is_group"]]
    if not include_done:
        rows = [r for r in rows if not r["done"]]
    q = (query or "").strip().lower()
    if q:
        hits = search_hits(username, user_scope_id, q, wanted)
        rows = [r for r in rows
                if q in (r["name"] or "").lower() or q in (r["preview"] or "").lower()
                or q in (r.get("subject") or "").lower() or r["key"] in hits]
    rows.sort(key=lambda r: r["last_ts"], reverse=True)
    counts = {
        "all": len(rows),
        "waits": sum(1 for r in rows if r["waits"]),
        "unread": sum(r["unread"] for r in rows),
        "agent": sum(1 for r in rows if r["answered_by_agent"]),
        "per_channel": {c: sum(1 for r in rows if r["channel"] == c) for c in CHANNELS},
        "waits_per_channel": {c: sum(1 for r in rows if r["channel"] == c and r["waits"]) for c in CHANNELS},
    }
    if view == "waits":
        rows = [r for r in rows if r["waits"]]
    elif view == "unread":
        rows = [r for r in rows if r["unread"] > 0]
    elif view == "agent":
        rows = [r for r in rows if r["answered_by_agent"]]
    return {"rows": rows[: max(int(limit), 1)], "counts": counts, "channels": list(wanted)}


# -- marks and history ------------------------------------------------------------------------

def mark_conversation(username: Optional[str], user_scope_id: Optional[str], channel: str, chat_id: str,
                      *, seen: bool = False, done: Optional[bool] = None) -> Dict[str, Any]:
    """The person opened a conversation (`seen`) or marked it done / not done (`done`).

    Messenger chats write `chat_marks`; a mail thread's seen goes to every unread message of
    the thread (IMAP's flag stays the read marker, the mail window's own rule moved
    server-side) and its done to `chat_marks`; a room's seen is refused because opening the
    room moves the cursor, its done goes to `chat_marks`."""
    from vaf.core.channel_message_store import mark_done, mark_seen
    channel = (channel or "").strip().lower()
    chat_id = str(chat_id or "").strip()
    if channel not in CHANNELS or not chat_id:
        raise ValueError("unknown conversation")
    out: Dict[str, Any] = {"channel": channel, "id": chat_id}
    if channel in MESSENGERS:
        row_user = _row_username(channel, username)
        scope = user_scope_id if channel != "discord" else None
        if seen:
            out["seen_ts"] = mark_seen(row_user, channel, chat_id, user_scope_id=scope)
        if done is not None:
            mark_done(row_user, channel, chat_id, user_scope_id=scope, done=bool(done))
            out["done"] = bool(done)
        return out
    if channel == "mail":
        if seen and user_scope_id:
            from vaf.mail.service import MailService
            svc = MailService(user_scope_id)
            for m in svc.thread_messages(int(chat_id)):
                if "\\Seen" not in (m.get("flags") or []):
                    svc.mark_read(int(m["id"]), True)
            out["seen"] = True
        if done is not None:
            mark_done(username or "", "mail", chat_id, user_scope_id=user_scope_id, done=bool(done))
            out["done"] = bool(done)
        return out
    if seen:
        raise ValueError("a room is read by opening it")
    if done is not None:
        mark_done(username or "", "room", chat_id, user_scope_id=user_scope_id, done=bool(done))
        out["done"] = bool(done)
    return out


def _stamp(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    from datetime import datetime
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


def conversation_history(username: Optional[str], user_scope_id: Optional[str], channel: str, chat_id: str,
                         limit: int = 200) -> List[Dict[str, Any]]:
    """One conversation, oldest first, in the shape the channel windows' pane reads:
    role (`user` = the other side, `assistant` = what left on our behalf), content, timestamp,
    content_type, and `sender` (`them`, `agent`, `you`, or a room member's label)."""
    channel = (channel or "").strip().lower()
    chat_id = str(chat_id or "").strip()
    limit = min(max(int(limit or 1), 1), 200)
    if channel in MESSENGERS:
        from vaf.core.channel_message_store import get_chat_messages, store_exists
        if channel == "discord" and not _local_admin(username, user_scope_id):
            return []
        row_user = _row_username(channel, username)
        scope = user_scope_id if channel != "discord" else None
        if not store_exists(row_user, scope):
            return []
        rows = get_chat_messages(row_user, chat_id, limit=limit, user_scope_id=scope, channel=channel)
        out = []
        for r in sorted(rows, key=lambda r: float(r.get("ts") or 0)):
            outbound = (r.get("direction") or "in") == "out"
            sender = "you" if (outbound and r.get("sender_jid") == OWNER_SENDER) else ("agent" if outbound else "them")
            out.append({"role": "assistant" if outbound else "user", "content": (r.get("body") or "")[:2000],
                        "timestamp": _stamp(r.get("ts")), "content_type": r.get("content_type") or "text",
                        "sender": sender})
        return out
    if channel == "mail":
        if not user_scope_id:
            return []
        from vaf.mail.service import MailService
        out = []
        for m in MailService(user_scope_id).thread_messages(int(chat_id))[-limit:]:
            mine = str(m.get("folder_special_use") or "").lower() == "\\sent"
            text = (m.get("snippet") or m.get("subject") or "")[:2000]
            out.append({"role": "assistant" if mine else "user", "content": text,
                        "timestamp": _stamp(m.get("date_ts") or m.get("internaldate_ts")),
                        "content_type": "mail", "sender": "you" if mine else (m.get("from_addr") or "them")})
        return out
    if channel == "room":
        from vaf.core.a2a.room import Room, derive_peer_id, participant_key
        room = Room.open(chat_id)
        human = derive_peer_id(participant_key("cli", user_scope_id), chat_id)
        out = []
        for line in room.transcript()[-limit:]:
            mine = line.get("sender") == human or line.get("peer") == human
            out.append({"role": "assistant" if mine else "user", "content": str(line.get("text") or "")[:2000],
                        "timestamp": _stamp(line.get("ts")), "content_type": str(line.get("kind") or "text"),
                        "sender": "you" if mine else str(line.get("label") or line.get("sender") or "them")})
        return out
    return []


def channel_label(channel: str) -> str:
    return _CHANNEL_NAMES.get(channel, channel.title() if channel else "")
