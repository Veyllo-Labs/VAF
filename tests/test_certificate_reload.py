# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A changed certificate reaches every running TLS listener without a restart.

Against a real uvicorn TLS server on a free loopback port: a new connection gets the new
certificate, a connection opened before keeps answering, a pair that fails the throwaway
check never reaches the live context, and a live load that fails is repaired with the
previous pair. Measured before building: a failed load into the LIVE context leaves it
without a certificate, and every later handshake fails until a good pair is loaded.
"""
import ast
import asyncio
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from cert_factory import fingerprint, isolated_home, key_pem, make_ca, make_key, make_leaf, pem
from vaf.network import ssl_utils

ROOT = Path(__file__).resolve().parents[1]
TLS_ON = {"local_network_tls_enabled": True}


async def _app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", b"2")]})
    await send({"type": "http.response.body", "body": b"ok"})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Listener:
    """uvicorn serving TLS from ``cert``/``key`` in a thread, registered for the reload."""

    def __init__(self, cert, key):
        self.port = _free_port()
        config = uvicorn.Config(_app, host="127.0.0.1", port=self.port, log_config=None,
                                log_level="critical", lifespan="off",
                                **ssl_utils.server_tls_options(str(cert), str(key)))
        self.server = uvicorn.Server(config)
        self.server.install_signal_handlers = lambda: None
        self.loop = asyncio.new_event_loop()
        ssl_utils.register_tls_listener("test", self.server, self.loop)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.server.serve())

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.loop.close()


def _client_context(*authorities) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.load_verify_locations(cadata=pem(*authorities).decode())
    return context


def _connect(port, context):
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    return context.wrap_socket(raw)


def _ask(conn) -> bytes:
    conn.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    data = b""
    while not data.endswith(b"ok"):
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _served_leaf(port, context) -> str:
    with _connect(port, context) as conn:
        der = conn.getpeercert(binary_form=True)
        assert _ask(conn).startswith(b"HTTP/1.1 200")
    from cryptography import x509
    return fingerprint(x509.load_der_x509_certificate(der))


@pytest.fixture
def pairs(tmp_path, monkeypatch):
    """VAF's own pair served by a running listener, and an own pair to switch to."""
    import vaf.network.binding as binding
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("enp3s0", "192.168.1.20")])
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["vafhost"])
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    cert, key = ssl_utils.ensure_ssl_certificates()
    local_ca = ssl_utils._leaf_of(str(ssl_dir / "ca.pem"))
    root, root_key = make_ca("Example Root")
    leaf, leaf_key = make_leaf(root, root_key, names=("vafhost", "192.168.1.20", "127.0.0.1"))
    listener = _Listener(cert, key)
    yield {"ssl_dir": ssl_dir, "local_ca": local_ca, "root": root, "leaf": leaf,
           "leaf_key": leaf_key, "listener": listener,
           "first": ssl_utils._leaf_of(cert)}
    listener.stop()


def test_a_new_handshake_gets_the_new_certificate_without_a_restart(pairs):
    """AC 11. A connection opened before the switch still answers; a new one gets the own
    certificate. MUTATION: drop the live load (reload only the files) - red."""
    port = pairs["listener"].port
    context = _client_context(pairs["local_ca"], pairs["root"])
    assert _served_leaf(port, context) == fingerprint(pairs["first"])
    before = _connect(port, context)
    assert _ask(before).startswith(b"HTTP/1.1 200")

    result = ssl_utils.install_certificate(pem(pairs["leaf"], pairs["root"]),
                                           key_pem(pairs["leaf_key"]))

    assert result.reload.listeners == {"test": "loaded"} and result.reload.ok
    assert _served_leaf(port, context) == fingerprint(pairs["leaf"])
    assert _ask(before).startswith(b"HTTP/1.1 200"), "the open connection was dropped"
    before.close()


