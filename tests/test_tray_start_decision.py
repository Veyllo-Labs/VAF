# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A start from a terminal (on macOS: every start from the app icon) decides whether VAF
already runs before it starts the service, and it must decide by the singleton port.

It decided by the wide finder `vaf stop` uses, which also matches `-m vaf.main tray` by
command line - the command line of a DASHBOARD left open by an earlier start. Measured on
macOS: the service exited, its dashboard ran on for a day and a half, and every icon start
since then printed "already running (PID <dashboard>)", attached to it, opened no window
and started nothing.
"""
import sys

import pytest

from vaf.core import instance

_DASHBOARD = [sys.executable, "-m", "vaf.main", "tray"]


class _Proc:
    def __init__(self, pid, argv):
        self.pid = pid
        self.info = {"pid": pid, "cmdline": list(argv)}
        self._argv = list(argv)

    def cmdline(self):
        return list(self._argv)

    def status(self):
        return "running"

    def environ(self):
        return {}

    def cwd(self):
        return "/work"


@pytest.fixture
def mac_picture(monkeypatch, tmp_path):
    """No listener on the singleton port, no record, no pid file, and one left-over
    dashboard in the process table."""
    import psutil
    import vaf.cli.cmd.service as svc
    import vaf.cli.cmd.top as top
    import vaf.main as main_mod

    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(instance, "singleton_listening", lambda timeout=0.5: False)
    left_over = _Proc(45156, _DASHBOARD)
    monkeypatch.setattr(psutil, "net_connections", lambda kind="tcp": [])
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter([left_over]))
    monkeypatch.setattr(svc, "_pid_file", lambda: tmp_path / "vaf.pid")

    spawned, dashboards = [], []

    class _Child:
        pid = 777

    def _popen(cmd, **kwargs):
        spawned.append(list(cmd))
        return _Child()

    monkeypatch.setattr(main_mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(top, "cmd_top", lambda **kw: dashboards.append(kw))
    monkeypatch.setattr(main_mod, "_stop_spawned_tray", lambda proc: None)
    return main_mod, spawned, dashboards


def test_a_left_over_dashboard_does_not_count_as_a_running_vaf(mac_picture):
    """MUTATION: decide by instance.locate_processes() again and this attaches to the
    dashboard: nothing is spawned."""
    main_mod, spawned, dashboards = mac_picture
    main_mod._run_tray_with_dashboard()
    assert spawned and spawned[0][-3:] == ["vaf.main", "tray", "--no-top"], spawned
    assert len(dashboards) == 1


def test_a_running_service_is_attached_to_not_started_twice(mac_picture, monkeypatch):
    main_mod, spawned, dashboards = mac_picture
    monkeypatch.setattr(instance, "find_service",
                        lambda: instance.Instance(pid=4242, mode="tray", recorded=True))
    main_mod._run_tray_with_dashboard()
    assert spawned == []
    assert len(dashboards) == 1


def test_the_dashboard_does_not_show_a_dashboard_as_the_service(mac_picture):
    """`vaf top` showed the left-over dashboard as "Service PID 45156"."""
    import vaf.cli.cmd.top as top
    assert top._service_pid() is None
