# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Skills that ship with the package, and the one that teaches agent rooms.

Two halves. The mechanism: a builtin skill is discovered from the package, visible
to every user with no manifest entry, loadable through the same gate use_skill
asks, and replaceable by a user-dir skill under the same id. The content: the A2A
skill is instructions run by agents on machines nobody here can see, so every tool
and every CLI command it names is checked against the registries that enforce them
- a skill that names a command that does not exist sends an agent into the void
with no way to read the failure.
"""
import re
from pathlib import Path

import pytest

from vaf.skills import templates
from vaf.skills.skill_md import parse_skill_meta

ROOT = Path(__file__).resolve().parents[1]
BUILTIN = ROOT / "vaf" / "skills" / "builtin"
A2A_SKILL = BUILTIN / "a2a_rooms" / "SKILL.md"


# ── the mechanism ──────────────────────────────────────────────────────────

def test_the_shipped_skill_parses_valid():
    parsed = parse_skill_meta(A2A_SKILL)
    assert parsed is not None and parsed["valid"], parsed.get("error")
    assert parsed["id"] == "a2a_rooms"


def test_builtin_skills_are_discovered_and_marked(tmp_path, monkeypatch):
    monkeypatch.setattr(templates, "_skills_dir", lambda: tmp_path / "none")
    skills = templates._discover_skills()
    assert "a2a_rooms" in skills
    assert skills["a2a_rooms"]["builtin"] is True


def test_a_user_dir_skill_under_the_same_id_wins(tmp_path, monkeypatch):
    """MUTATION: scan the user dir first.

    The override is what makes a shipped skill customisable without a fork: copy
    the folder into ~/.vaf/skills under the same name and edit. Scanned the other
    way round, the package would silently undo every customisation on upgrade.
    """
    override = tmp_path / "a2a_rooms"
    override.mkdir(parents=True)
    (override / "SKILL.md").write_text(
        "---\nname: My Rooms\ndescription: my own words\n---\n# mine\n",
        encoding="utf-8")
    monkeypatch.setattr(templates, "_skills_dir", lambda: tmp_path)

    skills = templates._discover_skills()
    assert skills["a2a_rooms"]["name"] == "My Rooms"
    assert skills["a2a_rooms"]["builtin"] is False


def test_a_builtin_skill_is_visible_and_loadable_for_a_plain_user(tmp_path, monkeypatch):
    """MUTATION: leave the builtin ids out of the registry's visibility answer.

    list_skills and use_skill ask two different functions. A builtin skill only
    listed would be offered by the router and then refused by the loader, which
    reads as a broken product on every fresh install.
    """
    from vaf.core import skills_registry

    monkeypatch.setattr(templates, "_skills_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(skills_registry, "load_manifest",
                        lambda: {"version": 1, "skills": {}})
    templates.reload_skills()
    try:
        listed = {s["id"] for s in templates.list_skills(user_scope_id="scope-user")}
        assert "a2a_rooms" in listed
        assert skills_registry.is_skill_visible_to_user("a2a_rooms", "scope-user")
    finally:
        templates.reload_skills()


def test_a_quarantined_override_does_not_resurrect_through_the_builtin(tmp_path, monkeypatch):
    """A user-dir copy that the scanner quarantined must stay gone: the builtin id
    may only fill the gap when the manifest does not know the id at all."""
    from vaf.core import skills_registry

    monkeypatch.setattr(skills_registry, "load_manifest", lambda: {
        "version": 1,
        "skills": {"a2a_rooms": {"shared_with": ["*"], "quarantined": True}},
    })
    assert not skills_registry.is_skill_visible_to_user("a2a_rooms", "scope-user")


# ── the content, checked against what enforces it ──────────────────────────

@pytest.fixture(scope="module")
def body() -> str:
    return A2A_SKILL.read_text(encoding="utf-8")


def test_every_tool_the_skill_names_exists(body):
    """MUTATION: rename a tool in the skill text.

    The skill is the router's teaching material: a tool name that drifted turns
    "work with another agent" into a tool-not-found loop.
    """
    from vaf.tools import room_tools

    real = {getattr(cls, "name") for cls in vars(room_tools).values()
            if isinstance(cls, type) and getattr(cls, "name", "").startswith("room_")}
    named = set(re.findall(r"`(room_[a-z_]+)`", body))
    assert named, "the skill names no tools at all"
    assert named <= real, f"the skill names tools that do not exist: {sorted(named - real)}"


def test_every_cli_command_the_skill_names_exists(body):
    """The commands a foreign agent is told about, checked against the CLI table
    the same way the protocol document is."""
    from typer.main import get_command

    from vaf.cli.cmd import a2a as a2a_cmd

    real = set(get_command(a2a_cmd.app).commands)
    named = set(re.findall(r"`vaf a2a ([a-z]+)`", body))
    assert named, "the skill names no CLI commands at all"
    assert named <= real, f"the skill names commands that do not exist: {sorted(named - real)}"


def test_every_send_kind_the_skill_names_is_one_the_room_accepts(body):
    from vaf.core.a2a.frame import KINDS

    section = body.split("## Talk")[1].split("##")[0]
    named = set(re.findall(r"`([a-z_]+)`", section)) - {"room_send", "reply_to", "status"}
    unknown = {k for k in named if k not in KINDS
               and k not in ("working", "input_required", "completed", "failed")}
    assert not unknown, f"the skill teaches kinds the room refuses: {sorted(unknown)}"


def test_the_editor_reads_the_shipped_body_and_the_override_wins(tmp_path, monkeypatch):
    """MUTATION: read only the user dir for the editor's source.

    The list showed the shipped skill's name over an EMPTY instructions box, and
    saving that box would have overridden a working skill with placeholder text.
    """
    from vaf.core import skills_registry as reg

    monkeypatch.setattr(reg, "get_skills_dir", lambda: tmp_path)
    source = reg.get_skill_md_source("a2a_rooms")
    assert source and "Agent Rooms" in source, "the shipped body never reached the editor"

    override = tmp_path / "a2a_rooms"
    override.mkdir(parents=True)
    (override / "SKILL.md").write_text("---\nname: Mine\ndescription: d\n---\n# mine\n",
                                       encoding="utf-8")
    assert "# mine" in (reg.get_skill_md_source("a2a_rooms") or "")
