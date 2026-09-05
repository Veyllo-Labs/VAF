# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What a repair run does, and just as importantly what it refuses to do.

Every docker call is recorded instead of executed, so these tests pin the
sequence: start the engine, bring missing containers up once, restart the ones
that answer nobody, report a port mismatch instead of "fixing" it, and never
issue a command that destroys data.
"""
import subprocess

import pytest

from vaf.core import service_health as sh


@pytest.fixture
def recorder(monkeypatch):
    """Records docker invocations; nothing reaches a real daemon."""
    calls = []

    def fake_run(args, timeout=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

    monkeypatch.setattr(sh, "_run_docker", fake_run)
    return calls


def _status(services):
    return {"docker": {"available": True, "reason": "ok", "detail": ""},
            "stack_root": "/repo", "services": services, "checked_at": "now"}


def _svc(key="redis", name="vaf-redis", **over):
    row = {"name": name, "service_key": key, "required": True, "exists": True,
           "running": True, "health": "healthy", "host_ports": [], "configured_port": 6379,
           "port_mismatch": False, "probe": None, "probe_ok": True, "state": "ok",
           "reason": "Connected."}
    row.update(over)
    return row


def _patch_env(monkeypatch, *, daemon="ok", root="/repo", statuses=None, up=True):
    monkeypatch.setattr(sh, "diagnose_docker_daemon",
                        lambda: {"ok": daemon == "ok", "reason": daemon, "detail": ""})
    monkeypatch.setattr(sh, "find_stack_root", lambda: root)
    ups = []
    monkeypatch.setattr(sh, "ensure_service_stack",
                        lambda log=None: (ups.append(1), up)[1])
    seq = list(statuses or [])

    def probe():
        return seq.pop(0) if len(seq) > 1 else (seq[0] if seq else _status([]))

    return ups, probe


def test_a_stopped_container_is_brought_up_exactly_once(monkeypatch, recorder):
    down = _svc(running=False, state="error")
    ups, probe = _patch_env(monkeypatch, statuses=[_status([down]), _status([_svc()]),
                                                   _status([_svc()])])
    result = sh.repair_service_stack(status_probe=probe)
    assert len(ups) == 1, "compose up must be one idempotent call, not one per container"
    assert [s for s in result["steps"] if s["step"] == "compose_up"][0]["ok"] is True
    assert result["ok"] is True


def test_a_healthy_stack_is_left_alone(monkeypatch, recorder):
    ups, probe = _patch_env(monkeypatch, statuses=[_status([_svc()])])
    result = sh.repair_service_stack(status_probe=probe)
    assert ups == [], "nothing was broken, so nothing may be started"
    assert recorder == []
    assert result["ok"] is True


def test_an_unreachable_container_is_restarted(monkeypatch, recorder):
    broken = _svc(probe_ok=False, state="error", reason="The container runs but does not answer.")
    _, probe = _patch_env(monkeypatch, statuses=[_status([broken]), _status([broken]),
                                                 _status([_svc()])])
    sh.repair_service_stack(status_probe=probe)
    assert ["restart", "-t", "5", "vaf-redis"] in recorder


def test_an_unhealthy_container_is_restarted(monkeypatch, recorder):
    sick = _svc(health="unhealthy", state="warn")
    _, probe = _patch_env(monkeypatch, statuses=[_status([sick]), _status([sick]),
                                                 _status([_svc()])])
    sh.repair_service_stack(status_probe=probe)
    assert ["restart", "-t", "5", "vaf-redis"] in recorder


def test_a_port_mismatch_is_reported_and_never_touched(monkeypatch, recorder):
    """Restarting cannot make two different port numbers agree, and rewriting
    the config key behind the user's back is not repair."""
    wrong = _svc(port_mismatch=True, probe_ok=False, state="error",
                 reason="The container publishes host port 6379, but VAF is configured "
                        "to reach it on 6380.")
    _, probe = _patch_env(monkeypatch, statuses=[_status([wrong])])
    result = sh.repair_service_stack(status_probe=probe)
    assert recorder == [], "a port mismatch must not produce any docker command"
    step = [s for s in result["steps"] if s["step"].startswith("config:")][0]
    assert step["ok"] is False
    assert "redis_url" in step["message"]
    assert result["ok"] is False


def test_repair_never_issues_a_destructive_command(monkeypatch, recorder):
    broken = _svc(probe_ok=False, state="error")
    _, probe = _patch_env(monkeypatch, statuses=[_status([broken]), _status([broken]),
                                                 _status([broken])])
    sh.repair_service_stack(status_probe=probe)
    flat = [" ".join(c) for c in recorder]
    for forbidden in ("down", "rm", "volume", "prune", "kill"):
        assert not any(forbidden in c.split() for c in flat), f"{forbidden} in {flat}"


