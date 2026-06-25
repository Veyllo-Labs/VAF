"""Regression: the integrated HTTPS proxy must construct without crashing.

Starlette 1.0 REMOVED the on_startup/on_shutdown __init__ arguments in favor of a `lifespan` context
manager. The proxy still passed `on_shutdown=[_shutdown_clients]`, so create_proxy_app() raised
`TypeError: Starlette.__init__() got an unexpected keyword argument 'on_shutdown'` at startup — the proxy
never bound the LAN port and remote devices got connection-refused. These tests pin the modern API and
that the shared httpx clients are still closed on shutdown.
"""
import asyncio

import vaf.network.https_proxy as proxy


def test_create_proxy_app_does_not_raise():
    app = proxy.create_proxy_app()
    assert app is not None  # would TypeError on the old on_shutdown= API


def test_lifespan_closes_httpx_clients():
    class _FakeClient:
        def __init__(self):
            self.is_closed = False

        async def aclose(self):
            self.is_closed = True

    fc, bc = _FakeClient(), _FakeClient()
    proxy._frontend_client = fc
    proxy._backend_client = bc

    app = proxy.create_proxy_app()

    async def _drive():
        msgs = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

        async def receive():
            return next(msgs)

        async def send(_msg):
            pass

        await app({"type": "lifespan"}, receive, send)

    asyncio.run(_drive())
    # The lifespan shutdown must have awaited _shutdown_clients(), closing both pooled clients.
    assert fc.is_closed and bc.is_closed


def test_stop_https_proxy_signals_exit_and_resets_status():
    """Disabling LAN/TLS (or a restart) must actually STOP the proxy — otherwise the LAN port stays open
    after hosting is turned off. stop_https_proxy() signals the server to exit and clears the runtime
    status so the UI no longer advertises a port nothing serves."""
    from vaf.network import runtime_status

    class _Srv:
        def __init__(self):
            self.should_exit = False

    runtime_status.set_proxy_bound(8443, 443)
    srv = _Srv()
    proxy._running_server = srv
    proxy.stop_https_proxy()
    assert srv.should_exit is True
    assert proxy._running_server is None
    assert runtime_status.get_proxy_status()["bound"] is False
