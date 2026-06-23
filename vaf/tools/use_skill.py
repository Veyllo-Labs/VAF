"""
use_skill - progressive-disclosure delivery for VAF Skills (Anthropic Agent Skills).

The router only ever sees each skill's name+description. When the agent decides a
skill is relevant it calls use_skill(skill_id); this tool loads the full SKILL.md
body into context plus a listing of the skill's bundled files (read-only references
the agent can open with read_file). The skill's scripts are NOT executed here - to
run one the agent uses the normal, separately-gated bash/python tools.
"""
from __future__ import annotations

import logging

from vaf.tools.base import BaseTool

logger = logging.getLogger(__name__)

# Mirror read_file's truncation budget; encourages bundled references over fat bodies.
_MAX_BODY_CHARS = 14000


class UseSkillTool(BaseTool):
    name = "use_skill"
    description = (
        "Load the full instructions for a named Skill. Skills are reusable expert "
        "procedures contributed by the user. You only see each skill's short "
        "description until you call this tool. Call use_skill(skill_id) to load the "
        "complete step-by-step instructions plus the list of bundled files (scripts, "
        "references) that ship with the skill, then follow them. Read any referenced "
        "bundled file with read_file using the absolute path provided. Use the "
        "skill_id from a [SKILL SUGGESTION] hint or from list_skills."
    )
    permission_level = "read"
    side_effect_class = "none"
    input_examples = [{"skill_id": "pdf_form_filler"}]
    parameters = {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": "The skill id, e.g. 'pdf_form_filler'.",
            }
        },
        "required": ["skill_id"],
    }

    def run(self, **kwargs) -> str:
        from vaf.core import skills_registry
        from vaf.skills import templates as skills_templates
        from vaf.skills.skill_md import derive_skill_id, parse_skill_md

        # user_scope_id is injected by the agent (None = admin / local).
        user_scope_id = kwargs.get("user_scope_id", None)

        raw = str(kwargs.get("skill_id", "")).strip()
        if raw.lower().startswith("skill:"):
            raw = raw.split(":", 1)[1].strip()
        skill_id = derive_skill_id(raw)

        def _available() -> str:
            ids = [s["id"] for s in skills_templates.list_skills(user_scope_id=user_scope_id)]
            return ", ".join(ids) if ids else "(none)"

        if not skill_id:
            return f"error: no skill_id given. Available skills: {_available()}"

        folder = skills_registry.skill_folder(skill_id)
        if not (folder / "SKILL.md").exists():
            return f"error: skill '{skill_id}' not found. Available skills: {_available()}"

        if not skills_registry.is_skill_visible_to_user(skill_id, user_scope_id):
            return f"error: skill '{skill_id}' is not available to you."

        parsed = parse_skill_md(folder / "SKILL.md")
        if not parsed.get("valid"):
            return f"error: skill '{skill_id}' is invalid: {parsed.get('error')}"

        body = parsed.get("body", "") or "(this skill has no instruction body)"
        if len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS] + (
                f"\n\n... (truncated - read the full file at {folder / 'SKILL.md'})"
            )

        lines = [f'[SKILL: {skill_id} - "{parsed.get("name")}"]', "", body]
        bundled = parsed.get("bundled_files") or []
        if bundled:
            lines += [
                "",
                "--- BUNDLED FILES (read with read_file using the absolute path) ---",
            ]
            for rel in bundled:
                lines.append(f"- {rel}  ->  {folder / rel}")
            lines += [
                "",
                "Read a bundled file only when the instructions reference it. To RUN a "
                "bundled script, use the bash/python tools (they confirm first).",
            ]
        return "\n".join(lines)
