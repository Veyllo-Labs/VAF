# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The web half of the remote-access settings, and who may read the network routes.

The connection map lists every connected device's address and user name; the settings
tab that shows it was admin-only, the route was not. Now every network route but the
pre-login /ws-config answers an admin only.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import vaf.network.binding as binding
from vaf.api import network_routes
from vaf.core.config import Config

PROXY_PEER = ("127.0.0.1", 40000)
LAN = binding.LocalInterface("enp3s0", "192.168.2.10", "192.168.2.0/24", "lan")
WG = binding.LocalInterface("wg0", "10.8.0.1", "10.8.0.0/24", "vpn")
TS = binding.LocalInterface("tailscale0", "100.101.102.103", "100.64.0.0/10", "vpn")


def _app(user=None):
    app = FastAPI()
    if user is not None:
        class _As(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.user = user
                return await call_next(request)
        app.add_middleware(_As)
    app.include_router(network_routes.router)
    return TestClient(app, client=PROXY_PEER)


@pytest.fixture
def stored(monkeypatch):
    values = {**Config.DEFAULTS, "local_network_enabled": True}
    saved = []
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: dict(values)))
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: values.get(k, d)))

    def save(cls, cfg):
        saved.append(cfg)
        values.update(cfg)
    monkeypatch.setattr(Config, "save", classmethod(save))
    monkeypatch.setattr(binding, "local_interfaces", lambda: [LAN, WG, TS])
    monkeypatch.setattr(network_routes, "_access_port", lambda: 8443)
    return values


USER = {"username": "bob", "role": "user", "user_scope_id": "ab12cd34-0000-4000-8000-000000000002"}


@pytest.mark.parametrize("method, path", [
    ("get", "/api/network/connections"), ("get", "/api/network/status"),
    ("get", "/api/network/access-url"), ("get", "/api/network/remote-access"),
    ("put", "/api/network/remote-access"),
])
def test_an_ordinary_account_reads_nothing_about_the_network(stored, method, path):
    """MUTATION: drop require_admin from /connections - red."""
    client = _app(USER)
    kwargs = {"json": {"allowed": [], "vpn_only": False}} if method == "put" else {}
    assert getattr(client, method)(path, **kwargs).status_code == 403


def test_the_login_page_still_gets_its_websocket_settings(stored):
    assert _app(USER).get("/api/network/ws-config").status_code == 200


def test_the_state_lists_interfaces_with_url_and_admission(stored):
    state = _app().get("/api/network/remote-access").json()
    by_name = {i["name"]: i for i in state["interfaces"]}
    assert by_name["enp3s0"]["url"] == "https://192.168.2.10:8443"
    assert by_name["wg0"]["admitted"] is True
    assert by_name["tailscale0"]["admitted"] is False and by_name["tailscale0"]["url"] is None
    assert state["mesh_vpn_admitted"] is False
    assert state["your_address"] == "127.0.0.1"


def test_admitting_tailscale_saves_and_shows_its_url(stored):
    client = _app()
    r = client.put("/api/network/remote-access", json={"allowed": ["100.64.0.0/10"], "vpn_only": False})
    assert r.status_code == 200, r.text
    assert stored["local_network_allowed_networks"] == ["100.64.0.0/10"]
    ts = next(i for i in r.json()["interfaces"] if i["name"] == "tailscale0")
    assert ts["url"] == "https://100.101.102.103:8443"


def test_a_public_network_is_refused_with_its_reason(stored):
    r = _app().put("/api/network/remote-access", json={"allowed": ["8.8.8.0/24"], "vpn_only": False})
    assert r.status_code == 422
    assert r.json()["detail"]["refused"] == [{"value": "8.8.8.0/24", "reason": "not_private"}]
    assert "local_network_allowed_networks" not in stored or stored["local_network_allowed_networks"] == []


def test_vpn_only_from_the_home_network_asks_before_locking_you_out(stored):
    """An admin on the home network switching to "VPN only" would lose this very page.
    MUTATION: save without the lockout check - red."""
    client = _app()
    home = {"X-Forwarded-For": "192.168.2.50"}
    r = client.put("/api/network/remote-access", json={"allowed": [], "vpn_only": True}, headers=home)
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "lockout", "address": "192.168.2.50"}
    assert stored["local_network_vpn_only"] is False

    r = client.put("/api/network/remote-access", json={"allowed": [], "vpn_only": True, "confirm": True},
                   headers=home)
    assert r.status_code == 200
    assert stored["local_network_vpn_only"] is True


def test_vpn_only_from_inside_the_vpn_needs_no_confirmation(stored):
    r = _app().put("/api/network/remote-access", json={"allowed": [], "vpn_only": True},
                   headers={"X-Forwarded-For": "10.8.0.7"})
    assert r.status_code == 200, r.text


def test_the_access_url_falls_back_to_the_vpn_on_a_server_without_lan(stored, monkeypatch):
    monkeypatch.setattr(binding, "local_interfaces", lambda: [WG])
    body = _app().get("/api/network/access-url").json()
    assert body["host"] == "10.8.0.1"
    assert body["url"] == "https://10.8.0.1:8443"


def test_a_page_with_an_older_list_cannot_bring_back_a_removed_network(stored):
    """Admin A loaded the page with [10.9.0.0/24, 100.64.0.0/10]; admin B removed
    10.9.0.0/24 since. A's switch would send the whole old list back. With the base the
    page loaded, the save is refused and A gets the current state to redo the change on.
    MUTATION: drop the base comparison - red."""
    stored["local_network_allowed_networks"] = ["100.64.0.0/10"]   # after B's removal
    client = _app()
    r = client.put("/api/network/remote-access", json={
        "allowed": ["10.9.0.0/24", "100.64.0.0/10"], "vpn_only": True,
        "base_allowed": ["10.9.0.0/24", "100.64.0.0/10"], "base_vpn_only": False})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale"
    assert r.json()["detail"]["state"]["allowed"] == ["100.64.0.0/10"]
    assert stored["local_network_allowed_networks"] == ["100.64.0.0/10"]
    assert stored["local_network_vpn_only"] is False

    r = client.put("/api/network/remote-access", json={
        "allowed": ["100.64.0.0/10", "10.20.0.0/16"], "vpn_only": False,
        "base_allowed": ["100.64.0.0/10"], "base_vpn_only": False})
    assert r.status_code == 200, r.text
    assert stored["local_network_allowed_networks"] == ["100.64.0.0/10", "10.20.0.0/16"]


def test_a_save_without_a_base_is_taken_as_it_is(stored):
    r = _app().put("/api/network/remote-access", json={"allowed": ["10.20.0.0/16"], "vpn_only": False})
    assert r.status_code == 200
