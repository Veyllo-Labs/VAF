# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The per-user browser pool, tested without docker.

The docker CLI sits behind one seam (`browser_pool._docker`); these tests pin
the DECISIONS: the pool is off by default and never surprises a small install,
capacity and the memory floor refuse politely (shared fallback, never an
error), instances are adopted across VAF restarts, container names carry a
scope HASH rather than the scope, and the manager registry hands out one
manager per instance with tickets routing to the right one.
"""

import types

import pytest

import vaf.core.browser_interactive as bi
import vaf.core.browser_pool as bp


class _FakeDocker:
    """Scriptable docker CLI: state per container name."""

    def __init__(self):
        self.containers = {}      # name -> state ("running"|"exited")
        self.networks = set()
        self.calls = []
        self.next_ports = {"9222/tcp": "49222", "6901/tcp": "46901"}

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "network":
            if args[1] == "create":
                self.networks.add(args[-1])
                return _done(0, args[-1])
            if args[1] == "inspect":
                name = args[2]
                return _done(0, name) if name in self.networks else _done(1, "", "no such network")
            return _done(1, "", f"unhandled network verb: {args}")
        if cmd == "inspect":
            name = args[1]
            if args[-1].startswith("{{.Config.Image}}"):
                if name in self.containers or name == "vaf-browser":
                    return _done(0, "vaf-vaf-browser\tvaf_vaf-browser-network")
                return _done(1, "", "no such container")
            if name in self.containers:
                return _done(0, self.containers[name])
            if name == "vaf-browser":
                return _done(0, "running")
            return _done(1, "", "no such container")
        if cmd == "ps":
            running = [n for n, s in self.containers.items() if s == "running"]
            return _done(0, "\n".join(running))
        if cmd == "run":
            name = args[args.index("--name") + 1]
            self.containers[name] = "running"
            return _done(0, "containerid")
        if cmd == "start":
            self.containers[args[1]] = "running"
            return _done(0, args[1])
        if cmd == "stop":
            self.containers[args[-1]] = "exited"
            return _done(0, "")
        if cmd == "port":
            port = self.next_ports.get(args[2])
            return _done(0, f"127.0.0.1:{port}") if port else _done(1, "")
        return _done(1, "", f"unhandled: {args}")


def _done(code, out="", err=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.delenv("VAF_BROWSER_POOL_MAX", raising=False)
    fake = _FakeDocker()
    monkeypatch.setattr(bp, "_docker", fake)
    monkeypatch.setattr(bp, "_mem_available_mb", lambda: 16000)
    p = bp.BrowserPool()
    p._ensure_reaper = lambda: None            # no threads in unit tests
    monkeypatch.setattr(p, "_wait_healthy", lambda inst: True)
    p._test_docker = fake
    return p


def test_pool_is_off_by_default_and_touches_nothing(pool):
    assert pool.resolve("scope-a") is None
    assert pool._test_docker.calls == []


def test_instance_is_created_with_hashed_name_and_loopback_ports(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    inst = pool.resolve("scope-a")
    assert inst is not None
    assert inst.container_name.startswith("vaf-browser-u-")
    assert "scope-a" not in inst.container_name        # scope never in container listings
    assert inst.cdp_base == "http://127.0.0.1:49222"
    assert inst.vnc_base == "http://127.0.0.1:46901"
    run_call = next(c for c in pool._test_docker.calls if c[0] == "run")
    assert "127.0.0.1::9222" in run_call and "127.0.0.1::6901" in run_call
    # per-user profile volume, also hashed
    assert any(a.startswith("vaf-browser-profile-") and ":/home/browser" in a for a in run_call)
    # the filtering resolvers compose gives the shared container are passed here too
    assert "1.1.1.2" in run_call and "1.0.0.2" in run_call
    # and the host-gateway name render_check aims localhost targets at
    assert "host.docker.internal:host-gateway" in run_call


def test_each_instance_gets_a_network_of_its_own(pool, monkeypatch):
    """Inside the container CDP and KasmVNC listen on 0.0.0.0 with no auth -
    safe only because nothing can reach them. On one shared bridge network a
    page in A's browser could dial B's container IP and drive it, which is the
    isolation the pool is sold as providing."""
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    a = pool.resolve("scope-a")
    b = pool.resolve("scope-b")
    nets = [c[c.index("--network") + 1] for c in pool._test_docker.calls if c[0] == "run"]
    assert len(nets) == 2 and nets[0] != nets[1]
    assert all(n.startswith("vaf-browser-net-") for n in nets)
    # never the shared browser's own network
    assert "vaf_vaf-browser-network" not in nets
    assert a.container_name != b.container_name


def test_capacity_gate_answers_shared_fallback(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "1")
    assert pool.resolve("scope-a") is not None
    assert pool.resolve("scope-b") is None             # full: shared browser, not an error
    # the first scope keeps its instance through the gate
    assert pool.resolve("scope-a") is not None


def test_memory_floor_refuses_new_instances(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "4")
    monkeypatch.setattr(bp, "_mem_available_mb", lambda: 800)
    assert pool.resolve("scope-a") is None


def test_exited_instance_is_adopted_and_restarted(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    name = "vaf-browser-u-" + bp._scope_hash("scope-a")
    pool._test_docker.containers[name] = "exited"      # left over from an earlier VAF process
    inst = pool.resolve("scope-a")
    assert inst is not None and inst.container_name == name
    assert ["start", name] in pool._test_docker.calls
    assert not any(c[0] == "run" for c in pool._test_docker.calls)


def test_peek_never_calls_docker(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    assert pool.peek("scope-a") is None
    assert pool._test_docker.calls == []
    pool.resolve("scope-a")
    n = len(pool._test_docker.calls)
    assert pool.peek("scope-a") is not None
    assert len(pool._test_docker.calls) == n


def test_docker_failure_falls_back_to_the_shared_browser(pool, monkeypatch):
    monkeypatch.setenv("VAF_BROWSER_POOL_MAX", "2")
    monkeypatch.setattr(bp, "_docker", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no docker")))
    assert pool.resolve("scope-a") is None


# ── manager registry (vaf/core/browser_interactive.py) ─────────────────────

@pytest.fixture
def registry(monkeypatch, tmp_path):
    """A clean manager registry with a stubbed pool and stubbed network seams."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VAF_USER_SCOPE_ID", raising=False)
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    monkeypatch.delenv("VAF_BROWSER_SCRUB", raising=False)
    monkeypatch.setattr(bi, "resolve_cdp_ws_url", lambda base: "ws://stub")
    monkeypatch.setattr(bi, "park_browser_idle", lambda base: None)
    monkeypatch.setattr(bi, "_cookie_op", lambda *a, **k: None)
    monkeypatch.setattr(bi, "_manager", None)
    monkeypatch.setattr(bi, "_pool_managers", {})

    class _StubPool:
        def __init__(self):
            self.instances = {}
        def resolve(self, scope):
            return self.instances.get(scope)
        def peek(self, scope):
            return self.instances.get(scope)

    stub = _StubPool()
    import vaf.core.browser_pool as bp_mod
    monkeypatch.setattr(bp_mod, "get_browser_pool", lambda: stub)
    return stub


