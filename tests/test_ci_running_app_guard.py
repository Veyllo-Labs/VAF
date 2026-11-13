# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The web-build stage of scripts/ci_check.sh refuses while a Next server or the
VAF tray runs, and it must match those by process title and exact argv elements:
the old `pgrep -f` matched every shell that merely carried the words in its
command line and failed a whole CI run with no server alive.

The real-process tests start short-lived Python children that only sleep; no
Node, no Next and no tray is ever started. The title literal is written as
"next-" + "server" wherever a child is spawned, so no command line on this
machine carries the word because of this file.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "running_app.py"
_GATE = _REPO / "scripts" / "ci_check.sh"

_TITLE = "next-" + "server"
_FULL_TITLE = _TITLE + " (v16.4.2)"
_SLEEP = "import time; time.sleep(20)"

posix_only = pytest.mark.skipif(os.name == "nt", reason="ci_check.sh is bash and never runs on Windows")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl(PR_SET_NAME) is Linux")


def _module():
    spec = importlib.util.spec_from_file_location("running_app", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spawn(argv, **kwargs):
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    # The child must be past its exec and, for the comm case, past its prctl
    # before the table is read.
    time.sleep(0.6)
    return proc


def _stop(proc):
    proc.kill()
    proc.wait(timeout=10)


def _web_stage() -> str:
    gate = _GATE.read_text(encoding="utf-8")
    return gate.split("Web build ===", 1)[1].split("All checks passed", 1)[0]


# ── criterion 1: the stage calls the helper, never pgrep ─────────────────────

def test_stage_8_calls_the_helper_and_has_no_pgrep():
    stage = _web_stage()
    assert "pgrep" not in stage, "the web build stage matches command lines again"
    call = stage.find("python scripts/running_app.py")
    assert call != -1, "the web build stage no longer asks whether the app runs"
    assert call < stage.find("npm run build"), "the running-app check must come before the build"


def _guard_block() -> str:
    lines = _web_stage().splitlines()
    start = next(i for i, line in enumerate(lines) if "RUNNING_APP=0" in line)
    end = next(i for i in range(start, len(lines)) if lines[i] == "    fi")
    return "\n".join(lines[start:end + 1])


@posix_only
@pytest.mark.parametrize("helper_exit, gate_exit, words", [
    (0, 0, None),
    (1, 1, "VAF is running"),
    (2, 1, "cannot tell whether VAF is running"),
    (137, 1, None),  # the helper killed by a signal
])
def test_the_stage_refuses_on_every_non_zero_answer(helper_exit, gate_exit, words):
    """The helper's exit 2 (table unreadable) must refuse as firmly as exit 1, and
    so must any other non-zero code: a crashed helper is not a green light."""
    script = f"set -e\npython() {{ return {helper_exit}; }}\n{_guard_block()}\nexit 0\n"
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert run.returncode == gate_exit, run.stderr
    if words:
        assert words in run.stderr


# ── criteria 2 and 3: the title, not the words ───────────────────────────────

@pytest.mark.parametrize("name, argv, expected", [
    (_FULL_TITLE, ["node"], True),                          # psutil's name, full title
    (_TITLE + " (v1", [], True),                            # the kernel's 15-byte comm
    (_TITLE + " ", None, True),                             # truncated to a short argv
    ("python", [_FULL_TITLE, "", ""], True),                # argv[0] carries the title
    ("python", [_TITLE], True),
    ("bash", ["bash", "-c", f"until pgrep -f {_TITLE}; do sleep 1; done"], False),
    ("python", ["python", "-c", f"print('{_TITLE} vaf.main tray')"], False),
    ("grep", ["grep", "-rn", _TITLE, "scripts/"], False),
    (_TITLE + ".sh", [_TITLE + ".sh"], False),
    (_TITLE + "X", [_TITLE + "X"], False),
    ("node", ["/opt/node/bin/node", "web/node_modules/next/dist/bin/next", "start"], False),
    (None, None, False),
    ("", [], False),
])
def test_title_shapes_are_a_next_server(name, argv, expected):
    assert _module().is_next_server(name, argv) is expected


@posix_only
def test_a_retitled_argv0_is_reported():
    mod = _module()
    fake = _spawn([_FULL_TITLE, "-c", _SLEEP], executable=sys.executable)
    try:
        hits = {m.pid: m.kind for m in mod.find_running_app()}
    finally:
        _stop(fake)
    assert hits.get(fake.pid) == "next-server"


@linux_only
def test_a_retitled_comm_is_reported():
    mod = _module()
    code = ("import ctypes, time; "
            "ctypes.CDLL(None).prctl(15, b'next-' + b'server (v1', 0, 0, 0); "
            "time.sleep(20)")
    fake = _spawn([sys.executable, "-c", code])
    try:
        hits = {m.pid: m.kind for m in mod.find_running_app()}
    finally:
        _stop(fake)
    assert hits.get(fake.pid) == "next-server"


@posix_only
def test_a_shell_quoting_the_words_is_not_reported():
    mod = _module()
    fake = _spawn([sys.executable, "-c", _SLEEP + "  # waiting for " + _TITLE + " vaf.main tray"])
    try:
        pids = {m.pid for m in mod.find_running_app()}
    finally:
        _stop(fake)
    assert fake.pid not in pids


# ── criterion 4: the tray is the framework's matcher ─────────────────────────

@pytest.mark.parametrize("argv, expected", [
    (["/x/venv/bin/python", "-m", "vaf.main", "tray", "--no-top"], True),
    (["/x/venv/bin/python", "/x/venv/bin/vaf", "tray"], True),
    (["bash", "-c", "echo vaf.main tray"], False),
    (["/x/venv/bin/python", "-m", "vaf.main", "top"], False),
])
def test_tray_command_lines(argv, expected):
    hits = _module().find_running_app([(4242, "python", argv)])
    assert ([m.kind for m in hits] == ["tray"]) is expected


def test_a_process_matching_both_is_reported_once_as_next():
    hits = _module().find_running_app([(4242, _FULL_TITLE, ["x", "vaf.main", "tray"])])
    assert [(m.pid, m.kind) for m in hits] == [(4242, "next-server")]


def test_this_process_is_never_reported():
    hits = _module().find_running_app([(os.getpid(), _FULL_TITLE, [_FULL_TITLE])])
    assert hits == []


@posix_only
def test_tray_entry_is_reported():
    mod = _module()
    fake = _spawn([sys.executable, "-c", _SLEEP, "vaf.main", "tray"])
    try:
        hits = {m.pid: m.kind for m in mod.find_running_app()}
    finally:
        _stop(fake)
    assert hits.get(fake.pid) == "tray"


@posix_only
def test_tray_matcher_is_the_frameworks(monkeypatch):
    from vaf.core import instance

    mod = _module()
    fake = _spawn([sys.executable, "-c", _SLEEP, "vaf.main", "tray"])
    try:
        monkeypatch.setattr(instance, "is_tray_entry", lambda argv: False)
        pids = {m.pid for m in mod.find_running_app()}
    finally:
        _stop(fake)
    assert fake.pid not in pids, "the helper carries a tray matcher of its own"


# ── criterion 5: the refusal names the process ───────────────────────────────

@posix_only
def test_refusal_names_pid_and_command():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO)
    fake = _spawn([_FULL_TITLE, "-c", _SLEEP], executable=sys.executable)
    try:
        run = subprocess.run([sys.executable, str(_SCRIPT)], capture_output=True, text=True,
                             env=env, timeout=60)
    finally:
        _stop(fake)
    # Exit 1, never 0: the fake is up. Whether anything else matched depends on
    # what runs on this machine, so only the fake's own line is asserted.
    assert run.returncode == 1, run.stderr
    assert f"PID {fake.pid} (next-server): {_FULL_TITLE}" in run.stderr


