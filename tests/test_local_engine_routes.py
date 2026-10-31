# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The web half of the local engine: /api/system/local-engine (GET, POST as a job, DELETE),
the save lanes that never carry the engine, the wizard's verdict in needs-setup, the
services snapshot's engine row and the banner's Cancel for a download another socket did not
start.

The routes call local_engine (state, validate, apply, uninstall_vllm) and have no step of
their own; the core is faked here, its own tests are tests/test_local_engine_apply.py.

MUTATIONS, each turns the named test red:
- call Config.set in the route instead of apply -> test_routes_call_the_core;
- claim the job slot before validate -> test_bad_request_400_before_job;
- drop require_admin from the DELETE -> test_admin_only;
- remove the strip from patch_config, or from save_config -> test_save_lanes_drop_engine_keys;
- drop the admin check of the shared cancel -> test_ws_cancel_admin_only;
- send the verdict's params in needs-setup -> test_needs_setup_vllm_support;
- leave the engine row out of the snapshot -> test_services_snapshot_has_engine_row.
"""
import asyncio
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vaf.api.system_routes as sr
from vaf.core import container_jobs
from vaf.core import local_engine as le


def _reset():
    sr._engine_state.update({"running": False, "started_at": None, "finished_at": None,
                             "engine": None, "model": None, "phase": None, "step": 0,
                             "steps": 0, "result": None, "error": None})


@pytest.fixture
def client(monkeypatch):
    _reset()
    monkeypatch.setattr(container_jobs, "_running", None)
    app = FastAPI()
    app.include_router(sr.router)
    app.dependency_overrides[sr.require_admin] = lambda: {"username": "u", "role": "admin"}
    monkeypatch.setattr(le, "state", lambda model=None: {"engine": "llama", "preview": model})
    return TestClient(app)


def _follow(client):
    for _ in range(250):
        job = client.get("/api/system/local-engine").json()["job"]
        if not job["running"]:
            return job
        time.sleep(0.02)
    raise AssertionError("the switch never finished")


def test_admin_only(monkeypatch):
    """Every method answers 403 to a signed-in account that is no admin, and none of the
    paths is exempt from the session check (no session: the middleware's 401)."""
    from vaf.auth.middleware import _is_auth_exempt
    monkeypatch.setattr(container_jobs, "_running", None)
    monkeypatch.setattr(le, "state", lambda model=None: pytest.fail("state for a non-admin"))
    monkeypatch.setattr(le, "uninstall_vllm", lambda: pytest.fail("uninstall for a non-admin"))
    monkeypatch.setattr(le, "validate", lambda *a, **k: pytest.fail("validate for a non-admin"))
    app = FastAPI()

    @app.middleware("http")
    async def _user(request, call_next):
        request.state.user = {"username": "bob", "role": "user",
                              "user_scope_id": "ab12cd34-0000-0000-0000-000000000001"}
        return await call_next(request)
    app.include_router(sr.router)
    c = TestClient(app)
    assert c.get("/api/system/local-engine").status_code == 403
    assert c.post("/api/system/local-engine", json={"engine": "vllm"}).status_code == 403
    assert c.delete("/api/system/local-engine/vllm").status_code == 403
    assert not _is_auth_exempt("/api/system/local-engine")
    assert not _is_auth_exempt("/api/system/local-engine/vllm")


def test_routes_call_the_core(client, monkeypatch):
    """GET is local_engine.state (with the preview model), POST runs local_engine.apply with
    the install log as its output, and the job reports its EngineSwitch."""
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "set", staticmethod(lambda k, v: pytest.fail("the route wrote config")))
    body = client.get("/api/system/local-engine?model=Qwen/Qwen3-8B-AWQ").json()
    assert body["engine"] == "llama" and body["preview"] == "Qwen/Qwen3-8B-AWQ"
    assert body["job"]["running"] is False
    calls = []

    def apply(engine, model=None, install=False, progress=None, output=None, log=None):
        calls.append((engine, model, install, output.endswith("vllm_install.log")))
        progress({"phase": "download_model", "step": 3, "steps": 6})
        return le.EngineSwitch("done", "switched_vllm", "The local engine is vLLM.", "vllm",
                               "Qwen/Qwen3-8B-AWQ")
    monkeypatch.setattr(le, "validate", lambda engine, model=None: "vllm")
    monkeypatch.setattr(le, "apply", apply)
    answer = client.post("/api/system/local-engine",
                         json={"engine": "vllm", "model": "Qwen/Qwen3-8B-AWQ", "install": True})
    assert answer.status_code == 202 and answer.json()["engine"] == "vllm"
    job = _follow(client)
    assert calls == [("vllm", "Qwen/Qwen3-8B-AWQ", True, True)]
    assert job["result"]["code"] == "switched_vllm" and job["phase"] == "download_model"
    assert (job["step"], job["steps"]) == (3, 6)
    removed = {"ok": True, "code": "removed", "text": "Removed.", "kept_model": "a/b",
               "kept_bytes": 5, "kept_path": "/hf"}
    monkeypatch.setattr(le, "uninstall_vllm", lambda: removed)
    assert client.delete("/api/system/local-engine/vllm").json() == removed
    monkeypatch.setattr(le, "uninstall_vllm", lambda: {"ok": False, "code": "engine_active",
                                                       "text": "vLLM is the local engine."})
    refused = client.delete("/api/system/local-engine/vllm")
    assert refused.status_code == 400 and refused.json()["code"] == "engine_active"
    assert container_jobs.running() is None, "the uninstall gave its slot back"


