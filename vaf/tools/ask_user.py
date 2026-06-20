"""ask_user tool — Thinking Mode only.

The background thinking run uses this to contact the user with ONE clean, user-facing message — a
specific question or a concrete proposal (e.g. "Soll ich dir eine Erinnerung einrichten?"). The
user-facing text is the explicit `message` argument, so the agent's chain-of-thought can never leak
into the chat (the old heuristic scraped the last assistant text and leaked reasoning).

The call also records a tracked request (status: asked) so the next run does not re-ask, and the main
agent can pick it up and carry it out when the user replies (see thinking_requests + chat_step pickup).
"""
import os

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
        "for the reply; do not also write the question as plain text."
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
        message = (kwargs.get("message") or "").strip()
        if not message:
            return "Error: message must not be empty."
        proposed_action = (kwargs.get("proposed_action") or "").strip() or None

        try:
            from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
            from vaf.core.thinking_mode import set_waiting_for_reply, current_run_seq, emit_message_to_web_ui
            from vaf.core import thinking_requests as treq
        except Exception as e:  # pragma: no cover - defensive
            return f"Error: ask_user is unavailable: {e}"

        # Use the user's REAL data scope (the same store the main agent reads). Never fall back to the
        # normalized "default" key here, or the main agent would not find the request.
        user_scope_id = kwargs.get("user_scope_id") or get_local_admin_scope_id()
        run_seq = current_run_seq(user_scope_id)
        req = treq.add_request(
            user_scope_id,
            question=message,
            run_seq=run_seq,
            proposed_action=proposed_action,
            thinking_run_id=os.environ.get("VAF_THINKING_RUN_ID"),
            source_note_id=(kwargs.get("source_note_id") or "").strip() or None,
            source_todo_id=(kwargs.get("source_todo_id") or "").strip() or None,
        )
        uname = (kwargs.get("username") or "").strip() or get_local_admin_username()
        set_waiting_for_reply(
            user_scope_id, username=uname, display_name=uname,
            question_text=message, request_id=req["id"],
        )
        sid = emit_message_to_web_ui(user_scope_id, message)
        if sid:
            return (
                f"Message delivered to the user (tracked as request {req['id']}). Stop now and wait for "
                "their reply — do not ask anything else this run; call thinking_done."
            )
        return (
            f"Recorded your question (request {req['id']}) and set the waiting state, but the user's chat "
            "was not reachable right now; it will surface on their next visit. Call thinking_done now."
        )
