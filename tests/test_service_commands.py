# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf start / stop / restart / status` and the launch primitive behind them.

The updater brings VAF back through relaunch(): the stopped instance's mode,
interpreter and working directory, so a windowed desktop app returns with its
window and tray icon. A restart that always chose headless is how a desktop
VAF used to lose both after `vaf restart`, and why the web update button had
to refuse every desktop launch.
"""
import inspect
import json
import os
import sys

import pytest
import typer

import vaf.cli.cmd.service as svc
from vaf.core import instance
from vaf.core.instance import Instance


@pytest.fixture(autouse=True)
def desktop(monkeypatch, tmp_path):
    monkeypatch.setattr(svc, "_is_server_mode", lambda: False)
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(svc, "_pid_file", lambda: tmp_path / "service.pid")
    monkeypatch.setattr(svc, "_log_file", lambda: tmp_path / "vaf_run.log")
    monkeypatch.delenv(instance.HEADLESS_ENV, raising=False)
    return tmp_path


@pytest.fixture
def popen(monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append((list(argv), kwargs))
            self.pid = 4242

    monkeypatch.setattr(svc.subprocess, "Popen", FakePopen)
    return calls


# ── start_instance ───────────────────────────────────────────────────────────

def test_headless_sets_the_env_var_that_selects_run_headless(popen, desktop):
    pid = svc.start_instance(instance.MODE_HEADLESS)
    argv, kwargs = popen[0]
    assert pid == 4242
    assert argv == [sys.executable, "-m", "vaf.main", "tray", "--no-top"]
    assert kwargs["env"][instance.HEADLESS_ENV] == "1"
    # The launcher's record, as `vaf start` and the tray dashboard write it.
    assert (desktop / "service.pid").read_text() == "4242"


def test_the_child_never_becomes_a_dashboard_wrapper(popen):
    """`vaf tray` in a terminal is the dashboard lane; the detached child must
    be the real tray, so it is spawned with --no-top like every launcher."""
    svc.start_instance(instance.MODE_TRAY)
    argv, _ = popen[0]
    assert argv[-1] == "--no-top"


def test_tray_strips_the_env_var_even_when_inherited(popen, monkeypatch):
    """The updater may run under a headless parent: the env it inherited must
    not turn a windowed relaunch headless."""
    monkeypatch.setenv(instance.HEADLESS_ENV, "1")
    svc.start_instance(instance.MODE_TRAY)
    _, kwargs = popen[0]
    assert instance.HEADLESS_ENV not in kwargs["env"]


def test_the_recorded_interpreter_and_directory_are_used(popen, tmp_path):
    svc.start_instance(instance.MODE_TRAY, python="/x/pythonw.exe", cwd=str(tmp_path))
    argv, kwargs = popen[0]
    assert argv[0] == "/x/pythonw.exe"
    assert kwargs["cwd"] == str(tmp_path)


def test_a_vanished_directory_is_not_passed(popen, tmp_path):
    svc.start_instance(instance.MODE_TRAY, cwd=str(tmp_path / "gone"))
    _, kwargs = popen[0]
    assert "cwd" not in kwargs


def test_the_launch_is_detached(popen):
    svc.start_instance(instance.MODE_HEADLESS)
    _, kwargs = popen[0]
    if sys.platform == "win32":
        assert kwargs.get("creationflags")
    else:
        assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is svc.subprocess.DEVNULL


def test_only_the_two_modes_can_be_started(popen):
    with pytest.raises(ValueError):
        svc.start_instance("run")
    assert popen == []


# ── relaunch ─────────────────────────────────────────────────────────────────

def test_relaunch_keeps_the_previous_mode(popen):
    svc.relaunch(Instance(pid=1, mode="tray", python=sys.executable, cwd=""))
    _, kwargs = popen[0]
    assert instance.HEADLESS_ENV not in kwargs["env"]


def test_relaunch_without_a_previous_instance_is_headless(popen):
    svc.relaunch(None)
    argv, kwargs = popen[0]
    assert argv[0] == sys.executable
    assert kwargs["env"][instance.HEADLESS_ENV] == "1"


def test_relaunch_ignores_an_interpreter_that_no_longer_exists(popen):
    svc.relaunch(Instance(pid=1, mode="headless", python="/nope/python"))
    argv, _ = popen[0]
    assert argv[0] == sys.executable


def test_relaunch_in_server_mode_is_systemds(monkeypatch, popen):
    monkeypatch.setattr(svc, "_is_server_mode", lambda: True)
    called = []

    def systemctl(action):
        called.append(action)
        raise typer.Exit(0)

    monkeypatch.setattr(svc, "_systemctl", systemctl)
    with pytest.raises(typer.Exit):
        svc.relaunch(Instance(pid=1, mode="tray"))
    assert called == ["start"] and popen == []


# ── _running_pid: the launcher's record, then the instance's own ─────────────

def test_running_pid_reads_a_live_service_pid_file(desktop, monkeypatch):
    (desktop / "service.pid").write_text(str(os.getpid()))
    monkeypatch.setattr(instance, "read_record", lambda: None)
    assert svc._running_pid() == os.getpid()


def test_running_pid_drops_a_stale_file_and_asks_the_record(desktop, monkeypatch):
    (desktop / "service.pid").write_text("999999")
    monkeypatch.setattr(svc, "_alive", lambda pid: False)
    monkeypatch.setattr(instance, "read_record", lambda: Instance(pid=77, mode="tray"))
    assert svc._running_pid() == 77
    assert not (desktop / "service.pid").exists()


def test_running_pid_without_any_record_is_none(monkeypatch):
    monkeypatch.setattr(instance, "read_record", lambda: None)
    assert svc._running_pid() is None


def test_the_liveness_probe_never_signals():
    """os.kill(pid, 0) is not a probe on Windows: it is TerminateProcess with
    exit code 0, so a status check would have ended the service it asked
    about. The service module must not call os.kill anywhere (the stop goes
    through psutil, which terminates on purpose and says so)."""
    import ast
    tree = ast.parse(inspect.getsource(svc))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "kill" and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "os"]
    assert calls == [], f"os.kill call(s) at line(s) {[c.lineno for c in calls]}"


# ── the commands ─────────────────────────────────────────────────────────────

def test_start_refuses_while_an_instance_runs(monkeypatch, popen):
    monkeypatch.setattr(svc, "_running_pid", lambda: 7)
    with pytest.raises(typer.Exit) as ei:
        svc.cmd_start()                    # direct call: watch normalises to False
    assert ei.value.exit_code == 0
    assert popen == []


def test_start_is_headless(monkeypatch, popen):
    monkeypatch.setattr(svc, "_running_pid", lambda: None)
    svc.cmd_start()
    _, kwargs = popen[0]
    assert kwargs["env"][instance.HEADLESS_ENV] == "1"


class FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _fake_psutil(monkeypatch, procs, survivors=()):
    import psutil as real
    by_pid = {p.pid: p for p in procs}

    def process(pid):
        try:
            return by_pid[pid]
        except KeyError:
            raise real.NoSuchProcess(pid)

    def wait_procs(plist, timeout=None):
        alive = [p for p in plist if p.pid in survivors]
        return [p for p in plist if p not in alive], alive

    fake = type("psutil", (), {
        "Process": staticmethod(process),
        "wait_procs": staticmethod(wait_procs),
        "Error": real.Error,
        "NoSuchProcess": real.NoSuchProcess,
    })
    monkeypatch.setattr(svc, "_psutil", lambda: fake)
    return fake


def _nothing_found(monkeypatch):
    monkeypatch.setattr(instance, "find_running", lambda: None)
    monkeypatch.setattr(svc, "_find_vaf_processes", lambda: [])
    monkeypatch.setattr(svc, "_running_pid", lambda: None)


def test_stop_terminates_the_recorded_instance_and_drops_both_records(monkeypatch, desktop):
    record = desktop / ".vaf" / "instance.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"pid": 555, "mode": "tray"}), encoding="utf-8")
    (desktop / "service.pid").write_text("555")
    proc = FakeProc(555)
    _fake_psutil(monkeypatch, [proc], survivors=(555,))
    monkeypatch.setattr(instance, "find_running",
                        lambda: Instance(pid=555, mode="tray", recorded=True))

    stopped = svc.stop_instance()
    assert stopped is not None and stopped.pid == 555 and stopped.mode == "tray"
    assert proc.terminated and proc.killed          # did not exit within the wait: killed
    assert not record.exists()                      # a killed process removes nothing itself
    assert not (desktop / "service.pid").exists()


def test_stop_leaves_a_newer_services_pid_file_alone(monkeypatch, desktop):
    """Another terminal may have started a newer service meanwhile; its
    record must survive the stop of the old one."""
    (desktop / "service.pid").write_text("999")
    _fake_psutil(monkeypatch, [FakeProc(555)])
    monkeypatch.setattr(instance, "find_running",
                        lambda: Instance(pid=555, mode="tray", recorded=True))
    svc.stop_instance()
    assert (desktop / "service.pid").read_text() == "999"


def test_stop_without_a_record_stops_every_process_the_finder_returns(monkeypatch):
    """Started by a version that kept no record: the finder's answer (the
    port owner, or every `vaf.main tray`) is the target set, as before."""
    procs = [FakeProc(10), FakeProc(20)]
    _fake_psutil(monkeypatch, procs)
    monkeypatch.setattr(instance, "find_running",
                        lambda: Instance(pid=10, mode="tray", recorded=False))
    monkeypatch.setattr(svc, "_find_vaf_processes", lambda: procs)
    stopped = svc.stop_instance()
    assert stopped is not None and stopped.pid == 10 and stopped.mode == "tray"
    assert all(p.terminated for p in procs) and not any(p.killed for p in procs)


def test_stop_falls_back_to_the_service_pid_file(monkeypatch, desktop):
    proc = FakeProc(31)
    _fake_psutil(monkeypatch, [proc])
    _nothing_found(monkeypatch)
    monkeypatch.setattr(svc, "_running_pid", lambda: 31)
    (desktop / "service.pid").write_text("31")
    stopped = svc.stop_instance()
    assert stopped is not None and stopped.pid == 31 and stopped.mode == "headless"
    assert proc.terminated
    assert not (desktop / "service.pid").exists()


def test_stop_with_nothing_running(monkeypatch):
    _fake_psutil(monkeypatch, [])
    _nothing_found(monkeypatch)
    assert svc.stop_instance() is None


def test_stop_never_targets_the_calling_process(monkeypatch):
    _fake_psutil(monkeypatch, [FakeProc(os.getpid())])
    monkeypatch.setattr(instance, "find_running",
                        lambda: Instance(pid=os.getpid(), mode="tray", recorded=True))
    assert svc.stop_instance() is None


def test_restart_brings_back_the_same_kind(monkeypatch, popen):
    """`vaf restart` on a windowed VAF used to return a headless one: the
    window and the tray icon were gone until the next manual launch."""
    monkeypatch.setattr(svc, "stop_instance",
                        lambda: Instance(pid=9, mode="tray", python=sys.executable, cwd=""))
    svc.cmd_restart()
    _, kwargs = popen[0]
    assert instance.HEADLESS_ENV not in kwargs["env"]


def test_restart_with_nothing_running_starts_headless(monkeypatch, popen):
    monkeypatch.setattr(svc, "stop_instance", lambda: None)
    svc.cmd_restart()
    _, kwargs = popen[0]
    assert kwargs["env"][instance.HEADLESS_ENV] == "1"


def test_status_reads_the_record(monkeypatch):
    seen = []
    monkeypatch.setattr(instance, "find_running", lambda: Instance(pid=9, mode="tray"))
    monkeypatch.setattr(svc.UI, "success", lambda msg: seen.append(msg))
    monkeypatch.setattr(svc.UI, "info", lambda msg: seen.append(msg))
    svc.cmd_status()
    assert any("PID 9" in m and "tray" in m for m in seen)


def test_status_falls_back_to_the_service_pid_file(monkeypatch):
    seen = []
    monkeypatch.setattr(instance, "find_running", lambda: None)
    monkeypatch.setattr(svc, "_running_pid", lambda: 5)
    monkeypatch.setattr(svc.UI, "success", lambda msg: seen.append(msg))
    monkeypatch.setattr(svc.UI, "info", lambda msg: seen.append(msg))
    svc.cmd_status()
    assert any("PID 5" in m for m in seen)


# ── surviving the shutdown broadcast ─────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="no pkill broadcast on Windows")
def test_stop_ignores_sigterm_while_vaf_shuts_down(monkeypatch):
    """The tray's quit runs pkill -f "python.*vaf.main", which matches the
    stopping process itself. It must ignore SIGTERM for exactly the stop
    window and restore the disposition afterwards."""
    import signal as _signal
    seen = []
    real = _signal.getsignal(_signal.SIGTERM)

    def fake_signal(sig, handler):
        seen.append((sig, handler))
        return real

    monkeypatch.setattr(svc.signal, "signal", fake_signal)
    proc = FakeProc(555)
    during = {}

    def wait_procs(plist, timeout=None):
        during["handler"] = seen[-1][1]        # what is installed while we wait
        return list(plist), []

    fake = _fake_psutil(monkeypatch, [proc])
    fake.wait_procs = staticmethod(wait_procs)
    monkeypatch.setattr(instance, "find_running",
                        lambda: Instance(pid=555, mode="tray", recorded=True))
    svc.stop_instance()
    assert during["handler"] is _signal.SIG_IGN
    assert seen[-1] == (_signal.SIGTERM, real)   # restored


@pytest.mark.skipif(sys.platform == "win32", reason="no pkill broadcast on Windows")
def test_the_shield_restores_on_failure_too(monkeypatch):
    import signal as _signal
    real = _signal.getsignal(_signal.SIGTERM)
    seen = []
    monkeypatch.setattr(svc.signal, "signal", lambda sig, h: (seen.append(h), real)[1])
    with pytest.raises(RuntimeError):
        with svc._surviving_the_shutdown():
            raise RuntimeError("boom")
    assert seen == [_signal.SIG_IGN, real]
