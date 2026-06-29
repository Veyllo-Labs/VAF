# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import json
import types

import pytest
import typer

import vaf.cli.cmd.update as upd


class FakeGit:
    """Records git calls; can inject a failure on a given subcommand."""

    def __init__(self):
        self.calls = []
        self.fail_on = None  # e.g. "checkout"
        self.dirty = ""

    def __call__(self, args, cwd="."):
        a = list(args)
        self.calls.append(a)
        if a[:1] == ["rev-parse"] and "--show-toplevel" in a:
            return 0, "/fake/root", ""
        if a == ["rev-parse", "HEAD"]:
            return 0, "abcdef1234567890", ""
        if a[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main", ""
        if a[:1] == ["status"]:
            return 0, self.dirty, ""
        if a and a[0] == self.fail_on:
            return 1, "", f"{self.fail_on} failed"
        return 0, "", ""


@pytest.fixture
def patched(monkeypatch, tmp_path):
    fg = FakeGit()
    monkeypatch.setattr(upd, "run_git", fg)
    monkeypatch.setattr(upd, "is_git_repo", lambda p: True)

    events = {"stopped": 0, "started": 0}
    monkeypatch.setattr(upd.service, "cmd_stop", lambda: events.__setitem__("stopped", events["stopped"] + 1))
    monkeypatch.setattr(upd.service, "cmd_start", lambda: events.__setitem__("started", events["started"] + 1))

    state = {"verify_version": "9.9.9", "pip_fail": False}

    def fake_run(cmd, **kw):
        if "pip" in cmd:
            return types.SimpleNamespace(returncode=1 if state["pip_fail"] else 0, stdout="", stderr="")
        if "--version" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=state["verify_version"], stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(upd.subprocess, "run", fake_run)
    monkeypatch.setattr(upd, "_run_migrations", lambda: None)

    bc = tmp_path / "last_update.json"
    monkeypatch.setattr(upd, "_breadcrumb_path", lambda: bc)
    monkeypatch.setattr(upd, "_resolve_latest_release", lambda pre=None: {
        "tag": "v9.9.9", "version": "9.9.9", "html_url": "u", "body": "", "prerelease": False
    })
    return types.SimpleNamespace(git=fg, events=events, state=state, breadcrumb=bc)


def test_apply_happy_path(patched):
    upd._apply(dry_run=False, assume_yes=True, target_tag=None)
    seq = [c[0] for c in patched.git.calls]
    assert "fetch" in seq and "checkout" in seq
    assert patched.events["stopped"] == 1 and patched.events["started"] == 1
    assert not patched.breadcrumb.exists()  # cleared on success


def test_apply_dirty_tree_aborts_without_touching_service(patched):
    patched.git.dirty = " M vaf/core/agent.py"
    with pytest.raises(typer.Exit) as ei:
        upd._apply(dry_run=False, assume_yes=True, target_tag=None)
    assert ei.value.exit_code == 1
    assert patched.events["stopped"] == 0
    assert not patched.breadcrumb.exists()


def test_apply_checkout_failure_rolls_back(patched):
    patched.git.fail_on = "checkout"
    with pytest.raises(typer.Exit) as ei:
        upd._apply(dry_run=False, assume_yes=True, target_tag=None)
    assert ei.value.exit_code == 1
    checkouts = [c for c in patched.git.calls if c[0] == "checkout"]
    assert len(checkouts) >= 2  # failed target checkout + rollback checkout
    assert patched.events["started"] >= 1
    assert not patched.breadcrumb.exists()


def test_apply_verify_failure_rolls_back(patched):
    patched.state["verify_version"] = "0.0.0"  # mismatch -> verify fails
    with pytest.raises(typer.Exit) as ei:
        upd._apply(dry_run=False, assume_yes=True, target_tag=None)
    assert ei.value.exit_code == 1
    checkouts = [c for c in patched.git.calls if c[0] == "checkout"]
    assert len(checkouts) >= 2
    assert not patched.breadcrumb.exists()


def test_apply_dry_run_changes_nothing(patched):
    with pytest.raises(typer.Exit) as ei:
        upd._apply(dry_run=True, assume_yes=True, target_tag=None)
    assert ei.value.exit_code == 0
    seq = [c[0] for c in patched.git.calls]
    assert "fetch" not in seq and "checkout" not in seq
    assert patched.events["stopped"] == 0
    assert not patched.breadcrumb.exists()


def test_recover_with_breadcrumb_rolls_back(patched):
    patched.breadcrumb.write_text(json.dumps({"recorded_head": "deadbeef", "branch": "main"}))
    upd._recover()
    checkouts = [c for c in patched.git.calls if c[0] == "checkout"]
    assert checkouts and checkouts[0][1] == "deadbeef"
    assert not patched.breadcrumb.exists()