def test_permission_denied_stops_before_touching_anything(monkeypatch, recorder):
    """VAF cannot fix a socket permission, and must not pretend it tried."""
    started = []
    monkeypatch.setattr(sh, "attempt_docker_daemon_start",
                        lambda log=None: started.append(1) or True)
    _, probe = _patch_env(monkeypatch, daemon="permission_denied", statuses=[_status([])])
    result = sh.repair_service_stack(status_probe=probe)
    assert started == []
    assert recorder == []
    assert result["steps"][0]["step"] == "daemon"
    assert "docker group" in result["steps"][0]["message"] or "usermod" in result["steps"][0]["message"]
    assert result["ok"] is False


def test_a_missing_docker_cli_says_so_and_stops(monkeypatch, recorder):
    _, probe = _patch_env(monkeypatch, daemon="no_cli", statuses=[_status([])])
    result = sh.repair_service_stack(status_probe=probe)
    assert result["steps"] == [result["steps"][0]]
    assert "Install Docker" in result["steps"][0]["message"]


def test_a_stopped_daemon_is_started_then_the_stack_continues(monkeypatch, recorder):
    monkeypatch.setattr(sh, "attempt_docker_daemon_start", lambda log=None: True)
    monkeypatch.setattr(sh, "is_docker_daemon_running", lambda: True)
    _, probe = _patch_env(monkeypatch, daemon="not_running", statuses=[_status([_svc()])])
    result = sh.repair_service_stack(status_probe=probe)
    daemon_step = result["steps"][0]
    assert daemon_step["action"] == "start"
    assert daemon_step["ok"] is True


def test_an_engine_that_never_comes_up_gives_a_named_instruction(monkeypatch, recorder):
    monkeypatch.setattr(sh, "attempt_docker_daemon_start", lambda log=None: False)
    monkeypatch.setattr(sh, "is_docker_daemon_running", lambda: False)
    _, probe = _patch_env(monkeypatch, daemon="not_running", statuses=[_status([])])
    result = sh.repair_service_stack(status_probe=probe)
    assert result["steps"][0]["ok"] is False
    assert any(word in result["steps"][0]["message"]
               for word in ("systemctl", "Docker Desktop", "colima", "Rancher"))


def test_a_pip_install_without_a_compose_file_is_honest(monkeypatch, recorder):
    """It says there is no stack here, and does not call that a failure: the
    step's own message states nothing is wrong, so a failed verdict would make
    the command contradict itself (and `vaf repair --check` on the same
    machine, which exits 0)."""
    _, probe = _patch_env(monkeypatch, root=None, statuses=[_status([_svc()])])
    result = sh.repair_service_stack(status_probe=probe)
    step = [s for s in result["steps"] if s["step"] == "stack_root"][0]
    assert step["ok"] is True
    assert "pip install" in step["message"]
    assert recorder == []
    assert result["ok"] is True, "a healthy stack managed elsewhere is not a failed repair"


def test_a_stopped_optional_container_is_started_too(monkeypatch, recorder):
    """It is shown to the user as a problem, so a repair that skips it answers
    a question nobody asked."""
    tts = _svc(key="tts", name="vaf-tts", required=False, exists=True, running=False,
               state="warn")
    ups, probe = _patch_env(monkeypatch, statuses=[_status([_svc(), tts]),
                                                   _status([_svc(), _svc(key="tts", name="vaf-tts", required=False)]),
                                                   _status([_svc(), _svc(key="tts", name="vaf-tts", required=False)])])
    result = sh.repair_service_stack(status_probe=probe)
    assert len(ups) == 1, "a stopped optional container must still trigger compose up"
    assert result["ok"] is True


def test_a_service_left_broken_is_named_even_when_the_run_succeeds(monkeypatch, recorder):
    """`ok` stays required-only so an optional image that will not build cannot
    fail the run, but the caller must not print "healthy" over a container the
    same output lists as stopped."""
    tts = _svc(key="tts", name="vaf-tts", required=False, exists=True, running=False,
               state="warn")
    _, probe = _patch_env(monkeypatch, statuses=[_status([_svc(), tts]),
                                                 _status([_svc(), tts]),
                                                 _status([_svc(), tts])])
    result = sh.repair_service_stack(status_probe=probe)
    assert result["ok"] is True
    assert result["degraded"] == ["vaf-tts"]


