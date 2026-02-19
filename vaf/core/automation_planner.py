"""
VAF Automation Planner - Per-user notes and todos for the automation calendar.
Stored under automation_planner / user_scope_id so data is isolated per user.
"""
import json
import uuid
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Any

from vaf.core.platform import Platform


def _planner_dir(user_scope_id: Optional[str]) -> Path:
    base = Platform.vaf_dir() / "automation_planner"
    if user_scope_id:
        return base / user_scope_id
    return base / "_default"


def _notes_path(user_scope_id: Optional[str]) -> Path:
    return _planner_dir(user_scope_id) / "notes.json"


def _todos_path(user_scope_id: Optional[str]) -> Path:
    return _planner_dir(user_scope_id) / "todos.json"


def _load_json(path: Path, default: List[Any]) -> List[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return default.copy()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default.copy()


def _save_json(path: Path, data: List[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# --- Notes ---

def list_notes(user_scope_id: Optional[str] = None) -> List[dict]:
    """Return all notes for the user. Each note: id, title (optional), content, created_at."""
    path = _notes_path(user_scope_id)
    raw = _load_json(path, [])
    return [n for n in raw if isinstance(n, dict) and n.get("id")]


def add_note(
    user_scope_id: Optional[str],
    content: str,
    title: Optional[str] = None,
) -> dict:
    """Add a note. Returns the created note with id and created_at."""
    path = _notes_path(user_scope_id)
    notes = _load_json(path, [])
    note = {
        "id": str(uuid.uuid4())[:8],
        "title": (title or "").strip() or None,
        "content": (content or "").strip(),
        "created_at": datetime.now().isoformat(),
    }
    notes.append(note)
    _save_json(path, notes)
    return note


def delete_note(user_scope_id: Optional[str], note_id: str) -> bool:
    """Remove a note by id. Returns True if removed."""
    path = _notes_path(user_scope_id)
    notes = _load_json(path, [])
    before = len(notes)
    notes = [n for n in notes if isinstance(n, dict) and n.get("id") != note_id]
    if len(notes) < before:
        _save_json(path, notes)
        return True
    return False


# --- Todos ---

def list_todos(user_scope_id: Optional[str] = None) -> List[dict]:
    """Return all todos for the user. Each todo: id, text, created_at, due_at, done."""
    path = _todos_path(user_scope_id)
    raw = _load_json(path, [])
    return [t for t in raw if isinstance(t, dict) and t.get("id")]


def add_todo(
    user_scope_id: Optional[str],
    text: str,
    due_at: Optional[str] = None,
) -> dict:
    """Add a todo. due_at can be ISO8601 or YYYY-MM-DD. Returns the created todo."""
    path = _todos_path(user_scope_id)
    todos = _load_json(path, [])
    todo = {
        "id": str(uuid.uuid4())[:8],
        "text": (text or "").strip(),
        "created_at": datetime.now().isoformat(),
        "due_at": (due_at or "").strip() or None,
        "done": False,
    }
    todos.append(todo)
    _save_json(path, todos)
    return todo


def update_todo(
    user_scope_id: Optional[str],
    todo_id: str,
    text: Optional[str] = None,
    done: Optional[bool] = None,
    due_at: Optional[str] = None,
) -> Optional[dict]:
    """Update a todo by id. Returns the updated todo or None if not found."""
    path = _todos_path(user_scope_id)
    todos = _load_json(path, [])
    for t in todos:
        if isinstance(t, dict) and t.get("id") == todo_id:
            if text is not None:
                t["text"] = text.strip()
            if done is not None:
                t["done"] = bool(done)
            if due_at is not None:
                t["due_at"] = due_at.strip() or None
            _save_json(path, todos)
            return t
    return None


def delete_todo(user_scope_id: Optional[str], todo_id: str) -> bool:
    """Remove a todo by id. Returns True if removed."""
    path = _todos_path(user_scope_id)
    todos = _load_json(path, [])
    before = len(todos)
    todos = [t for t in todos if isinstance(t, dict) and t.get("id") != todo_id]
    if len(todos) < before:
        _save_json(path, todos)
        return True
    return False
