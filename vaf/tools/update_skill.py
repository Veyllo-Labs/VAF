# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""update_skill tool — let a user edit a Skill they OWN (admin may edit any).

Authorization is by ownership (skills_registry.can_user_edit_skill), not visibility: a
user can only change their own skills; another user's private skill returns a uniform
"not yours". Owner, share list and created_by are preserved across the edit; the new
content still must validate and pass the safety scan.
"""
from __future__ import annotations

from vaf.tools.base import BaseTool
from vaf.tools.create_skill import _build_skill_md


class UpdateSkillTool(BaseTool):
    name = "update_skill"
    description = (
        "Edit a Skill you own (its name/description/body). Provide the `skill_id` plus EITHER "
        "a full `skill_md` OR `name` + `description` + `body` (the new full content replaces the "
        "old). Read the current source first with read_skill. You can only edit your own skills; "
        "the change must pass a safety scan."
    )
    permission_level = "write"
    side_effect_class = "reversible"
    parameters = {
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "The skill id to edit, e.g. 'daily_standup'."},
            "name": {"type": "string", "description": "Human-readable name (when not using skill_md)."},
            "description": {"type": "string", "description": "Short description shown to routing (when not using skill_md)."},
            "body": {"type": "string", "description": "The step-by-step instructions in markdown (when not using skill_md)."},
            "skill_md": {"type": "string", "description": "Optional: the full raw SKILL.md (frontmatter + body) instead of name/description/body."},
        },
        "required": ["skill_id"],
    }

    def run(self, **kwargs) -> str:
        from vaf.core import skills_registry
        from vaf.skills import templates as skills_templates
        from vaf.skills.skill_md import parse_skill_md_text
        from vaf.skills.scanner import scan_skill_md_text, format_findings, emit_skill_security_event

        user_scope_id = kwargs.get("user_scope_id")
        username = kwargs.get("username") or "admin"

        try:
            sid = skills_registry.validate_skill_id(str(kwargs.get("skill_id", "")))
        except ValueError as e:
            return f"error: {e}"
        # Authorization first (covers both 'unknown' and 'someone else's' — no existence leak).
        if not skills_registry.can_user_edit_skill(sid, user_scope_id):
            return f"error: skill '{sid}' not found or not yours to edit."
        if not (skills_registry.skill_folder(sid) / "SKILL.md").exists():
            return f"error: skill '{sid}' does not exist. Use create_skill."

        content = _build_skill_md(kwargs)
        parsed = parse_skill_md_text(content, sid)
        if not parsed.get("valid"):
            return f"error: invalid skill: {parsed.get('error')}"
        scan = scan_skill_md_text(content)
        if scan.get("level") == "high":
            emit_skill_security_event("skill_blocked", "update", sid, scan)
            return "error: skill blocked by safety scan.\n" + format_findings(scan)

        # Preserve ownership + share list + created_by across the edit.
        prior = skills_registry.get_skill_manifest_entry(sid) or {}
        skills_registry.save_skill_md(sid, content)
        skills_registry.register_skill(
            sid,
            created_by=(prior.get("created_by") or username),
            # `or []` (admin-only) guards against a missing share list ever defaulting to
            # public (["*"]) inside register_skill — an edit must never widen visibility.
            shared_with=prior.get("shared_with") or [],
            owner_scope_id=prior.get("owner_scope_id"),
            scan=scan,
        )
        skills_templates.reload_skills()
        return f"Updated skill '{sid}'."