def _inst(scope, name, cdp="http://127.0.0.1:41111", vnc="http://127.0.0.1:42222"):
    import time
    return bp.BrowserInstance(user_scope_id=scope, container_name=name,
                              cdp_base=cdp, vnc_base=vnc, last_used=time.time())


def _quiet(mgr):
    mgr._emit = lambda payload, session_id: None
    mgr._ensure_janitor = lambda: None
    return mgr


def test_scopes_with_instances_get_their_own_manager(registry, monkeypatch):
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    m_a = bi.get_manager_for_scope("scope-a")
    m_b = bi.get_manager_for_scope("scope-b")          # no instance: shared
    assert m_a is not m_b
    assert m_a.container_name() == "vaf-browser-u-aaa"
    assert m_a.cdp_base() == "http://127.0.0.1:41111"
    assert m_b is bi.get_interactive_manager()
    # stable: the same scope gets the same manager again
    assert bi.get_manager_for_scope("scope-a") is m_a


def test_parallel_leases_on_different_instances(registry, monkeypatch):
    """The pool's whole point: two users drive two browsers at the same time."""
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    registry.instances["scope-b"] = _inst("scope-b", "vaf-browser-u-bbb",
                                          cdp="http://127.0.0.1:43333", vnc="http://127.0.0.1:44444")
    m_a = _quiet(bi.get_manager_for_scope("scope-a"))
    m_b = _quiet(bi.get_manager_for_scope("scope-b"))
    import vaf.core.subagent_ipc as ipc_mod

    class _FakeIPC:
        def get_active_tasks(self, session_id=None):
            return []
        def get_pending_tasks(self, session_id=None):
            return []

    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIPC())
    r_a = m_a.start("scope-a", "sess-a")
    r_b = m_b.start("scope-b", "sess-b")
    assert r_a["status"] == "active" and r_b["status"] == "active"
    # Tickets route to the manager (and so the endpoint) that issued them.
    t_a = r_a["streamPath"].split("/t/")[1].split("/")[0]
    t_b = r_b["streamPath"].split("/t/")[1].split("/")[0]
    assert bi.get_manager_by_ticket(t_a) is m_a
    assert bi.get_manager_by_ticket(t_b) is m_b
    assert bi.get_manager_by_ticket("forged") is None
    # Session lookup finds the right browser for the chat-context snapshot.
    assert bi.manager_for_session("sess-a") is m_a
    assert bi.manager_for_session("sess-b") is m_b


