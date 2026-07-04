# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Coder run_tests / sandbox test runner.

The coding agent could not run the tests it wrote (the python_sandbox guard blocks file
I/O and the project lives on the host, not in the sandbox volume), so it shipped
unverified "tests pass" claims. run_project_tests copies the project into the isolated
sandbox, runs pytest there, and returns the real result. These tests pin the formatting,
the error branches, and - guaranteed - that the throwaway copy is always cleaned up.

The hermetic tests mock subprocess; a docker-gated integration test runs real pytest.
"""
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import vaf.tools.sandbox_test_runner as str_mod
from vaf.tools.sandbox_test_runner import (
    RunTestsTool,
    _format_result,
    _included_size,
    run_project_tests,
)


# ── pure formatting / sizing ────────────────────────────────────────────────

def test_format_result_pass_fail_timeout():
    assert _format_result("pytest", 0, "17 passed", "").startswith("TESTS PASSED")
    assert _format_result("pytest", 1, "1 failed", "").startswith("TESTS FAILED (exit 1)")
    assert _format_result("pytest", -1, "", "Timed out").startswith("TEST RUN TIMED OUT")


def test_no_emojis_in_output():
    """House rule: no emojis in this feature's committed output."""
    for rc in (0, 1, -1):
        r = _format_result("pytest", rc, "x", "")
        assert all(ord(c) < 128 for c in r), f"non-ASCII in result for rc={rc}: {r!r}"


def test_format_result_keeps_the_tail():
    out = "\n".join(f"line{i}" for i in range(2000))
    r = _format_result("pytest", 1, out, "")
    assert "truncated" in r
    assert "line1999" in r  # pytest's summary is at the end - the tail must survive


def test_included_size_excludes_heavy_dirs(tmp_path):
    (tmp_path / "app.py").write_text("x" * 100)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "blob").write_text("y" * 10_000)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "big").write_text("z" * 10_000)
    assert _included_size(str(tmp_path)) == 100  # only app.py counts


# ── error branches (no docker needed) ───────────────────────────────────────

def test_missing_project_dir():
    assert "project directory not found" in run_project_tests("/no/such/dir")


def test_sandbox_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(str_mod, "_sandbox_running", lambda: False)
    assert "sandbox (vaf-sandbox) is not running" in run_project_tests(str(tmp_path))


def test_project_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(str_mod, "_sandbox_running", lambda: True)
    monkeypatch.setattr(str_mod, "_included_size", lambda _b: str_mod._MAX_COPY_BYTES + 1)
    assert "too large to copy" in run_project_tests(str(tmp_path))


# ── the full path with subprocess mocked: cleanup is guaranteed ─────────────

class _FakeProc:
    def __init__(self, wait_rc=0):
        self.stdout = SimpleNamespace(close=lambda: None)
        self._wait_rc = wait_rc
    def wait(self, timeout=None):
        return self._wait_rc


def _install_fake_subprocess(monkeypatch, exec_rc=0, exec_out="ok", fail_at=None, tar_rc=0):
    """Record every subprocess.run argv; simulate mkdir/untar/exec/rm. fail_at lets a
    stage return non-zero to prove cleanup still runs."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        if "mkdir" in cmd:
            return SimpleNamespace(returncode=0 if fail_at != "mkdir" else 1, stdout="", stderr="mkdir err")
        if "tar" in cmd and "xzf" in cmd:  # untar
            return SimpleNamespace(returncode=0 if fail_at != "untar" else 2, stdout="", stderr="untar err")
        if "-w" in cmd:  # the exec_bounded test command
            return SimpleNamespace(returncode=exec_rc, stdout=exec_out, stderr="")
        if "rm" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_popen(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeProc(wait_rc=tar_rc)

    monkeypatch.setattr(str_mod, "_sandbox_running", lambda: True)
    monkeypatch.setattr(str_mod, "_included_size", lambda _b: 100)
    monkeypatch.setattr(str_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(str_mod.subprocess, "Popen", fake_popen)
    return calls


def _rm_was_called(calls):
    return any("rm" in c and "-rf" in c for c in calls)


def test_happy_path_reports_pass_and_cleans_up(tmp_path, monkeypatch):
    calls = _install_fake_subprocess(monkeypatch, exec_rc=0, exec_out="17 passed")
    out = run_project_tests(str(tmp_path))
    assert "TESTS PASSED" in out and "17 passed" in out
    assert _rm_was_called(calls), "the throwaway sandbox copy must be removed"


def test_cleanup_runs_even_when_the_command_fails(tmp_path, monkeypatch):
    calls = _install_fake_subprocess(monkeypatch, exec_rc=1, exec_out="1 failed")
    out = run_project_tests(str(tmp_path))
    assert "TESTS FAILED" in out
    assert _rm_was_called(calls)


def test_incomplete_copy_is_detected(tmp_path, monkeypatch):
    """A non-zero HOST tar exit means the copy is incomplete - do not run tests on it."""
    calls = _install_fake_subprocess(monkeypatch, tar_rc=2)
    out = run_project_tests(str(tmp_path))
    assert "copy is incomplete" in out
    assert _rm_was_called(calls)


def test_timeout_kills_by_cwd_not_pytest_name(tmp_path, monkeypatch):
    """On timeout the run's whole process tree (any command) is killed via /proc cwd,
    never a global 'pkill -f pytest' that would also hit other concurrent runs."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "mkdir" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "tar" in cmd and "xzf" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "-w" in cmd:  # the bounded exec -> simulate a timeout
            raise subprocess.TimeoutExpired(cmd, 1)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(str_mod, "_sandbox_running", lambda: True)
    monkeypatch.setattr(str_mod, "_included_size", lambda _b: 100)
    monkeypatch.setattr(str_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(str_mod.subprocess, "Popen", lambda cmd, **k: (calls.append(list(cmd)) or _FakeProc()))

    out = run_project_tests(str(tmp_path), command="npm test", timeout=1)
    assert "TEST RUN TIMED OUT" in out
    joined = [" ".join(str(x) for x in c) for c in calls]
    assert any("readlink" in j and "/cwd" in j for j in joined), "kill must be cwd-scoped"
    assert not any("pkill" in j and "pytest" in j for j in joined), "must not global-kill pytest"
    assert _rm_was_called(calls)


def test_cleanup_runs_even_when_copy_fails(tmp_path, monkeypatch):
    calls = _install_fake_subprocess(monkeypatch, fail_at="untar")
    out = run_project_tests(str(tmp_path))
    assert "failed to copy project" in out
    assert _rm_was_called(calls), "cleanup must run even if the copy-in failed"


def test_tool_is_side_effect_free_metadata():
    t = RunTestsTool("/tmp/x")
    assert t.name == "run_tests"
    assert t.side_effect_class == "none"  # never modifies the host project


# ── docker-gated real integration ───────────────────────────────────────────

def _sandbox_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", "vaf-sandbox"],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and "true" in r.stdout.lower()
    except Exception:
        return False


@pytest.mark.skipif(not _sandbox_available(), reason="vaf-sandbox container not running")
def test_integration_real_pytest_pass_and_fail(tmp_path):
    (tmp_path / "test_pass.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    passed = run_project_tests(str(tmp_path))
    assert "TESTS PASSED" in passed, passed

    (tmp_path / "test_fail.py").write_text("def test_bad():\n    assert 1 == 2\n")
    failed = run_project_tests(str(tmp_path))
    assert "TESTS FAILED" in failed and "test_bad" in failed, failed
