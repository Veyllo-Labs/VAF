# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The relay connector against a scripted relay: idle unless needed, every claim answer,
the token in the upgrade header only, renewal, close codes, off, rotate, dissolve, member
deletion and the notify rhythm. Nothing here touches the network."""
import asyncio
import json

import pytest

from app_link_client import paired_device
from app_link_env import SCOPE_A, app_link_home  # noqa: F401  (fixture)
from app_link_relay_stub import FakeConnect, FakeHttp, FakeResponse
from vaf.core import channel_pairing
from vaf.core.app_link import bridge, devices, pairing, relay
from vaf.core.channel_secrets import channel_secret, set_channel_secret

MEMBER = "0f8fad5b-d9cb-469f-a165-70867728950e"
MEMBER2 = "1f8fad5b-d9cb-469f-a165-70867728950e"
CID = "ab" * 16


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    bridge.reset()
    pairing.reset()
    monkeypatch.setattr(pairing, "direct_addresses", lambda: [])
    yield
    pairing.reset()
    bridge.reset()


def _connector(http, connect=None, clock=None):
    clock = clock or Clock()
    c = relay.RelayConnector(http_factory=lambda: http, ws_connect=connect or FakeConnect(),
                             clock=clock, wall=lambda: 1_800_000_000.0, rng=lambda: 0.5)
    c._http = http
    c.clock = clock
    return c


def _run(coro_fn):
    async def _go():
        out = await coro_fn()
        await asyncio.sleep(0)
        return out
    return asyncio.run(_go())


def _relay_qr(scope=SCOPE_A):
    pairing.open_window(scope, "alice")
    return pairing.issue(scope, "alice", "relay")


def _claimed(member=MEMBER, cid=CID, secret="vrs_first", instance="inst-1"):
    body = {"instanceId": instance, "member": member, "cid": cid}
    if secret:
        body["secret"] = secret
    return FakeResponse(200, body)


def test_idle_without_a_qr_or_a_relay_device(app_link_home):
    http, connect = FakeHttp(), FakeConnect()
    c = _connector(http, connect)
    set_channel_secret("veyllo_app", "relay_secret", "vrs_known")
    paired_device(SCOPE_A)                 # a direct-path device: no relay member
    _run(c.tick)
    assert http.calls == [] and connect.upgrades == []
    assert c.status()["state"] == "idle"


def test_claim_answers(app_link_home):
    p = _relay_qr()
    http = FakeHttp({"POST /v1/relay/claim": [FakeResponse(202), _claimed()]})
    c = _connector(http)
    _run(lambda: c.claim_tick(c.clock()))
    assert pairing.current(SCOPE_A)["state"] == "issued"
    assert http.calls[0]["json"] == {"pair": relay.pr.b64url(p.p)}
    assert "Authorization" not in http.calls[0]["headers"]
    c.clock.now += 3
    _run(lambda: c.claim_tick(c.clock()))
    assert pairing.current(SCOPE_A)["state"] == "claimed"
    assert pairing._issued[SCOPE_A].member == MEMBER and pairing._issued[SCOPE_A].cid == CID
    assert channel_secret("veyllo_app", "relay_secret") == "vrs_first"
    from vaf.core.app_link import settings
    assert settings()["relay_instance_id"] == "inst-1"
    assert "app_relay_changed" in app_link_home.kinds()
    # With S, the next claim carries it.
    _relay_qr()
    c.clock.now += 3
    http.script["POST /v1/relay/claim"] = [FakeResponse(202)]
    _run(lambda: c.claim_tick(c.clock()))
    assert http.calls[-1]["headers"] == {"Authorization": "Bearer vrs_first"}


@pytest.mark.parametrize("code,state", [(409, "used_elsewhere"), (410, "expired")])
def test_claim_used_or_expired(app_link_home, code, state):
    p = _relay_qr()
    c = _connector(FakeHttp({"POST /v1/relay/claim": [FakeResponse(code)]}))
    _run(lambda: c.claim_tick(c.clock()))
    assert pairing.current(SCOPE_A)["state"] == state
    assert channel_pairing.pairing_code_state("veyllo_app", p.k) == "unknown"
    if code == 409:
        assert "app_pairing_refused" in app_link_home.kinds()


def test_claim_secret_rejected_stops_polling(app_link_home):
    _relay_qr()
    http = FakeHttp({"POST /v1/relay/claim": [FakeResponse(401)]})
    c = _connector(http)
    _run(lambda: c.claim_tick(c.clock()))
    assert c.state == "secret_rejected"
    c.clock.now += 60
    _run(lambda: c.claim_tick(c.clock()))
    assert len(http.calls) == 1


@pytest.mark.parametrize("code,wait", [(429, 60), (503, 30)])
def test_claim_waits_on_429_and_503(app_link_home, code, wait):
    _relay_qr()
    http = FakeHttp({"POST /v1/relay/claim": [FakeResponse(code), FakeResponse(202)]})
    c = _connector(http)
    _run(lambda: c.claim_tick(c.clock()))
    c.clock.now += wait - 1
    _run(lambda: c.claim_tick(c.clock()))
    assert len(http.calls) == 1
    c.clock.now += 2
    _run(lambda: c.claim_tick(c.clock()))
    assert len(http.calls) == 2
    retry = FakeHttp({"POST /v1/relay/claim": [FakeResponse(code, headers={"retry-after": "7"}),
                                               FakeResponse(202)]})
    c2 = _connector(retry)
    _run(lambda: c2.claim_tick(c2.clock()))
    c2.clock.now += 8
    _run(lambda: c2.claim_tick(c2.clock()))
    assert len(retry.calls) == 2


def test_another_instance_is_refused(app_link_home):
    app_link_home.set_block(relay_instance_id="inst-mine")
    _relay_qr()
    c = _connector(FakeHttp({"POST /v1/relay/claim": [_claimed(instance="inst-other")]}))
    _run(lambda: c.claim_tick(c.clock()))
    from vaf.core.app_link import settings
    assert settings()["relay_instance_id"] == "inst-mine"
    assert channel_secret("veyllo_app", "relay_secret") == ""
    assert c.last_error == "instance_mismatch" and pairing.current(SCOPE_A)["state"] == "issued"


def _connected(app_link_home, *, expires_in=600):
    set_channel_secret("veyllo_app", "relay_secret", "vrs_known")
    paired_device(SCOPE_A, relay_member=MEMBER)
    http = FakeHttp({"POST /v1/relay/token": [FakeResponse(200, {
        "token": "tok-1", "url": "wss://relay.test/v1/connect/inst-1",
        "expiresAt": 1_800_000_000 + expires_in})]})
    connect = FakeConnect()
    return http, connect


def test_token_only_in_the_upgrade_header(app_link_home):
    http, connect = _connected(app_link_home)
    c = _connector(http, connect)

    async def _go():
        await c.tick()
        for _ in range(20):
            await asyncio.sleep(0)
        assert c.state == "connected"
        connect.last.close_from_relay(1000)
        await asyncio.sleep(0.05)
    asyncio.run(_go())
    url, headers = connect.upgrades[0]
    assert headers == {"Authorization": "Bearer tok-1"} and "tok-1" not in url
    assert http.calls[0]["headers"] == {"Authorization": "Bearer vrs_known"}


def test_a_token_url_must_be_wss_without_userinfo(app_link_home):
    set_channel_secret("veyllo_app", "relay_secret", "vrs_known")
    paired_device(SCOPE_A, relay_member=MEMBER)
    for url in ("ws://relay.test/x", "wss://user:pw@relay.test/x", "https://relay.test/x"):
        connect = FakeConnect()
        c = _connector(FakeHttp({"POST /v1/relay/token": [FakeResponse(200, {
            "token": "t", "url": url, "expiresAt": 1_800_000_600})]}), connect)

        async def _go():
            await c.tick()
            for _ in range(10):
                await asyncio.sleep(0)
        asyncio.run(_go())
        assert connect.upgrades == [] and c.state == "backoff", url


def test_in_band_renewal_before_expiry(app_link_home):
    http, connect = _connected(app_link_home, expires_in=59)
    c = _connector(http, connect)

    async def _go():
        await c.tick()
        for _ in range(30):
            await asyncio.sleep(0.05)
        connect.last.close_from_relay(1000)
        await asyncio.sleep(0.05)
    asyncio.run(_go())
    sent = connect.sockets[0].texts()
    assert {"t": "auth", "token": "tok-1"} in sent
    assert len([c for c in http.calls if c["path"] == "/v1/relay/token"]) >= 2


def test_frames_reach_link_sessions_and_never_the_broadcast_cid(app_link_home):
    http, connect = _connected(app_link_home)
    c = _connector(http, connect)

    async def _go():
        await c.tick()
        for _ in range(10):
            await asyncio.sleep(0)
        connect.last.push(b"\x01" + bytes.fromhex(CID) + b"\x16\x03\x01\x02\x00" + b"\x01" * 3)
        connect.last.push(json.dumps({"t": "apps", "cids": [CID]}))
        for _ in range(10):
            await asyncio.sleep(0)
        link = c._link
        assert link is not None and bytes.fromhex(CID) in link.sessions
        assert link.online == {CID}
        link._send_frame(b"\x01" + bytes(16) + b"x")
        link._send_frame(b"\x01" + bytes.fromhex(CID) + b"y")
        for _ in range(10):
            await asyncio.sleep(0)
        connect.last.close_from_relay(1000)
        await asyncio.sleep(0.05)
    asyncio.run(_go())
    sent = connect.sockets[0].binaries()
    assert b"\x01" + bytes.fromhex(CID) + b"y" in sent
    assert all(f[1:17] != bytes(16) for f in sent)


@pytest.mark.parametrize("code,state,wait", [(4001, "replaced", 300), (4003, "revoked", float("inf")),
                                             (4008, "backoff", 60)])
def test_close_codes(app_link_home, code, state, wait):
    c = _connector(FakeHttp())
    c.after_close(code)
    assert c.state == state
    assert c._hold_until - c.clock() == wait


def test_4002_reconnects_with_a_new_token_and_others_back_off(app_link_home):
    c = _connector(FakeHttp())
    c.after_close(4002)
    assert c._hold_until <= c.clock() and c.state == "connecting"
    c.after_close(1006)
    first = c._hold_until - c.clock()
    c.after_close(1006)
    c.after_close(1006)
    assert c.state == "backoff" and 0 < first <= 1 and c._hold_until - c.clock() <= 4
    for _ in range(20):
        c.after_close(1006)
    assert c._hold_until - c.clock() <= relay.BACKOFF_MAX_SECONDS


def test_replaced_is_not_reconnected_at_once(app_link_home):
    http, connect = _connected(app_link_home)
    c = _connector(http, connect)

    async def _go():
        await c.tick()
        for _ in range(10):
            await asyncio.sleep(0)
        connect.last.close_from_relay(4001)
        for _ in range(10):
            await asyncio.sleep(0)
        await c.tick()
        for _ in range(10):
            await asyncio.sleep(0)
    asyncio.run(_go())
    assert c.state == "replaced" and len(connect.upgrades) == 1


def test_off_closes_and_stops_claims_and_notify_but_keeps_s(app_link_home):
    http, connect = _connected(app_link_home)
    app_link_home.set_block(relay_instance_id="inst-1")
    c = _connector(http, connect)
    _relay_qr()
    row = devices.all_devices()[0]
    devices.outbox_add(row["device_id"], "m1")
    c.schedule_notify(devices.get(row["device_id"]))

    async def _go():
        await c.tick()
        for _ in range(10):
            await asyncio.sleep(0)
        assert c.state == "connected"
        relay.set_relay_enabled(False)
        c.clock.now += 120
        await c.tick()
        await c.notify_tick(c.clock())
        await asyncio.sleep(0.05)
    asyncio.run(_go())
    assert connect.sockets[0].close_code == 1000
    assert c.status()["state"] == "off"
    paths = http.paths()
    assert "/v1/relay/notify" not in paths
    assert paths.count("/v1/relay/claim") <= 1
    assert channel_secret("veyllo_app", "relay_secret") == "vrs_known"
    from vaf.core.app_link import settings
    assert settings()["relay_instance_id"] == "inst-1"
    assert "off" in [f.get("detail") for k, f in app_link_home.events if k == "app_relay_changed"]
    with pytest.raises(pairing.PairingRefused):
        pairing.issue(SCOPE_A, "alice", "relay")


def test_rotate_stores_the_new_secret(app_link_home):
    set_channel_secret("veyllo_app", "relay_secret", "vrs_old")
    http = FakeHttp({"POST /v1/relay/instance": [FakeResponse(200, {"secret": "vrs_new"})]})
    c = _connector(http)
    assert asyncio.run(c.rotate(username="admin")) is True
    assert http.calls[0]["json"] == {"action": "rotate"}
    assert http.calls[0]["headers"] == {"Authorization": "Bearer vrs_old"}
    assert channel_secret("veyllo_app", "relay_secret") == "vrs_new"


def test_dissolve_clears_s_instance_and_members_and_keeps_devices(app_link_home):
    set_channel_secret("veyllo_app", "relay_secret", "vrs_old")
    app_link_home.set_block(relay_instance_id="inst-1")
    kept = paired_device(SCOPE_A, relay_member=MEMBER)[0]
    gone = paired_device(SCOPE_A, relay_member=MEMBER2)[0]
    devices.remove_device(gone["device_id"], by="alice", reason="lost")
    http = FakeHttp({"DELETE /v1/relay/instance": [FakeResponse(200)]})
    assert asyncio.run(_connector(http).dissolve(username="admin")) is True
    assert channel_secret("veyllo_app", "relay_secret") == ""
    from vaf.core.app_link import settings
    assert settings()["relay_instance_id"] == ""
    row = devices.get(kept["device_id"])
    assert row is not None and row["relay_member"] is None and row["relay_cid"] is None
    assert devices.tombstones() == []
    assert "dissolved" in [f.get("detail") for k, f in app_link_home.events if k == "app_relay_changed"]


def test_members_are_deleted_one_by_one_and_retried(app_link_home):
    set_channel_secret("veyllo_app", "relay_secret", "vrs_known")
    for member in (MEMBER, MEMBER2):
        row = paired_device(SCOPE_A, relay_member=member)[0]
        devices.remove_device(row["device_id"], by="alice", reason="lost")
    answers = {MEMBER: [FakeResponse(200)], MEMBER2: [FakeResponse(500), FakeResponse(404)]}
    http = FakeHttp({"DELETE /v1/relay/members": lambda call: answers[call["params"]["member"]].pop(0)})
    c = _connector(http)
    _run(lambda: c.reconcile_members(c.clock()))
    assert devices.tombstones() == [MEMBER2]
    _run(lambda: c.reconcile_members(c.clock()))
    assert len(http.calls) == 2
    c.clock.now += 3
    _run(lambda: c.reconcile_members(c.clock()))
    assert devices.tombstones() == []
    assert all(call["headers"] == {"Authorization": "Bearer vrs_known"} for call in http.calls)


def test_notify_30s_then_5min(app_link_home):
    set_channel_secret("veyllo_app", "relay_secret", "vrs_known")
    row = paired_device(SCOPE_A, relay_member=MEMBER)[0]
    http = FakeHttp({"POST /v1/relay/notify": [FakeResponse(200)]})
    c = _connector(http)
    bridge.add_offline_hook(c.schedule_notify)
    bridge.add_online_hook(c.cancel_notify)
    try:
        bridge.deliver(SCOPE_A, "alice", "first")
        _run(lambda: c.notify_tick(c.clock()))
        assert http.calls == []
        c.clock.now += 29
        _run(lambda: c.notify_tick(c.clock()))
        assert http.calls == []
        c.clock.now += 2
        _run(lambda: c.notify_tick(c.clock()))
        assert [x["json"] for x in http.calls] == [{"member": MEMBER}]
        bridge.deliver(SCOPE_A, "alice", "second")
        c.clock.now += 60
        _run(lambda: c.notify_tick(c.clock()))
        assert len(http.calls) == 1
        c.clock.now += 300
        _run(lambda: c.notify_tick(c.clock()))
        assert len(http.calls) == 2
        # A device that comes back cancels the pending notice.
        c.clock.now += 400
        bridge.deliver(SCOPE_A, "alice", "third")
        bridge._came_online(row["device_id"])
        c.clock.now += 60
        _run(lambda: c.notify_tick(c.clock()))
        assert len(http.calls) == 2
        # Nothing is sent for a device whose outbox is empty.
        for entry in devices.get(row["device_id"])["outbox"]:
            devices.outbox_ack(row["device_id"], entry["id"])
        bridge.deliver(SCOPE_A, "alice", "fourth")
        devices.outbox_ack(row["device_id"], devices.get(row["device_id"])["outbox"][0]["id"])
        c.clock.now += 400
        _run(lambda: c.notify_tick(c.clock()))
        assert len(http.calls) == 2
    finally:
        bridge.remove_hooks(c.schedule_notify)
        bridge.remove_hooks(c.cancel_notify)


def test_notify_secret_rejected_and_unreachable(app_link_home):
    set_channel_secret("veyllo_app", "relay_secret", "vrs_known")
    row = paired_device(SCOPE_A, relay_member=MEMBER)[0]
    devices.outbox_add(row["device_id"], "m1")
    c = _connector(FakeHttp({"POST /v1/relay/notify": [FakeResponse(401)]}))
    c.schedule_notify(devices.get(row["device_id"]))
    c.clock.now += 31
    _run(lambda: c.notify_tick(c.clock()))
    assert c.state == "secret_rejected" and c.last_error == "invalid_relay_secret"
    http = FakeHttp({"POST /v1/relay/notify": [FakeResponse(404)]})
    c = _connector(http)
    c.schedule_notify(devices.get(row["device_id"]))
    c.clock.now += 31
    _run(lambda: c.notify_tick(c.clock()))
    c.clock.now += 400
    _run(lambda: c.notify_tick(c.clock()))
    assert len(http.calls) == 1 and c.state != "secret_rejected"


def test_the_member_and_cid_shapes():
    assert relay._member(MEMBER.upper()) == MEMBER and relay._member("nope") is None
    assert relay._cid_hex(bytes.fromhex(CID)) == CID and relay._cid_hex(CID.upper()) == CID
    assert relay._cid_hex("ab") is None


def test_no_relay_secret_or_token_in_logs_or_events(app_link_home, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    _relay_qr()
    http = FakeHttp({"POST /v1/relay/claim": [_claimed(secret="vrs_secret_value")],
                     "POST /v1/relay/token": [FakeResponse(200, {
                         "token": "tok-secret-value", "url": "wss://relay.test/v1/connect/inst-1",
                         "expiresAt": 1_800_000_600})]})
    connect = FakeConnect()
    c = _connector(http, connect)

    async def _go():
        await c.claim_tick(c.clock())
        await c.tick()
        for _ in range(10):
            await asyncio.sleep(0)
        connect.last.close_from_relay(4003)
        await asyncio.sleep(0.05)
    asyncio.run(_go())
    assert connect.upgrades, "the flow never connected"
    text = caplog.text + repr(app_link_home.events) + json.dumps(c.status())
    for value in ("vrs_secret_value", "tok-secret-value"):
        assert value not in text
