# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The agent's inbox: every conversation across every channel, the same rows the person sees.

One tool replaces the four per-channel listings (whatsapp_inbox, telegram_inbox, discord_inbox,
mail_inbox): the rows come from `vaf/core/inbox.py`, so the agent and the inbox window cannot
disagree about who wrote, when, what is unread and who waits for an answer. Reading a
conversation stays with the per-channel read tools and `room_read`; searching mail stays with
`find_mail`. Store-only: no bridge is asked, so the answer never waits on one.
"""
from datetime import datetime
from typing import Any, Dict, List

from vaf.tools.base import BaseTool

_MODE_WORDS = {
    "owner": "your own chat", "contact": "Front Office", "conversation": "reply window open",
    "readonly": "read-only", "needs_assign": "unassigned @lid", "admin": "admin", "relay": "relay",
    "mail": "", "room": "room",
}
_READ_TOOL = {"whatsapp": "read_whatsapp_chat", "telegram": "read_telegram_chat", "discord": "read_discord_chat"}

# Leading hint so a weak model chains the tools instead of re-listing the inbox: it names
# the next steps up front and forbids the re-call loop a 4B model fell into on mail_inbox.
_NEXT_STEP_HINT = (
    "NEXT STEP - to READ one conversation call read_whatsapp_chat / read_telegram_chat / "
    "read_discord_chat with its chat_id, read_mail with the IDs block below, or room_read with "
    "its room_id. To SEARCH mail call find_mail. Do NOT call inbox again for the same request.\n\n"
)


def _when(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _line(i: int, row: Dict[str, Any]) -> str:
    from vaf.core.inbox import channel_label
    channel = row["channel"]
    parts = [f"{i}. [{channel_label(channel)}] {row.get('name') or row['id']}"]
    if channel == "mail":
        parts.append(f"subject: {(row.get('subject') or '')[:60]}")
    elif channel == "room":
        parts.append(f"room_id={row['id']}")
    else:
        parts.append(f"chat_id={row['id']}")
    parts.append(f"last {_when(row.get('last_ts') or 0)}")
    if row.get("unread"):
        parts.append(f"{row['unread']} unread")
    if row.get("waits"):
        parts.append(f"WAITS FOR YOU ({row.get('waits_reason') or 'unanswered'})")
    mode = _MODE_WORDS.get(row.get("mode") or "", row.get("mode") or "")
    if mode:
        parts.append(mode)
    if row.get("is_group") and channel != "room":
        parts.append("group")
    if row.get("answered_by_agent"):
        parts.append("agent answered")
    if row.get("done"):
        parts.append("done")
    parts.append(f"{row.get('message_count') or 0} msg")
    preview = (row.get("preview") or "").replace("\n", " ").strip()
    if preview:
        who = row.get("preview_from") or "them"
        parts.append(f'{who}: "{preview[:90]}"')
    return " | ".join(parts)


def _hide_suspicious_mail(rows: List[Dict[str, Any]]) -> tuple:
    """The phishing filter the mail tools apply, over the inbox's mail rows."""
    from vaf.tools.mail_utils import filter_phishing_messages_for_agent
    mail = [r for r in rows if r["channel"] == "mail"]
    if not mail:
        return rows, 0
    shimmed = [{"from": r.get("name") or "", "subject": r.get("subject") or "", "body_snippet": r.get("preview") or "",
                "_row": r} for r in mail]
    safe, blocked = filter_phishing_messages_for_agent(shimmed)
    keep = {id(m["_row"]) for m in safe}
    return [r for r in rows if r["channel"] != "mail" or id(r) in keep], blocked


