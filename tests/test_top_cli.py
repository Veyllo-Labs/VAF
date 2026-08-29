# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf top --once`: one rendered snapshot, no live loop, no real probes."""
import typer
from typer.testing import CliRunner

import vaf.cli.cmd.top as top_mod

runner = CliRunner()


def _fake_snapshot(prev=None, service_pid=None):
    return {
        "ts": 0.0, "monotonic": 0.0,
        "host": {"hostname": "srv1", "os": "openSUSE Leap", "kernel": "6.12", "arch": "x86_64"},
        "vaf": {"version": "0.1.0a99", "server_mode": True, "provider": "veyllo",
                "model": "test.gguf", "lan_enabled": True},
        "uptime_s": 3661,
        "cpu": {"percent": 12.5, "cores": 8, "loadavg": (0.5, 0.4, 0.3)},
        "mem": {"total_mb": 16000, "used_mb": 4000, "percent": 25.0,
                "swap_total_mb": 0, "swap_used_mb": 0},
        "disk": {"path": "/home/user/.vaf", "total_gb": 100.0, "used_gb": 20.0, "percent": 20.0},
        "net": {"bytes_sent": 0, "bytes_recv": 0, "sent_rate_bps": 1024, "recv_rate_bps": 2048},
        "gpus": [{"vendor": "nvidia", "name": "RTX", "util_percent": 30,
                  "mem_used_mb": 1000, "mem_total_mb": 8192}],
        "service": {"pid": 4242, "uptime_s": 120, "cpu_percent": 2.0,
                    "rss_mb": 512, "processes": 3},
        "lan": {"enabled": True, "access_port": 8443,
                "urls": ["https://srv1:8443", "https://192.168.1.10:8443"]},
        "clients": {"listen_ports": [8443],
                    "clients": [{"ip": "192.168.1.77", "local": False,
                                 "connections": 2, "ports": [8443]},
                                {"ip": "127.0.0.1", "local": True,
                                 "connections": 1, "ports": [8001]}]},
    }


def _fake_services():
    return {"docker": {"available": True, "reason": "", "detail": ""},
            "services": [{"name": "vaf-postgres", "service_key": "postgres",
                          "state": "ok", "reason": "Connected."}],
            "starting": False, "checked_at": ""}


def test_once_renders_one_snapshot_without_probing(monkeypatch):
    monkeypatch.setattr("vaf.core.system_stats.collect_snapshot", _fake_snapshot)
    monkeypatch.setattr("vaf.core.service_health.collect_service_status",
                        lambda *a, **k: _fake_services())
    monkeypatch.setattr(top_mod, "_service_pid", lambda: 4242)
    monkeypatch.setattr(top_mod.time, "sleep", lambda s: None)

    app = typer.Typer()
    app.command(name="top")(top_mod.cmd_top)
    result = runner.invoke(app, ["--once"])

    assert result.exit_code == 0, result.output
    assert "vaf top" in result.output
    assert "srv1" in result.output
    assert "mode server" in result.output
    assert "veyllo (API)" in result.output
    assert "@@@g" in result.output, "the Veyllo mark must be part of the header"
    assert "https://srv1:8443" in result.output, "LAN hostname URL must be shown"
    assert "postgres" in result.output
    assert "PID 4242" in result.output
    assert "Network" in result.output
    assert "192.168.1.77" in result.output, "LAN clients must be listed per IP"
    assert "2 conns" in result.output
    assert "localhost" in result.output


def test_duration_and_rate_formatting():
    assert top_mod._fmt_duration(None) == "n/a"
    assert top_mod._fmt_duration(3661) == "01:01:01"
    assert top_mod._fmt_duration(90061) == "1d 01:01:01"
    assert top_mod._fmt_rate(None) == "n/a"
    assert top_mod._fmt_rate(500) == "500 B/s"
    assert top_mod._fmt_rate(2048) == "2.0 KB/s"
    assert top_mod._fmt_rate(3 * 1024 * 1024) == "3.0 MB/s"


