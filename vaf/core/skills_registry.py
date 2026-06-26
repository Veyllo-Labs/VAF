# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Skills Registry
===============
Manages user-authored / uploaded Skills (Anthropic Agent Skills, SKILL.md format)
stored as folders under ~/.vaf/skills/.

Storage layout:
    ~/.vaf/skills/
        manifest.json
        <skill_id>/
            SKILL.md            # YAML frontmatter (name, description) + markdown body
            scripts/ ...        # optional bundled files (read-only references)

Manifest structure (mirrors custom_tools_registry):
    {
        "version": 1,
        "skills": {
            "pdf_form_filler": {
                "folder":      "pdf_form_filler",
                "created_by":  "admin",
                "created_at":  "2026-06-23T...",
                "updated_at":  "2026-06-23T...",
                "shared_with": ["*"]   # "*" = all users, [] = admin only, [ids] = specific + admin
            }
        }
    }

Access rules are identical to custom tools:
    - Only admin users may create / delete / update permissions (callers enforce).
    - "shared_with": ["*"]  -> visible to everyone.
    - "shared_with": []     -> admin only.
    - "shared_with": [ids]  -> those users + admin.

The path layout deliberately mirrors ~/.vaf/workflows (per-item, user-owned),
NOT Platform.data_dir(): skills are user content surfaced under progressive
disclosure, like workflows. File I/O follows the workflow pattern (atomic write +
reload); visibility/scoping follows the custom-tools pattern (shared_with).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.skills.skill_md import derive_skill_id, parse_skill_md

logger = logging.getLogger(__name__)

# RLock: mutators hold it across load+modify+save and re-enter load_manifest().
_manifest_lock = threading.RLock()

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Ids that would collide with the filesystem jail / blocked dirs or our own files.
_RESERVED_IDS = {"git", "env", "ssh", "node_modules", "manifest"}


# ─────────────────────────────────────────────────────────────────────────────
# Directory helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_skills_dir() -> Path:
    """Return (and create if missing) the skills directory."""
    directory = Path.home() / ".vaf" / "skills"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def skill_folder(skill_id: str) -> Path:
    return get_skills_dir() / skill_id


def _manifest_path() -> Path:
    return get_skills_dir() / "manifest.json"


def validate_skill_id(skill_id: str) -> str:
    """Normalize and validate a skill id. Returns the id or raises ValueError."""
    sid = derive_skill_id(skill_id)
    if not sid:
        raise ValueError("skill_id is empty after normalization")
    if not _ID_RE.match(sid):
        raise ValueError(f"skill_id must be lowercase snake_case, got '{skill_id}'")
    if sid in _RESERVED_IDS:
        raise ValueError(f"skill_id '{sid}' is reserved")
    return sid


# ─────────────────────────────────────────────────────────────────────────────
# Manifest I/O  (always go through these)
# ─────────────────────────────────────────────────────────────────────────────

def load_manifest() -> Dict[str, Any]:
    with _manifest_lock:
        path = _manifest_path()
        if not path.exists():
            return {"version": 1, "skills": {}}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data.setdefault("version", 1)
            data.setdefault("skills", {})
            return data
        except Exception as exc:
            logger.error("skills_registry: failed to read manifest.json: %s", exc)
            return {"version": 1, "skills": {}}


def _save_manifest(data: Dict[str, Any]) -> None:
    """Atomically write manifest.json (temp + rename). Call with _manifest_lock held."""
    path = _manifest_path()
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Access-control helpers  (identical semantics to custom_tools_registry)
# ─────────────────────────────────────────────────────────────────────────────

def get_visible_skill_ids_for_user(user_scope_id: Optional[str]) -> List[str]:
    """
    Skill ids visible to the given user.
    - user_scope_id=None  -> admin, sees everything.
    - shared_with=["*"]   -> everyone.
    - shared_with=[]      -> admin only.
    - shared_with=[ids]   -> those users + admin.
    """
    manifest = load_manifest()
    visible: List[str] = []
    for sid, entry in manifest.get("skills", {}).items():
        shared_with: List[str] = entry.get("shared_with", ["*"])
        if user_scope_id is None:
            visible.append(sid)
        elif "*" in shared_with:
            visible.append(sid)
        elif user_scope_id in shared_with:
            visible.append(sid)
    return visible


