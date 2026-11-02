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


_REAL_CHAT_QUEUE = health._chat_queue_metrics
_REAL_ENGINE_QUEUE = health._engine_queue_metrics


@pytest.fixture(autouse=True)
def _no_queues_of_other_tests(monkeypatch):
    """The queue registry is process-wide; queues an earlier test registered (the speech
    client's) would add a row to every collection here, and a queue registered here must
    not outlive the test. Each test gets an empty registry of its own. The chat queue and
    the engine's queue are probes of their own: off unless a test turns them on, and the
    TaskQueue singleton an earlier test left behind is dropped (order independence)."""
    from vaf.core import fair_queue
    from vaf.core.task_queue import TaskQueue
    monkeypatch.setattr(fair_queue, "_registry", {})
    monkeypatch.setattr(health, "_chat_queue_metrics", lambda: [])
    monkeypatch.setattr(health, "_engine_queue_metrics", lambda: [])
    monkeypatch.setattr(TaskQueue, "_instance", None)


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
    backup = lambda: {"name": "backup", "status": "ok", "reason": "fine"}    # noqa: E731
    result = health.collect_health(status_probe=lambda: _status(_svc("vaf-redis", "ok")),
                                   worker_probe=lambda: {"started": 1, "alive": 0},
                                   usage=usage, backup_probe=backup)
    assert result["status"] == "down"
    assert [c["name"] for c in result["checks"]] == ["services", "disk", "backup", "agent"]

    def broken():
        raise RuntimeError("docker exploded")

    result = health.collect_health(status_probe=broken, worker_probe=lambda: None, usage=usage,
                                   backup_probe=backup)
    assert result["status"] == "down" and "docker exploded" in result["checks"][0]["reason"]


def test_the_snapshot_runs_once_per_window(monkeypatch):
    """A poller must not make every request run Docker, and an expired result is never handed
    out. MUTATION: collect on every call, or serve the cache past its age - red."""
    import asyncio
    calls = []
    monkeypatch.setattr(health, "_snapshot", {})
    monkeypatch.setattr(health, "_inflight", None)
    collect = lambda: calls.append(1) or {"status": "ok", "checks": [], "n": len(calls)}  # noqa: E731
    asyncio.run(health.snapshot(max_age=60, collect=collect))
    asyncio.run(health.snapshot(max_age=60, collect=collect))
    assert len(calls) == 1
    assert asyncio.run(health.snapshot(max_age=0, collect=collect))["n"] == 2


def test_callers_during_a_slow_check_wait_for_the_one_collection(monkeypatch):
    """A Docker that does not answer makes a check take seconds; the callers meanwhile must
    share the one collection, not start their own or hold a worker thread each. MUTATION:
    start a collection per caller - red."""
    import asyncio
    monkeypatch.setattr(health, "_snapshot", {})
    monkeypatch.setattr(health, "_inflight", None)
    calls = []
    release = threading.Event()

    def slow():
        calls.append(1)
        release.wait(10)
        return {"status": "degraded", "checks": []}

    async def crowd():
        waiting = [asyncio.ensure_future(health.snapshot(max_age=60, collect=slow))
                   for _ in range(20)]
        await asyncio.sleep(0.2)
        release.set()
        return await asyncio.gather(*waiting)

    results = asyncio.run(crowd())
    assert len(calls) == 1
    assert all(r["status"] == "degraded" for r in results)


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
    async def _snapshot():
        return {"status": verdict, "checks": [{"name": "disk", "status": verdict,
                                               "reason": "/home/user/.vaf"}]}

    monkeypatch.setattr(health, "snapshot", _snapshot)
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


@pytest.mark.parametrize("answer, says", [
    ("refused", None),
    ("timeout", "does not answer"),
    (500, "answered HTTP 500"),
    ("html", "without a health result"),
])
def test_a_vaf_that_listens_is_never_called_not_running(monkeypatch, answer, says):
    """Only nothing listening is "not running"; an error status or no answer in time is a
    running VAF in trouble. MUTATION: return None for an error status again - red."""
    import requests

    from vaf.cli.cmd import health as health_cmd

    def _get(url, timeout=None):
        if answer == "refused":
            raise requests.ConnectionError("refused")
        if answer == "timeout":
            raise requests.ReadTimeout("slow")
        if answer == "html":
            return types.SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(
                ValueError("not json")))
        return types.SimpleNamespace(status_code=answer, json=lambda: {})

    monkeypatch.setattr(requests, "get", _get)
    result = health_cmd.from_running_vaf()
    if says is None:
        assert result is None
    else:
        assert result["status"] == "down" and says in result["checks"][0]["reason"]