def test_a_pair_that_fails_the_check_never_reaches_the_live_context(pairs, monkeypatch):
    """AC 11. MUTATION: skip the throwaway check and load directly - red (the listener is
    touched: restored instead of untouched)."""
    port = pairs["listener"].port
    wrong = pairs["ssl_dir"] / "wrong"
    wrong.mkdir()
    (wrong / "c.pem").write_bytes(pem(pairs["leaf"], pairs["root"]))
    (wrong / "k.pem").write_bytes(key_pem(make_key()))
    monkeypatch.setattr(ssl_utils, "ensure_ssl_certificates",
                        lambda: (str(wrong / "c.pem"), str(wrong / "k.pem")))

    result = ssl_utils.reload_certificate()

    assert result.refused == "key_mismatch" and result.listeners == {}
    context = _client_context(pairs["local_ca"], pairs["root"])
    assert _served_leaf(port, context) == fingerprint(pairs["first"])


def test_a_live_load_that_fails_is_repaired_with_the_previous_pair(pairs, monkeypatch):
    """AC 11: a pair that passed the check and then fails on the listener (changed on disk
    in between). MUTATION: drop the restore of the previous pair - red (failed, and every
    handshake fails)."""
    port = pairs["listener"].port
    wrong = pairs["ssl_dir"] / "wrong"
    wrong.mkdir()
    (wrong / "c.pem").write_bytes(pem(pairs["leaf"], pairs["root"]))
    (wrong / "k.pem").write_bytes(key_pem(make_key()))
    monkeypatch.setattr(ssl_utils, "ensure_ssl_certificates",
                        lambda: (str(wrong / "c.pem"), str(wrong / "k.pem")))
    monkeypatch.setattr(ssl_utils, "_load_problem", lambda *_a: None)

    result = ssl_utils.reload_certificate()

    assert result.listeners == {"test": "restored"} and not result.ok
    context = _client_context(pairs["local_ca"], pairs["root"])
    assert _served_leaf(port, context) == fingerprint(pairs["first"])


def test_a_listener_that_kept_its_pair_keeps_its_files(tmp_path, monkeypatch):
    """AC 10: the files of an earlier own certificate are removed only once nothing serves
    them. A listener that had to keep its previous pair keeps that pair's files."""
    import types
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    custom = ssl_dir / "custom"
    custom.mkdir(parents=True)
    old_cert, old_key = custom / "old.pem", custom / "old.key"
    stray = custom / "stray.pem"
    for path in (old_cert, old_key, stray):
        path.write_bytes(b"x")

    class _Context:
        def load_cert_chain(self, cert, key, password=None):
            if cert != str(old_cert):
                raise ssl.SSLError("refused")

    server = types.SimpleNamespace(started=True, should_exit=False, config=types.SimpleNamespace(
        ssl=_Context(), ssl_certfile=str(old_cert), ssl_keyfile=str(old_key)))
    ssl_utils.register_tls_listener("kept", server, None)
    root, root_key = make_ca()
    leaf, key = make_leaf(root, root_key)
    ssl_utils.install_certificate(pem(leaf, root), key_pem(key))

    assert old_cert.exists() and old_key.exists(), "the files a listener still serves went"
    assert not stray.exists()


# ── every listener registers, and nobody else reads the keys ───────────────

def _functions_calling(name):
    found = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        tree = ast.parse(path.read_bytes())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = {getattr(c.func, "id", getattr(c.func, "attr", ""))
                         for c in ast.walk(node) if isinstance(c, ast.Call)}
                if name in calls:
                    found.append((path.relative_to(ROOT).as_posix(), node.name, calls))
    return found


def test_every_tls_listener_registers():
    """AC 12: each function that builds a listener from server_tls_options also registers it
    for the reload. MUTATION: delete the registration in run_server - red."""
    builders = _functions_calling("server_tls_options")
    assert {(f, n) for f, n, _c in builders} == {
        ("vaf/tray.py", "start_uvicorn"),
        ("vaf/network/https_proxy.py", "run_https_proxy"),
        ("vaf/core/web_server.py", "run_server"),
    }, "the scan found another TLS listener: it must register too"
    missing = [(f, n) for f, n, calls in builders if "register_tls_listener" not in calls]
    assert not missing, f"a TLS listener does not register for the reload: {missing}"


