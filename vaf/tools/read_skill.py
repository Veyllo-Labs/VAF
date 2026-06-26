# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""read_skill tool — show the raw SKILL.md source of a skill visible to the caller.

Unlike use_skill (which loads a skill's body to follow it), this returns the raw source
(frontmatter + body) so the agent can inspect a skill before editing it with update_skill.
A skill not visible to the caller returns a uniform "not found" — never leaking the
existence of another user's private skill.
"""
from __future__ import annotations

from vaf.tools.base import BaseTool


class ReadSkillTool(BaseTool):
    name = "read_skill"
    description = (
        "Show the raw SKILL.md source of a Skill you can see — its YAML frontmatter "
        "(name, description) plus the full instruction body. Use this to inspect a skill "
        "before changing it with update_skill. To USE a skill (load and follow it), call "
        "use_skill instead."
    )
    permission_level = "read"
    side_effect_class = "none"
    parameters = {
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "The skill id, e.g. 'daily_standup'."},
        },
        "required": ["skill_id"],
    }

    def run(self, **kwargs) -> str:
        from vaf.core import skills_registry
        from vaf.skills.skill_md import derive_skill_id

        user_scope_id = kwargs.get("user_scope_id")
        sid = derive_skill_id(str(kwargs.get("skill_id", "")).strip())
        if not sid:
            return "error: no skill_id given."
        # Uniform message for both 'unknown' and 'someone else's private skill' (no existence leak).
        if not skills_registry.is_skill_visible_to_user(sid, user_scope_id):
            return f"error: skill '{sid}' not found or not available to you."
        src = skills_registry.get_skill_md_source(sid)
        if src is None:
            return f"error: skill '{sid}' has no SKILL.md."
        editable = skills_registry.can_user_edit_skill(sid, user_scope_id)
        return f"[SKILL SOURCE: {sid} — editable: {'yes' if editable else 'no'}]\n\n{src}"
