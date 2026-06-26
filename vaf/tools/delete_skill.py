# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""delete_skill tool — let a user delete a Skill they OWN (admin may delete any).

Destructive + irreversible, so it is gated as a "dangerous" tool (the dispatch layer
prompts for confirmation). Authorization is by ownership: a user can only delete their
own skills; another user's private skill returns a uniform "not yours".
"""
from __future__ import annotations

from vaf.tools.base import BaseTool


class DeleteSkillTool(BaseTool):
    name = "delete_skill"
    description = (
        "Delete a Skill you own (removes its SKILL.md and bundled files permanently). You can "
        "only delete your own skills. This cannot be undone."
    )
    permission_level = "dangerous"
    side_effect_class = "irreversible"
    parameters = {
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "The skill id to delete, e.g. 'daily_standup'."},
        },
        "required": ["skill_id"],
    }

    def run(self, **kwargs) -> str:
        from vaf.core import skills_registry
        from vaf.skills import templates as skills_templates

        user_scope_id = kwargs.get("user_scope_id")
        try:
            sid = skills_registry.validate_skill_id(str(kwargs.get("skill_id", "")))
        except ValueError as e:
            return f"error: {e}"
        if not skills_registry.can_user_edit_skill(sid, user_scope_id):
            return f"error: skill '{sid}' not found or not yours to delete."
        try:
            skills_registry.delete_skill(sid)
        except FileNotFoundError:
            return f"error: skill '{sid}' not found."
        skills_templates.reload_skills()
        return f"Deleted skill '{sid}'."
