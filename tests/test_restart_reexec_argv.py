# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`restart` in both terminal lanes starts again what was started, never `python -m vaf`.

The restart exists in the classic lane (`_run_modern` in vaf/cli/cmd/run.py) and in the
full-screen app (`_exec_restart` in vaf/cli/tui_app/app.py). A `python -m <module>` start must
come back as `python -m <module>`: the launchers start `-m vaf.main`, and running that file as a
script puts vaf/ first on sys.path, where vaf/github shadows the `github` package; a wrapper
package's `__main__` module with a relative import cannot run as a script at all, and the
failure happens after the exec, where nothing reports it. Every other start reruns argv[0].
The package has no `__main__` module, so `python -m vaf` cannot start and is never produced.

Both lanes are driven with ONE argv table, so a drift between them turns one of the two tests
red, and `os.execl` is recorded. The last test runs a real wrapper package with `python -m`,
records the full-screen app's restart command there and then runs that command.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from test_cli_web_lane_queue_tasks import modern_lane  # noqa: F401 - modern_lane is a fixture

ROOT = Path(__file__).resolve().parents[1]

# (argv[0], the name in __main__.__spec__ or None, how it is started again). Synthetic paths.
ARGV_TABLE = [
    # python -m mypkg run: a wrapper package's __main__ module.
    ("/opt/app/mypkg/__main__.py", "mypkg.__main__", ("module", "mypkg")),
    # python -m vaf.main run: run_vaf.sh, run_vaf.bat.
    ("/opt/app/vaf/main.py", "vaf.main", ("module", "vaf.main")),
    # python -m pkg.sub run: a subpackage's __main__ module.
    ("/opt/app/pkg/sub/__main__.py", "pkg.sub.__main__", ("module", "pkg.sub")),
    # python vaf/main.py run and python mypkg/__main__.py run: a script file.
    ("/opt/app/vaf/main.py", None, ("script",)),
    ("/opt/app/mypkg/__main__.py", None, ("script",)),
    # The console script.
    ("/opt/app/venv/bin/vaf", None, ("script",)),
    # python /opt/app/tool.pyz run: a directory or zip archive; its spec name is bare.
    ("/opt/app/tool.pyz", "__main__", ("script",)),
    # A launcher that keeps its .exe suffix.
    ("C:\\app\\Scripts\\vaf.exe", None, ("exe",)),
]
_IDS = [f"{a}-{s}" for a, s, _ in ARGV_TABLE]


def _expected(argv0: str, how: tuple) -> tuple:
    if how[0] == "exe":
        return (argv0, argv0, "run")
    if how[0] == "module":
        return (sys.executable, sys.executable, "-m", how[1], "run")
    return (sys.executable, sys.executable, argv0, "run")


def _start(monkeypatch, argv0: str, spec_name: str | None) -> None:
    """Make this process look like one started that way."""
    monkeypatch.setattr(sys, "argv", [argv0, "run"])
    spec = types.SimpleNamespace(name=spec_name) if spec_name else None
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", spec, raising=False)


def _assert_never_module_vaf(calls) -> None:
    for call in calls:
        pairs = list(zip(call, call[1:]))
        assert ("-m", "vaf") not in pairs, call


@pytest.mark.parametrize("argv0,spec_name,how", ARGV_TABLE, ids=_IDS)
def test_classic_lane_restart_starts_the_started_program(
        modern_lane, monkeypatch, argv0, spec_name, how):  # noqa: F811
    _start(monkeypatch, argv0, spec_name)
    modern_lane.rec["script"] = ["restart", None]
    # The recorded execl raises; the restart's own `except` prints "Restart failed" and the
    # loop goes on, so the console check of the fixture does not apply here.
    rec = modern_lane.run(console_breaks=True)
    assert rec["execl"] == [_expected(argv0, how)]
    _assert_never_module_vaf(rec["execl"])


