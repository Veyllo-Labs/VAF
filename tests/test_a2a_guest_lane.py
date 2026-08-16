# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The VAF-less lane: what a harness with no VAF gets from the invitation and the host.

The room's server is the router, so an invitation must be enough on its own. That
splits into three promises, each pinned here: the briefing renders the guest section
exactly when a wire endpoint exists; the checksum it prints is computed from the file
it names; and the two downloads answer without an account, because a guest holds a
join ticket and nothing else.
"""
import hashlib
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.invite import guest_client_path, guest_client_sha256, invitation
from vaf.core.a2a.room import Room

ROOT = Path(__file__).resolve().parents[1]
WIRE_PEER = ROOT / "examples" / "12_a2a_wire_peer.py"


@pytest.fixture()
def circle(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    room = Room.create(kind="round", owner_scope="scope-a", base=tmp_path,
                       room_id="room-guest", topic="Guest lane")
    owner = room.join(display="VAF", scope_id="scope-a", peer_id="p-owner")
    return room, owner


# ── the briefing side ──────────────────────────────────────────────────────

def test_the_guest_section_rides_the_wire_endpoint(circle, monkeypatch):
    """MUTATION: drop the guest block, or render it without the checksum.

    A briefing that only speaks vaf commands hands a VAF-less harness an
    invitation it cannot act on; a download instruction without the checksum
    hands it code over a channel it cannot verify. The two belong together or
    not at all.
    """
    monkeypatch.setattr("vaf.core.a2a.invite.lan_endpoint",
                        lambda room_id: {"origin": "wss://h:8443",
                                         "url": f"wss://h:8443/ws/a2a/{room_id}",
                                         "ca_fingerprint": "cd" * 32})
    room, owner = circle
    row = invitation(room, owner)

    assert row["client_url"] == "https://h:8443/api/a2a/client.py"
    assert row["client_sha256"] == guest_client_sha256()
    assert row["client_url"] in row["briefing"]
    assert row["client_sha256"] in row["briefing"]
    assert (f"a2a_client.py join --url wss://h:8443/ws/a2a/room-guest"
            in row["briefing"].replace("\\\n       ", ""))
    assert row["ticket"] in row["briefing"]


def test_without_an_endpoint_there_is_no_guest_section(circle, monkeypatch):
    """Without wss there is no lane a VAF-less guest could use - the local lane
    IS the vaf command - so naming the client would promise a door that is not
    there."""
    monkeypatch.setattr("vaf.core.a2a.invite.lan_endpoint", lambda room_id: {})
    room, owner = circle
    row = invitation(room, owner)

    assert "client_url" not in row
    assert "a2a_client.py" not in row["briefing"]


def test_the_checksum_is_computed_from_the_file_it_names():
    """MUTATION: hardcode the hash. The next edit to the wire peer would make
    every invitation in the world name a checksum the download fails."""
    assert guest_client_path() == WIRE_PEER
    assert guest_client_sha256() == hashlib.sha256(WIRE_PEER.read_bytes()).hexdigest()


def test_a_wheel_install_points_at_the_repository(monkeypatch):
    """No file, no checksum, no dead download line - the repository copy instead."""
    monkeypatch.setattr("vaf.core.a2a.invite.guest_client_path", lambda: None)
    from vaf.core.a2a.invite import GUEST_CLIENT_REPO_URL, briefing
    text = briefing(room_id="room-guest", ticket="t-ab12", role="peer",
                    display="guest", endpoint={"origin": "wss://h:8443",
                                               "url": "wss://h:8443/ws/a2a/room-guest",
                                               "ca_fingerprint": "cd" * 32})
    assert GUEST_CLIENT_REPO_URL in text
    assert "/api/a2a/client.py" not in text


# ── the download side ──────────────────────────────────────────────────────

def test_the_download_endpoints_need_no_account():
    """MUTATION: drop the exemptions. A guest holds a ticket, not an account;
    behind auth both downloads answer 401 and the whole lane is a promise the
    server refuses to keep."""
    from vaf.auth.middleware import AUTH_EXEMPT_PATHS
    assert "/api/a2a/client.py" in AUTH_EXEMPT_PATHS
    assert "/api/a2a/ca.pem" in AUTH_EXEMPT_PATHS


@pytest.fixture()
def client():
    # No context manager, deliberately: entering one fires the app's startup
    # handlers, which spawn the server's real background machinery into the
    # test process and pollute the rest of the suite. The routes under test
    # need no startup state; every other TestClient in this suite does the same.
    from fastapi.testclient import TestClient

    from vaf.core.web_server import app
    return TestClient(app)


def test_the_host_serves_the_client_file_byte_for_byte(client):
    response = client.get("/api/a2a/client.py")
    assert response.status_code == 200
    assert response.content == WIRE_PEER.read_bytes(), (
        "the download differs from the file whose checksum the invitation carries")


def test_a_host_without_the_file_names_the_repository(client, monkeypatch):
    monkeypatch.setattr("vaf.core.a2a.invite.guest_client_path", lambda: None)
    response = client.get("/api/a2a/client.py")
    assert response.status_code == 404
    assert "raw.githubusercontent.com" in response.json()["detail"]


def test_the_ca_download_is_honest_about_a_missing_authority(client, monkeypatch, tmp_path):
    monkeypatch.setattr("vaf.network.ssl_utils.ca_certificate_path", lambda: None)
    assert client.get("/api/a2a/ca.pem").status_code == 404

    pem = tmp_path / "ca.pem"
    pem.write_bytes(b"-----BEGIN CERTIFICATE-----\nab12\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr("vaf.network.ssl_utils.ca_certificate_path", lambda: pem)
    response = client.get("/api/a2a/ca.pem")
    assert response.status_code == 200
    assert response.content == pem.read_bytes()
