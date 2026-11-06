# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Pairing: the window, the order of the checks, nothing before the web confirm, and the
events of every refusal."""
import base64

import pytest
from cryptography import x509

from app_link_client import AppClient, Pipe, ca_der, device_key, pair_message
from app_link_env import SCOPE_A, SCOPE_B, app_link_home  # noqa: F401  (fixture)
from vaf.core import channel_pairing
from vaf.core.app_link import bridge, devices, pairing, pki
from vaf.core.app_link import protocol as pr

_REAL_DIRECT = pairing.direct_addresses


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    bridge.reset()
    pairing.reset()
    monkeypatch.setattr(pairing, "direct_addresses", lambda: ["wss://10.0.0.1:8443/api/app-link"])
    yield
    pairing.reset()
    bridge.reset()


def _issue(scope=SCOPE_A, mode="local", username="alice"):
    pairing.open_window(scope, username)
    return pairing.issue(scope, username, mode)


def _scan(p, *, key=None, peer="10.0.0.5", k=None):
    key = key or device_key()
    pipe = Pipe(peer=peer)
    client = AppClient(ca_der(), sni=pr.SNI_PAIR).attach(pipe).connect()
    client.send(pair_message(k or p.k, key))
    return client, pipe, key


def test_issue_needs_the_window(app_link_home):
    with pytest.raises(pairing.PairingRefused) as exc:
        pairing.issue(SCOPE_A, "alice", "local")
    assert exc.value.code == "window_closed"
    p = _issue()
    parsed = pr.parse_qr(p.qr)
    assert parsed["m"] == "l" and parsed["d"] == ["wss://10.0.0.1:8443/api/app-link"]
    assert parsed["ca"] == pki.ca_sha256() and pr.b64url(parsed["k"]) == p.k
    relay = pairing.issue(SCOPE_A, "alice", "relay")
    parsed = pr.parse_qr(relay.qr)
    assert parsed["m"] == "r" and parsed["api"] == "https://relay.test" and len(parsed["p"]) == 32
    # The earlier K is withdrawn when a new QR is issued.
    assert channel_pairing.pairing_code_state("veyllo_app", p.k) == "unknown"
    app_link_home.set_block(relay_enabled=False)
    with pytest.raises(pairing.PairingRefused) as exc:
        pairing.issue(SCOPE_A, "alice", "relay")
    assert exc.value.code == "relay_off"


def test_local_needs_a_direct_path(app_link_home, monkeypatch):
    monkeypatch.setattr(pairing, "direct_addresses", lambda: [])
    pairing.open_window(SCOPE_A, "alice")
    with pytest.raises(pairing.PairingRefused) as exc:
        pairing.issue(SCOPE_A, "alice", "local")
    assert exc.value.code == "no_direct_path"


def test_direct_addresses_need_local_network_and_tls(app_link_home, monkeypatch):
    from vaf.core.config import Config
    from vaf.network import binding
    monkeypatch.setattr(binding, "access_addresses",
                        lambda cfg=None: [binding.LocalInterface("wg0", "10.66.66.1", "10.66.66.0/24", "vpn"),
                                          binding.LocalInterface("eth0", "fd00::20", "fd00::/64", "lan")])
    real = Config.get
    flags = {"local_network_enabled": True, "local_network_tls_enabled": False,
             "local_network_https_port": 443}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: flags[k] if k in flags else real(k, d)))
    assert _REAL_DIRECT() == []
    flags["local_network_tls_enabled"] = True
    assert _REAL_DIRECT() == ["wss://10.66.66.1:8443/api/app-link",
                                          "wss://[fd00::20]:8443/api/app-link"]


def test_a_bad_csr_does_not_burn_the_code(app_link_home):
    p = _issue()
    pipe = Pipe()
    client = AppClient(ca_der(), sni=pr.SNI_PAIR).attach(pipe).connect()
    bad = pair_message(p.k, device_key())
    bad["csr"] = base64.b64encode(b"\x30\x03\x02\x01\x00").decode()
    client.send(bad)
    assert client.of("pair_error") == [{"t": "pair_error", "code": "bad_csr"}]
    assert channel_pairing.pairing_code_state("veyllo_app", p.k) == "live"
    assert app_link_home.kinds()[-1] == "app_pairing_refused"


def test_used_unknown_and_busy(app_link_home):
    p = _issue()
    first, _pipe, _key = _scan(p)
    assert first.of("pair_wait")
    again, _p2, _k2 = _scan(p, peer="10.0.0.6")
    assert again.of("pair_error") == [{"t": "pair_error", "code": "used"}]
    unknown, _p3, _k3 = _scan(p, peer="10.0.0.7", k=pr.b64url(bytes(32)))
    assert unknown.of("pair_error") == [{"t": "pair_error", "code": "expired"}]
    # A second QR while the first device waits for confirmation: busy, its code not spent.
    p2 = pairing.issue(SCOPE_A, "alice", "local")
    busy, _p4, _k4 = _scan(p2, peer="10.0.0.8")
    assert busy.of("pair_error") == [{"t": "pair_error", "code": "busy"}]
    assert channel_pairing.pairing_code_state("veyllo_app", p2.k) == "live"
    refused = [f for k, f in app_link_home.events if k == "app_pairing_refused"]
    assert len(refused) == 3
    text = repr(app_link_home.events)
    assert p.k not in text and p2.k not in text


def test_nothing_exists_before_the_confirm_and_accept_creates_one_row(app_link_home):
    p = _issue(scope=SCOPE_B, username="bob")
    app_link_home.lane(SCOPE_B, False)
    client, pipe, key = _scan(p)
    assert devices.all_devices() == []
    state = pairing.current(SCOPE_B)
    assert state["state"] == "awaiting_confirm"
    assert state["device"]["fingerprint"] == pki.display_fingerprint(key.public_key())
    assert state["device"]["model"] == "Pixel 9"
    assert pairing.current(SCOPE_A) == {"state": "none"}
    assert pairing.confirm(SCOPE_A, True) == "none"
    assert pairing.confirm(SCOPE_B, True) == "paired"
    client.pump()
    rows = devices.all_devices()
    assert len(rows) == 1 and rows[0]["user_scope_id"] == SCOPE_B and rows[0]["username"] == "bob"
    paired = client.of("paired")[0]
    cert = x509.load_der_x509_certificate(base64.b64decode(paired["cert"]))
    cert.verify_directly_issued_by(pki.device_ca()[1])
    assert paired["device_id"] == rows[0]["device_id"] and paired["account"] == "bob"
    assert pki.spki_sha256(cert.public_key()) == rows[0]["spki_sha256"]
    assert str(cert.serial_number) == rows[0]["cert_serial"]
    assert {"t": "bye", "reason": "paired"} in client.frames and pipe.session.closed
    from vaf.core.messaging_connections import channel_enabled_for_scope
    assert channel_enabled_for_scope("veyllo_app", SCOPE_B)
    event = [f for k, f in app_link_home.events if k == "channel_paired"][0]
    assert event["channel"] == "veyllo_app" and event["path"] == rows[0]["device_id"]
    assert "direct" in event["detail"] and pki.display_fingerprint(key.public_key()) in event["detail"]


def test_deny_and_timeout(app_link_home, monkeypatch):
    p = _issue()
    client, _pipe, _key = _scan(p)
    assert pairing.confirm(SCOPE_A, False) == "denied"
    client.pump()
    assert client.of("pair_error") == [{"t": "pair_error", "code": "denied"}]
    assert devices.all_devices() == []
    p = pairing.issue(SCOPE_A, "alice", "local")
    client, _pipe, _key = _scan(p, peer="10.0.0.6")
    now = pairing._clock()
    monkeypatch.setattr(pairing, "_clock", lambda: now + pairing.CONFIRM_SECONDS + 1)
    pairing.sweep()
    client.pump()
    assert client.of("pair_error") == [{"t": "pair_error", "code": "expired"}]
    assert pairing.confirm(SCOPE_A, True) == "none"
    assert devices.all_devices() == []
    assert app_link_home.kinds().count("app_pairing_refused") == 2
    # A confirmation given too late, before any sweep ran, is refused at the confirm itself.
    monkeypatch.setattr(pairing, "_clock", lambda: now)
    p = pairing.issue(SCOPE_A, "alice", "local")
    client, _pipe, _key = _scan(p, peer="10.0.0.7")
    monkeypatch.setattr(pairing, "_clock", lambda: now + pairing.CONFIRM_SECONDS + 1)
    assert pairing.confirm(SCOPE_A, True) == "expired"
    client.pump()
    assert client.of("pair_error") == [{"t": "pair_error", "code": "expired"}]
    assert devices.all_devices() == []


def test_only_the_first_frame_may_be_pair(app_link_home):
    pipe = Pipe()
    client = AppClient(ca_der(), sni=pr.SNI_PAIR).attach(pipe).connect()
    client.send({"t": "hello", "v": 1, "device_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
                 "caps": []})
    assert client.of("error") == [{"t": "error", "code": "unexpected"}] and pipe.session.closed


def test_the_agent_name_comes_from_the_registered_resolver(app_link_home, monkeypatch):
    import vaf.core.app_link as app_link
    monkeypatch.setattr(app_link, "_agent_name_resolver", None)
    p = _issue()
    assert "n" not in pr.parse_qr(p.qr)
    app_link.set_agent_name_resolver(lambda username: f"Nova of {username}")
    p = pairing.issue(SCOPE_A, "alice", "local")
    assert pr.parse_qr(p.qr)["n"] == "Nova of alice"
    client, _pipe, _key = _scan(p)
    pairing.confirm(SCOPE_A, True)
    client.pump()
    assert client.of("paired")[0]["agent"] == "Nova of alice"
    # The framework link imports nothing from the auth layer; the harness registers it.
    from pathlib import Path
    for path in (Path(app_link.__file__).parent).glob("*.py"):
        assert "vaf.auth" not in path.read_text(encoding="utf-8"), path.name
    routes = Path(app_link.__file__).parents[2] / "api" / "app_link_routes.py"
    assert "set_agent_name_resolver(agent_display_name)" in routes.read_text(encoding="utf-8")


def test_a_late_confirm_on_a_closed_session_creates_nothing(app_link_home):
    p = _issue()
    client, pipe, _key = _scan(p)
    pipe.session.close("gone", bye=False)          # the phone gave up waiting
    assert pairing.confirm(SCOPE_A, True) == "expired"
    assert devices.all_devices() == []


def test_two_confirms_at_once_create_one_device_and_no_error(app_link_home, monkeypatch):
    import threading
    p = _issue()
    client, _pipe, _key = _scan(p)
    gate = threading.Barrier(2)
    results = []

    def _go():
        gate.wait()
        results.append(pairing.confirm(SCOPE_A, True))
    threads = [threading.Thread(target=_go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == ["none", "paired"]
    assert len(devices.all_devices()) == 1


def test_the_sweep_and_a_confirm_never_both_own_a_pairing(app_link_home, monkeypatch):
    p = _issue()
    client, _pipe, _key = _scan(p)
    now = pairing._clock()
    monkeypatch.setattr(pairing, "_clock", lambda: now + pairing.CONFIRM_SECONDS + 1)
    pairing.sweep()
    assert pairing.confirm(SCOPE_A, True) == "none"
    client.pump()
    assert client.of("pair_error") == [{"t": "pair_error", "code": "expired"}]
    assert devices.all_devices() == []
