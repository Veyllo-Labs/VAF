# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The invitation names a port somebody actually listens on - or says it is a guess.

The configured HTTPS port and the bound one routinely differ: 443 is privileged,
a desktop VAF cannot bind it and falls back to 8443 by design. The invitation
was built from the configuration alone, so the first cross-machine join in the
field dialled a port nothing listened on, saw only "connection refused", and
cost twenty minutes of port-scanning against a perfectly correct CA fingerprint.

Three sources, most truthful first: the in-process record (the tray hosts both
the proxy and most invitations), the running server's status endpoint (covers
`vaf a2a invite` in its own process), and the configuration - last, and marked
UNCONFIRMED so the invitation can say "this is a guess" instead of asserting it.
"""
import io
import json

import vaf.core.a2a.invite as inv


def _no_runtime(monkeypatch):
    import vaf.network.runtime_status as rs
    monkeypatch.setattr(rs, "effective_https_port", lambda default=None: default)


def _config_port(monkeypatch, port=443):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: port if key == "local_network_https_port" else default))


def test_the_in_process_record_wins(monkeypatch):
    _config_port(monkeypatch)
    import vaf.network.runtime_status as rs
    monkeypatch.setattr(rs, "effective_https_port", lambda default=None: 8443)
    assert inv._effective_port() == (8443, True)


def test_the_running_server_is_asked_when_the_process_knows_nothing(monkeypatch):
    """`vaf a2a invite` runs in its OWN process, where the in-process record is
    empty - the same trap the update spawn walked into. The running server
    answers on the internal channel."""
    _config_port(monkeypatch)
    _no_runtime(monkeypatch)
    import urllib.request

    def fake_urlopen(url, timeout=0):
        assert "127.0.0.1:8005/api/network/status" in url
        return io.BytesIO(json.dumps(
            {"effective_https_port": 8443, "configured_https_port": 443}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert inv._effective_port() == (8443, True)


def test_no_server_means_the_config_port_marked_unconfirmed(monkeypatch):
    _config_port(monkeypatch)
    _no_runtime(monkeypatch)
    import urllib.request

    def refuse(url, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert inv._effective_port() == (443, False)


def test_an_unconfirmed_port_is_said_out_loud_in_the_briefing():
    """The sentence is the fix for the human half: a peer that reads 'this is a
    guess' asks the host instead of port-scanning."""
    text = inv.briefing(
        room_id="room-x", ticket="t-abc", role="peer", display="Guest",
        endpoint={"origin": "wss://192.0.2.1:443",
                  "url": "wss://192.0.2.1:443/ws/a2a/room-x",
                  "ca_fingerprint": "aa:bb", "port_confirmed": False},
    )
    assert "CONFIGURATION" in text
    assert "8443" in text


def test_a_confirmed_port_carries_no_warning():
    text = inv.briefing(
        room_id="room-x", ticket="t-abc", role="peer", display="Guest",
        endpoint={"origin": "wss://192.0.2.1:8443",
                  "url": "wss://192.0.2.1:8443/ws/a2a/room-x",
                  "ca_fingerprint": "aa:bb", "port_confirmed": True},
    )
    assert "CONFIGURATION" not in text