def test_stop_resolution_never_starts_a_container(registry, monkeypatch):
    """A stop must land on the browser that holds the lease, and must never
    resolve (= possibly start) an instance to do so."""
    calls = []
    registry.resolve = lambda scope: calls.append(scope) or None
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    m_a = _quiet(bi._manager_for_instance(registry.instances["scope-a"]))
    import vaf.core.subagent_ipc as ipc_mod

    class _FakeIPC:
        def get_active_tasks(self, session_id=None):
            return []
        def get_pending_tasks(self, session_id=None):
            return []

    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIPC())
    m_a.start("scope-a", "sess-a")
    assert bi.get_manager_for_stop("scope-a", "sess-a") is m_a
    assert bi.get_manager_for_stop("scope-a", None) is m_a
    assert calls == []
    # nobody holds anything for this scope: the shared manager answers (a no-op stop)
    assert bi.get_manager_for_stop("scope-x", None) is bi.get_interactive_manager()


def test_run_hooks_route_by_the_container_the_run_pinned(registry, monkeypatch):
    """Addressed by CONTAINER, never re-resolved by scope: the pool's
    scope-to-instance map is mutable, so a scope lookup can answer differently
    at the end of a run than at its start - and clearing _agent_active on the
    wrong manager leaves the real one flagged for the life of the process."""
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    m_a = _quiet(bi._manager_for_instance(registry.instances["scope-a"]))
    bi.stop_for_agent_run(container_name="vaf-browser-u-aaa")
    assert m_a._agent_active is True
    assert bi.get_interactive_manager()._agent_active is False
    bi.agent_stream_started("sess-a", container_name="vaf-browser-u-aaa")
    assert m_a._agent_stream is not None
    # The pool forgets the instance mid-run (idle stop, capacity change): the
    # end hook must still land on the manager that was flagged.
    registry.instances.pop("scope-a")
    bi.agent_run_ended(container_name="vaf-browser-u-aaa")
    assert m_a._agent_active is False and m_a._agent_stream is None


