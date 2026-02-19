"""
VAF Automation Planner Tools - Notes and todos for the automation calendar.
Per-user storage; user_scope_id is injected by the agent from the current session.
"""
from typing import Optional

from vaf.tools.base import BaseTool
from vaf.core.automation_planner import (
    list_notes as _list_notes,
    add_note as _add_note,
    delete_note as _delete_note,
    list_todos as _list_todos,
    add_todo as _add_todo,
    update_todo as _update_todo,
    delete_todo as _delete_todo,
)


def _scope_str(user_scope_id) -> Optional[str]:
    if user_scope_id is None:
        return None
    return str(user_scope_id) if not isinstance(user_scope_id, str) else user_scope_id


class AddAutomationNoteTool(BaseTool):
    """Add a note to the automation calendar. User sees it in the Note section."""

    name = "add_automation_note"
    description = """Add a note for the automation calendar (for later/planning). The user sees it in the Note section of the automation calendar. Use when the user or you want to remember something for automation planning."""

    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Note content (required)."},
            "title": {"type": "string", "description": "Optional short title for the note."},
        },
        "required": ["content"],
    }

    def run(self, **kwargs) -> str:
        content = (kwargs.get("content") or "").strip()
        if not content:
            return "Error: content is required."
        user_scope_id = kwargs.get("user_scope_id")
        scope = _scope_str(user_scope_id)
        title = kwargs.get("title")
        try:
            note = _add_note(scope, content, title=title)
            return f"Note added (id: {note['id']}). Title: {note.get('title') or '(none)'}; created_at: {note.get('created_at', '')}."
        except Exception as e:
            return f"Error adding note: {e}"


class AddAutomationTodoTool(BaseTool):
    """Add a to-do to the automation calendar. User sees it in the To-do list."""

    name = "add_automation_todo"
    description = """Add a to-do for the automation calendar. The user sees it in the To-do list. Use when the user or you want to track a task (e.g. 'Prepare report by Friday'). Optional due_at: ISO8601 or YYYY-MM-DD."""

    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "To-do text (required)."},
            "due_at": {"type": "string", "description": "Optional due date (ISO8601 or YYYY-MM-DD)."},
        },
        "required": ["text"],
    }

    def run(self, **kwargs) -> str:
        text = (kwargs.get("text") or "").strip()
        if not text:
            return "Error: text is required."
        user_scope_id = kwargs.get("user_scope_id")
        scope = _scope_str(user_scope_id)
        due_at = kwargs.get("due_at")
        try:
            todo = _add_todo(scope, text, due_at=due_at)
            return f"To-do added (id: {todo['id']}). due_at: {todo.get('due_at') or '(none)'}; created_at: {todo.get('created_at', '')}."
        except Exception as e:
            return f"Error adding to-do: {e}"


class ListAutomationNotesTool(BaseTool):
    """List automation notes for the current user."""

    name = "list_automation_notes"
    description = "List current automation notes (with created_at). Shown in the automation calendar Note section."

    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs) -> str:
        user_scope_id = kwargs.get("user_scope_id")
        scope = _scope_str(user_scope_id)
        try:
            notes = _list_notes(scope)
            if not notes:
                return "No automation notes yet. Use add_automation_note to add one."
            lines = []
            for n in notes:
                title = n.get("title") or "(no title)"
                lines.append(f"- [{n.get('id')}] {title}: {n.get('content', '')[:80]}{'...' if len(n.get('content', '')) > 80 else ''} (created: {n.get('created_at', '')})")
            return "Automation notes:\n" + "\n".join(lines)
        except Exception as e:
            return f"Error listing notes: {e}"


class ListAutomationTodosTool(BaseTool):
    """List automation to-dos for the current user."""

    name = "list_automation_todos"
    description = "List current automation to-dos (created_at, due_at, done). Shown in the automation calendar To-do list."

    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs) -> str:
        user_scope_id = kwargs.get("user_scope_id")
        scope = _scope_str(user_scope_id)
        try:
            todos = _list_todos(scope)
            if not todos:
                return "No automation to-dos yet. Use add_automation_todo to add one."
            lines = []
            for t in todos:
                done = "✓" if t.get("done") else " "
                lines.append(f"- [{done}] [{t.get('id')}] {t.get('text', '')} (due: {t.get('due_at') or '—'}, created: {t.get('created_at', '')})")
            return "Automation to-dos:\n" + "\n".join(lines)
        except Exception as e:
            return f"Error listing to-dos: {e}"


class DeleteAutomationNoteTool(BaseTool):
    """Delete an automation note by id."""

    name = "delete_automation_note"
    description = "Delete an automation note when it is no longer needed. Use note_id from list_automation_notes."

    parameters = {
        "type": "object",
        "properties": {
            "note_id": {"type": "string", "description": "ID of the note to delete."},
        },
        "required": ["note_id"],
    }

    def run(self, **kwargs) -> str:
        note_id = (kwargs.get("note_id") or "").strip()
        if not note_id:
            return "Error: note_id is required."
        user_scope_id = kwargs.get("user_scope_id")
        scope = _scope_str(user_scope_id)
        try:
            ok = _delete_note(scope, note_id)
            return "Note deleted." if ok else "Note not found or already deleted."
        except Exception as e:
            return f"Error deleting note: {e}"


class DeleteAutomationTodoTool(BaseTool):
    """Delete an automation to-do by id."""

    name = "delete_automation_todo"
    description = "Delete an automation to-do (e.g. after completion or when obsolete). Use todo_id from list_automation_todos."

    parameters = {
        "type": "object",
        "properties": {
            "todo_id": {"type": "string", "description": "ID of the to-do to delete."},
        },
        "required": ["todo_id"],
    }

    def run(self, **kwargs) -> str:
        todo_id = (kwargs.get("todo_id") or "").strip()
        if not todo_id:
            return "Error: todo_id is required."
        user_scope_id = kwargs.get("user_scope_id")
        scope = _scope_str(user_scope_id)
        try:
            ok = _delete_todo(scope, todo_id)
            return "To-do deleted." if ok else "To-do not found or already deleted."
        except Exception as e:
            return f"Error deleting to-do: {e}"
