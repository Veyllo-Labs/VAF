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
        self.fail_on = None  # e.g. "checkout" -> that subcommand always fails
        self.fail_checkout_to = None  # fail `checkout <ref>` only for this ref (target fails, rollback ok)
        self.checkout_err = "checkout failed"
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
        if a[:1] == ["checkout"]:
            ref = a[1] if len(a) > 1 else ""
            if self.fail_on == "checkout" or (self.fail_checkout_to and ref == self.fail_checkout_to):
                return 1, "", self.checkout_err
            return 0, "", ""
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


def test_apply_non_git_converts_and_resets(patched, monkeypatch):
    # A non-git (ZIP) install must be converted into a git checkout in place — git init + an origin
    # remote pointing at the official repo — and adopt the target tag via `git reset --hard`
    # (not `git checkout`, since there is no prior HEAD).
    monkeypatch.setattr(upd, "is_git_repo", lambda p: False)
    upd._apply(dry_run=False, assume_yes=True, target_tag=None)
    calls = patched.git.calls
    heads = [c[0] for c in calls]
    assert "init" in heads                                   # converted to a git checkout
    assert any(c[0] == "remote" and upd._REPO_URL in c for c in calls)  # origin -> official repo
    assert ["reset", "--hard", "v9.9.9"] in calls            # adopts the tag
    assert "checkout" not in heads                           # never uses checkout in the non-git path
    assert patched.events["stopped"] == 1 and patched.events["started"] == 1
    assert not patched.breadcrumb.exists()                   # cleared on success


def test_apply_non_git_dry_run_changes_nothing(patched, monkeypatch):
    monkeypatch.setattr(upd, "is_git_repo", lambda p: False)
    with pytest.raises(typer.Exit):
        upd._apply(dry_run=True, assume_yes=True, target_tag=None)
    heads = [c[0] for c in patched.git.calls]
    assert "init" not in heads and "reset" not in heads and "fetch" not in heads
    assert patched.events["stopped"] == 0 and patched.events["started"] == 0


def test_apply_dirty_tree_aborts_without_touching_service(patched):
    patched.git.dirty = " M vaf/core/agent.py"
    with pytest.raises(typer.Exit) as ei:
        upd._apply(dry_run=False, assume_yes=True, target_tag=None)
    assert ei.value.exit_code == 1
    assert patched.events["stopped"] == 0
    assert not patched.breadcrumb.exists()


def test_apply_checkout_failure_rolls_back(patched):
    patched.git.fail_checkout_to = "v9.9.9"  # only the target checkout fails; rollback checkout succeeds
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


# ── _verify: exact version match, not substring (the alpha->stable false-pass) ──────────────────

def _fake_version_out(monkeypatch, text):
    monkeypatch.setattr(
        upd.subprocess, "run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=text, stderr=""),
    )


def test_verify_rejects_substring_false_pass(monkeypatch):
    # The tree still reports the alpha build, but we targeted stable 0.1.0 -> must NOT pass
    # ('0.1.0' is a substring of 'Version: 0.1.0a1', which the old containment check accepted).
    _fake_version_out(monkeypatch, "Version: 0.1.0a1")
    with pytest.raises(upd._UpdateError):
        upd._verify("0.1.0")


def test_verify_accepts_exact_match(monkeypatch):
    _fake_version_out(monkeypatch, "Version: 0.1.0a1")
    upd._verify("0.1.0a1")  # exact -> no raise


def test_verify_accepts_bare_version_output(monkeypatch):
    _fake_version_out(monkeypatch, "9.9.9")  # output without the 'Version:' prefix
    upd._verify("9.9.9")


# ── rollback / recover honesty when the rollback checkout itself fails ──────────────────────────

def test_apply_failed_rollback_keeps_breadcrumb(patched):
    # Every checkout fails (target AND rollback) -> rollback cannot restore -> keep the breadcrumb,
    # do not restart the service, do not claim success.
    patched.git.fail_on = "checkout"
    with pytest.raises(typer.Exit) as ei:
        upd._apply(dry_run=False, assume_yes=True, target_tag=None)
    assert ei.value.exit_code == 1
    assert patched.breadcrumb.exists()      # kept for `vaf update --recover`
    assert patched.events["started"] == 0   # no false restart on a failed rollback


def test_recover_failed_checkout_keeps_breadcrumb(patched):
    patched.breadcrumb.write_text(json.dumps({"recorded_head": "deadbeef", "branch": "main"}))
    patched.git.fail_on = "checkout"
    with pytest.raises(typer.Exit) as ei:
        upd._recover()
    assert ei.value.exit_code == 1
    assert patched.breadcrumb.exists()      # not cleared -> retryable


# ── --tag escape-hatch: validation + explicit downgrade warning ─────────────────────────────────

def _capture_ui(monkeypatch):
    msgs = []
    for name in ("error", "warning", "info", "success", "print", "event"):
        monkeypatch.setattr(upd.UI, name, lambda *a, **k: msgs.append(" ".join(str(x) for x in a)))
    return msgs


def test_apply_invalid_tag_errors(patched):
    with pytest.raises(typer.Exit) as ei:
        upd._apply(dry_run=False, assume_yes=True, target_tag="not-a-version")
    assert ei.value.exit_code == 1
    assert patched.events["stopped"] == 0   # bailed before touching the service
    assert not any(c[0] == "checkout" for c in patched.git.calls)


def test_apply_tag_downgrade_warns(patched, monkeypatch):
    msgs = _capture_ui(monkeypatch)
    # installed __version__ is the real 0.1.0a0; pinning to an older stable tag is a downgrade.
    with pytest.raises(typer.Exit):
        upd._apply(dry_run=True, assume_yes=True, target_tag="v0.0.1")
    assert any("DOWNGRADE" in m for m in msgs)
