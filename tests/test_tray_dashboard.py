# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf tray` in a terminal: dashboard in front, tray as detached child.

The contract has three lanes and each is pinned: an interactive terminal spawns
the child (with --no-top) and takes over with the dashboard, and leaving the
dashboard stops the tray we spawned; an ALREADY running VAF is only attached to
and never stopped; every non-interactive lane (no TTY, --no-top, systemd via
VAF_LOG_TO_JOURNAL) keeps the classic direct run."""
import sys
import types

import typer
from typer.testing import CliRunner

import vaf.main as main_mod

runner = CliRunner()


def _cli():
    app = typer.Typer()
    app.command(name="tray")(main_mod.tray_command)
    return app


def _wire(monkeypatch, tmp_path, running=None, tty=True):
    import vaf.cli.cmd.service as service_mod
    import vaf.cli.cmd.top as top_mod

    calls = {"popen": [], "top": 0, "killpg": [], "run_app": 0, "run_headless": 0}

    class FakeProc:
        pid = 4321
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(main_mod.os, "isatty", lambda fd: tty)
    monkeypatch.delenv("VAF_NATIVE_WRAPPER", raising=False)
    monkeypatch.delenv("VAF_LOG_TO_JOURNAL", raising=False)
    monkeypatch.setattr(service_mod, "_running_pid", lambda: running)
    monkeypatch.setattr(service_mod, "_find_vaf_processes", lambda: [])
    monkeypatch.setattr(service_mod, "_pid_file", lambda: tmp_path / "server.pid")
    monkeypatch.setattr(top_mod, "cmd_top",
                        lambda **kw: calls.__setitem__("top", calls["top"] + 1))
    monkeypatch.setattr(main_mod.subprocess, "Popen",
                        lambda argv, **kw: calls["popen"].append(argv) or FakeProc())
    monkeypatch.setattr(main_mod.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(main_mod.os, "killpg",
                        lambda pgid, sig: calls["killpg"].append((pgid, sig)))
    monkeypatch.setitem(sys.modules, "vaf.tray", types.SimpleNamespace(
        run_app=lambda: calls.__setitem__("run_app", calls["run_app"] + 1),
        run_headless=lambda: calls.__setitem__("run_headless", calls["run_headless"] + 1),
    ))
    # The dashboard lane opens the service log via Path.home() - keep it in tmp
    monkeypatch.setenv("HOME", str(tmp_path))
    return calls


def test_interactive_tray_spawns_child_and_takes_the_dashboard(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, running=None, tty=True)

    result = runner.invoke(_cli(), [])
    assert result.exit_code == 0, result.output
    assert len(calls["popen"]) == 1
    assert "--no-top" in calls["popen"][0], "the child must never recurse into the dashboard"
    assert calls["top"] == 1
    assert calls["killpg"], "leaving the dashboard must stop the tray we spawned"
    assert calls["run_app"] == 0


def test_attaching_to_a_running_vaf_never_stops_it(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, running=777, tty=True)

    result = runner.invoke(_cli(), [])
    assert result.exit_code == 0, result.output
    assert calls["popen"] == []
    assert calls["top"] == 1
    assert calls["killpg"] == [], "we did not start that VAF - leaving must not stop it"


def test_no_tty_and_no_top_keep_the_classic_run(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, running=None, tty=False)
    result = runner.invoke(_cli(), [])
    assert result.exit_code == 0, result.output
    assert calls["popen"] == [] and calls["top"] == 0
    assert calls["run_app"] == 1

    calls = _wire(monkeypatch, tmp_path, running=None, tty=True)
    result = runner.invoke(_cli(), ["--no-top"])
    assert result.exit_code == 0, result.output
    assert calls["popen"] == [] and calls["top"] == 0
    assert calls["run_app"] == 1


def test_the_crash_supervisor_watches_the_real_tray_not_the_dashboard():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "scripts" / "tray_supervisor.sh") \
        .read_text(encoding="utf-8")
    assert "vaf.main tray --no-top" in src, \
        "the supervisor must run the real tray; a dashboard wrapper here hides GPU aborts"


def test_start_defaults_to_the_dashboard_only_on_a_tty(monkeypatch, tmp_path):
    import vaf.cli.cmd.service as service_mod
    opened = []
    monkeypatch.setattr(service_mod, "_is_server_mode", lambda: False)
    monkeypatch.setattr(service_mod, "_running_pid", lambda: None)
    monkeypatch.setattr(service_mod.subprocess, "Popen",
                        lambda *a, **k: type("P", (), {"pid": 999})())
    monkeypatch.setattr(service_mod, "_pid_file", lambda: tmp_path / "server.pid")
    monkeypatch.setattr(service_mod, "_log_file", lambda: tmp_path / "vaf_run.log")
    monkeypatch.setattr(service_mod, "_open_dashboard", lambda: opened.append(True))

    app = typer.Typer()
    app.command(name="start")(service_mod.cmd_start)

    monkeypatch.setattr(service_mod.os, "isatty", lambda fd: True)
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert opened == [True], "a person at a terminal gets the dashboard by default"

    opened.clear()
    result = runner.invoke(app, ["--no-watch"])
    assert result.exit_code == 0, result.output
    assert opened == []

    opened.clear()
    monkeypatch.setattr(service_mod.os, "isatty", lambda fd: False)
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert opened == [], "scripts and pipes stay headless"


def test_a_crashing_dashboard_leaves_vaf_running(monkeypatch, tmp_path):
    """Live incident: the wrapper's finally stopped the tray on ANY viewer exit,
    so a dashboard problem (or an instant Ctrl+C on the first blank frame) took
    the one-second-old service down silently. Only the clean return path stops."""
    calls = _wire(monkeypatch, tmp_path, running=None, tty=True)
    import vaf.cli.cmd.top as top_mod

    def boom(**kw):
        raise RuntimeError("viewer broke")
    monkeypatch.setattr(top_mod, "cmd_top", boom)

    result = runner.invoke(_cli(), [])
    assert result.exit_code != 0
    assert calls["killpg"] == [], "a viewer crash must never stop the service"


def test_service_pid_file_never_collides_with_the_llama_backend_pid_file():
    """Live incident, twice: the service pid was written into the llama
    backend's pid file, whose orphan cleanup kills any pid there when llama's
    health endpoint does not answer - the freshly spawned tray cleaned ITSELF
    up one second after starting. The two files must stay distinct."""
    import re
    from pathlib import Path
    from vaf.cli.cmd.service import _pid_file

    backend_src = (Path(__file__).resolve().parent.parent / "vaf" / "core" / "backend.py") \
        .read_text(encoding="utf-8")
    m = re.search(r'self\.pid_file\s*=.*"([A-Za-z_]+\.pid)"', backend_src)
    assert m, "backend.py must still declare its llama pid file"
    assert _pid_file().name != m.group(1), \
        "service pid file and llama backend pid file must never share a name"


def test_process_finder_matches_argv_elements_not_quoted_strings(monkeypatch):
    """A shell whose -c payload merely QUOTES the words (a wrapper, a grep, a
    supervisor line) must never count as a running VAF - stop would kill it and
    the tray dashboard would 'attach' to it (live finding during verification)."""
    import psutil
    import vaf.cli.cmd.service as service_mod

    def fake_iter(attrs):
        mk = lambda pid, cmdline: type("P", (), {"info": {"pid": pid, "cmdline": cmdline}})()
        return [
            mk(11, ["/bin/bash", "-c", "echo vaf.main tray; timeout 40 launch.sh tray"]),
            mk(12, ["/usr/bin/python3", "-m", "vaf.main", "tray", "--no-top"]),
            mk(13, ["python", "-m", "vaf.main", "top"]),
        ]

    monkeypatch.setattr(psutil, "net_connections", lambda kind="tcp": [])  # no singleton owner
    monkeypatch.setattr(psutil, "process_iter", fake_iter)
    pids = [p.info["pid"] for p in service_mod._find_vaf_processes()]
    assert pids == [12], pids


def test_the_singleton_port_owner_is_the_service_not_a_viewer(monkeypatch):
    """argv cannot tell the service from a dashboard watching it - both run
    "-m vaf.main tray". The tray's singleton listener can: whoever owns that
    port IS the service (adversarial review finding: stop killed viewers and
    a second `vaf tray` attached to one instead of starting VAF)."""
    import psutil
    import vaf.cli.cmd.service as service_mod

    mk = lambda pid, cmdline: type("P", (), {"pid": pid,
                                             "info": {"pid": pid, "cmdline": cmdline}})()
    conn = type("C", (), {"status": psutil.CONN_LISTEN, "pid": 4242,
                          "laddr": type("A", (), {"port": service_mod.TRAY_SINGLETON_PORT})()})()
    monkeypatch.setattr(psutil, "net_connections", lambda kind="tcp": [conn])
    monkeypatch.setattr(psutil, "Process", lambda pid: mk(pid, ["python", "-m", "vaf.main", "tray", "--no-top"]))
    monkeypatch.setattr(psutil, "process_iter",
                        lambda attrs: [mk(99, ["python", "-m", "vaf.main", "tray"])])

    found = service_mod._find_vaf_processes()
    assert [p.pid for p in found] == [4242], "the port owner wins over any argv match"


def test_an_interactive_chat_session_is_never_taken_for_the_service(monkeypatch):
    import psutil
    import vaf.cli.cmd.service as service_mod

    mk = lambda pid, cmdline: type("P", (), {"pid": pid,
                                             "info": {"pid": pid, "cmdline": cmdline}})()
    monkeypatch.setattr(psutil, "net_connections", lambda kind="tcp": [])
    monkeypatch.setattr(psutil, "process_iter",
                        lambda attrs: [mk(51, ["python", "-m", "vaf.main", "run", "--web"])])
    assert service_mod._find_vaf_processes() == [], \
        "`vaf run` is somebody's session - stop must not end it"


def test_the_wrapper_only_drops_a_pid_record_that_is_still_its_own(monkeypatch, tmp_path):
    """Another terminal may have restarted VAF meanwhile; deleting that newer
    service's pid record makes status lie and lets the next start double-launch."""
    import vaf.cli.cmd.service as service_mod
    pf = tmp_path / "service.pid"
    monkeypatch.setattr(service_mod, "_pid_file", lambda: pf)
    monkeypatch.setattr(main_mod.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(main_mod.os, "killpg", lambda pgid, sig: None)

    class P:
        pid = 111
        def wait(self, timeout=None):
            return 0

    pf.write_text("222")                       # a NEWER service owns the record
    main_mod._stop_spawned_tray(P())
    assert pf.exists() and pf.read_text() == "222"

    pf.write_text("111")                       # our own child's record
    main_mod._stop_spawned_tray(P())
    assert not pf.exists()
