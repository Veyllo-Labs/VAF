# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""/api/sandbox: the caller's sandbox environments for Settings, Connections.

Each person sees and acts on their own; only an admin may ask for everybody's (?all=1)
and stop or delete another person's. Nothing here runs code in an environment."""
import types

import pytest

from vaf.core import environments as envmod


class _Mgr:
    def __init__(self):
        self.calls = []

    def list(self, scope, everyone=False):
        self.calls.append(("list", scope, everyone))
        return [envmod.Environment(id="0a1b2c3d", kind="temporary", network="none", owner="h1",
                                   container="c", volume="v", net="n", state="running",
                                   expires=2e9, name="try")]

    def processes(self, scope):
        return [{"handle": "e-0a1b2c3d-p12345678", "env": "0a1b2c3d", "command": "vite",
                 "state": "running", "started": 1.0}]

    def stop(self, scope, env_id, admin=False):
        self.calls.append(("stop", scope, env_id, admin))
        return types.SimpleNamespace(id=env_id)

    def delete(self, scope, env_id, admin=False):
        self.calls.append(("delete", scope, env_id, admin))
        if env_id == "missing":
            raise envmod.EnvironmentRefused("no environment 'missing'")
        return types.SimpleNamespace(id=env_id)


def _client(monkeypatch, role, scope="scope-alice"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from vaf.api.sandbox_routes import router
    import vaf.core.service_stack as stack
    import vaf.core.config as cfg
    mgr = _Mgr()
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: mgr)
    monkeypatch.setattr(stack, "is_docker_daemon_running", lambda: True)
    monkeypatch.setattr(cfg, "is_admin_identity", lambda r, s: r == "admin")
    app = FastAPI()

    @app.middleware("http")
    async def _as(request, call_next):
        request.state.user = {"user_scope_id": scope, "username": "alice", "role": role}
        return await call_next(request)

    app.include_router(router)
    return TestClient(app), mgr


def test_a_person_sees_their_own_environments_and_processes(monkeypatch):
    client, mgr = _client(monkeypatch, "user")
    body = client.get("/api/sandbox").json()
    assert body["available"] and body["is_admin"] is False
    assert body["environments"][0]["id"] == "0a1b2c3d" and "owner" not in body["environments"][0]
    assert body["processes"][0]["handle"] == "e-0a1b2c3d-p12345678"
    assert mgr.calls[0] == ("list", "scope-alice", False)


def test_everybodys_environments_are_for_admins_only(monkeypatch):
    """MUTATION: drop the admin check on ?all=1 - red."""
    client, _ = _client(monkeypatch, "user")
    assert client.get("/api/sandbox?all=1").status_code == 403
    assert client.delete("/api/sandbox/0a1b2c3d?all=1").status_code == 403
    client, mgr = _client(monkeypatch, "admin")
    body = client.get("/api/sandbox?all=1").json()
    assert body["environments"][0]["owner"] == "h1"
    assert client.delete("/api/sandbox/0a1b2c3d?all=1").status_code == 200
    assert mgr.calls[-1] == ("delete", "scope-alice", "0a1b2c3d", True)


def test_stop_and_delete_act_as_the_caller(monkeypatch):
    client, mgr = _client(monkeypatch, "user")
    assert client.post("/api/sandbox/0a1b2c3d/stop").json() == {"ok": True, "id": "0a1b2c3d", "action": "stop"}
    assert mgr.calls[-1] == ("stop", "scope-alice", "0a1b2c3d", False)
    r = client.delete("/api/sandbox/missing")
    assert r.status_code == 404 and "no environment" in r.json()["detail"]


def test_a_docker_failure_is_unavailable_not_an_internal_error(monkeypatch):
    """MUTATION: drop the broad except in _act - red: a docker that timed out answered 500."""
    client, mgr = _client(monkeypatch, "user")

    def _timeout(*a, **k):
        import subprocess
        raise subprocess.TimeoutExpired(cmd="docker", timeout=60)

    mgr.stop = _timeout
    r = client.post("/api/sandbox/0a1b2c3d/stop")
    assert r.status_code == 503 and r.json()["detail"].startswith("sandbox unavailable")
    assert client.delete("/api/sandbox/missing").status_code == 404     # still a 404


def test_a_listing_that_times_out_is_unavailable_not_an_internal_error(monkeypatch):
    """MUTATION: drop the broad except in _overview - red: the section got a 500."""
    client, mgr = _client(monkeypatch, "user")

    def _timeout(*a, **k):
        import subprocess
        raise subprocess.TimeoutExpired(cmd="docker", timeout=30)

    mgr.list = _timeout
    r = client.get("/api/sandbox")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False and body["reason"].startswith("sandbox unavailable")
    assert body["environments"] == [] and body["processes"] == []


def test_without_docker_the_section_says_so(monkeypatch):
    client, _ = _client(monkeypatch, "user")
    import vaf.core.service_stack as stack
    monkeypatch.setattr(stack, "is_docker_daemon_running", lambda: False)
    body = client.get("/api/sandbox").json()
    assert body["available"] is False and body["reason"] == "docker_unavailable"


def test_the_section_is_mounted_and_reads_its_strings():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    modal = (root / "web" / "components" / "SettingsModal.tsx").read_text(encoding="utf-8")
    assert "<SandboxSection />" in modal
    section = (root / "web" / "components" / "settings" / "SandboxSection.tsx").read_text(encoding="utf-8")
    assert "useTranslations('sandboxEnv')" in section and "/api/sandbox" in section
    # The refresh after an action reloads with the current selection, not the closure's:
    # the old scope's answer would win the race and leave the list dimmed for good.
    act = section[section.index("const act = async"):section.index("if (!data && !loadFailed)")]
    assert "void loadNow.current();" in act and "void load();" not in act
    assert "useEffect(() => { loadNow.current = load; }, [load]);" in section
    server = (root / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert "from vaf.api.sandbox_routes import router as sandbox_router" in server


def test_the_admin_view_says_its_processes_are_the_callers_own(monkeypatch):
    """Everybody's environments, but not everybody's commands. MUTATION: drop the scope - red."""
    import types
    from vaf.api import sandbox_routes
    import vaf.core.environments as envmod
    import vaf.core.service_stack as stack
    monkeypatch.setattr(stack, "is_docker_daemon_running", lambda: True)
    seen = {}
    fake = types.SimpleNamespace(list=lambda scope, everyone=False: [],
                                 processes=lambda scope: seen.setdefault("scope", scope) and [])
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: fake)
    out = sandbox_routes._overview("scope-admin", True)
    assert out["processes_scope"] == "own" and seen["scope"] == "scope-admin"


def test_an_action_on_a_docker_that_does_not_list_is_503_not_404(monkeypatch):
    """Unmeasured is not "no such environment". MUTATION: drop the EnvironmentsUnlisted
    branch in _act - red (404)."""
    import asyncio
    import types
    from fastapi import HTTPException
    from vaf.api import sandbox_routes
    import vaf.core.environments as envmod

    def _unlisted(*a, **k):
        raise envmod.EnvironmentsUnlisted("docker did not list the environments: down")

    monkeypatch.setattr(envmod, "get_environment_manager",
                        lambda: types.SimpleNamespace(stop=_unlisted, delete=_unlisted))
    monkeypatch.setattr(sandbox_routes, "_caller", lambda request: ({"user_scope_id": "scope-a"}, False))
    with pytest.raises(HTTPException) as err:
        asyncio.run(sandbox_routes._act(None, "0a1b2c3d", "stop", 0))
    assert err.value.status_code == 503 and "did not list" in err.value.detail
