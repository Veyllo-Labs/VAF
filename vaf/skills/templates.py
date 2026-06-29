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


def _skills_signature() -> tuple:
    """Cheap fingerprint of ~/.vaf/skills (skill count + newest SKILL.md mtime).

    Changes whenever a skill is added, removed, or edited. Used to auto-refresh
    the in-memory cache on read without a restart — and regardless of which
    process wrote the file (a hand-dropped SKILL.md or one created by a sub-agent
    running in a separate process). Just a handful of stat() calls; no watcher.
    """
    directory = _skills_dir()
    count = 0
    latest = 0
    try:
        if directory.exists():
            for child in directory.iterdir():
                if not child.is_dir():
                    continue
                try:
                    st = (child / "SKILL.md").stat()
                except OSError:
                    continue
                count += 1
                if st.st_mtime_ns > latest:
                    latest = st.st_mtime_ns
    except Exception:
        pass
    return (count, latest)


# Loaded once at import; auto-refreshed on read when the skills dir changes
# (see _ensure_fresh) and force-refreshed via reload_skills().
SKILLS: Dict[str, Dict[str, Any]] = _discover_skills()
_SKILLS_SIG: tuple = _skills_signature()


def _ensure_fresh() -> None:
    """Re-scan the skills dir and rebind SKILLS only when its signature changed."""
    global SKILLS, _SKILLS_SIG
    try:
        sig = _skills_signature()
    except Exception:
        return
    if sig != _SKILLS_SIG:
        SKILLS = _discover_skills()
        _SKILLS_SIG = sig


def get_skill(skill_id: str) -> Optional[Dict[str, Any]]:
    _ensure_fresh()
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
    _ensure_fresh()
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
            "owner_scope_id": entry.get("owner_scope_id"),
            "scan": entry.get("scan"),
        })
    return out


def reload_skills() -> None:
    """Reload all skills (after create / update / delete / import)."""
    global SKILLS, _SKILLS_SIG
    SKILLS = _discover_skills()
    _SKILLS_SIG = _skills_signature()
