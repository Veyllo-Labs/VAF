# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Skills discovery - mirrors vaf/workflows/templates.py.

Skills are discovered from ~/.vaf/skills/<skill_id>/SKILL.md (Anthropic Agent
Skills format). Only name+description are surfaced to the router (progressive
disclosure); the full body loads on demand via the use_skill tool.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.skills.skill_md import parse_skill_meta

logger = logging.getLogger(__name__)


def _skills_dir() -> Path:
    return Path.home() / ".vaf" / "skills"


def _discover_skills() -> Dict[str, Dict[str, Any]]:
    """Map skill_id -> parsed meta dict (valid AND invalid; callers filter)."""
    skills: Dict[str, Dict[str, Any]] = {}
    directory = _skills_dir()
    if not directory.exists():
        return skills
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            parsed = parse_skill_meta(skill_md)
            if parsed is None:
                continue
            skills[parsed["id"]] = parsed
        except Exception as exc:  # parser shouldn't raise, but never break discovery
            logger.warning("skills: could not load skill from %s: %s", child, exc)
            continue
    return skills


# Loaded once at import; refreshed via reload_skills().
SKILLS: Dict[str, Dict[str, Any]] = _discover_skills()


def get_skill(skill_id: str) -> Optional[Dict[str, Any]]:
    return SKILLS.get(skill_id)


def list_skills(user_scope_id: Optional[str] = None, include_invalid: bool = False) -> List[Dict[str, Any]]:
    """
    List skills visible to a user.

    user_scope_id=None means admin (sees all), matching the registry's semantics.
    include_invalid=True also returns skills that failed to parse (settings UI),
    each carrying its `error`; the router passes include_invalid=False so only
    routable skills are offered.

    Returns [{id, name, description, valid, error, shared_with, created_by}].
    """
    from vaf.core import skills_registry
    visible_ids = set(skills_registry.get_visible_skill_ids_for_user(user_scope_id))
    manifest = skills_registry.load_manifest().get("skills", {})

    out: List[Dict[str, Any]] = []
    for sid, parsed in SKILLS.items():
        if sid not in visible_ids:
            continue
        if not parsed.get("valid") and not include_invalid:
            continue
        entry = manifest.get(sid, {})
        out.append({
            "id": sid,
            "name": parsed.get("name") or sid,
            "description": parsed.get("description", ""),
            "valid": parsed.get("valid", False),
            "error": parsed.get("error"),
            "shared_with": entry.get("shared_with", ["*"]),
            "created_by": entry.get("created_by"),
            "scan": entry.get("scan"),
        })
    return out


def reload_skills() -> None:
    """Reload all skills (after create / update / delete / import)."""
    global SKILLS
    SKILLS = _discover_skills()
