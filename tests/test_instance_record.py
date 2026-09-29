# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The instance record: the running VAF says which process it is and how it
was started; `vaf stop`, `vaf status`, `vaf restart`, `vaf top` and the
self-updater read it, and fall back to the process table (the singleton-port
owner, else `vaf.main tray` by exact argv elements) for a VAF that kept none.

The record complements the service pid file a launcher writes: that file only
exists for `vaf start` and the tray dashboard, so the app shortcut, run_vaf.sh
and a bare tray were invisible to the updater and could only ever have been
brought back headless.
"""
import json
import os
import sys

import pytest

from vaf.core import instance


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))
    return tmp_path


class FakeProc:
    def __init__(self, pid, argv, env=None, status="running", cwd="/work", env_error=None,
                 listens=False):
        self.pid = pid
        self._listens = listens
        self.info = {"pid": pid, "cmdline": list(argv)}
        self._argv = list(argv)
        self._env = env or {}
        self._status = status
        self._cwd = cwd
        self._env_error = env_error

    def cmdline(self):
        return list(self._argv)

    def status(self):
        return self._status

    def environ(self):
        if self._env_error:
            raise self._env_error
        return dict(self._env)

    def cwd(self):
        return self._cwd

    def net_connections(self, kind="tcp"):
        """The process's OWN sockets - readable without root even on macOS."""
        if not self._listens:
            return []
        import psutil
        return [type("C", (), {"status": psutil.CONN_LISTEN,
                               "laddr": type("A", (), {"port": instance.TRAY_SINGLETON_PORT})()})()]