def test_without_a_stack_the_containers_are_no_problem_but_the_host_is():
    """A pip install manages no container; their rows made it down forever (and `vaf repair
    --check` exit 1 while the repair said there is nothing to do). MUTATION: classify the rows
    without a stack root, or drop the host row with them - red."""
    status = _status(_svc("vaf-memory-db", "error"), _svc("vaf-redis", "unknown"),
                     forwarding_ok=False, root=None)
    assert [p["name"] for p in sh.stack_problems(status)] == ["host"]
    assert health.services_check(_status(_svc("vaf-memory-db", "error"), root=None)) == {
        "name": "services", "status": "ok", "reason": "No service stack on this installation."}


def test_the_engines_root_counts_only_where_it_exists(tmp_path):
    """Under Docker Desktop the root is a path in its VM; its nearest ancestor on the host is
    an unrelated disk. MUTATION: add the root unchecked - red."""
    here = tmp_path / "docker"
    here.mkdir()
    status = {"docker": {"root_dir": str(here)}}
    assert here in health.disk_paths(status)
    status = {"docker": {"root_dir": str(tmp_path / "in-a-vm" / "var" / "lib" / "docker")}}
    assert not any("in-a-vm" in str(p) for p in health.disk_paths(status))


def test_a_caller_that_hangs_up_does_not_cancel_the_others(monkeypatch):
    """wrap_future passes a cancelled waiter on to the shared future: one monitor hanging up
    cancelled the collection every other caller waited for. MUTATION: leave the future
    pending instead of running - red."""
    import asyncio
    monkeypatch.setattr(health, "_snapshot", {})
    monkeypatch.setattr(health, "_inflight", None)
    release = threading.Event()

    def slow():
        release.wait(10)
        return {"status": "ok", "checks": []}

    async def scene():
        quitter = asyncio.ensure_future(health.snapshot(max_age=60, collect=slow))
        stayer = asyncio.ensure_future(health.snapshot(max_age=60, collect=slow))
        await asyncio.sleep(0.1)
        quitter.cancel()
        await asyncio.sleep(0.1)
        release.set()
        return await asyncio.wait_for(stayer, 10)

    assert asyncio.run(scene())["status"] == "ok"


# ── the fair queues ──────────────────────────────────────────────────────────────

def _quiet_checks():
    usage = lambda p: types.SimpleNamespace(free=40 * GB)                  # noqa: E731
    return dict(status_probe=lambda: _status(_svc("vaf-redis", "ok")),
                worker_probe=lambda: None, usage=usage,
                backup_probe=lambda: {"name": "backup", "status": "ok", "reason": "fine"},
                engine_probe=lambda: None)


