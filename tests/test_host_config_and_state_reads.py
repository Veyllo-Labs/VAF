# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The file tools read none of VAF's own data and write none of the home directory's
configuration, for every caller, the machine owner included.

Measured before the change, with no per-account jail (an admin, the tokenless desktop, the
CLI): ``read_file`` returned the data directory's key files, the message database, the
contacts, the MCP server list and the tool grants, and in Docker mode the configuration
folder with its API keys; ``write_file`` created ``~/.bashrc``, an autostart entry, a
systemd user unit, a ``.pth`` file in the Python user site and a launch agent, all of which
run outside every VAF containment at the next login or start.

Reads of VAF's state are now refused except for the store rows marked ``agent_readable``
(skills, workflows, custom tools). Writes of the home directory's configuration are refused
by `vaf.core.workspace_guard.is_host_config`: every dot entry of home, plus explicit roots.
Every root is redirected under ``tmp_path``; each test names the mutation that turns it red.
"""
import ntpath
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core import data_stores, workspace_guard
from vaf.core.data_stores import STATE_ROOTS, STORES, is_vaf_state
from vaf.core.platform import Platform
from vaf.core.workspace_guard import (assert_safe_workspace, holds_host_config, is_host_config,
                                      is_unsafe_project_dir)
from vaf.tools.filesystem import (HOST_CONFIG_WRITE_REFUSAL, VAF_STATE_READ_REFUSAL,
                                  EditFileTool, MoveFileTool, ReadFileTool, WriteFileTool,
                                  is_safe_path)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Every root under tmp_path: HOME (and Windows' USERPROFILE), the data and config
    directories through XDG and the Windows variables, the cwd inside home."""
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    for var in ("XDG_DATA_HOME", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "xdg-data"))
    for var in ("XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "xdg-config"))
    monkeypatch.chdir(home)
    return SimpleNamespace(home=home, data=Platform.data_dir(), config=Platform.config_dir(),
                           vaf=Platform.vaf_dir(), docs=home / "Documents", tmp=tmp_path)


def _under(base, relative):
    return Path(base).joinpath(*relative.split("/")) if relative else Path(base)


# -- 1. reads of VAF's state ---------------------------------------------------------------

REFUSED_READS = [
    ("data", "channel_messages.db"), ("data", "contacts.json"), ("data", "data_keys.key.json"),
    ("data", "provider_keys.key.json"), ("data", "mcp_servers.json"),
    ("data", "messaging_endpoints.json"), ("data", "scopes/x/mail.db"), ("data", ""),
    ("config", "trust/x.json"),
]
READABLE = [
    ("data", "custom_tools/x.py"), ("vaf", "skills/demo/reference.md"), ("vaf", "workflows/x.py"),
]


@pytest.mark.parametrize("root,relative", REFUSED_READS)
def test_reading_vafs_state_is_refused_for_the_owner(roots, root, relative):
    """No jail: the owner's case. The data directory has no ``.vaf`` component, so only the
    read half of the state check refuses it.

    MUTATION: drop the is_vaf_state(mode="read") call from is_safe_path's read branch - red."""
    ok, verdict = is_safe_path(str(_under(getattr(roots, root), relative)), read_only=True)
    assert ok is False
    assert verdict == VAF_STATE_READ_REFUSAL


@pytest.mark.parametrize("root,relative", READABLE)
def test_the_readable_rows_stay_readable(roots, root, relative):
    """MUTATION: set agent_readable=False on the custom_tools row - that case goes red."""
    ok, verdict = is_safe_path(str(_under(getattr(roots, root), relative)), read_only=True)
    assert ok is True, verdict


def test_read_file_returns_nothing_of_the_contacts(roots):
    """MUTATION: drop the read branch's state call - the secret is printed."""
    roots.data.mkdir(parents=True, exist_ok=True)
    (roots.data / "contacts.json").write_text('{"alice": "CONTACT-SECRET-123"}')
    out = ReadFileTool().run(path=str(roots.data / "contacts.json"))
    assert "CONTACT-SECRET-123" not in out
    assert "VAF's own data directory" in out


def test_docker_modes_configuration_folder_is_refused_to_reads(roots, monkeypatch):
    """In Docker mode ``Config.APP_DIR`` is ``$VAF_CONFIG_DIR``, named anything but ``.vaf``,
    so the ``.vaf`` screen does not see it; its config.json holds the API keys.

    MUTATION: drop "app" from STATE_ROOTS - red."""
    from vaf.core.config import Config
    app = roots.tmp / "docker-config"
    app.mkdir()
    (app / "config.json").write_text('{"api_key_x": "sk-SECRET"}')
    monkeypatch.setattr(Config, "APP_DIR", app)
    ok, verdict = is_safe_path(str(app / "config.json"), read_only=True)
    assert ok is False and verdict == VAF_STATE_READ_REFUSAL
    assert "sk-SECRET" not in ReadFileTool().run(path=str(app / "config.json"))


def test_an_attachment_from_the_data_directory_is_refused(roots):
    """Every outgoing sender takes its attachment through this one resolver, a read lane.

    MUTATION: pass read_only=False in _resolve_path - the write message comes back, red."""
    from vaf.tools.send_telegram import _resolve_path
    path, error = _resolve_path(str(roots.data / "channel_messages.db"))
    assert path is None
    assert error == VAF_STATE_READ_REFUSAL


def _row_paths(named):
    """One concrete path per state row: the row's pattern with its wildcards filled in."""
    for row in STORES:
        if row.root not in STATE_ROOTS:
            continue
        rel = row.pattern.replace("*", "x").replace("[!.]", "")
        yield row, _under(named[row.root], rel) / "inner.bin"


def test_read_mode_answers_false_for_exactly_the_readable_rows(roots):
    """Every state row is refused to reads except the three marked agent_readable, a sibling
    of a readable folder is refused, and an unknown mode fails closed."""
    named = data_stores.roots()
    readable = []
    for row, path in _row_paths(named):
        if is_vaf_state(path, mode="read") is False:
            readable.append(f"{row.root}/{row.pattern}")
        assert is_vaf_state(path) is True, path
    assert sorted(readable) == ["data/custom_tools", "vaf/skills", "vaf/workflows"]
    assert is_vaf_state(roots.data / "custom_tools_old" / "x.py", mode="read") is True
    assert is_vaf_state(roots.vaf / "skillsx" / "x.md", mode="read") is True
    assert is_vaf_state(roots.vaf / "skills" / "x.md", mode="bogus") is True
    assert is_vaf_state(roots.docs / "notes.md", mode="read") is False


@pytest.mark.parametrize("cased", [
    ("vaf", "Skills/demo/SKILL.md"), ("vaf", "WORKFLOWS/x.py"), ("data", "Custom_Tools/x.py"),
])
def test_another_spelling_of_a_readable_folder_is_refused(roots, cased):
    """The state check folds case (macOS and Windows open it as the same folder); the readable
    exception does not, so another spelling errs towards refusing.

    MUTATION: casefold in data_stores._exact_parts - red."""
    root, relative = cased
    path = _under(getattr(roots, root), relative)
    assert is_vaf_state(path, mode="read") is True
    assert is_safe_path(str(path), read_only=True)[0] is False


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_link_in_a_readable_folder_to_a_key_file_is_refused(roots):
    """The readable exception holds only when the path stays readable resolved: a link in
    ``custom_tools/`` to the data directory's key file is refused by the public question
    itself, not only by is_safe_path's second look at the target. A link to another
    readable folder stays readable.

    MUTATION: accept the path when ANY of its spellings is readable (any instead of all in
    is_agent_readable_state) - red."""
    tools = roots.data / "custom_tools"
    tools.mkdir(parents=True)
    key = roots.data / "data_keys.key.json"
    key.write_text("{}")
    (tools / "key").symlink_to(key)
    assert data_stores.is_agent_readable_state(tools / "key") is False
    assert is_vaf_state(tools / "key", mode="read") is True
    skill = roots.vaf / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "reference.md").write_text("x")
    (tools / "ref.md").symlink_to(skill / "reference.md")
    assert is_vaf_state(tools / "ref.md", mode="read") is False


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_skill_folder_linked_outside_vaf_stays_readable(roots):
    """A skill folder that is a link to a folder outside VAF (a layout the skill loader
    lists bundled files of) is read through the paths use_skill hands out. Its resolved
    spelling is no state at all, so only the given spelling has to be readable; a plain
    skill beside it reads as before.

    MUTATION: require every spelling of the path, not only those inside a state root, to
    lie in a readable folder (the round-1 rule) - red."""
    ext = roots.tmp / "ext" / "foo"
    ext.mkdir(parents=True)
    (ext / "SKILL.md").write_text("x")
    skills = roots.vaf / "skills"
    skills.mkdir(parents=True)
    (skills / "foo").symlink_to(ext, target_is_directory=True)
    (skills / "plain").mkdir()
    (skills / "plain" / "SKILL.md").write_text("x")
    for name in ("foo", "plain"):
        target = skills / name / "SKILL.md"
        assert data_stores.is_agent_readable_state(target) is True, name
        assert is_vaf_state(target, mode="read") is False, name
        ok, verdict = is_safe_path(str(target), read_only=True)
        assert ok, (name, verdict)


