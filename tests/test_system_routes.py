# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The admin system API: service status, repair as a job, update as a spawn.

The refusals matter as much as the happy paths here. An update started from a
browser has to come back by itself, so every situation where it would not is a
409 with a sentence naming what to do instead - never a button that promises a
restart and leaves the machine down or doubled.
"""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vaf.api.system_routes as sr


@pytest.fixture
def client(monkeypatch):
    sr._repair_state.update({
        "running": False, "started_at": None, "finished_at": None,
        "steps": [], "result": None, "error": None,
    })
    monkeypatch.setattr(sr, "_update_started_at", None, raising=False)
    app = FastAPI()
    app.include_router(sr.router)
    app.dependency_overrides[sr.require_admin] = lambda: {"username": "u", "role": "admin"}
    return TestClient(app)


def _status(services=()):
    return {"docker": {"available": True, "reason": "ok", "detail": ""},
            "stack_root": "/repo", "services": list(services), "checked_at": "now"}


# ── service status ───────────────────────────────────────────────────────────

def test_services_reports_the_framework_snapshot(client, monkeypatch):
    monkeypatch.setattr(sr, "collect_service_status",
                        lambda: _status([{"name": "vaf-redis", "state": "ok"}]))
    body = client.get("/api/system/services").json()
    assert body["docker"]["available"] is True
    assert body["services"][0]["name"] == "vaf-redis"


# ── repair as a job ──────────────────────────────────────────────────────────

def test_repair_starts_and_streams_its_steps(client, monkeypatch):
    """A repair can run for minutes. The client must be able to watch it
    instead of holding a request open until a proxy gives up."""
    def slow_repair(progress=None, **kw):
        progress({"step": "daemon", "action": "check", "ok": True, "message": "up"})
        progress({"step": "compose_up", "action": "up", "ok": True, "message": "started"})
        return {"ok": True, "steps": [], "status_after": _status()}

    monkeypatch.setattr(sr, "repair_service_stack", slow_repair)
    started = client.post("/api/system/services/repair")
    assert started.status_code == 202
    assert started.json()["started"] is True

    for _ in range(100):
        state = client.get("/api/system/services/repair").json()
        if not state["running"]:
            break
        time.sleep(0.02)
    assert [s["step"] for s in state["steps"]] == ["daemon", "compose_up"]
    assert state["result"]["ok"] is True
    assert state["error"] is None


def test_a_second_repair_is_refused_while_one_runs(client, monkeypatch):
    """Two runs would fight over the same containers."""
    release = {"go": False}

    def blocking_repair(progress=None, **kw):
        while not release["go"]:
            time.sleep(0.01)
        return {"ok": True, "steps": [], "status_after": _status()}

    monkeypatch.setattr(sr, "repair_service_stack", blocking_repair)
    assert client.post("/api/system/services/repair").status_code == 202
    try:
        assert client.post("/api/system/services/repair").status_code == 409
    finally:
        release["go"] = True


def test_a_crashing_repair_is_reported_not_swallowed(client, monkeypatch):
    def boom(progress=None, **kw):
        raise RuntimeError("docker exploded")

    monkeypatch.setattr(sr, "repair_service_stack", boom)
    client.post("/api/system/services/repair")
    for _ in range(100):
        state = client.get("/api/system/services/repair").json()
        if not state["running"]:
            break
        time.sleep(0.02)
    assert "docker exploded" in state["error"]
    assert state["running"] is False


# ── update state ─────────────────────────────────────────────────────────────

def test_update_state_reads_disk_and_never_the_network(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("GET /update went to the network")

    monkeypatch.setattr(sr, "check_now", boom)
    monkeypatch.setattr(sr, "read_update_cache",
                        lambda: {"checked_at": "2026-08-01T00:00:00+00:00",
                                 "latest_version": "9.9.9", "relevant": True})
    monkeypatch.setattr(sr, "read_last_update", lambda: None)
    monkeypatch.setattr(sr, "describe_update_ability", lambda: (True, ""))
    body = client.get("/api/system/update").json()
    assert body["cache"]["latest_version"] == "9.9.9"
    assert body["can_apply"] is True
    assert body["current"]


def test_update_state_surfaces_an_unfinished_update(client, monkeypatch):
    monkeypatch.setattr(sr, "read_update_cache", lambda: None)
    monkeypatch.setattr(sr, "read_last_update", lambda: {"target_tag": "v9.9.9"})
    monkeypatch.setattr(sr, "describe_update_ability", lambda: (True, ""))
    assert client.get("/api/system/update").json()["last_update"]["target_tag"] == "v9.9.9"


def test_the_check_button_is_the_only_thing_that_asks_github(client, monkeypatch):
    calls = []
    monkeypatch.setattr(sr, "check_now", lambda: calls.append(1) or {
        "current": "1.0.0", "latest": "9.9.9", "relevant": True,
        "checked_at": "now", "why": None, "message": ""})
    monkeypatch.setattr(sr, "describe_update_ability", lambda: (True, ""))
    body = client.post("/api/system/update/check").json()
    assert calls == [1]
    assert body["latest"] == "9.9.9"
    assert body["can_apply"] is True


# ── applying an update ───────────────────────────────────────────────────────

@pytest.fixture
def appliable(monkeypatch):
    spawned = []
    monkeypatch.setattr(sr, "describe_update_ability", lambda: (True, ""))
    monkeypatch.setattr(sr, "read_last_update", lambda: None)
    monkeypatch.setattr(sr, "_restart_blocker", lambda: None)
    monkeypatch.setattr(sr, "spawn_update_process",
                        lambda *a, **k: spawned.append(1) or
                        {"pid": 99, "log": "/tmp/u.log", "via": "popen", "command": "x"})
    return spawned


def test_apply_spawns_the_updater_and_answers_at_once(client, appliable):
    """The answer is the last thing this process does before stopping itself."""
    body = client.post("/api/system/update/apply").json()
    assert appliable == [1]
    assert body["started"] is True
    assert body["poll"] == "/api/version"
    assert body["pid"] == 99


def test_a_second_apply_is_refused_so_two_updaters_never_share_a_checkout(client, appliable):
    """Nothing downstream would notice the second one: both are detached, and
    both would fetch, stop and reset the same working tree against each other."""
    assert client.post("/api/system/update/apply").status_code == 200
    second = client.post("/api/system/update/apply")
    assert second.status_code == 409
    assert appliable == [1], "the second request must not spawn an updater"


def test_a_refused_spawn_releases_the_lane_again(client, appliable, monkeypatch):
    """A start that never happened must not block the next attempt forever."""
    def refuse(*a, **k):
        raise RuntimeError("must be started from a terminal")

    monkeypatch.setattr(sr, "spawn_update_process", refuse)
    assert client.post("/api/system/update/apply").status_code == 409
    monkeypatch.setattr(sr, "spawn_update_process",
                        lambda *a, **k: {"pid": 7, "log": "l", "via": "popen", "command": "c"})
    assert client.post("/api/system/update/apply").status_code == 200


def test_apply_refuses_when_an_earlier_update_did_not_finish(client, appliable, monkeypatch):
    monkeypatch.setattr(sr, "read_last_update", lambda: {"target_tag": "v9.9.9"})
    resp = client.post("/api/system/update/apply")
    assert resp.status_code == 409
    assert "--recover" in resp.json()["detail"]
    assert appliable == [], "nothing may start from a half-swapped checkout"


def test_apply_refuses_a_package_install(client, appliable, monkeypatch):
    monkeypatch.setattr(sr, "describe_update_ability",
                        lambda: (False, "This VAF was installed as a package"))
    resp = client.post("/api/system/update/apply")
    assert resp.status_code == 409
    assert "package" in resp.json()["detail"]
    assert appliable == []


def test_apply_refuses_while_a_repair_runs(client, appliable):
    sr._repair_state["running"] = True
    try:
        resp = client.post("/api/system/update/apply")
        assert resp.status_code == 409
        assert appliable == []
    finally:
        sr._repair_state["running"] = False


def test_apply_refuses_when_the_restart_would_not_come_back(client, appliable, monkeypatch):
    """Without the pidfile `vaf start` writes, the updater's stop step does
    nothing and its start step adds a SECOND server."""
    monkeypatch.setattr(sr, "_restart_blocker",
                        lambda: "This VAF was not started as a background service")
    resp = client.post("/api/system/update/apply")
    assert resp.status_code == 409
    assert "background service" in resp.json()["detail"]
    assert appliable == []


def test_a_refused_spawn_becomes_a_409_not_a_500(client, appliable, monkeypatch):
    """Server mode without systemd-run: the framework refuses rather than hand
    back a process that systemd will kill."""
    def refuse(*a, **k):
        raise RuntimeError("must be started from a terminal")

    monkeypatch.setattr(sr, "spawn_update_process", refuse)
    resp = client.post("/api/system/update/apply")
    assert resp.status_code == 409
    assert "terminal" in resp.json()["detail"]


# ── the blocker itself ───────────────────────────────────────────────────────

def test_server_mode_is_never_blocked(monkeypatch):
    import vaf.core.config as cfg
    monkeypatch.setattr(cfg.Config, "get",
                        staticmethod(lambda k, d=None: True if k == "server_mode" else d))
    assert sr._restart_blocker() is None


def test_desktop_mode_without_a_pidfile_is_blocked(monkeypatch, tmp_path):
    import vaf.core.config as cfg
    monkeypatch.setattr(cfg.Config, "get",
                        staticmethod(lambda k, d=None: False if k == "server_mode" else d))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))
    assert "background service" in (sr._restart_blocker() or "")


def test_desktop_mode_with_a_pidfile_is_fine(monkeypatch, tmp_path):
    import vaf.core.config as cfg
    monkeypatch.setattr(cfg.Config, "get",
                        staticmethod(lambda k, d=None: False if k == "server_mode" else d))
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))
    (tmp_path / ".vaf").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".vaf" / "server.pid").write_text("1234")
    assert sr._restart_blocker() is None