class InboxTool(BaseTool):
    """Every conversation of the user across every channel, newest first, with unread and
    waits-for-you. Use it to see where somebody waits or which chats exist before reading one."""
    name = "inbox"
    category = "messaging"
    identity_kwargs = ("user_scope_id", "username")
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "THE tool to check the user's inboxes: every conversation across WhatsApp, Telegram, Discord, "
        "mail and agent rooms, newest first, with unread counts, who waits for an answer, and which lane "
        "answers there. Call it the MOMENT the user asks to check, read, show or look at their messages, "
        "inbox, Posteingang, mails, chats or who is waiting - it is the ONLY source of their real messages "
        "(memory_search does NOT contain them). Narrow with channel (whatsapp, telegram, discord, mail, room; "
        "default all), view (all, waits, unread, agent), max_chats (when the user names a number, pass it), "
        "query, include_groups, include_done, include_bulk (promotions, social, newsletters, notifications and "
        "junk mail are hidden unless asked); account_id and folder narrow the mail lane. "
        "Then READ one conversation with read_whatsapp_chat / read_telegram_chat / read_discord_chat "
        "(chat_id), read_mail (the IDs block) or room_read (room_id); search mail with find_mail."
    )
    parameters = {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "enum": ["all", "whatsapp", "telegram", "discord", "mail", "room"],
                "description": "One channel, or all (default).",
            },
            "view": {
                "type": "string",
                "enum": ["all", "waits", "unread", "agent"],
                "description": "all (default), waits (somebody waits for the user), unread, agent (the agent answered last).",
            },
            "max_chats": {
                "type": "integer",
                "description": "Rows to return (1-200, default 30). When the user names a number, pass exactly that.",
            },
            "query": {
                "type": "string",
                "description": "Optional. Only conversations whose name, preview or stored messages match this text.",
            },
            "include_groups": {"type": "boolean", "description": "Include group chats and rooms (default true)."},
            "include_done": {"type": "boolean", "description": "Include conversations the user answered last, or marked done through the API (default false)."},
            "include_bulk": {"type": "boolean", "description": "Mail only. Include promotions, social, newsletters, notifications and junk mail (default false: primary mail only)."},
            "account_id": {"type": "string", "description": "Mail only. Email of one connected account."},
            "folder": {"type": "string", "description": "Mail only. IMAP folder name (default: every folder)."},
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        from vaf.core.inbox import CHANNELS, list_conversations

        username = (kwargs.get("username") or "admin").strip()
        user_scope_id = kwargs.get("user_scope_id")
        channel = (kwargs.get("channel") or "all").strip().lower()
        channels = list(CHANNELS) if channel in ("", "all") else [channel]
        if channel not in ("", "all") and channel not in CHANNELS:
            return f"Unknown channel '{channel}'. Use one of: all, whatsapp, telegram, discord, mail, room."
        view = (kwargs.get("view") or "all").strip().lower()
        try:
            max_chats = min(max(int(kwargs.get("max_chats") or 30), 1), 200)
        except (TypeError, ValueError):
            max_chats = 30
        include_groups = kwargs.get("include_groups")
        include_groups = True if include_groups is None else bool(include_groups)
        include_done = bool(kwargs.get("include_done") or False)
        include_bulk = bool(kwargs.get("include_bulk") or False)
        query = (kwargs.get("query") or "").strip()

        # The derived Telegram/Discord indexes are re-projected from the session files first,
        # as the per-channel tools did; the core lists what is stored and never syncs.
        if "telegram" in channels:
            try:
                from vaf.core.telegram_history import sync_telegram_history
                sync_telegram_history()
            except Exception:
                pass
        if "discord" in channels:
            try:
                from vaf.core.discord_history import sync_discord_history
                sync_discord_history()
            except Exception:
                pass

        # The mail account and folder narrow the lane at the source (before the counts and
        # the cut to max_chats), or a narrowed listing could lose a matching thread to the limit.
        account_id = (kwargs.get("account_id") or "").strip()
        folder = (kwargs.get("folder") or "").strip()
        result = list_conversations(username, user_scope_id, channels=channels, view=view,
                                    include_groups=include_groups, include_done=include_done,
                                    include_bulk=include_bulk, query=query, limit=max_chats,
                                    mail_account_id=account_id or None, mail_folder=folder or None)
        rows = result["rows"]
        rows, blocked = _hide_suspicious_mail(rows)
        counts = result["counts"]

        header = (f"Inbox: {counts['all']} conversations, {counts['waits']} wait for you, "
                  f"{counts['unread']} unread (newest first")
        header += f", view={view}" if view != "all" else ""
        header += f", query={query!r}" if query else ""
        header += ")"
        lines = [_line(i, r) for i, r in enumerate(rows, 1)]
        # "Nothing stored" is said only when the lane holds nothing at all; a view, a query,
        # a toggle or the cut to max_chats hiding a lane's rows is not the same thing, and
        # the header already says which of those is in force.
        stored = counts.get("stored_per_channel") or {}
        for ch in channels:
            if int(stored.get(ch) or 0) == 0:
                lines.append(_empty_line(ch))
        out = _NEXT_STEP_HINT + header + "\n" + "\n".join(lines)
        id_lines = []
        for i, r in enumerate(rows, 1):
            if r["channel"] == "mail":
                j = r.get("jump") or {}
                id_lines.append(f"  {i}: account_id={j.get('account_id') or ''} message_id={j.get('message_id') or ''!r} "
                                f"provider_message_id={j.get('provider_message_id') or ''} folder={j.get('folder') or 'INBOX'}")
        if id_lines:
            out += "\n\nIDs for read_mail (by index; do not invent or repeat entries):\n" + "\n".join(id_lines)
        if blocked:
            out += f"\n\n(Security) Hidden {blocked} suspicious mail thread(s) by phishing filter."
        hidden = int(counts.get("bulk_hidden") or 0)
        if hidden:
            out += f"\n\n({hidden} bulk mail thread(s) hidden: promotions, social, newsletters, notifications, junk; include_bulk=true lists them.)"
        return out


def _empty_line(channel: str) -> str:
    from vaf.core.inbox import channel_label
    if channel == "mail":
        return "Mail: no threads in the store yet (the mailbox syncs in the background)."
    if channel == "room":
        return "Rooms: none open."
    return f"{channel_label(channel)}: no stored chats yet (messages are stored as they arrive)."
