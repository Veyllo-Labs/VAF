# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Tag Links - Bidirectional tag connections for VAF Memory System.

When tags A and B are linked:
- All memories with A automatically get B (and vice versa)
- New memories saved with A get B, new memories with B get A
- Applies to existing memories on link creation (sync)
"""

from pathlib import Path
from typing import List, Set, Tuple
import json
import logging
from vaf.core.config import Config

logger = logging.getLogger(__name__)

_TAG_LINKS_FILE = "tag_links.json"


def _path() -> Path:
    """Path to tag links JSON file."""
    return Path(Config.APP_DIR) / _TAG_LINKS_FILE


def _load() -> List[Tuple[str, str]]:
    """Load tag links from file. Returns list of (tag_a, tag_b) pairs, normalized lowercase."""
    path = _path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        links = data.get("links", [])
        out = []
        for pair in links:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                a, b = str(pair[0]).strip().lower(), str(pair[1]).strip().lower()
                if a and b and a != b:
                    out.append((a, b) if a < b else (b, a))
        return list(dict.fromkeys(out))
    except Exception as e:
        logger.warning("Failed to load tag links: %s", e)
        return []


def _save(links: List[Tuple[str, str]]) -> None:
    """Save tag links to file."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"links": [[a, b] for a, b in links]}, indent=2),
        encoding="utf-8",
    )


def get_linked_tags(tag: str) -> Set[str]:
    """
    Get all tags linked to the given tag (bidirectional).

    Returns:
        Set of linked tags (excluding the input tag).
    """
    tag = (tag or "").strip().lower()
    if not tag:
        return set()
    result = set()
    for a, b in _load():
        if a == tag:
            result.add(b)
        elif b == tag:
            result.add(a)
    return result


def expand_tags_with_links(tags: List[str]) -> List[str]:
    """
    Expand a list of tags with all linked tags.

    Given tags [A, B] and link A-C, returns [A, B, C] (sorted, unique, lowercase).
    """
    if not tags:
        return []
    result = set()
    for t in tags:
        t = (t or "").strip().lower()
        if not t:
            continue
        result.add(t)
        result.update(get_linked_tags(t))
    return sorted(result)


def add_link(tag_a: str, tag_b: str) -> bool:
    """
    Add a bidirectional link between two tags.

    Returns True if link was added, False if already existed.
    """
    a, b = (tag_a or "").strip().lower(), (tag_b or "").strip().lower()
    if not a or not b or a == b:
        return False
    pair = (a, b) if a < b else (b, a)
    links = _load()
    if pair in links:
        return False
    links.append(pair)
    _save(links)
    logger.info("Tag link added: %s <-> %s", a, b)
    return True


def remove_link(tag_a: str, tag_b: str) -> bool:
    """
    Remove the link between two tags.

    Returns True if link was removed.
    """
    a, b = (tag_a or "").strip().lower(), (tag_b or "").strip().lower()
    pair = (a, b) if a < b else (b, a)
    links = _load()
    if pair not in links:
        return False
    links = [p for p in links if p != pair]
    _save(links)
    logger.info("Tag link removed: %s <-> %s", a, b)
    return True


def list_links() -> List[Tuple[str, str]]:
    """Return all tag links as list of (tag_a, tag_b) pairs."""
    return _load()


def has_link(tag_a: str, tag_b: str) -> bool:
    """Check if two tags are linked."""
    a, b = (tag_a or "").strip().lower(), (tag_b or "").strip().lower()
    pair = (a, b) if a < b else (b, a)
    return pair in _load()
