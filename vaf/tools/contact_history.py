# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What the person the agent is answering wrote before, across every channel.

A Front Office turn has no read tool, because every read tool reaches the owner's stores
through a free argument. This one has none: the headless runner pins the contact it is
answering on the agent (`agent._front_office_contact`, the record the ingress resolved from
the verified endpoint), and the tool reads that person's own correspondence with the owner
through `contacts_store.contact_timeline`, the same merge the contact book's timeline shows:
their WhatsApp, Telegram and Discord messages by endpoint, their mail by address, in both
directions. Never the owner's notes about them, never another person, never a mail the
phishing filter hides or the provider did not verify as theirs.

So "did you get my mail about the offer" asked on WhatsApp is answered from Bob's own mail,
and "as we said on WhatsApp" in a mail from the agent's view of Bob's chat.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from vaf.tools.base import BaseTool
from vaf.core.channels import CHANNEL_LABELS, CHAT_CHANNELS

_CHANNELS = CHAT_CHANNELS + ("mail", "all")
# Timeline pages read at most per call (each up to 3x the limit, 60 at least): bounded
# work for a contact with years of chat, enough to reach a mail behind a long conversation.
_MAX_PAGES = 5
_LABEL = {**CHANNEL_LABELS, "email": "Mail"}


def _when(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def _mail_verdicts(items: List[Dict[str, Any]], user_scope_id: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """The stored verdict summary per mail item (keyed by the item's id), for the badge word
    and the phishing filter; an item whose message the store cannot find reads as unknown."""
    if not user_scope_id or not any(it.get("kind") == "mail" for it in items):
        return {}
    try:
        from vaf.mail.service import MailService
        from vaf.mail.store import MailStore
        from vaf.mail.verification import summary
        if not MailStore.exists(user_scope_id):
            return {}
        svc = MailService(user_scope_id)
        out: Dict[str, Dict[str, Any]] = {}
        pks: Dict[str, int] = {}
        for it in items:
            if it.get("kind") != "mail":
                continue
            ref = it.get("ref") or {}
            pk = svc.store.pk_by_message_id(str(ref.get("message_id") or ""), ref.get("account_id") or None)
            if pk is not None:
                pks[it["id"]] = int(pk)
        verdicts = svc.store.message_auth(list(pks.values()))
        for item_id, pk in pks.items():
            out[item_id] = summary(verdicts.get(pk))
        return out
    except Exception:
        return {}


class ContactHistoryTool(BaseTool):
    name = "contact_history"
    category = "messaging"
    identity_kwargs = ("user_scope_id", "username")
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "Front Office only: what the person you are answering wrote to the owner before, and "
        "what the owner or you wrote to them, across WhatsApp, Telegram, Discord and mail, "
        "newest first. Use it when they refer to an earlier message or a mail ('did you get my "
        "mail about the offer?'). Optional channel filter and a word to search for. Nothing "
        "about the owner's other conversations is reachable here."
    )
    input_examples = [{"channel": "mail", "query": "offer"}, {"limit": 10}]
    parameters = {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "enum": list(_CHANNELS),
                        "description": "One channel, or all (the default)."},
            "query": {"type": "string", "description": "Only entries containing this word (case-insensitive)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 60, "description": "How many entries (default 20)."},
        },
    }

    def run(self, **kwargs) -> str:
        agent = kwargs.get("_agent")
        contact = getattr(agent, "_front_office_contact", None) if agent is not None else None
        if not isinstance(contact, dict) or not contact:
            return ("contact_history works only while answering a contact in Front Office; there is no "
                    "contact pinned to this turn.")
        username = kwargs.get("username") or None
        user_scope_id = kwargs.get("user_scope_id") or None
        channel = str(kwargs.get("channel") or "all").strip().lower()
        if channel not in _CHANNELS:
            channel = "all"
        query = str(kwargs.get("query") or "").strip().lower()
        try:
            limit = max(1, min(int(kwargs.get("limit") or 20), 60))
        except (TypeError, ValueError):
            limit = 20
        from vaf.core.contacts_store import contact_timeline
        from vaf.tools.mail_utils import filter_phishing_messages_for_agent

        # Messages and mail only: the notes are the owner's remarks about the person, the
        # events sit in the contact block already, and both stay with the owner. The
        # timeline is paged with its own cursor until `limit` entries survive the channel,
        # word and phishing filters, so a mail behind a long chat is still found.
        wanted = None if channel == "all" else ("email" if channel == "mail" else channel)
        kept: List[Dict[str, Any]] = []
        verdicts: Dict[str, Dict[str, Any]] = {}   # by item id, across every page read
        cursor = None
        for _ in range(_MAX_PAGES):
            page = contact_timeline(contact, username, user_scope_id, limit=max(limit * 3, 60),
                                    cursor=cursor, kinds=("message", "mail"))
            items = list(page.get("items") or [])
            if wanted:
                items = [it for it in items if it.get("channel") == wanted]
            verdicts.update(_mail_verdicts(items, user_scope_id))
            for it in items:
                if it.get("kind") == "mail" and it.get("direction") == "in":
                    ref = it.get("ref") or {}
                    row = {"from": ref.get("from") or "", "subject": it.get("title") or "", "body_snippet": it.get("body") or "",
                           "category": "", "auth": verdicts.get(it["id"]) or {}}
                    safe, _blocked = filter_phishing_messages_for_agent([row])
                    if not safe:
                        continue
                text = f"{it.get('title') or ''} {it.get('body') or ''}".lower()
                if query and query not in text:
                    continue
                kept.append(it)
            cursor = page.get("next_cursor")
            if len(kept) >= limit or not cursor:
                break
        kept = kept[:limit]
        name = str(contact.get("name") or "the contact")
        if not kept:
            where = "any channel" if channel == "all" else _LABEL.get("email" if channel == "mail" else channel, channel)
            return f"No earlier messages or mails with {name} on {where}" + (f" containing '{query}'" if query else "") + "."
        lines = [f"Earlier correspondence with {name} ({len(kept)} entries, newest first). "
                 "'them' wrote to the owner, 'us' is the owner's side (the owner or you)."]
        for it in kept:
            who = "them" if it.get("direction") == "in" else "us"
            label = _LABEL.get(it.get("channel") or "", it.get("channel") or "")
            head = f"- [{label}] {_when(it.get('ts'))} {who}"
            if it.get("kind") == "mail":
                v = verdicts.get(it["id"]) or {}
                state = v.get("state") or "unknown"
                if who == "them" and state != "verified":
                    head += f" (sender {state})"
                subject = (it.get("title") or "").strip()
                if subject:
                    head += f' subject "{subject[:80]}"'
            body = (it.get("body") or "").replace("\n", " ").strip()
            lines.append(f"{head}: {body[:400]}" if body else head)
        return "\n".join(lines)
