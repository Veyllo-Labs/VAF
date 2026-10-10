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


def test_prune_reports_what_it_did(mgr):
    res = CliRunner().invoke(env_cmd.app, ["prune"])
    assert res.exit_code == 0 and "Removed 1" in res.output and "cleared 2" in res.output
