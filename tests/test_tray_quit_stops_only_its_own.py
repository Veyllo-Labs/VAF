# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Quitting the tray stops what this instance started - and nothing else.

The quit used to run `pkill -f "python.*vaf.main"` and `pkill -f "node.*VAF"`. Matching by
NAME, they ended a `vaf run` chat in another terminal, `vaf a2a session` processes, any
shell whose command line mentioned vaf.main (measured: a tool shell died with exit 144),
and `vaf stop` itself; and they missed the WhatsApp bridge on every install whose path had
no upper-case "VAF". Parentage cannot be faked: the quit now stops its own children, each
with its tree and process group.
"""
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

REPO = Path(__file__).resolve().parents[1]

# A child that starts a grandchild and waits: the shape of a sub-agent (sh -> python).
_TREE = (
    "import subprocess, sys, time;"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
    "time.sleep(60)"
)


def _gone(proc, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def processes():
    started = []
    yield started
    for p in started:
        try:
            for c in psutil.Process(p.pid).children(recursive=True):
                c.kill()
        except Exception:
            pass
        try:
            p.kill()
        except Exception:
            pass


def test_our_children_go_with_their_trees_a_namesake_stays(processes):
    """MUTATION: stop by name again (anything with vaf.main in its argv) and the
    bystander dies; stop the direct child only and its grandchild survives."""
    from vaf import tray

    ours = subprocess.Popen([sys.executable, "-c", _TREE], start_new_session=True)
    processes.append(ours)
    # Not started by the tray, but carrying the name the old sweep matched on.
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                  "vaf.main", "run"])
    processes.append(bystander)
    time.sleep(0.5)
    child = psutil.Process(ours.pid)
    grandchildren = child.children(recursive=True)
    assert grandchildren, "the test tree did not start its grandchild"

    tray._stop_what_we_started([child])

    assert _gone(child)
    assert all(_gone(g) for g in grandchildren), "a grandchild outlived the quit"
    assert psutil.Process(bystander.pid).is_running(), "a process we did not start was stopped"


class _Parent:
    def __init__(self, argv):
        self._argv = argv
        self.terminated = False

    def cmdline(self):
        return list(self._argv)

    def terminate(self):
        self.terminated = True


@pytest.mark.parametrize("argv,ended", [
    ([sys.executable, "-m", "vaf.main", "tray"], True),             # the dashboard that started us
    ([sys.executable, "-m", "vaf.main", "tray", "--no-top"], False),  # a tray, not a dashboard
    (["/bin/bash", "./run_vaf.sh", "tray"], False),                   # the shell launcher
    (["/usr/lib/systemd/systemd", "--user"], False),                  # reparented / systemd
    ([sys.executable, "-m", "vaf.main", "start"], False),             # the `vaf start` launcher
])
def test_only_the_dashboard_that_started_us_is_ended(monkeypatch, argv, ended):
    from vaf import tray

    parent = _Parent(argv)
    me = type("Me", (), {"parent": lambda self: parent})()
    monkeypatch.setattr(psutil, "Process", lambda *a, **k: me)
    tray._end_the_dashboard_that_started_us()
    assert parent.terminated is ended


def test_the_quit_no_longer_sweeps_by_name():
    """A guard against the name sweep coming back: no pkill in the tray's code."""
    src = (REPO / "vaf" / "tray.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert 'subprocess.run(["pkill"' not in code and "['pkill'" not in code


def test_the_list_is_taken_before_the_docker_stop_starts():
    """The Docker stop runs on as a child after the process exits; stopping it half way
    would leave the stack running. The list of what we started is taken first."""
    src = (REPO / "vaf" / "tray.py").read_text(encoding="utf-8")
    quit_fn = src[src.index("def quit_app("):]
    assert quit_fn.index("_own_children()") < quit_fn.index("target=_stop_docker")
