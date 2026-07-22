# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract tests for the security event log (blocked/rejected access attempts).

Pins: dual sink (structured JSONL + human-readable security_<date>.log whose
line format the Logs window parser understands), flood throttle per (kind, ip),
no-secrets field surface, and the firewall-module derivations built on top.
"""
import json
import re
from datetime import datetime

from vaf.api.security_routes import derive_firewall_status, summarize_security_events
from vaf.core import security_events as se


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("VAF_LOG_DIR", str(tmp_path))
    # reset the module throttle between tests
    se._last_emit.clear()
    return tmp_path


def test_writer_writes_both_sinks(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    se.log_security_event("login_failed", ip="192.168.1.50", username="alice", detail="wrong password")

    day = datetime.now().strftime("%Y-%m-%d")
    jsonl = tmp_path / f"security_events_{day}.jsonl"
    logf = tmp_path / f"security_{day}.log"
    assert jsonl.exists() and logf.exists()

    entry = json.loads(jsonl.read_text(encoding="utf-8").strip())
    assert entry["kind"] == "login_failed"
    assert entry["ip"] == "192.168.1.50"
    assert entry["username"] == "alice"

    line = logf.read_text(encoding="utf-8").strip()
    # The Logs window's parseLogLine expects "<iso-ts> <rest>"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T[\d:.]+\s+\[login_failed\]", line)
    assert "ip=192.168.1.50" in line and "user=alice" in line


def test_flood_throttle_drops_identical_kind_ip(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    for _ in range(10):
        se.log_security_event("ip_blocked", ip="203.0.113.7", path="/api/x")
    # different ip is NOT throttled by the first key
    se.log_security_event("ip_blocked", ip="198.51.100.23", path="/api/x")

    day = datetime.now().strftime("%Y-%m-%d")
    events = se.read_security_events(day, limit=100)
    assert len(events) == 2
    assert {e["ip"] for e in events} == {"203.0.113.7", "198.51.100.23"}


def test_reader_missing_file_and_bad_lines(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    day = datetime.now().strftime("%Y-%m-%d")
    assert se.read_security_events(day) == []
    (tmp_path / f"security_events_{day}.jsonl").write_text('not json\n{"kind":"ok-line"}\n', encoding="utf-8")
    events = se.read_security_events(day)
    assert len(events) == 1 and events[0]["kind"] == "ok-line"


def test_writer_never_raises_on_broken_log_dir(monkeypatch):
    # Patch the dir resolver itself: pointing VAF_LOG_DIR at an unwritable path
    # is NOT enough - get_app_log_dir has a fallback chain and would silently
    # write into the real repo logs (which is exactly what this suite must
    # never do).
    def _boom():
        raise OSError("no log dir")
    monkeypatch.setattr(se, "get_app_log_dir", _boom)
    se._last_emit.clear()
    se.log_security_event("ip_blocked", ip="203.0.113.9")  # must not raise


def test_firewall_summary_and_derivation():
    events = [
        {"kind": "ip_blocked"}, {"kind": "ws_rejected"}, {"kind": "token_rejected"},
        {"kind": "login_failed"}, {"kind": "twofa_failed"}, {"kind": "unknown_kind"},
    ]
    counts = summarize_security_events(events)
    assert counts == {"blocked": 3, "failed_logins": 2}

    fw = derive_firewall_status(True, True, counts)
    assert fw["state"] == "ok" and fw["reason"] == "lan_enabled"
    assert fw["blocked_today"] == 3 and fw["failed_logins_today"] == 2

    fw_off = derive_firewall_status(False, True, {"blocked": 0, "failed_logins": 0})
    assert fw_off["reason"] == "lan_disabled" and fw_off["state"] == "ok"


def test_channel_field_and_per_sender_throttle(tmp_path, monkeypatch):
    """channel_rejected events carry the channel; two different senders on the
    same channel must BOTH be recorded (throttle is per sender, not per kind)."""
    _fresh(tmp_path, monkeypatch)
    se.log_security_event("channel_rejected", channel="telegram", username="1111", detail="text/not_paired")
    se.log_security_event("channel_rejected", channel="telegram", username="2222", detail="text/not_paired")
    se.log_security_event("channel_rejected", channel="telegram", username="1111", detail="text/not_paired")  # throttled

    day = datetime.now().strftime("%Y-%m-%d")
    events = se.read_security_events(day)
    assert len(events) == 2
    assert all(e["channel"] == "telegram" for e in events)
    assert {e["username"] for e in events} == {"1111", "2222"}
    log_line = (tmp_path / f"security_{day}.log").read_text(encoding="utf-8")
    assert "channel=telegram" in log_line
