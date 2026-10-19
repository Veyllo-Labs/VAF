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
    sr._profile_state.update({
        "running": False, "started_at": None, "finished_at": None,
        "profile": None, "result": None, "error": None,
    })
    monkeypatch.setattr(sr, "_update_started_at", None, raising=False)
    from vaf.core import container_jobs
    monkeypatch.setattr(container_jobs, "_running", None)
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
    monkeypatch.setattr(sr, "read_update_result", lambda: None)
    monkeypatch.setattr(sr, "describe_update_ability", lambda: (True, ""))
    body = client.get("/api/system/update").json()
    assert body["cache"]["latest_version"] == "9.9.9"
    assert body["can_apply"] is True
    assert body["current"]
    assert body["last_result"] is None


def test_update_state_surfaces_an_unfinished_update(client, monkeypatch):
    monkeypatch.setattr(sr, "read_update_cache", lambda: None)
    monkeypatch.setattr(sr, "read_last_update", lambda: {"target_tag": "v9.9.9"})
    monkeypatch.setattr(sr, "read_update_result", lambda: None)
    monkeypatch.setattr(sr, "describe_update_ability", lambda: (True, ""))
    assert client.get("/api/system/update").json()["last_update"]["target_tag"] == "v9.9.9"


def test_update_state_carries_the_last_runs_outcome(client, monkeypatch):
    """A rollback restarts the OLD version, which from outside looks exactly
    like "nothing happened". The recorded outcome is how the waiting dialog
    tells the difference, so the state endpoint must pass it through."""
    monkeypatch.setattr(sr, "read_update_cache", lambda: None)
    monkeypatch.setattr(sr, "read_last_update", lambda: None)
    monkeypatch.setattr(sr, "read_update_result",
                        lambda: {"outcome": "rolled_back", "from_version": "1.0.0",
                                 "target_version": "9.9.9",
                                 "finished_at": "2026-08-01T00:00:00+00:00",
                                 "error": "pip exploded"})
    monkeypatch.setattr(sr, "describe_update_ability", lambda: (True, ""))
    body = client.get("/api/system/update").json()
    assert body["last_result"]["outcome"] == "rolled_back"
    assert body["last_result"]["error"] == "pip exploded"


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
    # Server clock, same clock the updater stamps its outcome file with: the
    # client keeps it to accept only THIS run's outcome later.
    assert body["started_at"]


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
    from vaf.core import container_jobs
    assert container_jobs.claim(container_jobs.REPAIR) is None
    try:
        resp = client.post("/api/system/update/apply")
        assert resp.status_code == 409
        assert appliable == []
    finally:
        container_jobs.release(container_jobs.REPAIR)


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


def test_desktop_mode_with_no_identifiable_instance_is_blocked(monkeypatch, tmp_path):
    """No service pid record, no instance record, nothing in the process table:
    the stop step would do nothing and the start step would add a second
    server, so refuse. The table lookup is patched, or the developer's own
    running VAF would answer."""
    import vaf.core.config as cfg
    import vaf.core.instance as inst
    monkeypatch.setattr(cfg.Config, "get",
                        staticmethod(lambda k, d=None: False if k == "server_mode" else d))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(inst, "find_running", lambda: None)
    assert "vaf update" in (sr._restart_blocker() or "")


def test_desktop_mode_with_a_recorded_desktop_app_is_fine(monkeypatch, tmp_path):
    """The desktop launch (app shortcut, run_vaf.sh, a bare tray) writes no
    service pid file and was refused for it on every platform. The running VAF
    records itself now, so the updater can stop it and bring it back windowed."""
    import vaf.core.config as cfg
    import vaf.core.instance as inst
    monkeypatch.setattr(cfg.Config, "get",
                        staticmethod(lambda k, d=None: False if k == "server_mode" else d))
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(inst, "find_running", lambda: inst.Instance(pid=1234, mode="tray"))
    assert sr._restart_blocker() is None


def test_a_service_found_only_in_the_process_table_is_fine(monkeypatch, tmp_path):
    """Started by a version that kept no record: still identifiable by the
    singleton port it holds."""
    import vaf.core.config as cfg
    import vaf.core.instance as inst
    monkeypatch.setattr(cfg.Config, "get",
                        staticmethod(lambda k, d=None: False if k == "server_mode" else d))
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(inst, "find_running",
                        lambda: inst.Instance(pid=1234, mode="headless", recorded=False))
    assert sr._restart_blocker() is None