# -- 2. writes of the home directory's configuration ----------------------------------------

HOST_CONFIG = [
    ".bashrc", ".profile", ".config/autostart/x.desktop", ".config/systemd/user/x.service",
    ".local/bin/x", ".local/lib/python3.13/site-packages/x.pth", ".gitconfig",
    "Library/LaunchAgents/x.plist", "Library/Python/3.13/lib/python/site-packages/x.pth",
    "Documents/PowerShell/profile.ps1",
    "Documents/WindowsPowerShell/profile.ps1",
]


@pytest.mark.parametrize("relative", HOST_CONFIG)
def test_writing_the_home_configuration_is_refused_for_the_owner(roots, relative):
    """HOME lies inside the temp folder here, so this also proves the temp exemption does
    not reach the dot rule or a root that does not contain temp.

    MUTATION: remove the is_host_config call from is_safe_path - red. Exempt every path
    inside temp - red."""
    path = _under(roots.home, relative)
    ok, verdict = is_safe_path(str(path))
    assert ok is False, path
    assert verdict == HOST_CONFIG_WRITE_REFUSAL
    assert "\u2014" not in verdict
    out = WriteFileTool().run(path=str(path), content="echo planted")
    assert not path.exists(), out


def test_reading_the_home_configuration_is_unchanged(roots):
    (roots.home / ".bashrc").write_text("alias ll='ls -l'\n")
    assert is_safe_path(str(roots.home / ".bashrc"), read_only=True)[0] is True