def test_bad_request_400_before_job(client, monkeypatch):
    """A wrong request is the core's refusal with its code, before any job or slot."""
    monkeypatch.setattr(le, "support", lambda **kw: le.EngineSupport(le.LATER, "AMD here.", "amd"))
    monkeypatch.setattr(le, "apply", lambda *a, **k: pytest.fail("a wrong request reached apply"))
    for body, code in (({"engine": "vulkan"}, "unknown_engine"),
                       ({"engine": "vllm"}, "amd"),
                       ({"engine": "vllm", "model": "no-slash"}, "bad_repo")):
        answer = client.post("/api/system/local-engine", json=body)
        assert answer.status_code == 400 and answer.json()["code"] == code, answer.json()
    assert container_jobs.claim(container_jobs.REPAIR) is None, "a refusal kept the slot"
    container_jobs.release(container_jobs.REPAIR)
    assert sr._engine_state["running"] is False


def test_409_while_profile_switch(client, monkeypatch):
    """One container job at a time: not beside a profile switch, and a running switch keeps
    a second one, a repair and the uninstall out."""
    monkeypatch.setattr(le, "validate", lambda engine, model=None: engine)
    assert container_jobs.claim(container_jobs.PROFILE_SWITCH) is None
    answer = client.post("/api/system/local-engine", json={"engine": "llama"})
    assert answer.status_code == 409 and "profile switch" in answer.json()["detail"]
    container_jobs.release(container_jobs.PROFILE_SWITCH)
    release = {"go": False}

    def slow(engine, **kw):
        deadline = time.time() + 5
        while not release["go"] and time.time() < deadline:
            time.sleep(0.01)
        return le.EngineSwitch("done", "switched_llama", "llama.cpp.", "llama")
    monkeypatch.setattr(le, "apply", slow)
    try:
        assert client.post("/api/system/local-engine", json={"engine": "llama"}).status_code == 202
        assert client.post("/api/system/local-engine", json={"engine": "llama"}).status_code == 409
        assert client.post("/api/system/services/repair").status_code == 409
        assert client.delete("/api/system/local-engine/vllm").status_code == 409
    finally:
        release["go"] = True