def test_the_listeners_take_their_pair_from_ensure():
    """AC 12: the tray and run_server serve what ensure_ssl_certificates() returns (the
    proxy is handed the tray's pair)."""
    builders = {n: calls for _f, n, calls in _functions_calling("server_tls_options")}
    assert "ensure_ssl_certificates" in builders["start_uvicorn"]
    assert "ensure_ssl_certificates" in builders["run_server"]


def test_nobody_reads_the_certificate_keys():
    """AC 12: the two keys are read in ssl_utils only (config.py holds their defaults).
    MUTATION: re-add the read in the tray - red."""
    readers = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ("vaf/network/ssl_utils.py", "vaf/core/config.py"):
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        if "local_network_ssl_cert" in text or "local_network_ssl_key" in text:
            readers.append(rel)
    assert readers == []


def test_a_load_the_listener_never_got_to_is_cancelled(monkeypatch):
    """A listener whose loop is blocked past the wait: the load is cancelled, so the answer
    (timeout) is true afterwards too; it said "failed" while the queued load could still
    apply the pair later. MUTATION: drop the cancel and the cancelled flag - red (the load
    runs once the loop is free). The flag alone closes the window between the wait's end
    and the cancel, which no test can interleave on purpose."""
    import threading
    monkeypatch.setattr(ssl_utils, "_RELOAD_WAIT_S", 0.3)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    while not loop.is_running():
        time.sleep(0.01)
    release = threading.Event()
    loop.call_soon_threadsafe(lambda: release.wait(5))
    ran = []
    assert ssl_utils._run_on_loop(loop, lambda: ran.append(1) or "loaded") == "timeout"
    release.set()
    time.sleep(0.3)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()
    assert ran == []


def test_what_a_listener_loaded_is_served_until_a_load_succeeds(tmp_path, monkeypatch):
    """`vaf server cert reset` and a renewal rewrite server.pem in place; a listener whose
    reload then timed out still serves the previous certificate, and the invitation and
    /api/a2a/ca.pem must name that one, not the file. MUTATION: read the listener's file
    path again in served_chain - red."""
    import types
    from cert_factory import fingerprint
    ssl_dir = isolated_home(tmp_path, monkeypatch, **TLS_ON)
    import vaf.network.binding as binding
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("enp3s0", "192.168.1.20")])
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    cert, key = ssl_utils.ensure_ssl_certificates()
    first = ssl_utils.served_authority()[1]

    class _Context:
        def load_cert_chain(self, *_a, **_kw):
            raise AssertionError("not reached: the wait times out first")

    server = types.SimpleNamespace(started=True, should_exit=False, config=types.SimpleNamespace(
        ssl=_Context(), ssl_certfile=cert, ssl_keyfile=key))
    ssl_utils.register_tls_listener("proxy", server, None)
    monkeypatch.setattr(ssl_utils, "_run_on_loop", lambda _loop, _fn: "timeout")
    result = ssl_utils.renew_authority()                        # rewrites server.pem in place

    assert result.reload.listeners == {"proxy": "timeout"}
    assert fingerprint(ssl_utils._leaf_of(cert)) != fingerprint(ssl_utils.served_chain()[0])
    assert ssl_utils.served_authority()[1] == first, "the authority the listener serves"
    monkeypatch.setattr(ssl_utils, "_run_on_loop", lambda _loop, fn: fn())
    server.config.ssl = types.SimpleNamespace(load_cert_chain=lambda *_a, **_kw: None)
    assert ssl_utils.reload_certificate().listeners == {"proxy": "loaded"}
    assert ssl_utils.served_authority()[1] == result.new_authority