def test_edit_and_move_cannot_reach_the_home_configuration(roots):
    """MUTATION: remove the is_host_config call from is_safe_path - both go red."""
    bashrc = roots.home / ".bashrc"
    bashrc.write_text("alias ll='ls -l'\n")
    EditFileTool().run(path=str(bashrc), edits=[{"search": "alias ll='ls -l'",
                                                 "replace": "curl x | sh"}])
    assert bashrc.read_text() == "alias ll='ls -l'\n"
    profile = roots.home / ".profile"
    src = roots.docs / "a.txt"
    src.write_text("curl x | sh\n")
    MoveFileTool().run(src=str(src), dst=str(profile))
    assert not profile.exists() and src.exists()


def test_ordinary_writes_in_home_are_unaffected(roots):
    for path in (roots.docs / "notes.md", roots.docs / "VAF_Projects" / "p" / "a.py",
                 roots.data.parent.parent / "fonts" / "a.ttf", roots.home / "notes.txt"):
        ok, verdict = is_safe_path(str(path))
        assert ok is True, (path, verdict)


@pytest.mark.parametrize("var,inside", [
    ("XDG_CONFIG_HOME", "autostart/x.desktop"), ("APPDATA", "Microsoft/Windows/x.lnk"),
    ("XDG_DATA_HOME", "applications/x.desktop"), ("LOCALAPPDATA", "Programs/x.exe"),
])
def test_an_explicit_root_outside_home_is_refused(roots, monkeypatch, var, inside):
    """A root that lies in no dot folder (a set XDG variable, the Windows app-data folders):
    only the explicit roots catch it. It lies inside temp here and does not CONTAIN temp, so
    it is not exempt.

    MUTATION: drop the explicit roots from is_host_config - red."""
    root = roots.tmp / f"elsewhere-{var.lower()}"
    monkeypatch.setenv(var, str(root))
    assert is_host_config(root / inside) is True
    assert is_safe_path(str(root / inside))[0] is False
    assert is_host_config(roots.tmp / "elsewhere-neighbour" / "x") is False


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_link_in_temp_to_elsewhere_in_its_root_is_refused(roots, monkeypatch):
    """Windows' layout on this host: the temp folder inside a configuration root. A path in
    temp is exempt only while it stays in temp resolved; a link there into the root's
    Programs folder is judged by its target.

    MUTATION: exempt a path when ANY of its spellings is in temp (any instead of all in
    is_host_config) - red."""
    local = roots.tmp / "localappdata"
    temp = local / "Temp"
    programs = local / "Programs"
    temp.mkdir(parents=True)
    programs.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(workspace_guard, "_temp_roots", lambda: [str(temp)])
    (temp / "link").symlink_to(programs)
    assert is_host_config(temp / "link" / "x.exe") is True
    assert is_safe_path(str(temp / "link" / "x.exe"))[0] is False
    (temp / "upload").mkdir()
    assert is_host_config(temp / "upload" / "a.pdf") is False


