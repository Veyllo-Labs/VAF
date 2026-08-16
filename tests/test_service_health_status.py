# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Service status derivation: every state a container can be in, without Docker.

The derivations are pure functions over probe results, so these tests describe
the contract the repair run and the admin dialog both read.
"""
import subprocess

import pytest

from vaf.core import service_health as sh
from vaf.core import service_stack as ss

REDIS = next(s for s in ss.SERVICES if s.service_key == "redis")
SANDBOX = next(s for s in ss.SERVICES if s.service_key == "sandbox")
TTS = next(s for s in ss.SERVICES if s.service_key == "tts")


def _inspect(running=True, health=None, host_port="6379"):
    state = {"Running": running}
    if health is not None:
        state["Health"] = {"Status": health}
    bindings = {}
    if host_port is not None:
        bindings = {"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": host_port}]}
    return {"Name": "/vaf-redis", "State": state, "HostConfig": {"PortBindings": bindings}}


def test_running_and_answering_is_ok():
    row = sh.derive_service_status(REDIS, _inspect(), 6379, {"kind": "tcp", "ok": True})
    assert row["state"] == "ok"
    assert row["running"] is True
    assert row["port_mismatch"] is False


def test_stopped_required_container_is_an_error():
    row = sh.derive_service_status(REDIS, _inspect(running=False), 6379, None)
    assert row["state"] == "error"
    assert "stopped" in row["reason"]


def test_missing_required_container_is_an_error_missing_optional_is_absent():
    assert sh.derive_service_status(REDIS, None, 6379, None)["state"] == "error"
    optional = sh.derive_service_status(TTS, None, 5002, None)
    assert optional["state"] == "absent"
    assert optional["exists"] is False


def test_port_mismatch_beats_a_passing_probe():
    """The published port and the configured port disagree: a probe that
    happens to succeed against something else must not paint this green."""
    row = sh.derive_service_status(REDIS, _inspect(host_port="6379"), 6380,
                                   {"kind": "tcp", "ok": True})
    assert row["port_mismatch"] is True
    assert row["state"] == "error"
    assert "6379" in row["reason"] and "6380" in row["reason"]


def test_running_but_unreachable_is_an_error_for_required_services():
    row = sh.derive_service_status(REDIS, _inspect(), 6379,
                                   {"kind": "tcp", "ok": False, "detail": "refused"})
    assert row["state"] == "error"
    assert "does not answer" in row["reason"]
    assert row["probe_ok"] is False


def test_unhealthy_and_starting_are_warnings_not_errors():
    unhealthy = sh.derive_service_status(REDIS, _inspect(health="unhealthy"), 6379,
                                         {"kind": "tcp", "ok": True})
    starting = sh.derive_service_status(REDIS, _inspect(health="starting"), 6379,
                                        {"kind": "tcp", "ok": True})
    assert unhealthy["state"] == "warn"
    assert starting["state"] == "warn"


def test_a_service_without_ports_is_never_a_port_mismatch():
    inspect = {"Name": "/vaf-sandbox", "State": {"Running": True}, "HostConfig": {}}
    row = sh.derive_service_status(SANDBOX, inspect, None, None)
    assert row["state"] == "ok"
    assert row["port_mismatch"] is False


def test_docker_down_probes_nothing_at_all():
    """One `docker info` is the whole cost when the daemon is gone. Seven
    inspect/probe timeouts behind a web request is the failure this prevents."""
    calls = {"inspect": 0, "probe": 0}

    def inspect_probe(names):
        calls["inspect"] += 1
        return []

    def service_probe(spec, port):
        calls["probe"] += 1
        return None

    status = sh.collect_service_status(
        daemon_probe=lambda: {"ok": False, "reason": "not_running", "detail": "down"},
        inspect_probe=inspect_probe,
        port_reader=lambda spec: 1,
        service_probe=service_probe,
    )
    assert calls == {"inspect": 0, "probe": 0}
    assert status["docker"]["available"] is False
    assert len(status["services"]) == len(ss.SERVICES)
    assert {s["state"] for s in status["services"]} == {"unknown"}


def test_status_reports_every_service_even_when_none_exist():
    status = sh.collect_service_status(
        daemon_probe=lambda: {"ok": True, "reason": "ok", "detail": ""},
        inspect_probe=lambda names: [],
        port_reader=lambda spec: spec.default_port or None,
        service_probe=lambda spec, port: None,
    )
    assert [s["service_key"] for s in status["services"]] == [s.service_key for s in ss.SERVICES]
    assert all(s["exists"] is False for s in status["services"])


def test_stopped_container_is_never_probed():
    """Probing a stopped container only buys a timeout; its state is known."""
    probed = []
    inspect = {"Name": "/vaf-redis", "State": {"Running": False}, "HostConfig": {}}
    sh.collect_service_status(
        daemon_probe=lambda: {"ok": True, "reason": "ok", "detail": ""},
        inspect_probe=lambda names: [inspect],
        port_reader=lambda spec: 6379,
        service_probe=lambda spec, port: probed.append(spec.service_key),
    )
    assert probed == []


@pytest.mark.parametrize("stderr,expected", [
    ("Got permission denied while trying to connect to the Docker daemon socket",
     "permission_denied"),
    ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock", "not_running"),
])
def test_daemon_diagnosis_classifies_stderr(monkeypatch, stderr, expected):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    result = ss.diagnose_docker_daemon()
    assert result["ok"] is False
    assert result["reason"] == expected


def test_daemon_diagnosis_reports_a_missing_cli(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    assert ss.diagnose_docker_daemon()["reason"] == "no_cli"


def test_daemon_diagnosis_reports_success(monkeypatch):
    monkeypatch.setattr(
        ss.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr=""),
    )
    assert ss.diagnose_docker_daemon() == {"ok": True, "reason": "ok", "detail": ""}


def test_configured_port_prefers_the_environment_then_config_then_default():
    browser = next(s for s in ss.SERVICES if s.service_key == "vaf-browser")
    assert sh.configured_port(browser, environ={"VAF_BROWSER_CDP_URL": "http://127.0.0.1:9333"}) == 9333
    assert sh.configured_port(browser, environ={}) == 9222
    assert sh.configured_port(REDIS, config_get=lambda k, d: "redis://localhost:6390/0",
                              environ={}) == 6390
    assert sh.configured_port(REDIS, config_get=lambda k, d: "", environ={}) == 6379


def test_configured_port_survives_a_hand_edited_url():
    assert sh.configured_port(REDIS, config_get=lambda k, d: "not a url at all",
                              environ={}) == 6379
    assert sh.configured_port(SANDBOX, config_get=lambda k, d: "", environ={}) is None


# ── the start window ─────────────────────────────────────────────────────────

def _inspect_starting(health="starting", start_period_ns=30_000_000_000, ago_s=5):
    from datetime import datetime, timedelta, timezone
    started = (datetime.now(timezone.utc) - timedelta(seconds=ago_s))
    return {
        "Name": "/vaf-redis",
        "State": {"Running": True, "Health": {"Status": health},
                  "StartedAt": started.isoformat().replace("+00:00", "Z")},
        "HostConfig": {"PortBindings": {"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": "6379"}]}},
        "Config": {"Healthcheck": {"StartPeriod": start_period_ns}},
    }


def test_a_container_inside_its_start_window_is_starting_not_broken():
    """The reason this exists: right after a start the database does not answer
    yet, and calling that "does not answer" sends the user to a repair button
    for something that only needs a few more seconds."""
    row = sh.derive_service_status(REDIS, _inspect_starting(), 6379,
                                   {"kind": "tcp", "ok": False, "detail": "refused"})
    assert row["starting"] is True
    assert row["state"] == "warn"
    assert "starting up" in row["reason"]
    assert row["starting_seconds_left"] > 0


def test_the_countdown_comes_from_the_container_not_from_a_constant():
    """Start periods differ per service (30s for the database, 120s for the
    speech containers), so one invented number would be wrong for most."""
    quick = sh.derive_service_status(REDIS, _inspect_starting(start_period_ns=30_000_000_000, ago_s=10),
                                     6379, {"kind": "tcp", "ok": False})
    slow = sh.derive_service_status(REDIS, _inspect_starting(start_period_ns=120_000_000_000, ago_s=10),
                                    6379, {"kind": "tcp", "ok": False})
    assert 15 <= quick["starting_seconds_left"] <= 21
    assert 105 <= slow["starting_seconds_left"] <= 111


def test_a_container_past_its_window_that_still_fails_is_broken_again():
    row = sh.derive_service_status(REDIS, _inspect_starting(health="unhealthy", ago_s=600),
                                   6379, {"kind": "tcp", "ok": False})
    assert row["starting"] is False
    assert row["state"] == "error"


def test_docker_saying_starting_outranks_the_arithmetic():
    """A slow first boot can outlast its own start period and still be starting."""
    row = sh.derive_service_status(REDIS, _inspect_starting(health="starting", ago_s=600),
                                   6379, {"kind": "tcp", "ok": False})
    assert row["starting"] is True


def test_the_snapshot_says_the_stack_is_coming_up():
    status = sh.collect_service_status(
        daemon_probe=lambda: {"ok": True, "reason": "ok", "detail": ""},
        inspect_probe=lambda names: [_inspect_starting()],
        port_reader=lambda spec: 6379,
        service_probe=lambda spec, port: {"kind": "tcp", "ok": False},
    )
    assert status["starting"] is True
    assert status["starting_seconds_left"] > 0
