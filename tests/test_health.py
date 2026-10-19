# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The installation's health check (vaf/core/health.py): services, disk, agent loop.

What it has to hold: a monitor gets one word without an account and 503 when down; the
checks behind the word stay with an admin; the disk check finds the fullest disk where VAF
writes; a dead agent loop is down, not "ok because every container runs"; and the reading of
the service snapshot is the same one `vaf repair --check` exits on. Every test fails when its
guard is taken out.
"""
import threading
import types

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from typer.testing import CliRunner

from vaf.core import health
from vaf.core import service_health as sh

PROXY_PEER = ("127.0.0.1", 40000)
LAN_DEVICE = "192.168.1.77"
GB = 1024 ** 3


def _svc(name, state, required=True, needed=True, reason="r"):
    return {"name": name, "state": state, "required": required, "needed": needed,
            "reason": reason}


def _status(*services, forwarding_ok=None, root="/repo"):
    return {"docker": {"available": True, "reason": "ok", "detail": "", "root_dir": ""},
            "stack_root": root, "services": list(services),
            "host": {"forwarding_ok": forwarding_ok, "reason": "forwarding off"}}


# ── the reading of the service snapshot ──────────────────────────────────────────

def test_stack_problems_reads_the_snapshot_once_for_both_verdicts():
    """MUTATION: count an off or absent row, call a broken optional service down, or drop
    the host row - red."""
    problems = sh.stack_problems(_status(
        _svc("vaf-memory-db", "error"),
        _svc("vaf-redis", "unknown"),
        _svc("vaf-tts", "warn", required=False),
        _svc("vaf-stt", "off", required=False, needed=False),
        _svc("vaf-browser", "absent", required=False),
        _svc("vaf-gotenberg", "ok"),
        forwarding_ok=False))
    by_name = {p["name"]: p["severity"] for p in problems}
    assert by_name == {"vaf-memory-db": "down", "vaf-redis": "down",
                       "vaf-tts": "degraded", "host": "degraded"}


def test_repair_check_still_exits_on_a_required_service_only(monkeypatch):
    """The check's exit rule moved into stack_problems: a broken optional service keeps exit
    0, a broken required one exit 1."""
    import typer

    from vaf.cli.cmd import repair
    app = typer.Typer()
    app.command()(repair.cmd_repair)
    monkeypatch.setattr(repair, "collect_service_status",
                        lambda: _status(_svc("vaf-tts", "warn", required=False)))
    assert CliRunner().invoke(app, ["--check"]).exit_code == 0
    monkeypatch.setattr(repair, "collect_service_status",
                        lambda: _status(_svc("vaf-memory-db", "error")))
    assert CliRunner().invoke(app, ["--check"]).exit_code == 1


def test_the_engine_names_where_it_keeps_the_volumes(monkeypatch):
    """The disk check reads Docker's root from the one `docker info` the status runs.
    MUTATION: stop parsing it - red."""
    from vaf.core import service_stack as ss
    out = "Client:\n Version: 27\nServer:\n Docker Root Dir: /srv/docker\n Debug Mode: false\n"
    monkeypatch.setattr(ss, "resolve_docker_exe", lambda: "docker")
    monkeypatch.setattr(ss.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=out, stderr=""))
    assert ss.diagnose_docker_daemon()["root_dir"] == "/srv/docker"


# ── the checks ───────────────────────────────────────────────────────────────────

def test_services_check_takes_the_worst_problem():
    assert health.services_check(_status(_svc("vaf-gotenberg", "ok")))["status"] == "ok"
    assert health.services_check(_status(_svc("vaf-tts", "warn", required=False)))[
        "status"] == "degraded"
    assert health.services_check(_status(_svc("vaf-memory-db", "error"),
                                         _svc("vaf-tts", "warn", required=False)))[
        "status"] == "down"


def test_the_disk_check_finds_the_fullest_disk_and_counts_each_once(tmp_path):
    """MUTATION: report the first disk instead of the fullest, or flip a threshold - red."""
    big, small = tmp_path / "big", tmp_path / "small"
    big.mkdir()
    small.mkdir()
    free = {str(big): 50 * GB, str(small): 3 * GB}
    usage = lambda p: types.SimpleNamespace(free=free[p])                  # noqa: E731
    # Two folders on the same device are one disk: only the first one is read.
    assert health.disk_check([big, small], usage=usage)["status"] == "ok"
    real_stat = health.os.stat
    devices = {str(big): 1, str(small): 2}

    def stat(path, *a, **k):
        return types.SimpleNamespace(st_dev=devices[str(path)]) if str(path) in devices \
            else real_stat(path, *a, **k)

    import unittest.mock
    with unittest.mock.patch.object(health.os, "stat", stat):
        result = health.disk_check([big, small], usage=usage)
        assert result["status"] == "degraded" and str(small) in result["reason"]
        free[str(small)] = int(0.5 * GB)
        assert health.disk_check([big, small], usage=usage)["status"] == "down"


def test_a_folder_not_created_yet_is_measured_on_its_disk(tmp_path):
    """The backup folder exists only after the first backup. MUTATION: skip it - red."""
    seen = []
    usage = lambda p: seen.append(p) or types.SimpleNamespace(free=40 * GB)  # noqa: E731
    assert health.disk_check([tmp_path / "backups" / "later"], usage=usage)["status"] == "ok"
    assert seen == [str(tmp_path)]


@pytest.mark.parametrize("workers, expected", [
    (None, None),
    ({"started": 1, "alive": 1}, "ok"),
    ({"started": 3, "alive": 2}, "degraded"),
    ({"started": 1, "alive": 0}, "down"),
])
def test_the_agent_check(workers, expected):
    """MUTATION: call a dead loop ok - red."""
    result = health.agent_check(workers)
    assert (result or {}).get("status") == expected


def test_an_agent_loop_registers_itself(monkeypatch):
    """worker_status knows a loop only because the loop says so when it starts. MUTATION: drop
    the registration in run_headless_agent - red."""
    from vaf.core import headless_runner as hr

    class _Stop(Exception):
        pass

    def _boom():
        raise _Stop()

    monkeypatch.setattr(hr, "_WORKER_THREADS", {})
    monkeypatch.setattr(hr, "get_app_log_dir", _boom)
    loop = threading.Thread(target=lambda: _swallow(hr.run_headless_agent, _Stop))
    loop.start()
    loop.join(10)
    assert hr.worker_status() == {"started": 1, "alive": 0}


def _swallow(fn, kind):
    try:
        fn()
    except kind:
        pass


def test_the_whole_is_the_worst_check_and_never_raises(tmp_path):
    """MUTATION: take the first check's status, or let a failing probe raise - red."""
    usage = lambda p: types.SimpleNamespace(free=40 * GB)                  # noqa: E731
    result = health.collect_health(status_probe=lambda: _status(_svc("vaf-redis", "ok")),
                                   worker_probe=lambda: {"started": 1, "alive": 0},
                                   usage=usage)
    assert result["status"] == "down"
    assert [c["name"] for c in result["checks"]] == ["services", "disk", "agent"]

    def broken():
        raise RuntimeError("docker exploded")

    result = health.collect_health(status_probe=broken, worker_probe=lambda: None, usage=usage)
    assert result["status"] == "down" and "docker exploded" in result["checks"][0]["reason"]


