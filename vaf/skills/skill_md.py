"""
SKILL.md parser - Anthropic Agent Skills format.

A skill is a FOLDER containing a SKILL.md file:

    ~/.vaf/skills/<skill_id>/
        SKILL.md           # YAML frontmatter (name, description, ...) + Markdown body
        scripts/ ...       # optional bundled files (read-only references)

SKILL.md layout:

    ---
    name: PDF Form Filler
    description: Fills PDF forms from a JSON field map. Use when the user has a
                 fillable PDF and the values to put in it.
    ---
    # PDF Form Filler
    ... markdown instructions ...

This module is the FORMAT AUTHORITY: pure parsing - no I/O policy, no LLM, no
network. It reports validity; the registry/discovery layer decides routing
eligibility. A malformed skill NEVER raises - it comes back valid=False with an
error string so the UI can show "broken skill" instead of the skill vanishing.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Files/dirs never surfaced as bundled resources.
_IGNORED_NAMES = {"SKILL.md", "__pycache__", ".DS_Store"}
_MAX_BUNDLED_FILES = 200  # bound the listing for pathological folders

# A frontmatter fence line. A leading UTF-8 BOM is stripped at read time
# (encoding="utf-8-sig"), so the regex itself stays plain ASCII.
_FENCE_RE = re.compile(r"^---[ \t]*$")


def derive_skill_id(folder_name: str) -> str:
    """
    Derive a snake_case skill id from a folder name.

    The FOLDER is the skill's identity (Anthropic-faithful). The frontmatter
    `name` is a human display label and is NOT trusted as the id.

        "pdf-form-filler"  -> "pdf_form_filler"
        "My Cool Skill!"   -> "my_cool_skill"
    """
    s = (folder_name or "").strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _split_frontmatter(text: str) -> tuple[Optional[str], str]:
    """
    Split a SKILL.md string into (frontmatter_yaml, body).

    Returns (None, full_text) when there is no leading '---' fence or no closing
    fence (so a body-only file degrades gracefully to "missing frontmatter").
    """
    lines = text.splitlines()
    if not lines or not _FENCE_RE.match(lines[0]):
        return None, text
    for i in range(1, len(lines)):
        if _FENCE_RE.match(lines[i]):
            fm = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:])
            return fm, body.lstrip("\n")
    return None, text  # opening fence but never closed


def _safe_yaml(fm_raw: str) -> tuple[Dict[str, Any], Optional[str]]:
    """Parse frontmatter with safe_load; never raise. Returns (mapping, error)."""
    try:
        data = yaml.safe_load(fm_raw)
    except yaml.YAMLError as exc:
        return {}, f"invalid YAML frontmatter: {exc}"
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return {}, "frontmatter is not a key/value mapping"
    return data, None


def _validate_fields(name: str, desc: str, prior_err: Optional[str]) -> tuple[bool, Optional[str]]:
    if prior_err:
        return False, prior_err
    if not name:
        return False, "frontmatter missing required field: name"
    if not desc:
        return False, "frontmatter missing required field: description"
    return True, None


def _list_bundled_files(folder: Path) -> List[str]:
    """Relative POSIX paths of sibling files (not SKILL.md), with a symlink-escape guard."""
    base = folder.resolve()
    out: List[str] = []
    try:
        for p in sorted(folder.rglob("*")):
            if len(out) >= _MAX_BUNDLED_FILES:
                break
            if p.is_dir():
                continue
            if p.name in _IGNORED_NAMES or p.name.startswith("."):
                continue
            try:
                rp = p.resolve()
                if not rp.is_relative_to(base):
                    continue  # symlink escaping the folder - never list it
            except (OSError, ValueError):
                continue
            try:
                rel = p.relative_to(folder).as_posix()
            except ValueError:
                continue
            if rel == "SKILL.md":
                continue
            out.append(rel)
    except (OSError, ValueError):
        pass
    return out


def parse_skill_meta(skill_md_path: Path | str) -> Optional[Dict[str, Any]]:
    """
    Parse ONLY the frontmatter - cheap, does not read or return the body.
    Used by discovery/router so name+description routing never loads bodies.

    Returns {id, name, description, valid, error} or None if the file is missing.
    """
    path = Path(skill_md_path)
    if not path.exists():
        return None
    skill_id = derive_skill_id(path.parent.name)
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as exc:
        return {"id": skill_id, "name": skill_id, "description": "",
                "valid": False, "error": f"cannot read SKILL.md: {exc}"}
    fm_raw, _ = _split_frontmatter(text)
    if fm_raw is None:
        return {"id": skill_id, "name": skill_id, "description": "",
                "valid": False, "error": "missing YAML frontmatter fence"}
    fm, yaml_err = _safe_yaml(fm_raw)
    name = str(fm.get("name", "")).strip()
    desc = str(fm.get("description", "")).strip()
    valid, verr = _validate_fields(name, desc, yaml_err)
    return {"id": skill_id, "name": name or skill_id, "description": desc,
            "valid": valid, "error": verr}


def parse_skill_md_text(text: str, skill_id: str = "") -> Dict[str, Any]:
    """
    Validate a SKILL.md string with NO file I/O (used by the create/update
    handlers before writing to disk, so an invalid edit never clobbers a good
    skill). Returns {id, name, description, body, valid, error}.
    """
    fm_raw, body = _split_frontmatter(text or "")
    if fm_raw is None:
        return {"id": skill_id, "name": skill_id, "description": "", "body": body,
                "valid": False, "error": "missing YAML frontmatter fence"}
    fm, yaml_err = _safe_yaml(fm_raw)
    name = str(fm.get("name", "")).strip()
    desc = str(fm.get("description", "")).strip()
    valid, verr = _validate_fields(name, desc, yaml_err)
    return {"id": skill_id, "name": name or skill_id, "description": desc, "body": body,
            "valid": valid, "error": verr}


def parse_skill_md(skill_md_path: Path | str) -> Dict[str, Any]:
    """
    Parse a SKILL.md file into a normalized skill dict.

    Returns:
        {
            "id": str,              # derived from the parent folder name
            "name": str,            # frontmatter 'name' (falls back to id)
            "description": str,     # frontmatter 'description' (required for routing)
            "body": str,            # markdown after the closing '---'
            "bundled_files": [str], # relative POSIX paths of sibling files
            "frontmatter": dict,    # full parsed YAML
            "valid": bool,
            "error": str | None,
            "folder": str,          # absolute folder path
        }

    Never raises: a malformed skill comes back valid=False with an error.
    """
    path = Path(skill_md_path)
    folder = path.parent
    skill_id = derive_skill_id(folder.name)
    result: Dict[str, Any] = {
        "id": skill_id, "name": skill_id, "description": "", "body": "",
        "bundled_files": [], "frontmatter": {}, "valid": False, "error": None,
        "folder": str(folder.resolve()) if folder.exists() else str(folder),
    }
    if not path.exists():
        result["error"] = "SKILL.md not found"
        return result
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as exc:
        result["error"] = f"cannot read SKILL.md: {exc}"
        return result

    fm_raw, body = _split_frontmatter(text)
    result["body"] = body
    result["bundled_files"] = _list_bundled_files(folder)

    if fm_raw is None:
        result["error"] = "missing YAML frontmatter fence"
        return result

    fm, yaml_err = _safe_yaml(fm_raw)
    result["frontmatter"] = fm
    name = str(fm.get("name", "")).strip()
    desc = str(fm.get("description", "")).strip()
    if name:
        result["name"] = name
    result["description"] = desc
    valid, verr = _validate_fields(name, desc, yaml_err)
    result["valid"] = valid
    result["error"] = verr
    return result