def test_padding_is_dropped_and_the_command_is_capped():
    mod = _module()
    match = mod.Match(7, "next-server", (_FULL_TITLE, "", "", "a" * 400))
    text = mod._command(match)
    assert text.startswith(_FULL_TITLE + " a")
    assert len(text) == 300


def test_an_unreadable_argv_still_names_the_process():
    mod = _module()
    hits = mod.find_running_app([(7, _FULL_TITLE, None)])
    assert mod._command(hits[0]) == _FULL_TITLE


# ── criterion 6: strict when the table cannot be read ────────────────────────

def test_unreadable_table_refuses(monkeypatch, capsys):
    import psutil

    def broken(*_a, **_k):
        raise RuntimeError("no /proc")

    monkeypatch.setattr(psutil, "process_iter", broken)
    assert _module().main() == 2
    assert "cannot read the process table" in capsys.readouterr().err


def test_missing_psutil_refuses(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert _module().main() == 2
    assert "psutil is unavailable" in capsys.readouterr().err


class _Proc:
    def __init__(self, info=None, error=None):
        self._info, self._error = info, error

    @property
    def info(self):
        if self._error is not None:
            raise self._error
        return self._info


@pytest.mark.parametrize("error_name", ["NoSuchProcess", "AccessDenied", "ZombieProcess"])
def test_vanished_process_is_skipped(monkeypatch, error_name):
    import psutil

    error = getattr(psutil, error_name)(99)
    table = [
        _Proc(error=error),
        _Proc(info={"pid": 4242, "name": "python", "cmdline": [_FULL_TITLE]}),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_k: iter(table))
    hits = _module().find_running_app()
    assert [(m.pid, m.kind) for m in hits] == [(4242, "next-server")]


def test_the_scripts_own_tree_is_imported_first():
    """A worktree's gate must use that worktree's matcher, not the editable
    install's, so the script puts its own tree first on sys.path."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    code = f"import runpy, sys; runpy.run_path({str(_SCRIPT)!r}); print(sys.path[0])"
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, cwd=str(_SCRIPT.parent), timeout=60)
    assert run.returncode == 0, run.stderr
    assert Path(run.stdout.strip()) == _REPO
