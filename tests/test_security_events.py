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

from vaf.api import security_routes
from vaf.api.security_routes import (derive_firewall_status, events_for_module,
                                     summarize_security_events)
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
    """Channel events carry the channel; two different actors on the same channel must
    BOTH be recorded (throttle is per source, not per kind)."""
    _fresh(tmp_path, monkeypatch)
    se.log_security_event("channel_paired", channel="telegram", username="1111", detail="owner 42")
    se.log_security_event("channel_paired", channel="telegram", username="2222", detail="owner 42")
    se.log_security_event("channel_paired", channel="telegram", username="1111", detail="owner 42")  # throttled

    day = datetime.now().strftime("%Y-%m-%d")
    events = se.read_security_events(day)
    assert len(events) == 2
    assert all(e["channel"] == "telegram" for e in events)
    assert {e["username"] for e in events} == {"1111", "2222"}
    log_line = (tmp_path / f"security_{day}.log").read_text(encoding="utf-8")
    assert "channel=telegram" in log_line


def test_firewall_popup_lists_exactly_the_kinds_its_counter_counts():
    """The firewall popup's list and the deflected counter are drawn from the same kinds.

    Before the module filter the popup rendered every event of the day under
    "deflected attempts", so a messenger pairing sat under a number that did not
    include it. The list must be the counter's own population, nothing else.
    """
    events = [
        {"kind": "ip_blocked", "ts": "1"}, {"kind": "channel_paired", "ts": "2"},
        {"kind": "login_failed", "ts": "3"}, {"kind": "skill_blocked", "ts": "4"},
        {"kind": "contact_access_changed", "ts": "5"}, {"kind": "ws_rejected", "ts": "6"},
    ]
    listed = events_for_module(events, "firewall")
    assert [e["kind"] for e in listed] == ["ip_blocked", "login_failed", "ws_rejected"]
    assert len(listed) == sum(summarize_security_events(events).values())


def test_events_route_filters_by_module_before_the_limit(monkeypatch):
    """``module=firewall`` narrows the day, and the limit applies to the narrowed list."""
    import pytest
    from fastapi import HTTPException

    day = [{"kind": "channel_paired", "ts": str(i)} for i in range(20)]
    day += [{"kind": "ip_blocked", "ts": "a"}, {"kind": "twofa_failed", "ts": "b"}]
    seen = {}

    def _read(date, limit=100):
        seen["limit"] = limit
        return day[-limit:]

    monkeypatch.setattr(security_routes, "read_security_events", _read)
    out = security_routes.security_events(date="2026-01-02", limit=1, module="firewall", _={})
    assert [e["kind"] for e in out["events"]] == ["twofa_failed"]
    assert seen["limit"] == 1000, "the module view reads the whole day, then limits"

    out = security_routes.security_events(date="2026-01-02", limit=100, module=None, _={})
    assert len(out["events"]) == 22, "without a module the day is served as before"

    with pytest.raises(HTTPException) as exc:
        security_routes.security_events(date="2026-01-02", limit=100, module="skills", _={})
    assert exc.value.status_code == 400