def is_skill_visible_to_user(skill_id: str, user_scope_id: Optional[str]) -> bool:
    return skill_id in get_visible_skill_ids_for_user(user_scope_id)


def can_user_edit_skill(skill_id: str, user_scope_id: Optional[str]) -> bool:
    """Whether this user may edit/delete the skill (NOT the same as visibility).

    - admin (user_scope_id is None, or equals the local-admin scope) -> may edit any skill.
    - a real user -> only their OWN skill (entry.owner_scope_id == user_scope_id).
    - legacy / admin-WebUI skills without an owner -> admin-only.
    Returns False for an unknown skill id.
    """
    entry = get_skill_manifest_entry(skill_id)
    if entry is None:
        return False
    from vaf.core.config import get_local_admin_scope_id
    if user_scope_id is None or str(user_scope_id) == str(get_local_admin_scope_id()):
        return True
    owner = entry.get("owner_scope_id")
    if owner is None:
        return False
    return str(owner) == str(user_scope_id)


# ─────────────────────────────────────────────────────────────────────────────
# CRUD  (all admin-only — callers must enforce)
# ─────────────────────────────────────────────────────────────────────────────

def register_skill(
    skill_id: str,
    created_by: str,
    shared_with: Optional[List[str]] = None,
    scan: Optional[Dict[str, Any]] = None,
    owner_scope_id: Optional[str] = None,
) -> None:
    """Add or overwrite a skill entry in the manifest. Call after writing the folder.

    `scan` (optional) is the security-scanner result; a compact {score, level,
    count} is persisted so the UI can badge risky skills.

    `owner_scope_id` (optional) records WHICH user (scope id) owns this skill, so the
    agent's self-service skill tools can let a user edit/delete only their own skills
    (see `can_user_edit_skill`). Default None preserves an existing owner on re-register
    and leaves admin/WebUI-created skills without an owner (admin-only edit) — the
    existing admin path passes no owner and is unchanged.
    """
    if shared_with is None:
        shared_with = ["*"]
    now = datetime.now(timezone.utc).isoformat()
    with _manifest_lock:
        data = load_manifest()
        existing = data["skills"].get(skill_id, {})
        entry = {
            "folder": skill_id,
            "created_by": created_by,
            "created_at": existing.get("created_at", now),
            "updated_at": now,
            "shared_with": shared_with,
            # Preserve an existing owner when the caller passes None (e.g. an admin
            # WebUI update of a user-owned skill must not strip ownership).
            "owner_scope_id": owner_scope_id if owner_scope_id is not None else existing.get("owner_scope_id"),
        }
        if scan is not None:
            entry["scan"] = {
                "score": scan.get("score", 0),
                "level": scan.get("level", "clean"),
                "count": len(scan.get("findings", [])),
            }
        data["skills"][skill_id] = entry
        _save_manifest(data)
    logger.info("skills_registry: registered skill '%s' (shared_with=%s)", skill_id, shared_with)


def delete_skill(skill_id: str) -> None:
    """Remove the skill from the manifest and delete its folder."""
    with _manifest_lock:
        data = load_manifest()
        entry = data["skills"].pop(skill_id, None)
        if entry is None:
            raise FileNotFoundError(f"Skill '{skill_id}' not found in manifest")
        folder = skill_folder(skill_id)
        if folder.exists():
            try:
                shutil.rmtree(folder)
            except Exception as exc:
                logger.warning("skills_registry: could not delete folder '%s': %s", folder, exc)
        _save_manifest(data)
    logger.info("skills_registry: deleted skill '%s'", skill_id)