def test_every_step_is_reported_while_it_happens(monkeypatch, recorder):
    """The dialog and the terminal render a run that takes minutes; a report
    that only arrives at the end is a spinner with extra steps."""
    seen = []
    broken = _svc(running=False, state="error")
    _, probe = _patch_env(monkeypatch, statuses=[_status([broken]), _status([_svc()]),
                                                 _status([_svc()])])
    result = sh.repair_service_stack(progress=seen.append, status_probe=probe)
    assert [s["step"] for s in seen] == [s["step"] for s in result["steps"]]
    assert all({"step", "action", "ok", "message"} <= set(s) for s in seen)


def test_a_still_unreachable_service_gets_the_firewall_hint(monkeypatch, recorder):
    broken = _svc(probe_ok=False, state="error")
    _, probe = _patch_env(monkeypatch, statuses=[_status([broken]), _status([broken]),
                                                 _status([broken])])
    result = sh.repair_service_stack(status_probe=probe)
    hints = [s for s in result["steps"] if s["step"].startswith("firewall:")]
    assert len(hints) == 1
    assert "firewall" in hints[0]["message"].lower()


def test_a_starting_container_is_left_alone(monkeypatch, recorder):
    """Restarting a container that is still booting throws away the progress it
    has made and starts the wait over - the button would make things slower."""
    booting = _svc(starting=True, probe_ok=False, state="warn",
                   reason="The container is still starting up (about 12s left).")
    _, probe = _patch_env(monkeypatch, statuses=[_status([booting]), _status([booting]),
                                                 _status([_svc()])])
    result = sh.repair_service_stack(status_probe=probe)
    assert recorder == [], "a booting container must not be restarted"
    step = [s for s in result["steps"] if s["step"].startswith("starting:")][0]
    assert step["ok"] is True
    assert "still starting" in step["message"]


# ── the host's forwarding switch ─────────────────────────────────────────────────

def _host(ok):
    return {"ip_forward": 1 if ok else 0, "forwarding_ok": ok, "reason": "" if ok else "off"}


def test_forwarding_off_is_switched_on_before_any_container_is_touched(monkeypatch, recorder):
    """A restart cannot fix a host sysctl; switching it on first is what makes
    the rest of the run meaningful, and the recovering container gets a
    'check again' instead of a firewall hint that points the wrong way."""
    browser = _svc(key="vaf-browser", name="vaf-browser", required=False, probe_ok=False,
                   health="unhealthy", state="warn")
    seq = [dict(_status([browser]), host=_host(False))] * 3
    _, probe = _patch_env(monkeypatch, statuses=seq)
    order = []
    monkeypatch.setattr(sh, "enable_host_forwarding",
                        lambda: (order.append("sysctl"), {"ok": True, "detail": ""})[1])
    monkeypatch.setattr(sh, "_run_docker",
                        lambda args, timeout=None: (order.append(args[0]),
                                                    subprocess.CompletedProcess(list(args), 0, "", ""))[1])
    result = sh.repair_service_stack(status_probe=probe)
    assert order[0] == "sysctl"
    step = next(s for s in result["steps"] if s["step"] == "host_forwarding")
    assert step["ok"] is True and sh.HOST_FORWARDING_DROPIN in step["message"]
    assert not any(s["step"].startswith("firewall:") for s in result["steps"])
    assert any(s["step"] == "recovering:vaf-browser" and s["ok"] for s in result["steps"])


def test_forwarding_on_never_elevates(monkeypatch, recorder):
    seq = [dict(_status([_svc()]), host=_host(True))] * 3
    _, probe = _patch_env(monkeypatch, statuses=seq)
    monkeypatch.setattr(sh, "enable_host_forwarding",
                        lambda: (_ for _ in ()).throw(AssertionError("elevated for nothing")))
    result = sh.repair_service_stack(status_probe=probe)
    assert result["ok"] is True
    assert not any(s["step"] == "host_forwarding" for s in result["steps"])


def test_a_refused_forwarding_switch_gives_the_command_and_fails_the_run(monkeypatch, recorder):
    seq = [dict(_status([_svc()]), host=_host(False))] * 3
    _, probe = _patch_env(monkeypatch, statuses=seq)
    monkeypatch.setattr(sh, "enable_host_forwarding", lambda: {"ok": False, "detail": "Not authorized"})
    result = sh.repair_service_stack(status_probe=probe)
    step = next(s for s in result["steps"] if s["step"] == "host_forwarding")
    assert step["ok"] is False
    assert "sysctl -w net.ipv4.ip_forward=1" in step["message"] and "Not authorized" in step["message"]
    assert result["ok"] is False
