# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""git resolution: `vaf update` must find VAF's portable MinGit when git is not on PATH.

Regression cover for the Windows case where the installer downloaded MinGit but did not
persist it to PATH, so `run_git` (["git", ...]) failed with "Git is not installed." even
though a usable git existed.
"""
import os

import vaf.cli.cmd.git as g


def _reset_cache():
    g._GIT_EXE = None


def test_prefers_git_on_path(monkeypatch):
    _reset_cache()
    monkeypatch.setattr(g.shutil, "which", lambda name: "/usr/bin/git")
    assert g._resolve_git() == "/usr/bin/git"


def test_falls_back_to_bundled_mingit_when_not_on_path(monkeypatch, tmp_path):
    _reset_cache()
    monkeypatch.setattr(g.shutil, "which", lambda name: None)
    monkeypatch.setattr(g.os, "name", "nt")
    local = tmp_path
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    mingit = local / "Veyllo" / "git" / "cmd" / "git.exe"
    mingit.parent.mkdir(parents=True)
    mingit.write_text("")
    assert g._resolve_git() == str(mingit)


def test_last_resort_is_plain_git(monkeypatch):
    _reset_cache()
    monkeypatch.setattr(g.shutil, "which", lambda name: None)
    monkeypatch.setattr(g.os, "name", "posix")  # no Windows fallback paths
    assert g._resolve_git() == "git"


def test_result_is_memoized(monkeypatch):
    _reset_cache()
    calls = {"n": 0}
    def which(name):
        calls["n"] += 1
        return "/usr/bin/git"
    monkeypatch.setattr(g.shutil, "which", which)
    g._resolve_git(); g._resolve_git()
    assert calls["n"] == 1  # resolved once, then cached
    _reset_cache()
