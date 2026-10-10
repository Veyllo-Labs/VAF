# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf env`: the terminal's face of the sandbox environments (vaf/core/environments.py).

The group runs as the machine owner behind the terminal door, like `vaf ssh`. These
tests pin what the commands decide themselves: the owner's scope, the one-kind rule of
create, the exit code of exec, and that only --all / --all-owners reach other people's
environments."""
import pathlib
import re
import shlex
import types

import pytest
from typer.testing import CliRunner

import vaf.cli.cmd.env as env_cmd
from vaf.core import environments as envmod

MAIN = pathlib.Path(env_cmd.__file__).resolve().parents[2] / "main.py"


class _Mgr:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _call(*a, **k):
            self.calls.append((name, a, k))
            if name == "list":
                return [types.SimpleNamespace(describe=lambda: "0a1b2c3d, kind=temporary", owner="h")]
            if name == "create":
                return types.SimpleNamespace(describe=lambda: "0a1b2c3d", degraded="")
            if name == "exec":
                return envmod.ExecResult(3, "out\n", "")
            if name in ("stop", "delete"):
                return types.SimpleNamespace(id=a[1])
            if name == "prune":
                return {"removed": 1, "stopped": 0, "orphans": 2}
            return "ok"
        return _call


@pytest.fixture
def mgr(monkeypatch):
    m = _Mgr()
    monkeypatch.setattr(env_cmd, "_manager", lambda: m)
    monkeypatch.setattr(env_cmd, "_scope", lambda: "scope-owner")
    return m


def test_the_group_sits_behind_the_terminal_door():
    src = MAIN.read_text(encoding="utf-8")
    assert re.search(r'add_typer\(env_cmd\.app, name="env"[^)]*callback=_terminal_door', src, re.S)


def test_create_takes_exactly_one_kind_and_waits_for_the_image(mgr):
    r = CliRunner()
    assert r.invoke(env_cmd.app, ["create"]).exit_code == 2
    assert r.invoke(env_cmd.app, ["create", "--temp", "--project", "x"]).exit_code == 2
    assert r.invoke(env_cmd.app, ["create", "--temp", "--path", "/p"]).exit_code == 2
    res = r.invoke(env_cmd.app, ["create", "--project", "site", "--path", "/p", "--network", "registries"])
    assert res.exit_code == 0, res.output
    name, args, kw = mgr.calls[-1]
    assert name == "create" and args == ("scope-owner",)
    assert kw["kind"] == "project" and kw["project_path"] == "/p" and kw["wait_for_image"] is True


def test_a_docker_that_cannot_be_started_is_a_line_not_a_traceback(mgr, monkeypatch):
    """MUTATION: catch only EnvironmentRefused again - red: no docker CLI ended `vaf env
    shell` (and every other command) in a traceback."""
    import subprocess

    def _no_docker(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "docker")

    held = types.SimpleNamespace(get=lambda *a, **k: types.SimpleNamespace(container="vaf-env-x"),
                                 _ensure_running=lambda env: None, list=_no_docker)
    monkeypatch.setattr(env_cmd, "_manager", lambda: held)
    monkeypatch.setattr(subprocess, "call", _no_docker)
    res = CliRunner().invoke(env_cmd.app, ["shell", "0a1b2c3d"])
    assert res.exit_code == 1 and "docker could not be run" in res.output
    assert not isinstance(res.exception, FileNotFoundError)
    res = CliRunner().invoke(env_cmd.app, ["list"])
    assert res.exit_code == 1 and "docker could not be run" in res.output


def test_prune_says_when_docker_could_not_be_asked(monkeypatch):
    """A reaper pass that could not list anything returned zeros, and prune printed
    "Removed 0" as a success. MUTATION: let prune read a failed listing as an empty one -
    red."""
    from vaf.core import containers
    monkeypatch.setattr(containers, "docker",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="",
                                                              stderr="Cannot connect to the Docker daemon"))
    monkeypatch.setattr(env_cmd, "_manager", lambda: envmod.EnvironmentManager())
    res = CliRunner().invoke(env_cmd.app, ["prune"])
    assert res.exit_code == 1 and "Removed" not in res.output
    assert "did not list the environments" in res.output


def test_a_preview_without_a_screenshot_writes_no_file(monkeypatch, tmp_path):
    """MUTATION: decode r["screenshot_b64"] unchecked again - red: a KeyError traceback."""
    held = types.SimpleNamespace(render=lambda *a, **k: {"ok": True, "title": "x"})
    monkeypatch.setattr(env_cmd, "_manager", lambda: held)
    monkeypatch.setattr(env_cmd, "_scope", lambda: "scope-owner")
    out = tmp_path / "shot.png"
    res = CliRunner().invoke(env_cmd.app, ["preview", "0a1b2c3d", "http://localhost:3000", "--out", str(out)])
    assert res.exit_code == 1 and "without a screenshot" in res.output and not out.exists()


def test_exec_passes_the_command_and_its_exit_code(mgr):
    res = CliRunner().invoke(env_cmd.app, ["exec", "0a1b2c3d", "--", "python3", "-c", "print(1)"])
    assert res.exit_code == 3 and "out" in res.output
    name, args, kw = mgr.calls[-1]
    assert args == ("scope-owner", "0a1b2c3d", "python3 -c 'print(1)'")


def test_exec_keeps_each_word_whole_and_one_word_is_a_shell_line(mgr):
    """MUTATION: join the words with spaces again - red: `print('a b')` reached sh as
    two words and python3 saw a syntax error."""
    r = CliRunner()
    r.invoke(env_cmd.app, ["exec", "0a1b2c3d", "--", "python3", "-c", "print('a b')"])
    command = mgr.calls[-1][1][2]
    assert shlex.split(command) == ["python3", "-c", "print('a b')"]
    r.invoke(env_cmd.app, ["exec", "0a1b2c3d", "--background", "--", "node", "my server.js"])
    assert mgr.calls[-1][0] == "start_process"
    assert shlex.split(mgr.calls[-1][1][2]) == ["node", "my server.js"]
    r.invoke(env_cmd.app, ["exec", "0a1b2c3d", "--", "pip install x && pytest -q"])
    assert mgr.calls[-1][1][2] == "pip install x && pytest -q"


def test_only_the_flags_reach_other_peoples_environments(mgr):
    """MUTATION: pass admin=True always - red."""
    r = CliRunner()
    r.invoke(env_cmd.app, ["list"])
    assert mgr.calls[-1][2] == {"everyone": False}
    r.invoke(env_cmd.app, ["list", "--all"])
    assert mgr.calls[-1][2] == {"everyone": True}
    r.invoke(env_cmd.app, ["delete", "0a1b2c3d", "--yes"])
    assert mgr.calls[-1][2] == {"admin": False}
    r.invoke(env_cmd.app, ["delete", "0a1b2c3d", "--yes", "--all-owners"])
    assert mgr.calls[-1][2] == {"admin": True}


def test_a_refusal_is_an_error_and_exit_1(monkeypatch):
    class _Refusing:
        def list(self, *a, **k):
            raise envmod.EnvironmentRefused("no account to own the environment")
    monkeypatch.setattr(env_cmd, "_manager", lambda: _Refusing())
    monkeypatch.setattr(env_cmd, "_scope", lambda: None)
    res = CliRunner().invoke(env_cmd.app, ["list"])
    assert res.exit_code == 1 and "no account" in res.output


def test_a_timeout_exits_124_whatever_killed_the_command(monkeypatch):
    """`timeout -s KILL` ends a command with 137. MUTATION: exit with the return code on a
    timeout again - red."""
    from vaf.core import environments as envmod
    monkeypatch.setattr(env_cmd, "_manager", lambda: types.SimpleNamespace(
        exec=lambda *a, **k: envmod.ExecResult(137, "", "", timed_out=True)))
    monkeypatch.setattr(env_cmd, "_scope", lambda: "s")
    assert CliRunner().invoke(env_cmd.app, ["exec", "0a1b2c3d", "--", "sleep", "999"]).exit_code == 124


def test_preview_prints_the_page_text(monkeypatch, tmp_path):
    """The help promises the console and the text. MUTATION: drop the text lines - red."""
    import base64
    shot = {"ok": True, "screenshot_b64": base64.b64encode(b"png").decode(), "title": "Home",
            "page_errors": [], "console": ["hello"], "text": "Counter 0"}
    monkeypatch.setattr(env_cmd, "_manager", lambda: types.SimpleNamespace(render=lambda *a, **k: shot))
    monkeypatch.setattr(env_cmd, "_scope", lambda: "s")
    res = CliRunner().invoke(env_cmd.app, ["preview", "0a1b2c3d", "index.html", "--out", str(tmp_path / "p.png")])
    assert res.exit_code == 0 and "Rendered text:\nCounter 0" in res.output, res.output


def test_prune_reports_what_it_did(mgr):
    res = CliRunner().invoke(env_cmd.app, ["prune"])
    assert res.exit_code == 0 and "Removed 1" in res.output and "cleared 2" in res.output


def test_a_docker_that_hangs_is_a_line_not_a_traceback(monkeypatch):
    """containers.docker raises TimeoutExpired when docker does not answer. MUTATION: catch
    only EnvironmentRefused and OSError again - red."""
    import subprocess

    def _hang(*a, **k):
        raise subprocess.TimeoutExpired(["docker", "ps"], 30)

    monkeypatch.setattr(env_cmd, "_manager", lambda: types.SimpleNamespace(list=_hang, processes=_hang))
    monkeypatch.setattr(env_cmd, "_scope", lambda: "scope-owner")
    for args in (["list"], ["ps"]):
        res = CliRunner().invoke(env_cmd.app, args)
        assert res.exit_code == 1 and "did not answer in time" in res.output, args