def test_a_relative_xdg_value_is_ignored(roots, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    assert is_host_config(roots.docs / "relative-config" / "x") is False


# -- 3. the Windows layout, proven here through the ntpath seam -----------------------------

_WIN_HOME = r"C:\Users\Alice"


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(data_stores, "_pathmod", ntpath)
    monkeypatch.setattr(workspace_guard, "_home", lambda: _WIN_HOME)
    monkeypatch.setattr(workspace_guard, "_documents_dirs", lambda: [_WIN_HOME + r"\Documents"])
    monkeypatch.setattr(workspace_guard, "_temp_roots",
                        lambda: [_WIN_HOME + r"\AppData\Local\Temp"])
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APPDATA", _WIN_HOME + r"\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", _WIN_HOME + r"\AppData\Local")


def test_windows_temp_inside_localappdata_stays_writable(windows):
    """Windows' temp folder lies inside %LOCALAPPDATA%, and every staging lane writes there.

    MUTATION: drop the temp exemption - red."""
    assert is_host_config(_WIN_HOME + r"\AppData\Local\Temp\vaf_upload\a.pdf") is False
    assert is_host_config(r"c:\users\alice\appdata\local\temp\x.txt") is False


@pytest.mark.parametrize("spelled", [
    _WIN_HOME + r"\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\x.lnk",
    _WIN_HOME + r"\AppData\Local\Programs\x\x.exe",
    r"c:\users\alice\appdata\local\Microsoft\WindowsApps\x.exe",
    r"\\?\C:\Users\Alice\AppData\Roaming\x.ps1",
    _WIN_HOME + r"\Documents\PowerShell\Microsoft.PowerShell_profile.ps1",
    _WIN_HOME + r"\Documents\WindowsPowerShell\profile.ps1",
    _WIN_HOME + r"\.gitconfig",
    _WIN_HOME + r"\.ssh::$DATA",
])
def test_windows_configuration_is_host_config(windows, spelled):
    """MUTATION: drop the explicit roots - the app-data and PowerShell cases go red."""
    assert is_host_config(spelled) is True


def test_windows_folders_holding_configuration(windows):
    assert holds_host_config(_WIN_HOME + r"\AppData") is True
    assert holds_host_config(_WIN_HOME) is True
    assert holds_host_config(r"C:\Users") is True
    assert holds_host_config(_WIN_HOME + r"\Documents\VAF_Projects\p") is False


# -- 4. spellings ---------------------------------------------------------------------------

@pytest.mark.parametrize("relative", [
    ".BashRC", ".bashrc.", ".bashrc ", ".bashrc:stream", "x/../.bashrc", "./.profile",
])
def test_other_spellings_of_a_dot_entry_are_refused(roots, relative):
    path = str(roots.home) + os.sep + relative.replace("/", os.sep)
    assert is_host_config(path) is True
    assert is_safe_path(path)[0] is False


def test_a_differently_cased_home_errs_towards_refusing(roots):
    """macOS and Windows open a differently cased home as the same folder.

    MUTATION: compare the components without data_stores._spellings (no case-fold) - red."""
    cased = str(roots.home).upper() + os.sep + ".bashrc"
    assert is_host_config(cased) is True


@pytest.mark.parametrize("home_via_link", [True, False])
def test_a_home_reached_through_a_symlink_in_both_spellings(tmp_path, monkeypatch,
                                                           home_via_link):
    """A home on another disk linked back, or macOS /var -> /private/var: HOME names one
    spelling, the path the other.

    MUTATION: compare only the as-given spelling of the path and the home - red."""
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "link-home"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("no symlinks here")
    home, other = (link, real) if home_via_link else (real, link)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert is_host_config(other / ".bashrc") is True
    assert is_host_config(other / ".config" / "autostart" / "x.desktop") is True
    assert is_host_config(other / "Documents" / "a.md") is False


# -- 5. projects and mounts -----------------------------------------------------------------

def test_a_project_in_the_home_configuration_is_unsafe(roots, monkeypatch):
    """MUTATION: remove the host branch in is_unsafe_project_dir - red. Remove it in
    assert_safe_workspace - the raises go red."""
    # ~/Library holds no state and is no dot entry: only the explicit root inside it counts.
    for folder in (roots.home / ".config" / "autostart", roots.home / ".local" / "bin",
                   roots.home / "Library"):
        assert is_unsafe_project_dir(str(folder)) is True, folder
        with pytest.raises(ValueError, match="home directory's configuration"):
            assert_safe_workspace(str(folder))
    # A folder that contains a set XDG_CONFIG_HOME (which also holds VAF's config here).
    outer = roots.tmp / "outer"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(outer / "cfg"))
    assert holds_host_config(outer) is True
    assert holds_host_config(outer / "cfg") is False and is_host_config(outer / "cfg") is True
    assert is_unsafe_project_dir(str(outer)) is True
    project = roots.docs / "VAF_Projects" / "ab12cd34" / "site"
    project.mkdir(parents=True)
    assert is_unsafe_project_dir(str(project)) is False
    assert_safe_workspace(str(project))