def test_job_follows_to_result(client, monkeypatch):
    """A crash is reported as the job's error, never swallowed, and the slot comes back."""
    monkeypatch.setattr(le, "validate", lambda engine, model=None: engine)

    def boom(engine, **kw):
        raise RuntimeError("pip exploded")
    monkeypatch.setattr(le, "apply", boom)
    assert client.post("/api/system/local-engine", json={"engine": "vllm"}).status_code == 202
    job = _follow(client)
    assert "pip exploded" in job["error"] and job["finished_at"]
    assert container_jobs.running() is None


def test_save_lanes_drop_engine_keys(monkeypatch):
    """A page loaded before a switch sends `local_engine: llama` with its next Save; the
    stored vllm stays. Both lanes: PATCH /api/config here, the WebSocket save by its source
    (its handler is inline in the socket loop, like the profile strip's guard)."""
    import inspect

    from vaf.api import config_routes
    from vaf.core import web_server
    from vaf.core.config import Config
    state = {"local_engine": "vllm", "vllm_model": "Qwen/Qwen3-8B-AWQ", "server_mode": False}
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: dict(state)))
    monkeypatch.setattr(Config, "save", classmethod(
        lambda cls, cfg: (state.clear(), state.update(cfg))[0]))
    user = {"role": "admin", "user_scope_id": "ab12cd34-0000-0000-0000-000000000000",
            "username": "Alice"}
    asyncio.run(config_routes.patch_config(
        {"local_engine": "llama", "vllm_model": "auto", "tts_auto_speak": True},
        types.SimpleNamespace(state=types.SimpleNamespace(user=user)), user))
    assert state["local_engine"] == "vllm" and state["vllm_model"] == "Qwen/Qwen3-8B-AWQ"
    assert state["tts_auto_speak"] is True
    source = inspect.getsource(web_server)
    lane = source[source.index('elif type == "save_config":'):]
    lane = lane[:lane.index("Config.split_connection_toggles")]
    assert "new_config = without_engine_keys(new_config)" in lane


def test_ws_cancel_admin_only(monkeypatch):
    """The banner's Cancel: a socket's own download through its event; a download it did
    not start (the automatic GGUF, a vLLM model) only for an admin."""
    import threading

    from vaf.core import web_server
    from vaf.core.model_download_state import MODEL_DOWNLOAD
    event = threading.Event()
    monkeypatch.setitem(web_server._active_model_download_cancels, 101, event)
    assert web_server.cancel_model_download(101, is_admin=False) == "own" and event.is_set()
    MODEL_DOWNLOAD.start("Qwen/Qwen3-4B-AWQ", "Qwen3-4B-AWQ")
    try:
        assert web_server.cancel_model_download(202, is_admin=False) == "refused"
        assert MODEL_DOWNLOAD.cancel_requested is False
        assert web_server.cancel_model_download(202, is_admin=True) == "shared"
        assert MODEL_DOWNLOAD.cancel_requested is True
    finally:
        MODEL_DOWNLOAD.finish()
        MODEL_DOWNLOAD.start("", "")
        MODEL_DOWNLOAD.finish()
    assert web_server.cancel_model_download(202, is_admin=True) == "none"


