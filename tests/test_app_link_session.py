# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The inner TLS session: who gets in, on which name, and that nothing is dispatched before
the device is known."""
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app_link_client import AppClient, Pipe, ca_der, link, paired_device
from app_link_env import SCOPE_A, app_link_home  # noqa: F401  (fixture)
from cert_factory import make_ca, make_leaf
from vaf.core.app_link import bridge, devices, pki
from vaf.core.app_link import protocol as pr


@pytest.fixture(autouse=True)
def _fresh_bridge():
    bridge.reset()
    from vaf.core.app_link import pairing
    pairing.reset()
    yield
    bridge.reset()


def _dispatched(monkeypatch):
    seen = []
    monkeypatch.setattr(bridge, "on_hello", lambda s, m: seen.append(("hello", m)))
    monkeypatch.setattr(bridge, "on_message", lambda s, m, files: seen.append(("msg", m)))
    from vaf.core.app_link import pairing
    monkeypatch.setattr(pairing, "on_pair", lambda s, m: seen.append(("pair", m)))
    return seen


def test_pair_name_without_certificate_works(app_link_home):
    pipe = Pipe()
    client = AppClient(ca_der(), sni=pr.SNI_PAIR).attach(pipe).connect()
    assert client.handshaken and client.error is None
    assert pipe.session.state == "pair" and not pipe.session.closed
    assert client.tls.selected_alpn_protocol() == pr.ALPN


def test_link_with_a_paired_device_gets_welcome(app_link_home):
    client, pipe, row = link(SCOPE_A)
    welcome = client.of("welcome")
    assert welcome and welcome[0]["chat"] == devices.chat_session_id(SCOPE_A)
    assert pipe.session.device["device_id"] == row["device_id"]
    assert bridge.is_connected(row["device_id"])


def _refused_link(monkeypatch, *, key=None, der=None, device_id=None):
    # Another device of the account stands, so a lookup that ignored the certificate would
    # find a row; the hello names the device the certificate claims to be.
    if device_id is None:
        device_id = paired_device(SCOPE_A, model="Other")[0]["device_id"]
    seen = _dispatched(monkeypatch)
    pipe = Pipe()
    client = AppClient(ca_der(), sni=pr.SNI_LINK, key=key, cert_der=der).attach(pipe).connect()
    client.send({"t": "hello", "v": 1, "device_id": device_id, "caps": []})
    assert pipe.session.closed, "the session stayed open"
    assert pipe.session.close_reason == "unknown_device" or pipe.session.close_reason.startswith("tls:")
    assert seen == [] and pipe.session.dispatched == 0
    assert not client.of("welcome")
    return pipe


def test_link_without_certificate_is_refused(app_link_home, monkeypatch):
    pipe = _refused_link(monkeypatch)
    assert pipe.session.close_reason == "unknown_device"


def test_link_with_a_foreign_certificate_is_refused(app_link_home, monkeypatch):
    other_ca, other_key = make_ca("Not the device CA")
    key = ec.generate_private_key(ec.SECP256R1())
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import ExtendedKeyUsageOID
    leaf, _ = make_leaf(other_ca, other_key, names=("phone",), key=key,
                        eku=(ExtendedKeyUsageOID.CLIENT_AUTH,))
    pipe = _refused_link(monkeypatch, key=key, der=leaf.public_bytes(serialization.Encoding.DER))
    assert pipe.session.close_reason.startswith("tls:")


def test_link_with_a_device_ca_certificate_without_a_row_is_refused(app_link_home, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    der = pki.issue_device_cert(key.public_key(), "no-row")
    _refused_link(monkeypatch, key=key, der=der)


def test_link_of_a_removed_device_is_refused(app_link_home, monkeypatch):
    row, key, der = paired_device(SCOPE_A)
    devices.remove_device(row["device_id"], by="alice", reason="lost")
    _refused_link(monkeypatch, key=key, der=der, device_id=row["device_id"])


def test_link_with_a_reissued_serial_is_refused(app_link_home, monkeypatch):
    row, key, _der = paired_device(SCOPE_A)
    other = pki.issue_device_cert(key.public_key(), row["device_id"])
    _refused_link(monkeypatch, key=key, der=other, device_id=row["device_id"])


def test_link_of_a_device_whose_account_lane_is_off_is_refused(app_link_home, monkeypatch):
    row, key, der = paired_device(SCOPE_A)
    app_link_home.lane(SCOPE_A, False)
    _refused_link(monkeypatch, key=key, der=der, device_id=row["device_id"])


def test_unknown_or_missing_name_and_missing_alpn_are_refused(app_link_home, monkeypatch):
    seen = _dispatched(monkeypatch)
    for kwargs in ({"sni": "evil.vaf.internal"}, {"sni": None}, {"sni": pr.SNI_PAIR, "alpn": False}):
        pipe = Pipe(peer=f"10.0.0.{len(seen) + 10}")
        AppClient(ca_der(), **kwargs).attach(pipe).connect()
        assert pipe.session.closed, kwargs
        assert pipe.session.state == "closed" and pipe.session.dispatched == 0
    assert seen == []


def test_no_session_ticket_is_issued(app_link_home):
    client, _pipe, _row = link(SCOPE_A)
    assert client.tls.session is None or not client.tls.session.has_ticket


def test_hello_must_name_the_certificates_device(app_link_home):
    client, pipe, row = link(SCOPE_A, hello=False)
    client.send({"t": "hello", "v": 1, "device_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
                 "caps": []})
    assert client.of("error")[0]["code"] == "wrong_device"
    assert pipe.session.closed and not client.of("welcome")


def test_unknown_type_is_answered(app_link_home):
    client, pipe, _row = link(SCOPE_A)
    client.send({"t": "welcome"})
    assert client.of("error")[-1] == {"t": "error", "code": "unknown_type", "ref": "welcome"}
    assert not pipe.session.closed


def test_a_newer_session_replaces_the_older(app_link_home):
    device = paired_device(SCOPE_A)
    first, pipe1, _ = link(SCOPE_A, device=device)
    second, pipe2, _ = link(SCOPE_A, device=device, peer="10.0.0.6", connection="c2")
    first.pump()
    assert pipe1.session.closed and not pipe2.session.closed
    assert {"t": "bye", "reason": "replaced"} in first.frames


def test_handshake_limits(app_link_home):
    from vaf.core.app_link import session as sess
    pipe = Pipe()
    pipe.feed(b"\x16\x03\x01" + b"\x00" * (sess.HANDSHAKE_MAX_BYTES + 10))
    assert pipe.session.closed
    pipe = Pipe(peer="10.0.0.9")
    pipe.session.check_timeouts(pipe.session.started_at + sess.HANDSHAKE_SECONDS + 1)
    assert pipe.session.closed and pipe.session.close_reason == "handshake_timeout"


def test_session_rate_limits_per_peer_and_connection(app_link_home):
    from vaf.core.app_link import session as sess
    made = [Pipe(peer="10.0.0.50", connection="flood").session for _ in range(sess.SESSIONS_PER_MINUTE_PER_PEER + 2)]
    assert made.count(None) == 2
    assert "app_pairing_refused" in app_link_home.kinds()
    others = [Pipe(peer=f"10.1.0.{i}", connection="conn-x").session
              for i in range(sess.SESSIONS_PER_MINUTE_PER_CONNECTION + 1)]
    assert others[-1] is None and all(s is not None for s in others[:-1])


def test_a_pair_session_must_send_its_message_in_time(app_link_home):
    from vaf.core.app_link import session as sess
    pipe = Pipe()
    AppClient(ca_der(), sni=pr.SNI_PAIR).attach(pipe).connect()
    session = pipe.session
    assert session.state == "pair"
    session.check_timeouts(session.handshake_done_at + sess.FIRST_MESSAGE_SECONDS - 1)
    assert not session.closed
    session.check_timeouts(session.handshake_done_at + sess.FIRST_MESSAGE_SECONDS + 1)
    assert session.closed and session.close_reason == "first_message_timeout"


def test_a_link_session_must_say_hello_in_time(app_link_home):
    from vaf.core.app_link import session as sess
    _client, pipe, _row = link(SCOPE_A, hello=False)
    session = pipe.session
    assert session.state == "await_hello"
    session.check_timeouts(session.handshake_done_at + sess.FIRST_MESSAGE_SECONDS + 1)
    assert session.closed


def test_no_big_frame_before_a_device_is_known(app_link_home):
    from vaf.core.app_link import session as sess
    pipe = Pipe()
    client = AppClient(ca_der(), sni=pr.SNI_PAIR).attach(pipe).connect()
    client.send_raw(bytes([1]) + (sess.PRE_OPEN_MAX_PAYLOAD + 1).to_bytes(4, "big") + b"x" * 1000)
    assert pipe.session.closed and pipe.session.close_reason == "bad_frame"
    # Once the device said hello, a frame bigger than that is a normal message.
    client, pipe, row = link(SCOPE_A, peer="10.0.0.9", connection="c9")
    text = "x" * (sess.PRE_OPEN_MAX_PAYLOAD + 1000)
    client.send({"t": "msg", "id": "0f8fad5b-d9cb-469f-a165-70867728950e",
                 "chat": devices.chat_session_id(SCOPE_A), "text": text})
    assert not pipe.session.closed
    assert {"t": "ack", "id": "0f8fad5b-d9cb-469f-a165-70867728950e"} in client.frames


def test_a_finished_file_waits_for_its_siblings_without_timing_out(app_link_home, monkeypatch):
    import hashlib

    from vaf.core.app_link import session as sess
    monkeypatch.setattr(bridge, "on_message", lambda s, m, files: None)
    client, pipe, row = link(SCOPE_A)
    a, b = b"A" * 100, b"B" * 200
    xa, xb = b"\x01" * 16, b"\x02" * 16
    client.send({"t": "msg", "id": "0f8fad5b-d9cb-469f-a165-70867728950e",
                 "chat": devices.chat_session_id(SCOPE_A), "text": "two files",
                 "attachments": [
                     {"xfer": pr.b64url(xa), "name": "a.txt", "mime": "text/plain", "size": len(a),
                      "sha256": hashlib.sha256(a).hexdigest()},
                     {"xfer": pr.b64url(xb), "name": "b.txt", "mime": "text/plain", "size": len(b),
                      "sha256": hashlib.sha256(b).hexdigest()}]})
    client.send_raw(pr.encode_chunk(xa, 0, a))
    client.send_raw(pr.encode_chunk(xb, 0, b[:100]))
    session = pipe.session
    transfers = session._transfers
    transfers[xa].last_at -= sess.TRANSFER_SILENCE_SECONDS + 30   # A finished long ago
    session.check_timeouts(transfers[xb].last_at + 1)              # B is still arriving
    assert xa in transfers and xb in transfers
    assert not [f for f in client.frames if f.get("code") == "xfer_abort"]


def test_an_empty_message_is_refused(app_link_home):
    client, _pipe, row = link(SCOPE_A)
    client.send({"t": "msg", "id": "0f8fad5b-d9cb-469f-a165-70867728950e",
                 "chat": devices.chat_session_id(SCOPE_A), "text": "  "})
    assert {"t": "error", "code": "bad_field", "ref": "text"} in client.frames
    from vaf.core.task_queue import TaskQueue
    assert not TaskQueue().has_queued(devices.chat_session_id(SCOPE_A))


def test_a_long_connected_device_keeps_counting_as_used(app_link_home, monkeypatch):
    client, _pipe, row = link(SCOPE_A)
    touched = []
    monkeypatch.setattr(devices, "touch", lambda did, transport, cid=None: touched.append(did))
    client.send({"t": "ack", "id": "0f8fad5b-d9cb-469f-a165-70867728950e"})
    assert touched == []                                   # just touched at hello
    monkeypatch.setitem(bridge._touched, row["device_id"], -10_000.0)
    client.send({"t": "ack", "id": "0f8fad5b-d9cb-469f-a165-70867728950e"})
    client.send({"t": "ack", "id": "0f8fad5b-d9cb-469f-a165-70867728950e"})
    assert touched == [row["device_id"]]


def test_a_big_frame_right_after_hello_in_one_read_is_taken(app_link_home):
    from vaf.core.app_link import session as sess
    client, pipe, row = link(SCOPE_A, hello=False)
    text = "y" * (sess.PRE_OPEN_MAX_PAYLOAD + 2000)
    both = pr.encode_json({"t": "hello", "v": 1, "device_id": row["device_id"], "caps": []}) \
        + pr.encode_json({"t": "msg", "id": "0f8fad5b-d9cb-469f-a165-70867728950e",
                          "chat": devices.chat_session_id(SCOPE_A), "text": text})
    client.send_raw(both)              # one TLS write: both frames arrive in one read
    assert not pipe.session.closed, pipe.session.close_reason
    assert client.of("welcome") and {"t": "ack", "id": "0f8fad5b-d9cb-469f-a165-70867728950e"} in client.frames
