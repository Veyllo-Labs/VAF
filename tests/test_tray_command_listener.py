# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The tray's singleton listener survives a misbehaving client.

It is how a second start asks the running VAF to open its window (ACTIVATE), and since
`instance.find_service()` it is also asked "is VAF up?" by connections that send nothing.
Measured on macOS against a copy of the loop: ONE reset connection ended the listener
thread for good, and one client that connected and stayed silent held it forever in a
blocking recv. Either way every later ACTIVATE was lost, and after the backlog filled up
the port refused connections, so find_service() answered "not running" for a running VAF.
"""
import socket
import struct
import threading
import time

import pytest


@pytest.fixture
def listener(monkeypatch):
    import vaf.tray as tray

    opened = []
    monkeypatch.setattr(tray, "open_webui", lambda icon: opened.append(True))
    monkeypatch.setattr(tray.tray_context, "should_exit", False)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    thread = threading.Thread(target=tray.command_listener, args=(srv,), daemon=True)
    thread.start()
    yield port, opened, thread
    monkeypatch.setattr(tray.tray_context, "should_exit", True)
    thread.join(timeout=5)
    srv.close()


def _activate(port):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
        c.sendall(b"ACTIVATE")


def _wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_a_reset_connection_does_not_end_the_listener(listener):
    """MUTATION: `break` on a per-connection error again and the ACTIVATE is never seen."""
    port, opened, thread = listener
    rst = socket.create_connection(("127.0.0.1", port), timeout=2)
    rst.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    rst.close()
    time.sleep(0.3)
    _activate(port)
    assert _wait_for(lambda: opened), "the listener died on one reset connection"
    assert thread.is_alive()


def test_a_silent_client_does_not_hold_the_listener(listener):
    """MUTATION: drop the timeout on the accepted socket and the listener waits on the
    silent client forever."""
    port, opened, thread = listener
    silent = socket.create_connection(("127.0.0.1", port), timeout=2)
    try:
        time.sleep(0.2)
        _activate(port)
        assert _wait_for(lambda: opened, timeout=4), "a silent client blocked the listener"
    finally:
        silent.close()


def test_a_probe_that_sends_nothing_opens_no_window(listener):
    port, opened, thread = listener
    for _ in range(20):
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    time.sleep(0.5)
    assert opened == []
    assert thread.is_alive()
