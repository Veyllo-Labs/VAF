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
    monkeypatch.setattr(main_mod, "_await_spawned_tray",
                        lambda proc, log: instance.Instance(pid=proc.pid, mode="tray",
                                                            recorded=False))
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


# -- "started" means the child holds the port ------------------------------------------------


class _Child:
    def __init__(self, pid=777, exits_with=None):
        self.pid = pid
        self._code = exits_with

    def poll(self):
        return self._code


@pytest.fixture
def quick(monkeypatch):
    import vaf.main as main_mod
    import vaf.cli.ui as ui
    said = []
    for level in ("info", "success", "warning", "error"):
        monkeypatch.setattr(ui.UI, level, lambda msg, _l=level: said.append((_l, msg)))
    monkeypatch.setattr(main_mod, "SPAWN_ANSWER_TIMEOUT_S", 2.0)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return main_mod, said


def test_a_child_that_exits_is_reported_not_announced(quick, monkeypatch, tmp_path):
    """MUTATION: announce "VAF tray started" right after spawning again and a child that
    failed its singleton check is shown as a running VAF."""
    main_mod, said = quick
    monkeypatch.setattr(instance, "find_service", lambda: None)
    monkeypatch.setattr(instance, "port_held_by_another", lambda: False)
    assert main_mod._await_spawned_tray(_Child(exits_with=1), tmp_path / "vaf_run.log") is None
    assert any(l == "error" and "exited while starting" in m for l, m in said), said


def test_a_port_held_by_another_program_is_named(quick, monkeypatch, tmp_path):
    main_mod, said = quick
    monkeypatch.setattr(instance, "find_service", lambda: None)
    monkeypatch.setattr(instance, "port_held_by_another", lambda: True)
    assert main_mod._await_spawned_tray(_Child(exits_with=0), tmp_path / "vaf_run.log") is None
    assert any(l == "error" and "another program" in m for l, m in said), said


def test_the_child_answering_is_the_success(quick, monkeypatch, tmp_path):
    main_mod, said = quick
    monkeypatch.setattr(instance, "find_service",
                        lambda: instance.Instance(pid=777, mode="tray", recorded=True))
    found = main_mod._await_spawned_tray(_Child(), tmp_path / "vaf_run.log")
    assert found is not None and found.pid == 777
    assert not any(l == "error" for l, _ in said)


def test_the_start_refuses_up_front_when_another_program_holds_the_port(mac_picture, monkeypatch):
    main_mod, spawned, dashboards = mac_picture
    monkeypatch.setattr(instance, "port_held_by_another", lambda: True)
    main_mod._run_tray_with_dashboard()
    assert spawned == [] and dashboards == []


# -- the pid file names the service that runs -----------------------------------------------


@pytest.mark.parametrize("started_pid,file_before,file_after", [
    (None, "777", None),          # our child failed: its pid goes
    (4242, "777", "4242"),        # another start won: the winner's pid
    (777, "777", "777"),          # ours runs: unchanged
    (None, "999", "999"),         # someone else wrote it since: not ours to touch
])
def test_the_pid_file_is_reconciled_after_the_start(tmp_path, monkeypatch, started_pid,
                                                    file_before, file_after):
    """MUTATION: drop _reconcile_pid_file and a failed start leaves its dead child in the
    file, a lost race overwrites the winner's pid with it."""
    import vaf.cli.cmd.service as svc
    import vaf.main as main_mod
    pf = tmp_path / "vaf.pid"
    pf.write_text(file_before)
    monkeypatch.setattr(svc, "_pid_file", lambda: pf)
    started = None if started_pid is None else instance.Instance(pid=started_pid, mode="tray")
    main_mod._reconcile_pid_file(_Child(pid=777), started)
    assert (pf.read_text() if pf.exists() else None) == file_after


def test_the_start_flow_reconciles_the_pid_file(mac_picture, monkeypatch, tmp_path):
    """The flow, not only the helper: a failed start leaves no pid of its dead child."""
    main_mod, spawned, dashboards = mac_picture
    monkeypatch.setattr(main_mod, "_await_spawned_tray", lambda proc, log: None)
    main_mod._run_tray_with_dashboard()
    assert spawned, "the start did spawn"
    assert not (tmp_path / "vaf.pid").exists()
    assert dashboards == []