def update_skill_permissions(skill_id: str, shared_with: List[str]) -> None:
    """Update the shared_with list for an existing skill."""
    with _manifest_lock:
        data = load_manifest()
        if skill_id not in data["skills"]:
            raise KeyError(f"Skill '{skill_id}' not found in manifest")
        data["skills"][skill_id]["shared_with"] = shared_with
        data["skills"][skill_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_manifest(data)
    logger.info("skills_registry: updated permissions for '%s' -> %s", skill_id, shared_with)


# ─────────────────────────────────────────────────────────────────────────────
# SKILL.md file helpers (editor "write" path)
# ─────────────────────────────────────────────────────────────────────────────

def save_skill_md(skill_id: str, content: str) -> Path:
    """
    Write SKILL.md for a skill folder atomically (creates the folder).
    Returns the path. Caller validates content (parse_skill_md) and calls
    register_skill afterwards.
    """
    folder = skill_folder(skill_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    fd, tmp_path = tempfile.mkstemp(dir=folder, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


def get_skill_md_source(skill_id: str) -> Optional[str]:
    """Return the raw SKILL.md text for the editor, or None if missing."""
    path = skill_folder(skill_id) / "SKILL.md"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("skills_registry: cannot read SKILL.md for '%s': %s", skill_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Zip / folder import (safe-extract)
# ─────────────────────────────────────────────────────────────────────────────

def import_skill_zip(
    zip_path: Path | str,
    created_by: str,
    shared_with: Optional[List[str]] = None,
    override: bool = False,
) -> str:
    """
    Extract an uploaded .zip into ~/.vaf/skills/<skill_id>/, validate, register.

    Returns the skill_id. Raises ValueError on any unsafe / invalid archive:
      - no SKILL.md at the root or inside a single top-level folder
      - any entry that escapes the destination (Zip-Slip / absolute path)
      - any symlink entry
      - a SKILL.md that fails to parse
    Raises SkillScanBlocked when the security scanner flags HIGH risk (unless
    override=True). The scanner result is always recorded in the manifest.
    """
    zip_path = Path(zip_path)
    if shared_with is None:
        shared_with = ["*"]

    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n and not n.endswith("/")]
        if not names:
            raise ValueError("zip archive is empty")

        # Locate SKILL.md at archive root or inside a single top-level folder.
        root_prefix = None
        for n in names:
            parts = n.replace("\\", "/").split("/")
            if parts[-1] == "SKILL.md":
                if len(parts) == 1:
                    root_prefix = ""
                    break
                if len(parts) == 2:
                    root_prefix = parts[0] + "/"
                    break
        if root_prefix is None:
            raise ValueError(
                "zip must contain a SKILL.md at the archive root or inside a single top-level folder"
            )

        base_name = root_prefix.rstrip("/") if root_prefix else zip_path.stem
        skill_id = validate_skill_id(base_name)
        dest = (get_skills_dir() / skill_id).resolve()

        # Stage into a temp dir inside the skills folder, then move atomically.
        with tempfile.TemporaryDirectory(dir=str(get_skills_dir())) as staging:
            extract_dir = (Path(staging) / "extracted").resolve()
            extract_dir.mkdir()
            for member in zf.infolist():
                mname = member.filename.replace("\\", "/")
                if mname.endswith("/"):
                    continue
                # Reject symlinks (Unix mode 0o120000 in external_attr high bits).
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"zip contains a symlink entry ('{mname}') - refused")
                rel = mname[len(root_prefix):] if root_prefix and mname.startswith(root_prefix) else mname
                rel = rel.lstrip("/")
                if not rel:
                    continue
                target = (extract_dir / rel).resolve()
                if target != extract_dir and not target.is_relative_to(extract_dir):
                    raise ValueError(f"zip entry escapes skill folder (path traversal): '{mname}'")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)

            parsed = parse_skill_md(extract_dir / "SKILL.md")
            if not parsed.get("valid"):
                raise ValueError(f"invalid SKILL.md: {parsed.get('error')}")

            # Security scan the full bundle (body + every bundled file) before install.
            from vaf.skills.scanner import scan_skill_folder, SkillScanBlocked
            scan = scan_skill_folder(extract_dir)
            if scan["level"] == "high" and not override:
                raise SkillScanBlocked(scan)

            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(extract_dir), str(dest))

    register_skill(skill_id, created_by=created_by, shared_with=shared_with, scan=scan)
    logger.info("skills_registry: imported skill '%s' from zip (scan=%s)", skill_id, scan["level"])
    return skill_id


# ─────────────────────────────────────────────────────────────────────────────
# Convenience read helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_all_skill_ids() -> List[str]:
    """All registered skill ids (admin view — no filtering)."""
    return list(load_manifest().get("skills", {}).keys())


def get_skill_manifest_entry(skill_id: str) -> Optional[Dict[str, Any]]:
    return load_manifest().get("skills", {}).get(skill_id)