def test_desktop_mode_with_a_pidfile_is_fine(monkeypatch, tmp_path):
    import vaf.core.config as cfg
    monkeypatch.setattr(cfg.Config, "get",
                        staticmethod(lambda k, d=None: False if k == "server_mode" else d))
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))
    (tmp_path / ".vaf").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".vaf" / "service.pid").write_text("1234")
    assert sr._restart_blocker() is None


# ── the inference profile ────────────────────────────────────────────────────

def _follow_switch(client):
    for _ in range(200):
        state = client.get("/api/system/inference-profile").json()["switch"]
        if not state["running"]:
            return state
        time.sleep(0.02)
    raise AssertionError("the switch never finished")


def test_the_profile_is_read_and_switched_through_the_framework(client, monkeypatch):
    """The web switch is `vaf profile`'s implementation, not a second one. MUTATION: write
    the config key here instead of calling inference_profile.apply - red."""
    from vaf.core import inference_profile
    monkeypatch.setattr(inference_profile, "current", lambda config=None: "api")
    monkeypatch.setattr(inference_profile, "conflicts", lambda config=None: ["one"])
    body = client.get("/api/system/inference-profile").json()
    assert body["profile"] == "api" and "stt" in body["left_out"] and body["conflicts"] == ["one"]
    calls = []
    monkeypatch.setattr(inference_profile, "apply",
                        lambda profile, prune=False, stack=True: calls.append((profile, prune))
                        or {"profile": profile, "errors": []})
    assert client.post("/api/system/inference-profile",
                       json={"profile": "api", "prune": True}).status_code == 202
    state = _follow_switch(client)
    assert calls == [("api", True)]
    assert state["result"] == {"profile": "api", "errors": []} and state["error"] is None


def test_an_unknown_profile_is_a_400_not_a_write(client, monkeypatch):
    """Refused before the job starts, by the framework's own check. MUTATION: drop the
    validate call - the job starts and the answer is a 202."""
    from vaf.core import inference_profile
    monkeypatch.setattr(inference_profile, "apply",
                        lambda *a, **k: pytest.fail("a wrong request reached apply"))
    answer = client.post("/api/system/inference-profile", json={"profile": "cloud"})
    assert answer.status_code == 400 and "Unknown profile" in answer.json()["detail"]
    answer = client.post("/api/system/inference-profile", json={"profile": "local", "prune": True})
    assert answer.status_code == 400 and sr._profile_state["running"] is False


def test_a_switch_is_a_job_so_a_slow_stack_start_outlives_the_proxy(client, monkeypatch):
    """Back to Full runs `compose up`, which pulls the 7.5 GB speech image after a prune;
    the HTTPS proxy gives up after 60 seconds. The POST must answer before the work ends,
    and a second switch must not start a second `compose up` beside it. MUTATION: run
    apply inside the request again, or drop the running check - red."""
    from vaf.core import inference_profile
    release = {"go": False}

    def slow_apply(profile, prune=False, stack=True):
        deadline = time.time() + 5      # bounded: a regression fails, it does not hang
        while not release["go"] and time.time() < deadline:
            time.sleep(0.01)
        return {"profile": profile, "errors": []}

    monkeypatch.setattr(inference_profile, "apply", slow_apply)
    try:
        assert client.post("/api/system/inference-profile",
                           json={"profile": "local"}).status_code == 202
        assert client.get("/api/system/inference-profile").json()["switch"]["running"] is True
        assert client.post("/api/system/inference-profile",
                           json={"profile": "api"}).status_code == 409
        assert client.post("/api/system/services/repair").status_code == 409
    finally:
        release["go"] = True
    assert _follow_switch(client)["result"]["profile"] == "local"


def test_no_switch_starts_while_a_repair_runs(client, monkeypatch):
    """A repair and a switch drive the same containers. MUTATION: drop the repair check in
    the switch - red."""
    from vaf.core import inference_profile
    monkeypatch.setattr(inference_profile, "apply",
                        lambda *a, **k: pytest.fail("a switch started beside a repair"))
    from vaf.core import container_jobs
    assert container_jobs.claim(container_jobs.REPAIR) is None
    answer = client.post("/api/system/inference-profile", json={"profile": "api"})
    assert answer.status_code == 409 and "repair" in answer.json()["detail"]


def test_a_crashing_switch_is_reported_not_swallowed(client, monkeypatch):
    from vaf.core import inference_profile

    def boom(profile, prune=False, stack=True):
        raise RuntimeError("docker exploded")

    monkeypatch.setattr(inference_profile, "apply", boom)
    assert client.post("/api/system/inference-profile",
                       json={"profile": "api"}).status_code == 202
    state = _follow_switch(client)
    assert "docker exploded" in state["error"] and state["result"] is None
