# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Short self-timers for the agent: set_timer / list_timers / cancel_timer.

These schedule a precise, short, one-shot delay that fires proactively in the SAME live
chat (CLI + WebUI). On fire the agent either delivers a fixed message or runs a task prompt.
See vaf/core/timers.py for the store/scheduler and the __TIMER__ delivery marker.

Use these for short in-chat timers (seconds to a few minutes). For longer or persistent /
recurring reminders use create_automation (frequency='once'), which survives restarts.

Timers are blocked on messaging channels (Telegram/WhatsApp/Discord) because proactive
in-chat delivery there is not wired up — use create_automation for those.
"""
from vaf.tools.base import BaseTool
from vaf.core.timers import add_timer, list_timers, cancel_timer


def _fmt_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _resolve_session(agent):
    """Return (session_id, source, user_scope_id, username, role) from the injected agent."""
    # Use the live CHAT session set for this turn -- NOT agent._session_id, which is a random per-instance
    # UUID from _register_session (process/shutdown tracking). A timer attached to that UUID fired into a
    # session the Web UI never listens on, so the user saw nothing when it elapsed.
    session_id = getattr(agent, "current_session_id", None)
    if not session_id:
        try:
            from vaf.core.subagent_ipc import get_current_session_id
            session_id = get_current_session_id()
        except Exception:
            session_id = None
    source = getattr(agent, "_current_chat_source", None) or "web"
    return (
        session_id,
        source,
        getattr(agent, "_current_user_scope_id", None),
        getattr(agent, "_current_username", None),
        getattr(agent, "_current_user_role", None),
    )


class SetTimerTool(BaseTool):
    """Schedule a short, one-shot timer that fires proactively in the current chat."""

    name = "set_timer"
    permission_level = "write"
    side_effect_class = "reversible"
    # Proactive in-chat delivery is only wired for the live CLI / WebUI session.
    channel_restrictions = ("telegram", "whatsapp", "discord")
    description = (
        "Schedule a SHORT, one-time timer that fires proactively in THIS chat after a relative "
        "delay (seconds). Use for 'in N seconds/minutes …' style requests. When it fires you are WOKEN "
        "UP and run a real turn (you can think, call tools, reply) — it is not a passive text post.\n"
        "- Provide exactly ONE of: 'message' (a short note/reminder you are woken to handle — act on it "
        "or tell the user) OR 'task' (a concrete instruction you carry out when the timer fires).\n"
        "- 'seconds' is the delay from now (>= 1). For minutes/hours multiply (e.g. 90 = 90s, "
        "300 = 5 min).\n"
        "Use create_automation (frequency='once') instead for longer/persistent reminders that "
        "must survive a restart, or for recurring schedules. Timers here are in-memory and lost "
        "on restart."
    )
    input_examples = [
        {"seconds": 60, "message": "test"},
        {"seconds": 90, "task": "Tell the user the current time."},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "integer",
                "minimum": 1,
                "description": "Delay from now, in seconds (e.g. 10, 60, 300).",
            },
            "message": {
                "type": "string",
                "description": "A short note/reminder for when the timer fires; you are woken up and handle it (act on it, or tell the user). Provide this OR 'task', not both.",
            },
            "task": {
                "type": "string",
                "description": "Instruction to carry out when the timer fires (the agent runs a real turn). Provide this OR 'message', not both.",
            },
            "label": {
                "type": "string",
                "description": "Optional short label for the timer (shown in list_timers).",
            },
        },
        "required": ["seconds"],
    }

    def run(self, **kwargs) -> str:
        agent = kwargs.get("_agent")
        seconds = kwargs.get("seconds")
        message = (kwargs.get("message") or "").strip() or None
        task = (kwargs.get("task") or "").strip() or None
        label = (kwargs.get("label") or "").strip()

        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return "Error: 'seconds' must be an integer number of seconds (>= 1)."
        if seconds < 1:
            return "Error: 'seconds' must be at least 1."
        if bool(message) == bool(task):
            return "Error: provide exactly one of 'message' or 'task'."

        session_id, source, scope, username, role = _resolve_session(agent)
        if not session_id:
            return "Error: no active session to attach the timer to."

        timer = add_timer(
            session_id=session_id,
            seconds=seconds,
            source=source,
            user_scope_id=scope,
            username=username,
            role=role,
            message=message,
            task=task,
            label=label,
        )
        what = f'send the message: "{message}"' if message else f'work on: "{task}"'
        return f"Timer set (#{timer.id}): in {_fmt_duration(seconds)} I will {what}."


class ListTimersTool(BaseTool):
    """List the pending timers for the current chat."""

    name = "list_timers"
    permission_level = "read"
    side_effect_class = "none"
    description = "List the currently pending one-shot timers for this chat (id, time left, and what they do)."
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs) -> str:
        agent = kwargs.get("_agent")
        session_id, *_ = _resolve_session(agent)
        timers = list_timers(session_id=session_id)
        if not timers:
            return "No timers are currently pending."
        lines = []
        for t in timers:
            what = f'message: "{t.message}"' if t.message is not None else f'task: "{t.task}"'
            lbl = f" [{t.label}]" if t.label else ""
            lines.append(f"#{t.id}{lbl}: in {_fmt_duration(t.seconds_remaining)} -> {what}")
        return "Pending timers:\n" + "\n".join(lines)


class CancelTimerTool(BaseTool):
    """Cancel a pending timer by its id."""

    name = "cancel_timer"
    permission_level = "write"
    side_effect_class = "reversible"
    description = "Cancel a pending one-shot timer by its id (the '#id' shown by set_timer / list_timers)."
    parameters = {
        "type": "object",
        "properties": {
            "timer_id": {"type": "string", "description": "The timer id to cancel (without the '#')."}
        },
        "required": ["timer_id"],
    }

    def run(self, **kwargs) -> str:
        timer_id = (kwargs.get("timer_id") or "").strip().lstrip("#")
        if not timer_id:
            return "Error: 'timer_id' is required."
        if cancel_timer(timer_id):
            return f"Timer #{timer_id} cancelled."
        return f"No pending timer with id #{timer_id} (it may have already fired or been cancelled)."
