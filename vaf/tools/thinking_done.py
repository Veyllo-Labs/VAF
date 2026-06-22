"""
Thinking-done tool: only available when VAF_THINKING_MODE=1.
The agent calls this to signal that the current thinking pass is finished.
"""
from vaf.tools.base import BaseTool


class ThinkingDoneTool(BaseTool):
    """Signal that this thinking pass is complete. Call when you have nothing left to do for the user in this run."""

    name = "thinking_done"
    permission_level = "system"
    side_effect_class = "reversible"
    description = (
        "Call this when you have finished this thinking pass. "
        "You may use multiple turns to work on todos, automations, and messages; when you are done, call thinking_done. "
        "If you still have ONE question or proposal for the user and have not already called ask_user this "
        "run, put the final, user-facing text in `message` (with proposed_action / source_note_id / "
        "source_todo_id as needed) — the system delivers it and tracks it exactly like ask_user. A "
        "question written as plain text is NOT delivered; use `message` or ask_user."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Optional short summary of what you did in this pass.",
            },
            "message": {
                "type": "string",
                "description": (
                    "Optional: a final, clean, user-facing message to deliver — a specific question or "
                    "concrete proposal. Use ONLY if you did not already call ask_user this run. Put ONLY "
                    "the polished text here — no reasoning, no 'I should…', no tool talk."
                ),
            },
            "proposed_action": {
                "type": "string",
                "description": "Optional: short note of what you would do if the user agrees (main agent carries it out on confirm).",
            },
            "source_note_id": {
                "type": "string",
                "description": "Optional: id of the automation NOTE this message is about; marked handled when the user confirms.",
            },
            "source_todo_id": {
                "type": "string",
                "description": "Optional: id of the automation TODO this message is about; marked done when the user confirms.",
            },
        },
        "required": [],
    }

    def run(self, **kwargs) -> str:
        summary = (kwargs.get("summary") or "").strip()
        message = (kwargs.get("message") or "").strip()
        delivered_note = ""
        if message:
            # Fallback delivery: a weak model often composes the question but forgets to call ask_user.
            # Route that final text through the SAME tracked path here — unless ask_user already raised a
            # request this run (guard against a double message).
            try:
                from vaf.core.thinking_mode import deliver_tracked_message, run_has_open_request
                from vaf.core.config import get_local_admin_scope_id
                scope = kwargs.get("user_scope_id") or get_local_admin_scope_id()
                if run_has_open_request(scope):
                    delivered_note = " (a question was already delivered this run; the extra message was not re-sent)"
                else:
                    req = deliver_tracked_message(
                        scope, message,
                        proposed_action=kwargs.get("proposed_action"),
                        source_note_id=kwargs.get("source_note_id"),
                        source_todo_id=kwargs.get("source_todo_id"),
                        username=kwargs.get("username"),
                    )
                    if req and req.get("delivered"):
                        delivered_note = f" (message delivered to the user, tracked as request {req['id']})"
                    elif req:
                        delivered_note = f" (message recorded as request {req['id']}; it will surface on the user's next visit)"
            except Exception:  # pragma: no cover - defensive: never fail the run on delivery
                delivered_note = ""
        return (summary or "Done.") + delivered_note
