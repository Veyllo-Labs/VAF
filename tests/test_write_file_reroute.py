# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""WriteFileTool home-reroute guard regressions.

The guard (reroute stray home-directory writes into the VAF output dir) was dead code
for months: function-local 'from pathlib import Path' imports later in run() shadowed
the module-level Path for the whole function scope, so the guard raised
UnboundLocalError on line 1 and a broad except swallowed it - every relative-path
write from a workflow landed in the process cwd (observed live: a workflow draft
written to the user's home root, which the /api/file endpoint then rightly refused
to serve). These tests pin the revived behavior.

Hermetic: HOME and cwd point at a pytest tmp dir (Path.home() follows $HOME on POSIX).
"""
import os

import pytest

from vaf.tools.filesystem import WriteFileTool


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()  # resolve: the guard compares resolved paths
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows: ntpath/pathlib ignore HOME, honor USERPROFILE
    monkeypatch.chdir(home)  # process cwd == home root, like the live tray
    return home


def test_bare_filename_reroutes_to_output_dir(fake_home):
    """THE live bug: path='draft' from a workflow step must not land in the home root."""
    out = WriteFileTool().run(path="workflow_draft_test", content="hello")
    assert "successfully" in out.lower() or "written" in out.lower(), out
    assert not (fake_home / "workflow_draft_test").exists(), "file littered the home root"
    rerouted = fake_home / "Documents" / "VAF" / "workflow_draft_test"
    assert rerouted.exists() and rerouted.read_text() == "hello"


def test_new_home_subfolder_reroutes(fake_home):
    """The guard's original purpose: ~/newproject/file.txt goes to the output dir."""
    WriteFileTool().run(path="newproject/notes.txt", content="x")
    assert not (fake_home / "newproject").exists()
    assert (fake_home / "Documents" / "VAF" / "newproject" / "notes.txt").exists()


def test_existing_home_root_file_is_not_overwritten_in_place(fake_home):
    """Overwrite variant: an existing file at the home root must also be rerouted."""
    (fake_home / "existing_note").write_text("old")
    WriteFileTool().run(path="existing_note", content="new")
    assert (fake_home / "existing_note").read_text() == "old", "home-root file was overwritten"
    assert (fake_home / "Documents" / "VAF" / "existing_note").read_text() == "new"


def test_known_home_dirs_stay_put(fake_home):
    """Writes into Documents/Downloads etc. are legitimate and must NOT be rerouted."""
    (fake_home / "Documents").mkdir(exist_ok=True)
    WriteFileTool().run(path="Documents/kept.txt", content="stay")
    assert (fake_home / "Documents" / "kept.txt").read_text() == "stay"
    assert not (fake_home / "Documents" / "VAF" / "Documents").exists()


def test_explicit_absolute_home_file_written_in_place(fake_home):
    """An explicit absolute path to an existing home-root dotfile (read-modify-write
    of ~/.bashrc) must be written IN PLACE, not forked into Documents/VAF."""
    dotfile = fake_home / ".bashrc"
    dotfile.write_text("old")
    WriteFileTool().run(path=str(dotfile), content="new")
    assert dotfile.read_text() == "new"
    assert not (fake_home / "Documents" / "VAF" / ".bashrc").exists()


def test_explicit_tilde_home_file_written_in_place(fake_home):
    """Same via a ~-anchored path."""
    (fake_home / ".gitconfig").write_text("old")
    WriteFileTool().run(path="~/.gitconfig", content="new")
    assert (fake_home / ".gitconfig").read_text() == "new"
    assert not (fake_home / "Documents" / "VAF" / ".gitconfig").exists()


def test_symlinked_home_still_reroutes(tmp_path, monkeypatch):
    """If $HOME is an unresolved symlink, a cwd-relative bare write must STILL be
    rerouted (the resolved-cwd would otherwise slip past a raw comparison)."""
    real = (tmp_path / "real_home").resolve()
    real.mkdir()
    link = tmp_path / "link_home"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/CI")
    monkeypatch.setenv("HOME", str(link))
    monkeypatch.setenv("USERPROFILE", str(link))
    monkeypatch.chdir(link)
    WriteFileTool().run(path="draft", content="x")
    assert not (real / "draft").exists() and not (link / "draft").exists()
    assert (real / "Documents" / "VAF" / "draft").read_text() == "x"
