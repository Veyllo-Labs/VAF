# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Schedule a one-shot, persistent reminder for the user.

The narrow reminder lane (see vaf/core/reminders.py): a reminder is stored DATA
that the scheduler delivers verbatim at the given time - no agent run, no tools.
This is the mechanism the daily calendar check uses (create_automation is
deliberately unavailable in automation runs), and it survives restarts, unlike
set_timer.
"""
from vaf.tools.base import BaseTool


class ScheduleReminderTool(BaseTool):
    """Schedule/list/cancel one-shot reminders delivered on the user's main messenger."""
    name = "schedule_reminder"
    permission_level = "write"
    side_effect_class = "reversible"
    channel_restrictions = ()
    admin_only = False
    description = (
        "Schedule a ONE-SHOT reminder: the message is stored and delivered VERBATIM at fire_at "
        "on the user's main messenger (Web UI notification fallback). Persistent - survives "
        "restarts. Write the FINAL user-facing reminder text; no LLM processes it at delivery. "
        "Use for clock-time reminders (e.g. 30 minutes before a calendar event). "
        "NOT for short in-chat delays (use set_timer) and NOT for recurring schedules "
        "(use create_automation). action='list' shows pending, action='cancel' with reminder_id cancels."
    )
    input_examples = [
        {"message": "Dein Meeting 'NLND Open Day' startet in 30 Minuten.", "fire_at": "2026-07-14 08:30"},
        {"action": "list"},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "cancel"],
                "description": "Default create.",
            },
            "message": {
                "type": "string",
                "description": "The final reminder text, delivered verbatim (create).",
            },
            "fire_at": {
                "type": "string",
                "description": "Delivery time 'YYYY-MM-DD HH:MM' in the user's timezone; bare 'HH:MM' means today (create).",
            },
            "reminder_id": {
                "type": "string",
                "description": "Id of the pending reminder to cancel (cancel).",
            },
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        from vaf.core import reminders as rem
        action = (kwargs.get("action") or "create").strip().lower()
        user_scope_id = kwargs.get("user_scope_id")
        username = kwargs.get("username") or "admin"

        if action == "list":
            items = rem.list_reminders(user_scope_id)
            if not items:
                return "No pending reminders."
            lines = ["Pending reminders:"]
            for r in items:
                lines.append(f"- [{r['id']}] {str(r.get('fire_at'))[:16]} -> {r['message'][:120]}")
            return "\n".join(lines)

        if action == "cancel":
            rid = (kwargs.get("reminder_id") or "").strip()
            if not rid:
                return "Pass reminder_id to cancel (see action='list')."
            ok = rem.cancel_reminder(user_scope_id, rid)
            return f"Reminder {rid} cancelled." if ok else f"No pending reminder with id {rid}."

        res = rem.create_reminder(user_scope_id, username,
                                  kwargs.get("message") or "", kwargs.get("fire_at") or "")
        if not res.get("ok"):
            return f"Could not schedule reminder: {res.get('error')}"
        r = res["reminder"]
        return (
            f"Reminder scheduled (id {r['id']}) for {str(r['fire_at'])[:16]}: it will be "
            f"delivered verbatim on the user's main messenger at that time."
        )
