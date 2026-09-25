# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What became of the messages the agent prepared: sent, discarded, replaced, or still waiting.

A send the agent makes on the person's web chat turn waits for them as a draft
(`vaf/core/outbound_hold.py`), and the agent hears what became of it twice, both times pushed
to it: the wake turn after a send, and the `[Context:` note at the chat's next turn. It could
not ASK. Live incident: asked whether a mail had gone out, the agent searched for a tool that
lists drafts, found none, searched a mailbox folder by a name the account does not use, and
told the person a mail sent the evening before was still waiting.

The ledger answers here, the same rows the card in the chat and `vaf outbox list` read. It is
the only record a messenger send leaves (there is no Sent folder for a WhatsApp message), and
for a mail it is the outbox's own word, which a mailbox search can only confirm later.
"""
from datetime import datetime
from typing import Any, Dict, Optional

from vaf.tools.base import BaseTool

MAX_ROWS = 20


def _when(ts: float, username: Optional[str]) -> str:
    """A draft's time in the person's own clock and format, or "" when it has none."""
    if not ts:
        return ""
    try:
        from vaf.core.user_time import format_user_datetime, resolve_user_timezone
        tz = resolve_user_timezone(username)
        dt = datetime.fromtimestamp(float(ts), tz) if tz else datetime.fromtimestamp(float(ts))
        return format_user_datetime(dt, username=username, seconds=False)
    except Exception:
        return ""


def _line(row: Dict[str, Any], username: Optional[str]) -> str:
    from vaf.core.outbound_hold import status_line
    line = status_line(row)
    decided = str(row.get("state") or "") in ("sent", "sending", "discarded", "replaced")
    when = _when(row.get("decided_ts") if decided else row.get("created_ts"), username)
    if when:
        line += f" [{'decided' if decided else 'written'} {when}]"
    subject = str(row.get("subject") or "").strip()
    if subject:
        line += f' Subject: "{subject[:80]}"'
    return line


class ListDraftsTool(BaseTool):
    """The drafts of this chat and what became of each, or one draft by its ref."""
    name = "list_drafts"
    category = "messaging"
    identity_kwargs = ("user_scope_id", "username", "session_id")
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "What became of the messages you prepared as drafts (mail and messenger): SENT by the "
        "user, DISCARDED, REPLACED, or still WAITING for the user, with the time. Call it when "
        "the user asks whether a message went out, or before you say what happened to a draft. "
        "Without arguments it lists this chat's drafts, newest first; pass draft (the ref a "
        "result named, e.g. 'mail:12' or 'call:7', or its bare number) for one draft from any "
        "chat. It is the outbox's own record; a messenger send leaves no other."
    )
    parameters = {
        "type": "object",
        "properties": {
            "draft": {
                "type": "string",
                "description": "Optional. One draft: 'mail:12', 'call:7', or the bare number.",
            },
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        from vaf.core import outbound_hold
        from vaf.core.config import resolve_caller_username

        user_scope_id = kwargs.get("user_scope_id")
        username = kwargs.get("username")
        # The funnel names a caller without a name by a stable bucket of their scope, because it
        # looks nothing up per dispatch. The ledger keys a parked call on the REAL account name,
        # looked up once when it was parked (`outbound_hold.park_messenger_call`), so the bucket
        # would find none of that person's drafts: ask the way the park did.
        if user_scope_id and username == resolve_caller_username(None, user_scope_id):
            username = None
        username = resolve_caller_username(username, user_scope_id, allow_lookup=True)
        ref = str(kwargs.get("draft") or "").strip()
        if ref:
            rows = outbound_hold.draft_rows(ref, username=username, user_scope_id=user_scope_id)
            if not rows:
                return (f"No draft '{ref}' of this user. A ref looks like 'mail:12' or "
                        "'call:7', as the tool result that created the draft named it.")
            return "\n".join(_line(r, username) for r in rows)

        session_id = str(kwargs.get("session_id") or "").strip()
        if session_id:
            rows = outbound_hold.chat_drafts(username, user_scope_id, session_id)
            scope_word = "this chat"
        else:
            # No chat in this lane (a workflow step): what still waits for the person is the
            # one listing that means something without a conversation.
            rows = outbound_hold.pending(username, user_scope_id)
            scope_word = "this user, waiting"
        if not rows:
            return f"No drafts ({scope_word})."
        shown = rows[:MAX_ROWS]
        out = [f"Drafts ({scope_word}), newest first:"]
        out += [f"- {_line(r, username)}" for r in shown]
        if len(rows) > len(shown):
            out.append(f"({len(rows) - len(shown)} older ones not listed; ask for one by its ref.)")
        return "\n".join(out)
