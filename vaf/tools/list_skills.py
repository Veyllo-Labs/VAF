# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""list_skills tool — list the Skills available to the calling user (user-isolated).

Visibility is governed by the registry's shared_with rules, so a user only ever sees
skills shared with them (or public ones, or — for admin — everything). Skills the user
owns are flagged so they know which ones update_skill / delete_skill will accept.
"""
from __future__ import annotations

from vaf.tools.base import BaseTool


class ListSkillsTool(BaseTool):
    name = "list_skills"
    description = (
        "List the Skills available to you (reusable expert procedures). Returns each skill's "
        "id, name and short description; skills you OWN are flagged [yours] and can be changed "
        "with update_skill / delete_skill. Call use_skill(skill_id) to load a skill's full "
        "instructions, read_skill(skill_id) to see its raw source, or create_skill to add one."
    )
    permission_level = "read"
    side_effect_class = "none"
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **kwargs) -> str:
        from vaf.skills import templates as skills_templates
        from vaf.core.config import get_local_admin_scope_id

        user_scope_id = kwargs.get("user_scope_id")
        try:
            rows = skills_templates.list_skills(user_scope_id=user_scope_id)
        except Exception as e:  # pragma: no cover - defensive
            return f"error: could not list skills: {e}"
        if not rows:
            return "You have no skills available yet. Use create_skill to make one."

        is_admin = user_scope_id is None or str(user_scope_id) == str(get_local_admin_scope_id())
        lines = ["Available skills:"]
        for r in rows:
            sid = r.get("id")
            owner = r.get("owner_scope_id")
            editable = is_admin or (owner is not None and str(owner) == str(user_scope_id))
            name = r.get("name") or sid
            desc = (r.get("description") or "").strip().replace("\n", " ")
            lines.append(f"- {sid}: {name} — {desc}{' [yours]' if editable else ''}")
        return "\n".join(lines)
