# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A relay that misbehaves gets nothing through: broken bytes close the session with nothing
dispatched, a stream moved to another cid is still the device its certificate names, a flood
of handshakes is cut, and a frame without the relay head is dropped."""
import pytest

from app_link_client import AppClient, ca_der, paired_device
from app_link_env import SCOPE_A, app_link_home  # noqa: F401  (fixture)
from app_link_relay_stub import RelayPipe
from cert_factory import make_ca
from vaf.core.app_link import bridge, devices, pairing
from vaf.core.app_link import protocol as pr
from vaf.core.app_link.relay import RelayLink

CID_A = bytes.fromhex("aa" * 16)
CID_B = bytes.fromhex("bb" * 16)
UID = "0f8fad5b-d9cb-469f-a165-70867728950e"


@pytest.fixture(autouse=True)
def _fresh():
    bridge.reset()
    pairing.reset()
    yield
    bridge.reset()


class _Relay:
    """One relay connection: frames from VAF go to the pipe of their cid."""

    def __init__(self, connection="r1"):
        self.pipes = {}
        self.link = RelayLink(self._from_vaf, connection)

    def _from_vaf(self, frame):
        cid, _ = pr.unwrap_relay(frame)
        assert cid != bytes(16), "VAF never sends the broadcast cid"
        pipe = self.pipes.get(cid)
        if pipe is not None:
            pipe.deliver(frame)

    def app(self, cid, device, mode="clean", route=None):
        row, key, der = device
        pipe = RelayPipe(self.link, cid, mode, route=route)
        self.pipes[cid] = pipe
        client = AppClient(ca_der(), sni=pr.SNI_LINK, key=key, cert_der=der).attach(pipe).connect()
        client.send({"t": "hello", "v": 1, "device_id": row["device_id"], "caps": []})
        return client, pipe


def _dispatched(monkeypatch):
    seen = []
    real = bridge.on_message
    monkeypatch.setattr(bridge, "on_message", lambda s, m, files: (seen.append(m), real(s, m, files)))
    return seen


@pytest.mark.parametrize("mode", ["tamper", "replay", "reorder", "truncate"])
def test_broken_bytes_close_the_session_with_nothing_dispatched(app_link_home, monkeypatch, mode):
    seen = _dispatched(monkeypatch)
    relay = _Relay()
    client, pipe = relay.app(CID_A, paired_device(SCOPE_A), mode=mode)
    assert client.of("welcome")
    session = relay.link.sessions[CID_A]
    pipe.armed = True
    client.send({"t": "msg", "id": UID, "chat": devices.chat_session_id(SCOPE_A), "text": "one"})
    client.send({"t": "msg", "id": "1" + UID[1:], "chat": devices.chat_session_id(SCOPE_A), "text": "two"})
    client.send({"t": "msg", "id": "2" + UID[1:], "chat": devices.chat_session_id(SCOPE_A), "text": "three"})
    assert session.closed and session.close_reason.startswith(("tls:", "bad_frame")), session.close_reason
    if mode == "replay":
        # The first copy is genuine and arrives; its replay is what is refused.
        assert [m["text"] for m in seen] == ["one"]
    else:
        assert seen == []


def test_a_stream_moved_to_another_cid_is_still_its_device(app_link_home):
    relay = _Relay()
    device_a = paired_device(SCOPE_A, model="Phone A")
    device_b = paired_device(SCOPE_A, model="Phone B")
    client_b, _pipe_b = relay.app(CID_B, device_b)
    client_a, _pipe_a = relay.app(CID_A, device_a)
    assert relay.link.sessions[CID_A].device["device_id"] == device_a[0]["device_id"]
    # The relay reports B gone and carries A's next connection behind B's cid.
    relay.link.on_control({"t": "app", "cid": CID_B.hex(), "on": False})
    assert CID_B not in relay.link.sessions
    moved, _pipe = relay.app(CID_B, device_a)
    assert moved.of("welcome")
    session = relay.link.sessions[CID_B]
    assert session.device["device_id"] == device_a[0]["device_id"]
    assert session.relay_cid == CID_B.hex()
    # A keeps the cid it was first seen on: the address does not move the device.
    assert devices.get(device_a[0]["device_id"])["relay_cid"] == CID_A.hex()


def test_a_redirect_to_another_vaf_fails_the_ca_pin(app_link_home):
    relay = _Relay()
    other_ca, _ = make_ca("Another VAF")
    from cryptography.hazmat.primitives import serialization
    row, key, der = paired_device(SCOPE_A)
    pipe = RelayPipe(relay.link, CID_A)
    relay.pipes[CID_A] = pipe
    client = AppClient(other_ca.public_bytes(serialization.Encoding.DER), sni=pr.SNI_LINK,
                       key=key, cert_der=der).attach(pipe).connect()
    assert client.error is not None and "CERTIFICATE_VERIFY_FAILED" in str(client.error)
    assert not client.of("welcome")


def test_a_handshake_flood_is_cut(app_link_home):
    from vaf.core.app_link import session as sess
    relay = _Relay("flood")
    hello = b"\x16\x03\x01\x00\x05hello"
    for _ in range(sess.SESSIONS_PER_MINUTE_PER_PEER + 3):
        relay.link.sessions.pop(CID_A, None)
        relay.link.on_binary(pr.wrap_relay(CID_A, hello))
    per_cid = [k for k in app_link_home.kinds() if k == "app_pairing_refused"]
    assert len(per_cid) == 3
    for i in range(sess.SESSIONS_PER_MINUTE_PER_CONNECTION + 5):
        relay.link.on_binary(pr.wrap_relay(bytes([i + 1]) * 16, hello))
    alive = [s for s in relay.link.sessions.values() if s is not None]
    assert len(alive) <= sess.SESSIONS_PER_MINUTE_PER_CONNECTION


def test_a_frame_without_the_head_is_dropped(app_link_home):
    relay = _Relay()
    for frame in (b"\x02" + CID_A + b"tls", b"\x01" + b"\x00" * 10, b"", b"\x01" + bytes(16) + b"x"):
        relay.link.on_binary(frame)
    assert relay.link.sessions == {}
    assert bridge._all == set()


def test_the_table_is_pruned_from_a_snapshot(app_link_home):
    """A closed session is taken out while the table is walked: iterating the live dict
    would raise "dictionary changed size during iteration"."""
    gone = []
    link = RelayLink(lambda frame: None, "r2", gone=gone.append)
    for i in range(3):
        link.on_binary(pr.wrap_relay(bytes([i + 1]) * 16, b"\x16\x03\x01\x02\x00" + b"\x01" * 3))
    for session in link.sessions.values():
        session.close("gone", bye=False)
    link._prune()
    assert link.sessions == {} and sorted(gone) == [bytes([i + 1]) * 16 for i in range(3)]
