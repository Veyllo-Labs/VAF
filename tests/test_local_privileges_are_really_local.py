# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: privileges meant for "the person sitting at this machine" must not reach the LAN.

VAF deliberately gives the local operator conveniences the network must not get: a long-lived
token (there is no CLI login, the person is physically at the keyboard), and an exemption from
re-proving 2FA when refreshing. Both were keyed on ``request.client.host``.

That stopped meaning what it says the moment the integrated HTTPS proxy appeared: it terminates
TLS on 0.0.0.0 and relays every LAN device to the backend over loopback, so the peer is
127.0.0.1 for phones and laptops too. The rules kept working as written and silently applied to
the whole network - a 30-day token for any device that logged in, and the 2FA shortcut for any
remote admin refreshing a session.

The fix does not change the policy. It restores the ability to tell who is actually local, which
is what the policy always meant. These tests pin BOTH directions: the local operator keeps every
convenience, and a LAN device gets none of them.
"""
from starlette.requests import Request

from vaf.api.auth_routes import _client_ip

LAN_DEVICE = "192.168.1.42"
PROXY_PEER = ("127.0.0.1", 40000)  # what the backend sees for anything the proxy relays


def _request(peer, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": headers,
        "client": peer,
    })


def _is_treated_as_local(peer, xff: str | None = None) -> bool:
    """Mirrors the check both privilege sites perform on the resolved address."""
    return _client_ip(_request(peer, xff)) in ("127.0.0.1", "::1", "localhost")


def test_lan_device_is_not_treated_as_the_local_operator():
    """THE regression, and it covers both privileges at once: the long-lived token
    (auth_routes login) and the 2FA exemption on refresh both branch on this answer.

    Pre-fix the proxy peer made this True for every LAN device.
    """
    assert _is_treated_as_local(PROXY_PEER, LAN_DEVICE) is False


def test_the_person_at_the_machine_keeps_their_privileges():
    """The intent must survive the fix. VAF has no CLI login and the local operator is
    physically at the keyboard, so they keep the long-lived token and the 2FA shortcut."""
    # Direct loopback (desktop through the Next.js /api route: no forwarding header at all).
    assert _is_treated_as_local(PROXY_PEER) is True
    # Same-host browser relayed by the proxy (the OAuth callback shape): the hop is loopback too.
    assert _is_treated_as_local(PROXY_PEER, "127.0.0.1") is True


def test_a_lan_device_cannot_claim_to_be_local():
    """A direct (non-relayed) client keeps its socket address, so a forged header buys nothing.
    Relayed clients cannot forge either: the proxy strips client-supplied copies before setting
    its own (see tests/test_proxied_lan_auth.py)."""
    assert _is_treated_as_local((LAN_DEVICE, 50000), "127.0.0.1") is False


def test_provenance_and_audit_record_the_device_not_the_proxy():
    """Session provenance ("where am I logged in from") and the security event log both used the
    peer, so every LAN login and every failed attempt was recorded as 127.0.0.1 - useless for
    telling a mistyped password from an intruder."""
    assert _client_ip(_request(PROXY_PEER, LAN_DEVICE)) == LAN_DEVICE