class _Stop(Exception):
    """Raised by the recorded os.execl, as the real one never returns."""


@pytest.mark.parametrize("argv0,spec_name,how", ARGV_TABLE, ids=_IDS)
def test_app_exec_restart_starts_the_started_program(monkeypatch, argv0, spec_name, how):
    from vaf.cli.tui_app import app as app_mod
    from vaf.cli.ui import UI
    from vaf.core.frontend_manager import FrontendManager

    stops = []
    monkeypatch.setattr(FrontendManager, "stop_frontend",
                        lambda self, *a, **k: stops.append(k))
    calls = []

    def _execl(*a):
        calls.append(a)
        raise _Stop("execl")
    monkeypatch.setattr(os, "execl", _execl)
    errors = []
    monkeypatch.setattr(UI, "error", staticmethod(lambda msg, *a, **k: errors.append(str(msg))))
    _start(monkeypatch, argv0, spec_name)

    app_mod._exec_restart()

    assert stops == [{"wait_for_exit": True}]
    assert calls == [_expected(argv0, how)]
    _assert_never_module_vaf(calls)
    assert errors and "Restart failed" in errors[0]


# A wrapper package that calls VAF from its `__main__` module, with a relative import there.
# First start: it drives the full-screen app's real restart with os.execl recorded and prints
# the recorded command. Second start (the env marker is set): it reports how it was started.
_WRAPPER_ENTRY = '''\
import json
import os
import sys


def main():
    if os.environ.get("RESTARTWRAP_PHASE") == "restarted":
        spec = getattr(sys.modules["__main__"], "__spec__", None)
        here = os.path.dirname(os.path.abspath(__file__))
        print("REPORT " + json.dumps({
            "spec": getattr(spec, "name", None),
            "argv": sys.argv[1:],
            "package_dir_on_path": here in [os.path.abspath(p or ".") for p in sys.path],
        }))
        return
    from vaf.core.frontend_manager import FrontendManager
    FrontendManager.stop_frontend = lambda self, *a, **k: None
    calls = []

    def _execl(*a):
        calls.append(list(a))
        raise RuntimeError("recorded")
    os.execl = _execl
    from vaf.cli.tui_app.app import _exec_restart
    _exec_restart()
    print("RECORDED " + json.dumps(calls))
'''


def _line(stdout: str, tag: str):
    for line in stdout.splitlines():
        if line.startswith(tag + " "):
            return json.loads(line[len(tag) + 1:])
    return None


def test_python_m_wrapper_with_relative_import_restarts_into_itself(tmp_path):
    pkg = tmp_path / "restartwrap"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "entry.py").write_text(_WRAPPER_ENTRY, encoding="utf-8")
    (pkg / "__main__.py").write_text("from .entry import main\n\nmain()\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT), env.get("PYTHONPATH")) if p)
    env.pop("RESTARTWRAP_PHASE", None)

    first = subprocess.run([sys.executable, "-m", "restartwrap", "run"], cwd=str(tmp_path),
                           env=env, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stderr[-2000:]
    calls = _line(first.stdout, "RECORDED")
    assert calls and len(calls) == 1, first.stdout[-2000:] + first.stderr[-2000:]
    call = calls[0]

    # What the exec would have run, in the same directory and environment. It must start: a
    # script-mode rerun of restartwrap/__main__.py dies on its relative import.
    env["RESTARTWRAP_PHASE"] = "restarted"
    second = subprocess.run(call[1:], executable=call[0], cwd=str(tmp_path), env=env,
                            capture_output=True, text=True, timeout=120)
    assert second.returncode == 0, second.stderr[-2000:]
    report = _line(second.stdout, "REPORT")
    assert report == {"spec": "restartwrap.__main__", "argv": ["run"],
                      "package_dir_on_path": False}, second.stdout + second.stderr
    assert call[1:] == [sys.executable, "-m", "restartwrap", "run"]
