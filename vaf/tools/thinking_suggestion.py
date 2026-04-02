"""
Save thinking suggestion – used by the agent during thinking mode to persist ideas.
Per-user storage; user_scope_id is injected by the agent from the current session.
"""
from typing import Optional

from vaf.tools.base import BaseTool
from vaf.core.thinking_suggestions import add_suggestion as _add_suggestion


def _scope_str(user_scope_id) -> Optional[str]:
    if user_scope_id is None:
        return None
    return str(user_scope_id) if not isinstance(user_scope_id, str) else user_scope_id


VALID_CATEGORIES = ("automation", "user_knowledge", "proactive_help", "system_health", "todo_note")
VALID_ACTION_TYPES = ("create_automation", "ask_user", "todo_note", "")


class SaveThinkingSuggestionTool(BaseTool):
    """Save a suggestion produced during thinking mode. User can review in settings/suggestions."""

    name = "save_thinking_suggestion"
    permission_level = "system"
    side_effect_class = "reversible"
    description = """Save a thinking suggestion for the user to review later. Use during thinking mode when you identify something to suggest: new automation, question to ask the user, proactive help idea, or system health note. category: automation | user_knowledge | proactive_help | system_health | todo_note. action_type (optional): e.g. create_automation | ask_user."""

    parameters = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Category: automation, user_knowledge, proactive_help, system_health, todo_note",
            },
            "suggestion": {"type": "string", "description": "The suggestion text (required)."},
            "priority": {"type": "integer", "description": "Optional priority (default 0)."},
            "action_type": {
                "type": "string",
                "description": "Optional: create_automation, ask_user, or leave empty.",
            },
        },
        "required": ["category", "suggestion"],
    }

    def run(self, **kwargs) -> str:
        category = (kwargs.get("category") or "proactive_help").strip().lower()
        if category not in VALID_CATEGORIES:
            category = "proactive_help"
        suggestion = (kwargs.get("suggestion") or "").strip()
        if not suggestion:
            return "Error: suggestion is required."
        user_scope_id = kwargs.get("user_scope_id")
        scope = _scope_str(user_scope_id)
        priority = kwargs.get("priority", 0)
        action_type = (kwargs.get("action_type") or "").strip().lower() or None
        try:
            entry = _add_suggestion(
                scope,
                category=category,
                suggestion=suggestion,
                priority=priority,
                action_taken=False,
                status="pending",
                thinking_run_id=None,
                action_type=action_type,
            )
            return f"Suggestion saved (id: {entry['id']}, category: {entry['category']}). User can review in settings/suggestions."
        except Exception as e:
            return f"Error saving suggestion: {e}"
