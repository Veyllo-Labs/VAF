# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Stop reaches a command the agent runs in the foreground, and what that command started.

The measured gap: `host_bash` and `python_exec` waited in `subprocess.run`. Stop freed the
agent (the bounded run abandons its worker thread) while the command ran on until its own
timeout, and at that timeout only the shell was killed - its children ran on. They now go
through `processes.run_foreground`: their own process group, a sliced wait, the whole tree
ended on Stop. Each test names the mutation it catches.
"""
import contextlib
import sys
import threading
import time

import pytest

PY = f'"{sys.executable}"'


def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_stop_ends_a_foreground_host_command_and_what_it_started(tmp_path):
    """subprocess.run could not be stopped, and its timeout killed only the shell.
    MUTATION: go back to subprocess.run in host_bash."""
    import psutil

    from vaf.core.bounded_run import run_bounded
    from vaf.tools.host_bash import HostBashTool
    pidfile = tmp_path / "child.pid"
    code = ("import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open(r'{pidfile}', 'w').write(str(p.pid)); time.sleep(60)")
    stop = threading.Event()
    box = {}

    def _call():
        return HostBashTool().run(command=f'{PY} -c "{code}"', timeout=60)

    t = threading.Thread(target=lambda: box.update(out=run_bounded(
        _call, timeout=90, stop_check=stop.is_set, poll=0.1, label="host_bash")))
    t.start()
    assert _wait(lambda: pidfile.exists() and pidfile.read_text().strip(), 15)
    child = int(pidfile.read_text())
    stop.set()
    t.join(timeout=5)

    def _gone(pid):
        try:
            return not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True

    assert _wait(lambda: _gone(child), 3), "the command's child outlived Stop"


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_stop_reaches_what_the_command_detached(tmp_path):
    """A grandchild whose parent already exited is nobody's child any more; only the group
    the command leads still reaches it. MUTATION: drop the group from run_foreground."""
    import psutil

    from vaf.core.bounded_run import run_bounded
    from vaf.tools.host_bash import HostBashTool
    pidfile = tmp_path / "orphan.pid"
    middle = tmp_path / "middle.py"
    middle.write_text(
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open(r'{pidfile}', 'w').write(str(p.pid))\n")
    command = (f'{PY} -c "import subprocess, sys, time; '
               f'subprocess.run([sys.executable, r\'{middle}\']); time.sleep(60)"')
    stop = threading.Event()
    t = threading.Thread(target=lambda: run_bounded(
        lambda: HostBashTool().run(command=command, timeout=60),
        timeout=90, stop_check=stop.is_set, poll=0.1, label="host_bash"), daemon=True)
    t.start()
    assert _wait(lambda: pidfile.exists() and pidfile.read_text().strip(), 15)
    orphan = int(pidfile.read_text())
    try:
        stop.set()
        t.join(timeout=5)

        def _gone():
            try:
                return psutil.Process(orphan).status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return True

        assert _wait(_gone, 3), "a detached grandchild outlived Stop"
    finally:
        with contextlib.suppress(Exception):
            psutil.Process(orphan).kill()


def test_a_foreground_command_returns_its_output_as_before():
    from vaf.tools.host_bash import HostBashTool
    out = HostBashTool().run(command=f'{PY} -c "import sys; print(\'out\'); print(\'err\', file=sys.stderr)"')
    assert "out" in out and "[stderr]\nerr" in out and out.rstrip().endswith("OK")


def test_python_exec_has_a_ceiling_on_its_timeout():
    """The model's number was the dispatcher's wait too. MUTATION: drop the min()."""
    from vaf.tools.python_exec import PythonExecTool
    assert PythonExecTool()._code_timeout({"timeout": 99999}) == 600
    assert PythonExecTool().budget_seconds({"timeout": 99999}) == 615
