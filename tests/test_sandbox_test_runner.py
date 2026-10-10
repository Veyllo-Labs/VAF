# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Coder run_tests / sandbox test runner.

The coding agent could not run the tests it wrote (the python_sandbox guard blocks file
I/O and the project lives on the host), so it shipped unverified "tests pass" claims.
run_project_tests copies the project into the caller's own scratch environment, runs
pytest there, and returns the real result. These tests pin the formatting, the error
branches, whose sandbox it runs in, and - guaranteed - that the throwaway copy is always
removed. A fake environment manager stands in for docker.
"""
import io
import tarfile
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


def test_project_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(str_mod, "_included_size", lambda _b: str_mod._MAX_COPY_BYTES + 1)
    assert "too large to copy" in run_project_tests(str(tmp_path))


# ── the full path with a fake environment: cleanup is guaranteed ────────────

class _FakeMgr:
    """scratch_for and exec_in, recording every command; stages can be made to fail."""

    def __init__(self, exec_rc=0, exec_out="ok", fail_at=None, pytest_present=True,
                 timed_out=False, refuse=None):
        self.calls, self.scopes = [], []
        self.exec_rc, self.exec_out, self.fail_at = exec_rc, exec_out, fail_at
        self.pytest_present, self.timed_out, self.refuse = pytest_present, timed_out, refuse

    def scratch_for(self, scope):
        self.scopes.append(scope)
        if self.refuse:
            from vaf.core.environments import EnvironmentRefused
            raise EnvironmentRefused(self.refuse)
        return SimpleNamespace(container="vaf-env-ab12cd34ef56-scratch")

    def exec_in(self, env, argv, **kw):
        from vaf.core.environments import ExecResult
        self.calls.append((argv, kw))
        if argv[0] == "mkdir":
            return ExecResult(1 if self.fail_at == "mkdir" else 0, "", "mkdir err")
        if argv[:3] == ["python3", "-m", "pytest"]:
            return ExecResult(0 if self.pytest_present else 1, "", "")
        if argv[:4] == ["python3", "-m", "pip", "install"]:
            return ExecResult(1 if self.fail_at == "install" else 0, "", "no network")
        if argv[0] == "rm":
            return ExecResult(0, "", "")
        return ExecResult(-1 if self.timed_out else self.exec_rc, self.exec_out, "",
                          timed_out=self.timed_out)


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        mgr = _FakeMgr(**kw)
        import vaf.core.environments as envmod
        monkeypatch.setattr(envmod, "get_environment_manager", lambda: mgr)
        mgr.untars = []

        def _docker(args, timeout=60, **k):
            mgr.untars.append((args, k.get("input")))
            return SimpleNamespace(returncode=2 if mgr.fail_at == "untar" else 0,
                                   stdout=b"", stderr=b"untar err")

        monkeypatch.setattr("vaf.core.containers.docker", _docker)
        return mgr
    return install


def _removed(mgr):
    return any(argv[:2] == ["rm", "-rf"] for argv, _ in mgr.calls)


def test_happy_path_reports_pass_runs_in_the_copy_and_cleans_up(tmp_path, fake):
    mgr = fake(exec_rc=0, exec_out="17 passed")
    (tmp_path / "test_a.py").write_text("def test_a(): pass\n")
    out = run_project_tests(str(tmp_path), user_scope_id="scope-alice")
    assert "TESTS PASSED" in out and "17 passed" in out
    assert mgr.scopes == ["scope-alice"], "the run must go to the caller's own sandbox"
    run = [(a, k) for a, k in mgr.calls if a[:2] == ["sh", "-c"]][0]
    assert run[1]["cwd"].startswith("/tmp/vaf_tests_")
    assert _removed(mgr), "the throwaway sandbox copy must be removed"


def test_the_copy_carries_the_project_without_heavy_dirs(tmp_path, fake):
    mgr = fake()
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    (tmp_path / "pkg" / "node_modules").mkdir(parents=True)
    (tmp_path / "pkg" / "node_modules" / "big.js").write_text("//")
    (tmp_path / "pkg" / "mod.py").write_text("y = 2\n")
    run_project_tests(str(tmp_path), command="python3 -m unittest")
    args, payload = mgr.untars[0]
    assert args[:3] == ["exec", "-i", "vaf-env-ab12cd34ef56-scratch"] and "--no-same-owner" in args
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        names = sorted(tar.getnames())
    assert names == ["app.py", "pkg", "pkg/mod.py"], names


def test_cleanup_runs_even_when_the_command_fails(tmp_path, fake):
    mgr = fake(exec_rc=1, exec_out="1 failed")
    out = run_project_tests(str(tmp_path))
    assert "TESTS FAILED" in out
    assert _removed(mgr)


def test_a_timeout_reads_as_one(tmp_path, fake):
    mgr = fake(timed_out=True)
    out = run_project_tests(str(tmp_path), command="npm test", timeout=1)
    assert "TEST RUN TIMED OUT" in out and "Timed out after 1s" in out
    assert _removed(mgr)


def test_stop_reaches_the_test_run_and_reads_as_a_stop(tmp_path, fake, monkeypatch):
    """MUTATION: drop check_stop from either exec_in - red: Stop let the tests run to their
    timeout inside the container."""
    import vaf.core.tool_dispatch as td
    from vaf.core.environments import ExecResult
    from vaf.tools.sandbox_test_runner import run_tests_in_environment
    stop = lambda: True
    monkeypatch.setattr(td, "current_session_stop_check", lambda: stop)
    import vaf.tools.sandbox_test_runner as runner
    monkeypatch.setattr(runner, "current_session_stop_check", lambda: stop)
    mgr = fake()
    run_project_tests(str(tmp_path), command="npm test")
    assert [kw.get("check_stop") for argv, kw in mgr.calls if argv[:2] == ["sh", "-c"]] == [stop]
    mgr.calls.clear()
    run_tests_in_environment(SimpleNamespace(container="c"), "npm test")
    assert [kw.get("check_stop") for argv, kw in mgr.calls if argv[:2] == ["sh", "-c"]] == [stop]
    report = runner._run_result("npm test", ExecResult(-1, "", "", cancelled=True), 180)
    assert report.startswith("TEST RUN STOPPED") and "TIMED OUT" not in report.upper()


def test_cleanup_runs_even_when_copy_fails(tmp_path, fake):
    mgr = fake(fail_at="untar")
    out = run_project_tests(str(tmp_path))
    assert out.startswith("Cannot run tests:") and "failed to copy project" in out
    assert _removed(mgr), "cleanup must run even if the copy-in failed"


def test_a_refused_sandbox_is_a_cannot_run(tmp_path, fake):
    """The coder counts a run as failed by this prefix (context.py)."""
    fake(refuse="no account to own the environment")
    out = run_project_tests(str(tmp_path))
    assert out.startswith("Cannot run tests:") and "no account" in out


def test_pytest_is_installed_on_demand_only_when_missing(tmp_path, fake):
    """The fallback image the scratch environment runs on while the real one builds has
    no pytest, and pytest is the DEFAULT command."""
    mgr = fake(pytest_present=False)
    run_project_tests(str(tmp_path))
    assert any(a[:4] == ["python3", "-m", "pip", "install"] for a, _ in mgr.calls)
    mgr = fake(pytest_present=True)
    run_project_tests(str(tmp_path))
    assert not any(a[:4] == ["python3", "-m", "pip", "install"] for a, _ in mgr.calls)
    mgr = fake(pytest_present=False, fail_at="install")
    out = run_project_tests(str(tmp_path))
    assert out.startswith("Cannot run tests:") and "installing it failed" in out


def test_tool_metadata_and_identity():
    t = RunTestsTool("/tmp/x")
    assert t.name == "run_tests"
    assert t.side_effect_class == "none"  # never modifies the host project
    assert RunTestsTool.identity_kwargs == ("user_scope_id",)


def test_the_tool_passes_the_callers_scope(tmp_path, fake):
    mgr = fake()
    RunTestsTool(str(tmp_path)).run(user_scope_id="scope-bob")
    assert mgr.scopes == ["scope-bob"]


def test_a_timeout_that_is_not_a_number_falls_back_instead_of_raising(monkeypatch):
    """MUTATION: back to int(kwargs["timeout"]) in run - red: the tool raised ValueError."""
    import vaf.tools.sandbox_test_runner as runner
    seen = []
    monkeypatch.setattr(runner, "run_project_tests",
                        lambda base, cmd, timeout=180, user_scope_id=None: seen.append(timeout) or "ok")
    monkeypatch.setattr(runner, "run_tests_in_environment",
                        lambda env, cmd, timeout=180: seen.append(timeout) or "ok")
    runner.RunTestsTool("/p").run(timeout="soon")
    runner.RunTestsTool("/p", environment=object()).run(timeout="soon")
    runner.RunTestsTool("/p").run(timeout="90")
    assert seen == [180, 180, 90]
