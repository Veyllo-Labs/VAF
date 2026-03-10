"""
Thinking Workspace write tool (Thinking Mode only, safe write zone).
"""
from typing import Optional

from vaf.tools.base import BaseTool
from vaf.core.thinking_workspace import create_task, write_workspace_file


def _scope_str(user_scope_id) -> Optional[str]:
    if user_scope_id is None:
        return None
    return str(user_scope_id) if not isinstance(user_scope_id, str) else user_scope_id


class ThinkingWorkspaceWriteTool(BaseTool):
    """Create tasks and write files in Thinking Workspace."""

    name = "thinking_workspace_write"
    description = (
        "Write safe artifacts into per-user Thinking Workspace: create_task or write_file. "
        "No destructive operations."
    )

    parameters = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["create_task", "write_file"],
                "description": "Write operation",
            },
            "title": {"type": "string", "description": "Task title for mode=create_task"},
            "source": {"type": "string", "description": "Task source for mode=create_task"},
            "description": {"type": "string", "description": "Task description for mode=create_task"},
            "task_id": {"type": "string", "description": "Task ID for mode=write_file"},
            "path": {"type": "string", "description": "Workspace relative path for mode=write_file"},
            "content": {"type": "string", "description": "Text content for mode=write_file"},
            "append": {"type": "boolean", "description": "Append instead of overwrite for mode=write_file"},
        },
        "required": ["mode"],
    }

    def run(self, **kwargs) -> str:
        scope = _scope_str(kwargs.get("user_scope_id"))
        mode = (kwargs.get("mode") or "").strip()
        try:
            if mode == "create_task":
                title = (kwargs.get("title") or "").strip()
                source = (kwargs.get("source") or "").strip() or "thinking_agent"
                desc = (kwargs.get("description") or "").strip()
                task = create_task(scope, title=title or "Untitled task", source=source, description=desc)
                return f"Workspace task created: [{task.get('id')}] {task.get('title')}"
            if mode == "write_file":
                task_id = (kwargs.get("task_id") or "").strip()
                path = (kwargs.get("path") or "").strip()
                if not task_id or not path:
                    return "Error: task_id and path are required for mode=write_file."
                content = kwargs.get("content") or ""
                append = bool(kwargs.get("append", False))
                write_workspace_file(scope, task_id, path, content, append=append)
                return f"Workspace file written: task={task_id}, path={path}, append={append}"
            return "Error: Invalid mode."
        except Exception as e:
            return f"Error writing workspace: {e}"

