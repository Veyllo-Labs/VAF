# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""ask_user tool — Thinking Mode only.

The background thinking run uses this to contact the user with ONE clean, user-facing message — a
specific question or a concrete proposal (e.g. "Soll ich dir eine Erinnerung einrichten?"). The
user-facing text is the explicit `message` argument, so the agent's chain-of-thought can never leak
into the chat (the old heuristic scraped the last assistant text and leaked reasoning).

The call also records a tracked request (status: asked) so the next run does not re-ask, and the main
agent can pick it up and carry it out when the user replies (see thinking_requests + chat_step pickup).
"""
from vaf.tools.base import BaseTool


class AskUserTool(BaseTool):
    """Contact the user with one clean question/proposal and track it (Thinking Mode only)."""

    name = "ask_user"
    permission_level = "system"
    side_effect_class = "reversible"
    description = (
        "Contact the user with ONE clean, user-facing message — a specific question or a concrete "
        "proposal (e.g. 'Es wird heute heiss in Berlin (34 Grad). Soll ich dir eine Erinnerung "
        "einrichten, deine Kleidung zu checken?'). Put ONLY the final, polished text in `message` — no "
        "reasoning, no tool talk, no 'I should…'. Use this at most once per run and only when you "
        "genuinely need the user's decision. The system delivers it, tracks it as a request, and waits "
        "for the reply; do not also write the question as plain text. "
        "In a scheduled automation this is a HIGH BAR: use it ONLY for a genuine blocker or an important "
        "clarification you cannot resolve on your own — NEVER for status ('starting', 'working on it'). If "
        "you can proceed on a reasonable assumption, do so and note the assumption in your result instead "
        "of asking. When you do ask, your full working context is handed to the user's main agent, which "
        "continues the task after they reply."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The final, user-facing message. Short, natural, no reasoning or tool talk.",
            },
            "proposed_action": {
                "type": "string",
                "description": (
                    "Optional: a short note of what you would do if the user agrees (e.g. 'create a "
                    "reminder automation: check clothes when Berlin forecast > 30C'). The main agent "
                    "uses this to carry the task out after the user confirms."
                ),
            },
            "details": {
                "type": "string",
                "description": (
                    "IMPORTANT when your message references something you found or prepared (e.g. 'I "
                    "found 15 cooling methods, want the list?'): put the ACTUAL content here — the real "
                    "list/facts/findings. It is NOT shown to the user, but it is handed to the main agent "
                    "so that when the user asks for specifics it answers with YOUR real findings instead "
                    "of making something up. Never tease content without filling this."
                ),
            },
            "source_note_id": {
                "type": "string",
                "description": (
                    "Optional: the id of the automation NOTE this question is about (from "
                    "list_automation_notes). When the user confirms, that note is marked handled so it "
                    "stops re-surfacing in future runs. Pass it whenever your question stems from a note."
                ),
            },
            "source_todo_id": {
                "type": "string",
                "description": "Optional: the id of the automation TODO this question is about; marked done on confirm.",
            },
            "user_scope_id": {
                "type": "string",
                "description": "Internal: user scope id injected by the framework. Leave blank.",
            },
        },
        "required": ["message"],
    }

    def run(self, **kwargs) -> str:
        import os

        message = (kwargs.get("message") or "").strip()
        if not message:
            return "Error: message must not be empty."

        # In a scheduled automation, this is a background handoff to the user's MAIN agent: store the
        # automation agent's FULL working context as a bundle, record the linked request, then end. The
        # main agent loads the bundle and continues when the user replies. (The thinking-mode path below
        # is untouched.)
        if os.environ.get("VAF_IN_AUTOMATION", "").strip() in ("1", "true", "yes"):
            return self._run_automation_handoff(**kwargs)

        try:
            # Single shared delivery path (also used by the thinking_done fallback): records a tracked
            # request, sets waiting_for_reply, and emits the exact text to the Web UI. The user's REAL
            # data scope is resolved inside (never the normalized "default" key, or the main agent would
            # not find the request).
            from vaf.core.thinking_mode import deliver_tracked_message
        except Exception as e:  # pragma: no cover - defensive
            return f"Error: ask_user is unavailable: {e}"

        req = deliver_tracked_message(
            kwargs.get("user_scope_id"),
            message,
            proposed_action=kwargs.get("proposed_action"),
            source_note_id=kwargs.get("source_note_id"),
            source_todo_id=kwargs.get("source_todo_id"),
            username=kwargs.get("username"),
            details=kwargs.get("details"),
        )
        if not req:
            # message was non-empty (checked above) but delivery returned nothing. WHY depends on the
            # per-run proactive MODE — give mode-specific guidance so the model stops retrying blindly.
            try:
                from vaf.core.thinking_mode import get_proactive_mode
                _mode = get_proactive_mode(kwargs.get("user_scope_id"))
            except Exception:
                _mode = "grounded"
            if _mode == "off":
                # Not in a proactive step (gather / forced-resolution), or a message was already delivered
                # this run. Retrying cannot succeed — stop now.
                return (
                    "Not sent: you are not in a proactive step right now (or a message was already "
                    "delivered this run). Do NOT retry ask_user — call thinking_done now."
                )
            # grounded step: the evidence gate dropped it (not grounded in real retrieved memory).
            return (
                "Not sent: a proactive suggestion must quote the REAL memory it is based on, verbatim, in "
                "`message` or `details` (paraphrasing or inventing is rejected). If you have nothing "
                "grounded, call thinking_done — you will then ask the user a get-to-know question instead."
            )
        if req.get("delivered"):
            return (
                f"Message delivered to the user (tracked as request {req['id']}). Stop now and wait for "
                "their reply — do not ask anything else this run; call thinking_done."
            )
        return (
            f"Recorded your question (request {req['id']}) and set the waiting state, but the user's chat "
            "was not reachable right now; it will surface on their next visit. Call thinking_done now."
        )

    def _run_automation_handoff(self, **kwargs) -> str:
        """A scheduled automation that hit a genuine blocker hands off its FULL working context to the
        user's main agent. Stores the agent history as a handoff bundle + records the linked request, then
        ends — the main agent loads the bundle and continues with full context when the user replies."""
        message = (kwargs.get("message") or "").strip()
        try:
            from vaf.core.handoff_bundle import deliver_handoff
        except Exception as e:  # pragma: no cover - defensive
            return f"Error: ask_user handoff is unavailable: {e}"

        _agent = kwargs.get("_agent")
        _history = None
        if _agent is not None:
            try:
                _history = list(getattr(_agent, "history", None) or [])
            except Exception:
                _history = None

        # session_id left as injected (None for automations) -> deliver_handoff anchors to the user's
        # latest real web session, so the question lands in the main chat where the reply is picked up.
        req = deliver_handoff(
            kwargs.get("user_scope_id"),
            message=message,
            proposed_action=kwargs.get("proposed_action"),
            details=kwargs.get("details"),
            history=_history,
            session_id=kwargs.get("session_id"),
            username=kwargs.get("username"),
        )
        if not req:
            return "Error: the handoff message was empty and was not sent."
        if req.get("delivered"):
            return (
                f"Handoff delivered to the user (request {req['id']}, bundle {req.get('bundle_id')}). Stop "
                "now — your full working context is saved; the user's main agent continues with it when they "
                "reply. Do not ask or do anything else."
            )
        return (
            f"Handoff recorded (request {req['id']}, bundle {req.get('bundle_id')}); the user's chat was not "
            "reachable right now, so it surfaces on their next visit. Stop now — do not ask anything else."
        )
