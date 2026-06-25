# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Thinking Workspace handoff tool (Thinking Mode only).
"""
from typing import Optional

from vaf.tools.base import BaseTool
from vaf.core.thinking_workspace import create_handoff


def _scope_str(user_scope_id) -> Optional[str]:
    if user_scope_id is None:
        return None
    return str(user_scope_id) if not isinstance(user_scope_id, str) else user_scope_id


class ThinkingWorkspaceHandoffTool(BaseTool):
    """Create approval-required handoff proposals in Thinking Workspace."""

    name = "thinking_workspace_handoff"
    permission_level = "system"
    side_effect_class = "reversible"
    description = (
        "Create a pending handoff proposal in Thinking Workspace. "
        "Use this for externally visible actions that require user approval."
    )

    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Workspace task id"},
            "title": {"type": "string", "description": "Handoff title"},
            "content": {"type": "string", "description": "Handoff details/summary markdown"},
            "proposed_action": {"type": "string", "description": "Action label, e.g. review_and_approve"},
            "automation_action": {
                "type": "object",
                "description": "Optional automation action for approval bridge. "
                "Example create: {operation:'create', prompt:'...', name:'...', frequency:'daily', time:'08:00'}. "
                "Example update: {operation:'update', task_id:'abcd1234', time:'09:00', enabled:true}.",
            },
        },
        "required": ["task_id", "title", "content"],
    }

    def run(self, **kwargs) -> str:
        scope = _scope_str(kwargs.get("user_scope_id"))
        task_id = (kwargs.get("task_id") or "").strip()
        title = (kwargs.get("title") or "").strip()
        content = kwargs.get("content") or ""
        proposed_action = (kwargs.get("proposed_action") or "").strip()
        automation_action = kwargs.get("automation_action")
        if not task_id:
            return "Error: task_id is required."
        if not title:
            return "Error: title is required."
        if not str(content).strip():
            return "Error: content is required."
        try:
            handoff = create_handoff(
                user_scope_id=scope,
                task_id=task_id,
                title=title,
                content=str(content),
                proposed_action=proposed_action,
                automation_action=automation_action if isinstance(automation_action, dict) else None,
            )
            return f"Handoff created: [{handoff.get('id')}] for task {task_id}"
        except Exception as e:
            return f"Error creating handoff: {e}"