def test_bar_never_dies_on_missing_values():
    assert top_mod._bar(None) == "n/a"
    assert "100.0%" in top_mod._bar(150.0)  # clamped
    assert "0.0%" in top_mod._bar(-5)


def test_log_source_prefers_the_freshest_file(tmp_path, monkeypatch):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", lambda key, default=None: False)
    old = tmp_path / "vaf_run.log"
    new = tmp_path / "tray_debug.log"
    old.write_text("old\n")
    new.write_text("new\n")
    import os
    os.utime(old, (1000, 1000))

    source = top_mod._resolve_log_source(candidates=[old, new, tmp_path / "missing.log"])
    assert source == {"kind": "file", "path": new}
    assert top_mod._resolve_log_source(candidates=[tmp_path / "missing.log"]) is None


def test_log_source_uses_the_journal_in_server_mode(monkeypatch):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", lambda key, default=None: key == "server_mode")
    monkeypatch.setattr(top_mod.shutil, "which", lambda name: "/usr/bin/journalctl")
    monkeypatch.setattr(top_mod.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    assert top_mod._resolve_log_source() == {"kind": "journal"}


def test_log_tail_follows_appends_and_survives_truncation(tmp_path):
    log = tmp_path / "vaf_run.log"
    log.write_text("one\ntwo\n")
    tail = top_mod._LogTail({"kind": "file", "path": log})
    tail._backfill_file(log)
    assert tail.lines(10) == ["one", "two"]

    with open(log, "a") as fh:
        fh.write("three\npart")
    tail.poll_file()
    assert tail.lines(10) == ["one", "two", "three"], "an unfinished line must wait"

    with open(log, "a") as fh:
        fh.write("ial\n")
    tail.poll_file()
    assert tail.lines(10)[-1] == "partial"

    log.write_text("fresh\n")   # rotation/truncation
    tail.poll_file()
    assert tail.lines(10)[-1] == "fresh"


def test_start_watch_opens_the_dashboard_when_already_running(monkeypatch, tmp_path):
    import vaf.cli.cmd.service as service_mod
    opened = []
    monkeypatch.setattr(service_mod, "_is_server_mode", lambda: False)
    monkeypatch.setattr(service_mod, "_running_pid", lambda: 123)
    monkeypatch.setattr(service_mod, "_open_dashboard", lambda: opened.append(True))

    app = typer.Typer()
    app.command(name="start")(service_mod.cmd_start)
    result = runner.invoke(app, ["--watch"])
    assert result.exit_code == 0, result.output
    assert opened == [True]

    # a direct call (cmd_restart, updater) must NOT inherit --watch semantics
    monkeypatch.setattr(service_mod, "_running_pid", lambda: None)
    monkeypatch.setattr(service_mod.subprocess, "Popen",
                        lambda *a, **k: type("P", (), {"pid": 999})())
    monkeypatch.setattr(service_mod, "_pid_file",
                        lambda: type("F", (), {"write_text": staticmethod(lambda t: None)})())
    monkeypatch.setattr(service_mod, "_log_file", lambda: tmp_path / "vaf_run.log")
    opened.clear()
    service_mod.cmd_start()
    assert opened == [], "OptionInfo default must normalize to watch=False"


def test_log_source_drops_files_older_than_the_running_service(tmp_path, monkeypatch):
    """An old file from a previous run must not pose as the CURRENT service's
    output (live finding: a tray from the desktop entry logs to no file, and the
    pane showed a week-old tray_debug.log instead)."""
    import os
    import time
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", lambda key, default=None: False)

    old = tmp_path / "tray_debug.log"
    old.write_text("old\n")
    os.utime(old, (1000, 1000))
    assert top_mod._resolve_log_source(candidates=[old],
                                       not_older_than=time.time()) is None

    fresh = tmp_path / "vaf_run.log"
    fresh.write_text("fresh\n")
    src = top_mod._resolve_log_source(candidates=[old, fresh],
                                      not_older_than=time.time() - 60)
    assert src == {"kind": "file", "path": fresh}