def _fake_table(monkeypatch, procs, port_owner=None):
    """psutil as the finder sees it: the singleton-port owner (when given),
    then the process table."""
    import psutil as real
    by_pid = {p.pid: p for p in procs}

    def process(pid):
        try:
            return by_pid[pid]
        except KeyError:
            raise real.NoSuchProcess(pid)

    conns = []
    if port_owner is not None:
        conns = [type("C", (), {"status": real.CONN_LISTEN, "pid": port_owner,
                                "laddr": type("A", (), {"port": instance.TRAY_SINGLETON_PORT})()})()]
    monkeypatch.setattr(real, "net_connections", lambda kind="tcp": conns)
    monkeypatch.setattr(real, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.setattr(real, "Process", process)
    return real


_TRAY = [sys.executable, "-m", "vaf.main", "tray", "--no-top"]


# ── the instance's own side ──────────────────────────────────────────────────

def test_register_records_this_process(home):
    path = instance.register(instance.MODE_TRAY)
    assert path == home / ".vaf" / "instance.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data["mode"] == "tray"
    assert data["python"] == sys.executable   # the venv interpreter, not argv[0]
    assert data["cwd"] == os.getcwd()
    assert data["started_at"]


def test_a_wrong_mode_is_a_programming_error(home):
    with pytest.raises(ValueError):
        instance.register("run")


def test_register_survives_an_unwritable_home(monkeypatch, tmp_path):
    """A read-only home must not stop VAF from starting."""
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    monkeypatch.setattr(instance, "record_path", lambda: blocker / "instance.json")
    assert instance.register(instance.MODE_HEADLESS) is None


def test_unregister_removes_only_its_own_record(home):
    path = home / ".vaf" / "instance.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 999_999, "mode": "tray"}), encoding="utf-8")
    instance.unregister()             # a refused second instance must not erase the first's
    assert path.exists()
    instance.register(instance.MODE_TRAY)
    instance.unregister()
    assert not path.exists()


def test_forget_removes_the_record_for_a_killed_pid(home):
    path = home / ".vaf" / "instance.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 4321, "mode": "headless"}), encoding="utf-8")
    instance.forget(1234)
    assert path.exists()
    instance.forget(4321)
    assert not path.exists()


# ── the tooling side ─────────────────────────────────────────────────────────

def test_read_record_trusts_only_a_live_tray_pid(home, monkeypatch):
    instance.register(instance.MODE_TRAY)
    monkeypatch.setattr(instance, "_is_live_vaf_pid", lambda pid: True)
    inst = instance.read_record()
    assert inst is not None and inst.pid == os.getpid() and inst.mode == "tray" and inst.recorded
    assert inst.python == sys.executable

    # The process is gone, or the pid now belongs to something else: stale, removed.
    monkeypatch.setattr(instance, "_is_live_vaf_pid", lambda pid: False)
    assert instance.read_record() is None
    assert not instance.record_path().exists()


@pytest.mark.parametrize("raw", ["garbage", "[]", json.dumps({"pid": "x", "mode": "tray"}),
                                 json.dumps({"pid": 5, "mode": "run"})])
def test_an_unusable_record_is_removed(home, monkeypatch, raw):
    path = home / ".vaf" / "instance.json"
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(instance, "_is_live_vaf_pid", lambda pid: True)
    assert instance.read_record() is None
    assert not path.exists()


def test_live_check_wants_a_tray_entry_point(monkeypatch):
    """A reused pid running something else is not VAF, a zombie is not alive,
    and an interactive `vaf run` session is never the service."""
    procs = [FakeProc(1, _TRAY),
             FakeProc(2, ["/usr/bin/sleep", "60"]),
             FakeProc(3, _TRAY, status="zombie"),
             FakeProc(4, [sys.executable, "-m", "vaf.main", "run", "--web"])]
    _fake_table(monkeypatch, procs)
    assert instance._is_live_vaf_pid(1)
    assert not instance._is_live_vaf_pid(2)
    assert not instance._is_live_vaf_pid(3)
    assert not instance._is_live_vaf_pid(4)
    assert not instance._is_live_vaf_pid(5)


@pytest.mark.parametrize("argv, expected", [
    (["/venv/bin/python", "-m", "vaf.main", "tray"], True),
    (["/venv/bin/python", "-m", "vaf.main", "tray", "--no-top"], True),
    (["/venv/bin/python", "/venv/bin/vaf", "tray"], True),                    # console script
    (["C:\\VAF\\venv\\Scripts\\pythonw.exe", "-m", "vaf.main", "tray"], True),
    (["C:\\VAF\\venv\\Scripts\\vaf.exe", "tray"], True),
    (["/venv/bin/python", "-m", "vaf.main", "run", "--web"], False),          # somebody's session
    (["/venv/bin/python", "-m", "vaf.main", "update", "--yes"], False),       # the updater itself
    (["/venv/bin/python", "-m", "vaf.main", "top"], False),                   # a viewer
    (["/bin/bash", "-c", "echo vaf.main tray; timeout 40 launch.sh tray"], False),  # quoted words
    (["/venv/bin/python", "-m", "vaf.main"], False),
    ([], False),
    (None, False),
])
def test_is_tray_entry(argv, expected):
    assert instance.is_tray_entry(argv) is expected


def test_scan_reads_the_mode_from_the_process_environment(monkeypatch):
    procs = [
        FakeProc(30, _TRAY, env={"VAF_NATIVE_WRAPPER": "1"}),
        FakeProc(10, _TRAY, env={}),
        FakeProc(40, [sys.executable, "-m", "vaf.main", "run"], env={}),
        FakeProc(50, _TRAY, env_error=PermissionError()),
        FakeProc(60, ["/usr/bin/sleep", "60"]),
        FakeProc(70, [sys.executable, "-m", "vaf.main", "update", "--yes"]),
    ]
    _fake_table(monkeypatch, procs)
    found = instance.scan_processes()
    assert [(i.pid, i.mode) for i in found] == [
        (30, "headless"),      # the env var that selects run_headless()
        (10, "tray"),          # no env var: the desktop app
        (50, "headless"),      # unreadable environment: the safe default
    ]
    assert all(not i.recorded for i in found)
    # A scanned instance names no interpreter: argv[0] of a macOS venv python is
    # the framework binary, which cannot see the venv. The relaunch uses its own.
    assert all(i.python == "" for i in found)
    assert found[0].cwd == "/work"


def test_scan_prefers_the_singleton_port_owner(monkeypatch):
    """argv cannot tell the service from a dashboard watching it; the port
    can, and the owner's environment still says which mode it runs in."""
    owner = FakeProc(4242, _TRAY, env={})
    viewer = FakeProc(99, [sys.executable, "-m", "vaf.main", "tray"], env={})
    _fake_table(monkeypatch, [viewer, owner], port_owner=4242)
    found = instance.scan_processes()
    assert [(i.pid, i.mode) for i in found] == [(4242, "tray")]


def test_scan_without_psutil_is_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert instance.scan_processes() == []
    assert instance.locate_processes() == []


def test_find_running_prefers_the_record_then_the_table(home, monkeypatch):
    monkeypatch.setattr(instance, "scan_processes",
                        lambda: [instance.Instance(pid=5, mode="headless", recorded=False)])
    found = instance.find_running()
    assert found is not None and found.pid == 5 and not found.recorded

    instance.register(instance.MODE_TRAY)
    monkeypatch.setattr(instance, "_is_live_vaf_pid", lambda pid: True)
    found = instance.find_running()
    assert found is not None and found.pid == os.getpid() and found.recorded

    monkeypatch.setattr(instance, "scan_processes", lambda: [])
    instance.unregister()
    assert instance.find_running() is None


def test_the_cli_finder_reads_through_the_framework(monkeypatch):
    """One implementation of the identity rule: the CLI's finder is a name for
    the framework's, not a second copy."""
    import vaf.cli.cmd.service as svc
    sentinel = [object()]
    monkeypatch.setattr(instance, "locate_processes", lambda: sentinel)
    assert svc._find_vaf_processes() is sentinel
    assert svc.TRAY_SINGLETON_PORT == instance.TRAY_SINGLETON_PORT


# ── "is VAF up?": the strict answer ──────────────────────────────────────────

_DASHBOARD = [sys.executable, "-m", "vaf.main", "tray"]


def test_find_service_needs_the_singleton_port(home, monkeypatch):
    """The macOS lockout: the service had exited, a dashboard from its start kept
    running with the same command line, and the wide finder named it. Nothing
    accepts on the port, so nothing runs - whatever the process table holds, and
    even with a record whose pid a dashboard reused."""
    _fake_table(monkeypatch, [FakeProc(45, _DASHBOARD)])
    monkeypatch.setattr(instance, "singleton_listening", lambda timeout=0.5: False)
    assert instance.locate_processes(), "the wide finder does name the dashboard"
    assert instance.find_service() is None

    instance.register(instance.MODE_TRAY)
    monkeypatch.setattr(instance, "_is_live_vaf_pid", lambda pid: True)
    assert instance.find_service() is None


def test_find_service_names_the_recorded_instance_while_it_holds_the_port(home, monkeypatch):
    monkeypatch.setattr(instance, "singleton_listening", lambda timeout=0.5: True)
    instance.register(instance.MODE_TRAY)
    monkeypatch.setattr(instance, "_is_live_vaf_pid", lambda pid: True)
    _fake_table(monkeypatch, [FakeProc(os.getpid(), _TRAY, listens=True)])
    found = instance.find_service()
    assert found is not None and found.pid == os.getpid() and found.recorded


def test_a_record_alone_is_no_evidence(home, monkeypatch):
    """MUTATION: return the record without asking whether its process listens. A pid
    the dead service left in its record can be reused by a dashboard (same command
    line), and with another program on the port that dashboard would be the service."""
    monkeypatch.setattr(instance, "singleton_listening", lambda timeout=0.5: True)
    instance.register(instance.MODE_TRAY)
    monkeypatch.setattr(instance, "_is_live_vaf_pid", lambda pid: True)
    _fake_table(monkeypatch, [FakeProc(os.getpid(), _DASHBOARD, listens=False)])
    assert instance.find_service() is None
    assert instance.port_held_by_another() is True


def test_find_service_without_a_record_names_only_a_vaf_port_owner(home, monkeypatch):
    """Without a record the port's owner names the service - when it IS one."""
    monkeypatch.setattr(instance, "singleton_listening", lambda timeout=0.5: True)
    _fake_table(monkeypatch, [FakeProc(45, _DASHBOARD), FakeProc(46, _TRAY, listens=True)],
                port_owner=46)
    found = instance.find_service()
    assert found is not None and found.pid == 46 and not found.recorded


def test_another_program_on_the_port_is_not_vaf(home, monkeypatch):
    """MUTATION: fall back to the argv scan (or skip the owner's tray check) and the
    left-over dashboard is named as the service again - the lockout, one step removed."""
    monkeypatch.setattr(instance, "singleton_listening", lambda timeout=0.5: True)
    stranger = FakeProc(77, ["/usr/bin/some-daemon", "--port", "8002"])
    _fake_table(monkeypatch, [FakeProc(45, _DASHBOARD), stranger], port_owner=77)
    assert instance.find_service() is None


def test_an_owner_that_cannot_be_seen_is_found_by_its_own_sockets(home, monkeypatch):
    """macOS without root: the machine-wide owner lookup is empty, but each process's
    OWN sockets are readable. The one that listens is the service; the dashboard with
    the same command line does not listen."""
    monkeypatch.setattr(instance, "singleton_listening", lambda timeout=0.5: True)
    _fake_table(monkeypatch, [FakeProc(45, _DASHBOARD), FakeProc(46, _TRAY, listens=True)])
    found = instance.find_service()
    assert found is not None and found.pid == 46


def test_nobody_listening_among_the_candidates_is_not_guessed(home, monkeypatch):
    monkeypatch.setattr(instance, "singleton_listening", lambda timeout=0.5: True)
    _fake_table(monkeypatch, [FakeProc(45, _DASHBOARD), FakeProc(46, _TRAY)])
    assert instance.find_service() is None
    assert instance.port_held_by_another() is True


def test_singleton_listening_connects_and_never_binds(monkeypatch):
    """A real listener on a free port stands in for the tray's: the probe finds
    it, and finds nothing once it is gone."""
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    monkeypatch.setattr(instance, "TRAY_SINGLETON_PORT", port)
    try:
        assert instance.singleton_listening() is True
    finally:
        srv.close()
    assert instance.singleton_listening() is False


def test_a_process_can_read_its_own_listener_on_every_platform(monkeypatch):
    """The evidence find_service() relies on, measured on the real OS: a process's OWN
    sockets include its listener. Runs on the macOS CI runner too, where the machine-wide
    owner lookup needs root and this per-process read must work without it."""
    import socket
    import psutil
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    monkeypatch.setattr(instance, "TRAY_SINGLETON_PORT", srv.getsockname()[1])
    try:
        assert instance._listens_on_singleton(psutil.Process()) is True
    finally:
        srv.close()
    assert instance._listens_on_singleton(psutil.Process()) is False
