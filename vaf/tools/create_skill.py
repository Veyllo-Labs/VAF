# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""create_skill tool — let a user create their OWN private Skill via the agent.

Mirrors the admin WebUI create path (validate -> safety-scan -> save -> register) but
without the admin gate: the new skill is owned by and private to the calling user
(shared_with = [owner_scope]). A HIGH safety-scan result blocks the create (no override
is exposed to the agent — sharing/override stay an admin/WebUI action).
"""
from __future__ import annotations

from vaf.tools.base import BaseTool


def _build_skill_md(kwargs: dict) -> str:
    """Build SKILL.md content from a raw `skill_md`, else from name/description/body."""
    raw = (kwargs.get("skill_md") or "").strip()
    if raw:
        return raw if raw.endswith("\n") else raw + "\n"
    import yaml
    fm = yaml.safe_dump(
        {"name": (kwargs.get("name") or "").strip(), "description": (kwargs.get("description") or "").strip()},
        sort_keys=False, allow_unicode=True,
    ).strip()
    return f"---\n{fm}\n---\n\n{kwargs.get('body') or ''}\n"


class CreateSkillTool(BaseTool):
    name = "create_skill"
    category    = "skills"
    identity_kwargs = ("user_scope_id", "username")
    description = (
        "Create a new private Skill owned by the current user — a reusable expert procedure "
        "(SKILL.md). Provide a snake_case `skill_id` plus EITHER a full `skill_md` (YAML "
        "frontmatter + body) OR `name` + `description` + `body`. The `description` is what "
        "future routing matches on, so make it specific. The skill is private to you (and "
        "admins) until an admin shares it, and must pass a safety scan. Change it later with "
        "update_skill."
    )
    permission_level = "write"
    side_effect_class = "reversible"
    parameters = {
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "description": "snake_case id, e.g. 'daily_standup'."},
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
        from vaf.core.config import get_local_admin_scope_id

        user_scope_id = kwargs.get("user_scope_id")
        username = kwargs.get("username") or "admin"
        # Admin (scope None) gets a real owner id too, so every skill has a consistent owner.
        owner_scope = user_scope_id if user_scope_id is not None else get_local_admin_scope_id()

        try:
            sid = skills_registry.validate_skill_id(str(kwargs.get("skill_id", "")))
        except ValueError as e:
            return f"error: {e}"
        # resolve_skill_folder so a SHIPPED id counts as taken too: a user-dir copy
        # replaces the builtin at discovery for everyone, so creating one here would
        # let any user knock a shipped skill out for every other user.
        taken = skills_registry.resolve_skill_folder(sid)
        if taken is not None:
            if taken == skills_registry.skill_folder(sid):
                return f"error: skill '{sid}' already exists. Use update_skill to modify it."
            return f"error: skill id '{sid}' ships with VAF; choose a different id."

        content = _build_skill_md(kwargs)
        parsed = parse_skill_md_text(content, sid)
        if not parsed.get("valid"):
            return f"error: invalid skill: {parsed.get('error')}"
        # Known-bad first: if these exact bytes are on the machine's list, no score
        # and no wording gets past it. Cheap - one hash of the text.
        from vaf.core.threat_db import check_bytes, emit_threat_block
        listed = check_bytes(content.encode("utf-8"))
        if listed is not None:
            emit_threat_block("skill_create", sid, listed, username)
            return ("error: this skill content is on the machine's known-bad list "
                    f"({listed.get('reason') or 'listed as dangerous'}). "
                    "An administrator must delist it first.")

        scan = scan_skill_md_text(content)
        if scan.get("level") == "high":
            emit_skill_security_event("skill_blocked", "create", sid, scan)
            return "error: skill blocked by safety scan.\n" + format_findings(scan)

        skills_registry.save_skill_md(sid, content)
        skills_registry.register_skill(
            sid, created_by=username, shared_with=[owner_scope],
            owner_scope_id=owner_scope, scan=scan,
        )
        skills_templates.reload_skills()
        return f"Created skill '{sid}' (private to you). Load it with use_skill, change it with update_skill."