def test_needs_setup_vllm_support(monkeypatch):
    """The wizard learns the verdict's class (level and code), never the GPU's name or
    numbers: the answer needs no session. Under the api profile it is not sent."""
    from vaf.api import auth_routes
    from vaf.core import inference_profile
    monkeypatch.setattr(auth_routes.account_seal, "admin_established", lambda: False)

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Db:
        async def execute(self, *a, **k):
            return _Result()

    class _Ctx:
        async def __aenter__(self):
            return _Db()

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(auth_routes, "get_auth_db", lambda: _Ctx())
    monkeypatch.setattr(auth_routes, "_setup_code_required", lambda request: False)
    calls = []
    import vaf.core.gpu_detection as gd
    monkeypatch.setattr(gd, "get_primary_gpu", lambda: None)
    monkeypatch.setattr(le, "_SUPPORT_CLASS", None)
    monkeypatch.setattr(le, "_SUPPORT_RETRY_AT", 0.0)
    monkeypatch.setattr(le, "support", lambda **kw: calls.append(1) or le.EngineSupport(
        le.SUPPORTED, "vLLM runs here (NVIDIA RTX 3080, 10 GB).", "supported",
        {"gpu": "RTX 3080", "vram_gb": "10"}))
    monkeypatch.setattr(inference_profile, "current", lambda config=None: "local")
    request = types.SimpleNamespace()
    answer = asyncio.run(auth_routes.needs_setup(request))
    assert answer["vllm_support"] == {"level": "supported", "code": "supported"}
    assert "RTX" not in str(answer) and "10" not in str(answer["vllm_support"])
    again = asyncio.run(auth_routes.needs_setup(request))
    assert calls == [1], "an unauthenticated poll started the GPU probes again"
    assert again["vllm_support"] == {"level": "supported", "code": "supported"}, \
        "a conclusive verdict was not kept"
    monkeypatch.setattr(inference_profile, "current", lambda config=None: "api")
    assert "vllm_support" not in asyncio.run(auth_routes.needs_setup(request))
    # A detection that fails leaves the field out; it never turns setup into a 503.
    monkeypatch.setattr(inference_profile, "current", lambda config=None: "local")
    monkeypatch.setattr(le, "_SUPPORT_CLASS", None)
    monkeypatch.setattr(le, "_SUPPORT_RETRY_AT", 0.0)
    monkeypatch.setattr(le, "support", lambda **kw: (_ for _ in ()).throw(OSError("no lspci")))
    answer = asyncio.run(auth_routes.needs_setup(request))
    assert answer["needs_setup"] is True and "vllm_support" not in answer


def test_services_snapshot_has_engine_row():
    """Update and Repair's node reads the snapshot's `local_engine` row (both branches),
    and nothing in the repair reads it as a service."""
    from vaf.core import service_health as sh
    row = {"engine": "vllm", "state": "degraded", "code": "not_installed", "params": {},
           "text": "not installed", "version": "", "model": ""}
    down = sh.collect_service_status(
        daemon_probe=lambda: {"ok": False, "reason": "down"}, host_probe=lambda: None,
        engine_probe=lambda: row)
    assert down["local_engine"] == row
    assert all(s.get("service_key") != "local_engine" for s in down["services"])
    up = sh.collect_service_status(
        daemon_probe=lambda: {"ok": True}, inspect_probe=lambda names: [],
        port_reader=lambda spec: None, service_probe=lambda spec, port: None,
        host_probe=lambda: None, sandbox_probe=lambda: None,
        browser_pool_probe=lambda: None, start_probe=lambda: False,
        engine_probe=lambda: row)
    assert up["local_engine"] == row


def test_engine_row_never_raises(monkeypatch):
    from vaf.core import service_health as sh
    monkeypatch.setattr(le, "status_row", lambda config=None: 1 / 0)
    row = sh.probe_local_engine()
    assert row["state"] == "unknown" and "division" in row["text"]


def test_ws_cancel_call_site_asks_the_shared_admin_rule():
    """The handler hands the socket's admin answer of `connection_is_admin` to the cancel, not
    a hand-rolled copy (a constant there would keep every behaviour test green)."""
    import inspect

    from vaf.core import web_server
    source = inspect.getsource(web_server)
    lane = source[source.index('elif type == "cancel_model_download":'):]
    lane = lane[:lane.index("elif type ==", 10)]
    assert "manager.connection_is_admin(websocket)" in lane
    assert "is_admin_identity(" not in lane


def test_a_finished_job_has_its_end_time_before_it_reads_finished():
    """A poll that sees running False must find finished_at set."""
    import inspect
    body = inspect.getsource(sr._run_engine_switch)
    tail = body[body.index("finally:"):]
    assert tail.index('_engine_state["finished_at"]') < tail.index('_engine_state["running"] = False')
