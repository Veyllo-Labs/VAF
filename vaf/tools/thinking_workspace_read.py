# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Thinking Workspace read tool (Thinking Mode only).
"""
from typing import Optional

from vaf.tools.base import BaseTool
from vaf.core.thinking_workspace import list_tasks, read_workspace_file, list_pending_handoffs


def _scope_str(user_scope_id) -> Optional[str]:
    if user_scope_id is None:
        return None
    return str(user_scope_id) if not isinstance(user_scope_id, str) else user_scope_id


class ThinkingWorkspaceReadTool(BaseTool):
    """Read-only access to per-user thinking workspace."""

    name = "thinking_workspace_read"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "Read per-user Thinking Workspace data: open tasks, pending handoffs, or a workspace file."
    )

    parameters = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["tasks", "pending_handoffs", "file"],
                "description": "Read mode",
            },
            "task_id": {"type": "string", "description": "Task ID for mode=file"},
            "path": {"type": "string", "description": "Workspace file path for mode=file"},
        },
        "required": ["mode"],
    }

    def run(self, **kwargs) -> str:
        scope = _scope_str(kwargs.get("user_scope_id"))
        mode = (kwargs.get("mode") or "").strip()
        try:
            if mode == "tasks":
                tasks = list_tasks(scope, status="open")
                if not tasks:
                    return "No open workspace tasks."
                lines = [f"- [{t.get('id')}] {t.get('title')} ({t.get('source')})" for t in tasks[:20]]
                return "Open workspace tasks:\n" + "\n".join(lines)
            if mode == "pending_handoffs":
                items = list_pending_handoffs(scope)
                if not items:
                    return "No pending handoffs."
                lines = [f"- [{h.get('id')}] task={h.get('task_id')} title={h.get('title')}" for h in items[:20]]
                return "Pending handoffs:\n" + "\n".join(lines)
            if mode == "file":
                task_id = (kwargs.get("task_id") or "").strip()
                path = (kwargs.get("path") or "").strip()
                if not task_id or not path:
                    return "Error: task_id and path are required for mode=file."
                content = read_workspace_file(scope, task_id, path)
                return content if content.strip() else "(empty file)"
            return "Error: Invalid mode."
        except Exception as e:
            return f"Error reading workspace: {e}"

