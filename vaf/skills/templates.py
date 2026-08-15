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


def _title_of(parsed: Dict[str, Any]) -> str:
    """A human headline for a skill, or "" when it has none.

    `metadata.title` in the frontmatter, read defensively: it comes from a file
    anybody may drop into the skills folder, so a title that is not a string, or
    is a whole page long, is not a title. "" means "no headline given" and lets a
    surface fall back to the name rather than printing an empty line.
    """
    meta = (parsed.get("frontmatter") or {}).get("metadata")
    if not isinstance(meta, dict):
        return ""
    title = meta.get("title")
    return title.strip()[:80] if isinstance(title, str) and title.strip() else ""


def _skills_dir() -> Path:
    return Path.home() / ".vaf" / "skills"


def _builtin_skills_dir() -> Path:
    """Skills that SHIP with VAF, inside the package: vaf/skills/builtin/<id>/SKILL.md.

    A second discovery root instead of a copy-on-first-start seed, for two reasons
    that both bit this repo before: a seeded copy never receives an update (the text
    documents commands, and commands move), and seeding needs a verified call site
    on every startup path - web, tray, CLI - which is exactly the wiring that has
    been forgotten here before. A package file is simply there.

    Deliberate boundary: the security scanner keeps to the USER dir. A builtin
    skill is repository content, reviewed the way code is; scanning it would rate
    our own release with a tool meant for strangers' uploads.
    """
    return Path(__file__).parent / "builtin"


def _scan_one_root(skills: Dict[str, Dict[str, Any]], directory: Path,
                   builtin: bool) -> None:
    if not directory.exists():
        return
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
            parsed["builtin"] = builtin
            skills[parsed["id"]] = parsed
        except Exception as exc:  # parser shouldn't raise, but never break discovery
            logger.warning("skills: could not load skill from %s: %s", child, exc)
            continue


def _discover_skills() -> Dict[str, Dict[str, Any]]:
    """Map skill_id -> parsed meta dict (valid AND invalid; callers filter).

    Builtin first, the user's own dir second: on an id collision the USER'S copy
    wins, which is what makes a shipped skill customisable without a fork - copy
    the folder into ~/.vaf/skills under the same name and edit.
    """
    skills: Dict[str, Dict[str, Any]] = {}
    _scan_one_root(skills, _builtin_skills_dir(), builtin=True)
    _scan_one_root(skills, _skills_dir(), builtin=False)
    return skills


def builtin_skill_ids() -> List[str]:
    """The ids that ship with the package. The registry's visibility check calls
    this, because a builtin skill has no manifest entry to be visible through."""
    directory = _builtin_skills_dir()
    if not directory.exists():
        return []
    return sorted(child.name for child in directory.iterdir()
                  if child.is_dir() and (child / "SKILL.md").exists())


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
        # A builtin skill ships with the package and has no manifest entry to be
        # visible through: it is visible to everyone by construction. The registry
        # answers the same way for use_skill (is_skill_visible_to_user), so the
        # list and the loader cannot disagree about what exists.
        if sid not in visible_ids and not parsed.get("builtin"):
            continue
        if not parsed.get("valid") and not include_invalid:
            continue
        entry = manifest.get(sid, {})
        out.append({
            "id": sid,
            "name": parsed.get("name") or sid,
            # The Agent Skills format wants `name` to be the folder's name, so it
            # is an identifier and not a headline. A human title belongs in
            # `metadata`, which the format allows for exactly this - and a
            # surface that has one shows it instead of the identifier.
            "title": _title_of(parsed),
            "description": parsed.get("description", ""),
            "valid": parsed.get("valid", False),
            "error": parsed.get("error"),
            "shared_with": entry.get("shared_with", ["*"]),
            "created_by": entry.get("created_by") or ("vaf" if parsed.get("builtin") else None),
            "owner_scope_id": entry.get("owner_scope_id"),
            "scan": entry.get("scan"),
            "builtin": bool(parsed.get("builtin")),
        })
    return out


def reload_skills() -> None:
    """Reload all skills (after create / update / delete / import)."""
    global SKILLS, _SKILLS_SIG
    SKILLS = _discover_skills()
    _SKILLS_SIG = _skills_signature()
