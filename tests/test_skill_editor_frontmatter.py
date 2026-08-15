# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Editing a skill in the web editor must not throw away what it cannot show.

The editor has two fields, name and description, and the save path used to
rebuild the whole YAML frontmatter from exactly those two. Every other key the
Agent Skills format allows - `metadata`, `license`, `allowed-tools` - was
deleted the first time anybody pressed save. That makes VAF a lossy stop for
any skill written elsewhere, which is the opposite of what implementing a
shared format is for.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_save_path_merges_the_frontmatter_it_found():
    """MUTATION: build the frontmatter from name and description again.

    Read as source rather than driven through a socket: this branch sits inside
    the WebSocket command loop, several hundred lines into an async handler with
    auth, scanning and manifest writes around it. The behaviour that matters is
    one decision - merge or replace - and it is visible here without standing up
    half the server.
    """
    server = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    branch = server.split("The editor may send raw SKILL.md", 1)[1][:2000]
    assert "_keep" in branch and "frontmatter" in branch, (
        "the save path no longer keeps the frontmatter it found - every key the "
        "editor has no field for is deleted on save")
    assert re.search(r"_keep\[.name.\]\s*=", branch), "the name must still be owned by the editor"
    assert re.search(r"_keep\[.description.\]\s*=", branch)
    assert "_parse_meta" in branch, "nothing reads the existing file to merge with"


def test_the_projection_carries_a_title_and_the_ui_prefers_it():
    """MUTATION: show `name` in the skills list again, or drop `title` from the
    projection.

    `name` is the folder's identifier in this format, so a list that shows it
    reads like a directory listing. The headline lives in `metadata.title`, which
    the format allows, and a skill without one falls back to the name rather
    than showing an empty card.
    """
    templates = (ROOT / "vaf" / "skills" / "templates.py").read_text(encoding="utf-8")
    assert '"title": _title_of(parsed)' in templates, "the projection has no title"

    modal = (ROOT / "web" / "components" / "SettingsModal.tsx").read_text(encoding="utf-8")
    assert "{s.title || s.name}" in modal, (
        "the skills list shows the identifier instead of the headline")


def test_a_title_that_is_not_a_title_is_ignored(tmp_path, monkeypatch):
    """MUTATION: pass metadata.title through as it arrives.

    A skill file is something anybody may drop into the folder, so the same rule
    as everywhere else: usable or dropped, never trusted as written.
    """
    from vaf.skills import templates

    for name, meta, expected in [
        ("good", "metadata:\n  title: A Good Title\n", "A Good Title"),
        ("listy", "metadata:\n  title:\n    - not\n    - a title\n", ""),
        ("empty", "metadata:\n  title: '   '\n", ""),
        ("nometa", "", ""),
    ]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: something a router can match on.\n"
            f"{meta}---\n# {name}\nbody\n", encoding="utf-8")

    from vaf.core import skills_registry
    monkeypatch.setattr(templates, "_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(skills_registry, "load_manifest", lambda: {
        "version": 1,
        "skills": {n: {"shared_with": ["*"]} for n in ("good", "listy", "empty", "nometa")},
    })
    templates.reload_skills()
    try:
        found = {s["id"]: s.get("title") for s in templates.list_skills(include_invalid=True)}
        assert found.get("good") == "A Good Title"
        assert found.get("listy") == ""
        assert found.get("empty") == ""
        assert found.get("nometa") == ""
    finally:
        templates.reload_skills()


def test_a_very_long_title_cannot_take_over_the_list(tmp_path, monkeypatch):
    from vaf.skills import templates

    folder = tmp_path / "long"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: long\ndescription: something a router can match on.\n"
        "metadata:\n  title: " + "T" * 500 + "\n---\n# long\nbody\n", encoding="utf-8")
    from vaf.core import skills_registry
    monkeypatch.setattr(templates, "_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(skills_registry, "load_manifest", lambda: {
        "version": 1, "skills": {"long": {"shared_with": ["*"]}},
    })
    templates.reload_skills()
    try:
        title = {s["id"]: s.get("title") for s in templates.list_skills(include_invalid=True)}["long"]
        assert len(title) == 80
    finally:
        templates.reload_skills()
