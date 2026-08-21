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

import threading
import types

import pytest

import vaf.core.browser_interactive as bi
import vaf.core.browser_pool as bp


class _FakeDocker:
    """Scriptable docker CLI: state per container name."""

    def __init__(self):
        self.containers = {}      # name -> state ("running"|"exited")
        self.calls = []
        self.next_ports = {"9222/tcp": "49222", "6901/tcp": "46901"}

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        cmd = args[0]
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


def test_run_hooks_route_to_the_runs_instance(registry, monkeypatch):
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    m_a = _quiet(bi._manager_for_instance(registry.instances["scope-a"]))
    bi.stop_for_agent_run(user_scope_id="scope-a", persistent=True)
    assert m_a._agent_active is True
    assert bi.get_interactive_manager()._agent_active is False
    bi.agent_stream_started("sess-a", user_scope_id="scope-a")
    assert m_a._agent_stream is not None
    bi.agent_run_ended(user_scope_id="scope-a")
    assert m_a._agent_active is False and m_a._agent_stream is None


def test_reaper_asks_the_registry_about_activity(registry):
    registry.instances["scope-a"] = _inst("scope-a", "vaf-browser-u-aaa")
    mgr = _quiet(bi._manager_for_instance(registry.instances["scope-a"]))
    assert bi.peek_manager_for_container("vaf-browser-u-aaa") is mgr
    assert mgr.has_activity() is False
    mgr._agent_active = True
    assert mgr.has_activity() is True
    assert bi.peek_manager_for_container("vaf-browser") is bi.get_interactive_manager()
