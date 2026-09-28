# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`jail_allows` asks the file jail from outside a tool run, and it must answer exactly what
the jail of a running tool answers - otherwise a route built on it and the tools of the same
account disagree about the same file, which is how the web file routes drifted before it
existed (they knew the account folder, not the skills; the save routes knew nothing)."""
import os

import pytest

import vaf.tools.filesystem as fs

SCOPE = "ab12cd34-0000-0000-0000-000000000000"
OTHER = "ffff0000"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    fs._shared_room_roots_cache.clear()
    return tmp_path


def _paths(home):
    projects = home / "Documents" / "VAF_Projects"
    skill = home / "skills" / "visible-skill"
    room = projects / OTHER / "room-folder"
    return {
        "own": projects / "ab12cd34" / "chat" / "a.md",
        "other": projects / OTHER / "chat" / "b.md",
        "room": room / "shared.md",
        "skill": skill / "SKILL.md",
        "owner_docs": home / "Documents" / "taxes.pdf",
        "data_dir": home / ".local" / "share" / "vaf" / "channel_messages.db",
        "legacy_flat": projects / "some-session" / "c.md",
    }, skill, room


@pytest.mark.parametrize("mode", ["read", "write"])
def test_the_answer_is_the_answer_of_a_running_tool(home, monkeypatch, mode):
    """MUTATION: give jail_allows its own rule (e.g. the old uid8-prefix check the routes
    carried) and the skill or the room row disagrees with the tool's jail."""
    paths, skill, room = _paths(home)
    monkeypatch.setattr(fs, "_visible_skill_roots", lambda scope: [skill])
    monkeypatch.setattr(fs, "_shared_room_roots", lambda scope: [room])
    for name, target in paths.items():
        with fs.user_jail(SCOPE, "user", mode=mode):
            tool_says = fs._librarian_jail_ok(os.path.abspath(target))
        assert fs.jail_allows(target, user_scope_id=SCOPE, user_role="user", mode=mode) is tool_says, name


def test_what_a_non_admin_account_reaches(home, monkeypatch):
    paths, skill, room = _paths(home)
    monkeypatch.setattr(fs, "_visible_skill_roots", lambda scope: [skill])
    monkeypatch.setattr(fs, "_shared_room_roots", lambda scope: [room])

    def allows(name, mode):
        return fs.jail_allows(paths[name], user_scope_id=SCOPE, user_role="user", mode=mode)

    assert allows("own", "read") and allows("own", "write")
    assert allows("room", "read") and allows("room", "write")
    assert allows("skill", "read") and not allows("skill", "write")
    for refused in ("other", "owner_docs", "data_dir", "legacy_flat"):
        assert not allows(refused, "read"), refused
        assert not allows(refused, "write"), refused


def test_an_admin_is_not_jailed(home):
    paths, _, _ = _paths(home)
    assert fs.jail_allows(paths["owner_docs"], user_scope_id=SCOPE, user_role="admin")


def test_a_link_is_judged_by_where_it_points(home):
    """A link planted in the account's own tree must not carry a read out of it."""
    paths, _, _ = _paths(home)
    own_dir = paths["own"].parent
    own_dir.mkdir(parents=True)
    paths["owner_docs"].write_text("secret")
    link = own_dir / "innocent.md"
    try:
        link.symlink_to(paths["owner_docs"])
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")
    assert not fs.jail_allows(link, user_scope_id=SCOPE, user_role="user")


def test_asking_does_not_touch_the_jail_of_the_running_tool(home):
    """Called from inside a jailed run about ANOTHER identity: the run's own jail stays."""
    with fs.user_jail(SCOPE, "user"):
        before = fs._librarian_scope_ctx.get()
        fs.jail_allows(home / "x", user_scope_id="", user_role=None)
        fs.jail_allows(home / "x", user_scope_id=SCOPE, user_role="admin")
        assert fs._librarian_scope_ctx.get() is before


def test_an_error_is_a_refusal(home, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store unreadable")
    monkeypatch.setattr(fs, "compute_user_jail", boom)
    paths, _, _ = _paths(home)
    assert fs.jail_allows(paths["own"], user_scope_id=SCOPE, user_role="user") is False


def test_the_facade_serves_the_function_itself():
    import vaf
    assert vaf.jail_allows is fs.jail_allows