def _hold_and_refuse(q, reason):
    """One served request, then a refusal of the given kind while the slot is held."""
    from vaf.core.fair_queue import QueueBusy
    with q.slot("tenant-a"):
        pass
    held, release = threading.Event(), threading.Event()

    def hold():
        with q.slot("tenant-a"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(5)
    gone = threading.Event()
    gone.set()
    try:
        with q.slot("tenant-b", deadline_s=0.05 if reason == "timeout" else None,
                    cancelled=gone.is_set if reason == "cancelled" else None, poll_s=0.01):
            pass
    except QueueBusy as busy:
        assert busy.reason == reason
    release.set()
    t.join(5)


@pytest.mark.parametrize("reason, expected", [("timeout", "degraded"), ("cancelled", "ok")])
def test_queues_row_degraded_after_refusal(reason, expected):
    """Somebody turned away (timeout, full) in the window degrades; a hang-up does not.
    MUTATION: count cancelled as degraded - red."""
    from vaf.core import speech_client
    q = speech_client.speech_queue("stt")
    _hold_and_refuse(q, reason)
    result = health.collect_health(**_quiet_checks())
    row = next(c for c in result["checks"] if c["name"] == "queues")
    assert row["status"] == expected
    assert "stt: 0 waiting, 2 served" in row["reason"]
    # A degraded row says why first; a hang-up is no reason to.
    assert row["reason"].startswith("1 turned away") == (expected == "degraded")
    # Both causes of "full": the whole queue, or one person's share of it.
    assert ("share" in row["reason"]) == (expected == "degraded")
    assert [m["name"] for m in row["metrics"]] == ["stt"]
    assert row["metrics"][0]["refused"][reason] == 1
    assert result["status"] == expected


def test_no_queues_no_row():
    """A process that registered no queue reports none (the agent-row pattern).
    MUTATION: always add the row - red."""
    result = health.collect_health(**_quiet_checks())
    assert "queues" not in [c["name"] for c in result["checks"]]


def test_public_health_stays_word_only(monkeypatch):
    """The queue numbers are for an admin: /api/health still answers the word alone.
    MUTATION: put the metrics into /api/health - red."""
    from vaf.core import speech_client
    q = speech_client.speech_queue("tts")
    _hold_and_refuse(q, "timeout")

    async def _snapshot():
        return health.collect_health(**_quiet_checks())

    monkeypatch.setattr(health, "snapshot", _snapshot)
    answer = TestClient(_public_app(), client=PROXY_PEER).get(
        "/api/health", headers={"X-Forwarded-For": LAN_DEVICE})
    assert answer.json() == {"status": "degraded"}


def test_vaf_health_json_carries_the_queue_numbers(monkeypatch):
    import json
    from vaf.core import speech_client
    app, health_cmd = _health_app()
    q = speech_client.speech_queue("stt")
    _hold_and_refuse(q, "timeout")
    monkeypatch.setattr(health_cmd, "from_running_vaf",
                        lambda timeout=30.0: health.collect_health(**_quiet_checks()))
    out = CliRunner().invoke(app, ["--json"])
    assert out.exit_code == 1
    row = next(c for c in json.loads(out.output)["checks"] if c["name"] == "queues")
    assert row["metrics"][0]["name"] == "stt" and row["metrics"][0]["refused"]["timeout"] == 1
    text = CliRunner().invoke(app, [])
    assert "queues" in text.output and "stt: 0 waiting" in text.output


# ── the chat queue and the engine's queue ────────────────────────────────────────

def _runner_with_loops(monkeypatch, status):
    import sys
    monkeypatch.setitem(sys.modules, "vaf.core.headless_runner",
                        types.SimpleNamespace(worker_status=lambda: status))


def test_the_chat_queue_joins_the_row(monkeypatch):
    """In the process that runs the turns, the queues row carries a `chat` entry, and a
    waiting turn never degrades it; elsewhere (no agent loops) it has none.
    MUTATION: drop the worker_status check - the terminal reports a chat queue, red."""
    from vaf.core.task_queue import TaskQueue
    monkeypatch.setattr(health, "_chat_queue_metrics", _REAL_CHAT_QUEUE)
    tq = TaskQueue()
    tq._legacy_mode = True
    tq.add(session_id="chat-1", input_text="x", source="web",
           metadata={"enqueue_user_scope_id": "ab12cd34-tenant"})
    tq.add(session_id="chat-2", input_text="y", source="web",
           metadata={"enqueue_user_scope_id": "ab12cd34-tenant"})
    assert tq.get(timeout=0.0, worker_id="w") is not None
    _runner_with_loops(monkeypatch, {"started": 1, "alive": 1})
    row = next(c for c in health.collect_health(**_quiet_checks())["checks"]
               if c["name"] == "queues")
    assert row["status"] == "ok"
    assert [m["name"] for m in row["metrics"]] == ["chat"]
    assert "chat: 1 waiting" in row["reason"] and "ab12cd34" not in row["reason"]
    _runner_with_loops(monkeypatch, None)
    result = health.collect_health(**_quiet_checks())
    assert "queues" not in [c["name"] for c in result["checks"]]


METRICS_PAGE = """\
# HELP vllm:num_requests_waiting Number of requests waiting to be processed.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{engine="0",model_name="org/model"} 2.0
vllm:num_requests_waiting{engine="1",model_name="org/model"} 1.0
# TYPE vllm:num_requests_waiting_by_reason gauge
vllm:num_requests_waiting_by_reason{engine="0",model_name="org/model",reason="capacity"} 5.0
vllm:num_requests_waiting_by_reason{engine="1",model_name="org/model",reason="deferred"} 4.0
vllm:num_requests_running{engine="0",model_name="org/model"} 1.0
vllm:num_requests_running{engine="1",model_name="org/model"} 1.0
vllm:request_queue_time_seconds_sum{engine="0",model_name="org/model"} 2.0
vllm:request_queue_time_seconds_sum{engine="1",model_name="org/model"} 1.0
vllm:request_queue_time_seconds_count{engine="0",model_name="org/model"} 3.0
vllm:request_queue_time_seconds_count{engine="1",model_name="org/model"} 1.0
vllm:request_queue_time_seconds_bucket{engine="0",model_name="org/model",le="0.3"} 3.0
"""


def test_vllm_waiting_is_read_exactly():
    """Summed over both engines; the `..._by_reason` gauge, whose name starts with the
    waiting gauge's, is not counted; the mean queue time is since the start.
    MUTATION: a prefix match on the gauge name - 12 waiting instead of 3, red."""
    from vaf.core import vllm_server
    assert vllm_server.parse_request_counts(METRICS_PAGE) == {
        "waiting": 3, "running": 2, "queue_time_mean_ms": 750}
    before_first = "vllm:num_requests_waiting 0\nvllm:num_requests_running 0\n"
    assert vllm_server.parse_request_counts(before_first) == {
        "waiting": 0, "running": 0, "queue_time_mean_ms": None}
    assert vllm_server.parse_request_counts("# no gauges\nprocess_cpu_seconds_total 1\n") is None


def test_no_metrics_request_without_a_running_vllm(monkeypatch):
    """No HTTP request at all unless vLLM is the engine, the profile allows local inference
    and its server process is there; then one, and its counts never degrade the row.
    MUTATION: probe /metrics when the pid is absent - red."""
    import requests

    from vaf.core import inference_profile, local_engine, vllm_server
    monkeypatch.setattr(health, "_engine_queue_metrics", _REAL_ENGINE_QUEUE)
    asked = []
    real_get = requests.get

    def get(url, *a, **k):
        if not str(url).endswith("/metrics"):
            return real_get(url, *a, **k)
        asked.append(url)
        return types.SimpleNamespace(status_code=200, text=METRICS_PAGE)

    monkeypatch.setattr(requests, "get", get)
    state = {"vllm": False, "local": True, "pid": 4321}
    monkeypatch.setattr(local_engine, "is_vllm", lambda config=None: state["vllm"])
    monkeypatch.setattr(inference_profile, "local_inference_allowed",
                        lambda config=None: state["local"])
    monkeypatch.setattr(vllm_server, "running_pid", lambda: state["pid"])

    assert health._engine_queue_metrics() == [] and asked == []       # llama.cpp
    state.update(vllm=True, pid=None)
    assert health._engine_queue_metrics() == [] and asked == []       # vLLM not running
    state.update(pid=4321, local=False)
    assert health._engine_queue_metrics() == [] and asked == []       # the api profile
    state.update(local=True)
    entry = health._engine_queue_metrics()
    assert len(asked) == 1
    assert entry == [{"name": "vllm", "kind": "engine", "waiting": 3, "running": 2,
                      "queue_time_mean_ms": 750}]
    row = next(c for c in health.collect_health(**_quiet_checks())["checks"]
               if c["name"] == "queues")
    assert row["status"] == "ok"
    assert row["reason"] == "vllm: 3 waiting, 2 running, queue time mean 750 ms since start"


def test_vaf_health_prints_chat_and_vllm(monkeypatch):
    """`vaf health` prints both lines as the row's reason, and --json carries the entries."""
    import json

    from vaf.core.task_queue import TaskQueue
    app, health_cmd = _health_app()
    chat = TaskQueue().metrics()
    chat["waiting"], chat["running"] = 2, 1
    engine = {"name": "vllm", "kind": "engine", "waiting": 4, "running": 2,
              "queue_time_mean_ms": 1500}
    monkeypatch.setattr(health_cmd, "from_running_vaf", lambda timeout=30.0: health.collect_health(
        **_quiet_checks(), queue_probe=lambda: [chat, engine]))
    out = CliRunner().invoke(app, [])
    assert out.exit_code == 0
    assert "chat: 2 waiting" in out.output
    assert "vllm: 4 waiting, 2 running, queue time mean 1.5 s since start" in out.output
    as_json = CliRunner().invoke(app, ["--json"])
    row = next(c for c in json.loads(as_json.output)["checks"] if c["name"] == "queues")
    assert [m["name"] for m in row["metrics"]] == ["chat", "vllm"]