def test_the_snapshot_runs_once_per_window(monkeypatch):
    """A poller must not make every request run Docker. MUTATION: collect on every call - red."""
    calls = []
    monkeypatch.setattr(health, "_snapshot", {})
    collect = lambda: calls.append(1) or {"status": "ok", "checks": []}       # noqa: E731
    health.snapshot(max_age=60, collect=collect)
    health.snapshot(max_age=60, collect=collect)
    assert len(calls) == 1
    health.snapshot(max_age=0, collect=collect)
    assert len(calls) == 2


# ── who may ask what ─────────────────────────────────────────────────────────────

def _public_app():
    from vaf.auth.middleware import AuthMiddleware
    from vaf.core import web_server
    async def _route(request):
        return await web_server.installation_health()

    app = Starlette(routes=[Route("/api/health", _route)])
    app.add_middleware(AuthMiddleware)
    return app


@pytest.mark.parametrize("verdict, code", [("ok", 200), ("degraded", 200), ("down", 503)])
def test_a_monitor_gets_one_word_without_an_account(monkeypatch, verdict, code):
    """A monitor on the LAN or VPN holds no account. It gets the word and the code, never the
    checks (paths, container names). MUTATION: drop the exemption, return the checks, or
    answer 200 when down - red."""
    monkeypatch.setattr(health, "snapshot", lambda: {
        "status": verdict, "checks": [{"name": "disk", "status": verdict,
                                       "reason": "/home/user/.vaf"}]})
    answer = TestClient(_public_app(), client=PROXY_PEER).get(
        "/api/health", headers={"X-Forwarded-For": LAN_DEVICE})
    assert answer.status_code == code
    assert answer.json() == {"status": verdict}


def test_the_checks_are_for_an_admin(monkeypatch):
    """MUTATION: drop require_admin from /api/system/health - red."""
    from fastapi import FastAPI, HTTPException

    import vaf.api.system_routes as sr
    monkeypatch.setattr(health, "collect_health", lambda: {"status": "ok", "checks": []})
    app = FastAPI()
    app.include_router(sr.router)

    def _refuse():
        raise HTTPException(status_code=403, detail="Only an admin can do this.")

    app.dependency_overrides[sr.require_admin] = _refuse
    assert TestClient(app).get("/api/system/health").status_code == 403
    app.dependency_overrides[sr.require_admin] = lambda: {"role": "admin"}
    assert TestClient(app).get("/api/system/health").json()["status"] == "ok"


# ── vaf health ───────────────────────────────────────────────────────────────────

def _health_app():
    import typer

    from vaf.cli.cmd import health as health_cmd
    app = typer.Typer()
    app.command()(health_cmd.cmd_health)
    return app, health_cmd


@pytest.mark.parametrize("verdict, code", [("ok", 0), ("degraded", 1), ("down", 2)])
def test_vaf_health_exits_like_a_monitoring_plugin(monkeypatch, verdict, code):
    """MUTATION: exit 0 on degraded, or 1 on down - red."""
    app, health_cmd = _health_app()
    monkeypatch.setattr(health_cmd, "from_running_vaf", lambda timeout=30.0: {
        "status": verdict, "checks": [{"name": "agent", "status": verdict, "reason": "x"}]})
    out = CliRunner().invoke(app, [])
    assert out.exit_code == code and verdict in out.output


def test_vaf_health_without_a_running_vaf_says_so_first(monkeypatch):
    """Nothing answering on this machine is the first finding, and the checks a terminal can
    run still run. MUTATION: report the local checks alone - red."""
    import json
    app, health_cmd = _health_app()
    monkeypatch.setattr(health_cmd, "from_running_vaf", lambda timeout=30.0: None)
    monkeypatch.setattr(health, "collect_health", lambda: {
        "status": "ok", "checks": [{"name": "disk", "status": "ok", "reason": "fine"}]})
    out = CliRunner().invoke(app, ["--json"])
    body = json.loads(out.output)
    assert out.exit_code == 2 and body["status"] == "down"
    assert [c["name"] for c in body["checks"]] == ["vaf", "disk"]