def test_a_dedicated_instance_is_never_scrubbed_as_a_stranger(registry, monkeypatch):
    """A pooled manager starts life knowing whose browser it is. Without that
    its own user reads as an unknown jar on the first use after every process
    start - and in full mode the profile wipe would delete the history,
    passwords and downloads the per-user volume exists to keep."""
    monkeypatch.setenv("VAF_BROWSER_SCRUB", "full")
    wipes, ops = [], []
    monkeypatch.setattr(bi, "request_profile_wipe", lambda *a: wipes.append(a))
    monkeypatch.setattr(bi, "_cookie_op", lambda base, op, cookies=None: ops.append(op))
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    mgr = _quiet(bi._manager_for_instance(registry.instances["scope-a"]))
    assert mgr._dedicated_scope == "scope-a"
    # Half one of the fix: a fresh manager already knows whose browser this is,
    # so its own user cannot read as an unknown jar on the first use.
    assert mgr._last_cookie_scope == "scope-a"
    mgr.hand_jar_to_run(user_scope_id="scope-a", persistent=True)
    assert wipes == [] and ops == []
    # The clean-start promise of a non-persistent run still holds, without a wipe.
    mgr.hand_jar_to_run(user_scope_id="scope-a", persistent=False)
    assert wipes == [] and ops == ["scrub"]
    # Half two: the guard itself. Even asked for a scope that is not this
    # browser's, a dedicated instance is never profile-wiped - the volume holds
    # one person's history, passwords and downloads, and nobody else's.
    ops.clear()
    mgr.hand_jar_to_run(user_scope_id="scope-somebody-else", persistent=True)
    assert wipes == []


def test_the_jar_handover_sits_after_the_gate_and_after_the_watchdog():
    """Two orderings this lane got wrong in one round, both static and both
    cheap to pin: a handover BEFORE the concurrency gate scrubs a browser
    another run is still driving, and a handover before the stop watchdog is
    armed swallows a Stop for as long as an unresponsive browser makes it wait
    (up to the 30s CDP deadline)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "vaf" / "tools" / "browser_agent.py").read_text(encoding="utf-8")
    run_body = src.split("    def run(self, **kwargs) -> str:", 1)[1]
    run_body = run_body.split("    async def _run_browser", 1)[0]
    assert "hand_jar_to_run" not in run_body, \
        "the handover must not run before the concurrency gate"
    inner = src.split("    async def _run_browser", 1)[1]
    assert "hand_jar_to_run" in inner
    assert inner.index("browser-stop-watchdog") < inner.index("hand_jar_to_run"), \
        "the stop watchdog must be armed before the handover can block"
    # and it must not block the event loop while it waits
    handover = inner[inner.index("hand_jar_to_run") - 400:inner.index("hand_jar_to_run") + 200]
    assert "run_in_executor" in handover


def test_watch_only_is_enforced_by_the_relay_not_by_the_viewer():
    """A run's grant carries view_only in the client's URL and the window puts
    pointer-events off on top of it, but both live in the PAGE: anything that
    speaks the stream socket could still send RFB pointer and key events and
    drive the browser the agent is working in. The relay must therefore drop
    client-to-container traffic on an agent grant. Static guard, because the
    check lives in a closure inside the websocket route."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    route = src.split('@app.websocket("/api/browser-vnc/t/{ticket}/websockify")', 1)[1]
    route = route.split("\n@app.", 1)[0]
    assert "watch_only = isinstance(" in route and "AgentStream" in route
    upstream = route.split("async def _to_upstream", 1)[1].split("async def _to_client", 1)[0]
    assert "if watch_only:" in upstream
    # and it must not fall through to a send on that branch
    assert upstream.index("if watch_only:") < upstream.index("upstream.send")


def test_reaper_asks_the_registry_about_activity(registry):
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    mgr = _quiet(bi._manager_for_instance(registry.instances["scope-a"]))
    assert bi.peek_manager_for_container("vaf-browser-u-aaa") is mgr
    assert mgr.has_activity() is False
    mgr._agent_active = True
    assert mgr.has_activity() is True
    assert bi.peek_manager_for_container("vaf-browser") is bi.get_interactive_manager()
