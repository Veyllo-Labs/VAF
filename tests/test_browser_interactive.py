# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The interactive-browser lease and its login lane, tested without a container.

Everything network-shaped (CDP resolution, cookie transfer, tab parking) is
patched at the module seam, so these tests pin the DECISIONS: who gets the
lease, when a ticket dies, what an agent run does to a person's session, and
that the storage-state file format round-trips unchanged between the export
and the load side (the agent's persistent sessions read the same files).
"""

import json

import pytest

import vaf.core.browser_interactive as bi


@pytest.fixture
def mgr(monkeypatch, tmp_path):
    """A fresh manager with every network seam stubbed and HOME sandboxed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VAF_USER_SCOPE_ID", raising=False)
    monkeypatch.setattr(bi, "resolve_cdp_ws_url", lambda base: "ws://stub/devtools/browser/x")
    monkeypatch.setattr(bi, "park_browser_idle", lambda base: None)
    cookie_calls = []

    def fake_cookie_op(base, op, cookies=None):
        cookie_calls.append((op, cookies))
        if op == "get":
            return [{"name": "exported", "value": "1", "domain": "example.com", "path": "/",
                     "expires": 123.0, "httpOnly": True, "secure": True, "sameSite": "Strict"}]
        return None

    monkeypatch.setattr(bi, "_cookie_op", fake_cookie_op)
    m = bi.InteractiveBrowserManager()
    emitted = []
    m._emit = lambda payload, session_id: emitted.append((dict(payload), session_id))
    # No janitor thread in unit tests: its 5s cadence would outlive the test.
    m._ensure_janitor = lambda: None
    m._test_cookie_calls = cookie_calls
    m._test_emitted = emitted
    return m


def _no_ipc_tasks(monkeypatch):
    class _FakeIPC:
        def get_active_tasks(self, session_id=None):
            return []

        def get_pending_tasks(self, session_id=None):
            return []

    import vaf.core.subagent_ipc as ipc_mod
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIPC())


# ── lease matrix ──────────────────────────────────────────────────────────

def test_free_lease_is_taken_and_ticket_validates(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    r = mgr.start("scope-a", "sess-1")
    assert r["status"] == "active"
    ticket = r["streamPath"].split("/t/")[1].split("/")[0]
    assert mgr.validate_ticket(ticket) is not None
    assert mgr.validate_ticket("forged") is None


def test_same_window_start_updates_settings_and_keeps_ticket(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    t1 = mgr.start("scope-a", "sess-1")["streamPath"]
    r = mgr.start("scope-a", "sess-1", save=True)
    assert r["status"] == "active" and r["saving"] is True
    # Same window: the running iframe must not lose its ticket over a toggle.
    assert r["streamPath"] == t1


def test_same_scope_other_window_supersedes_with_fresh_ticket(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    t1 = mgr.start("scope-a", "sess-1")["streamPath"]
    old_ticket = t1.split("/t/")[1].split("/")[0]
    r = mgr.start("scope-a", "sess-2")
    assert r["status"] == "active"
    assert r["streamPath"] != t1
    assert mgr.validate_ticket(old_ticket) is None
    # The losing window was told why its stream died.
    assert any(p.get("reason") == "superseded" and sid == "sess-1"
               for p, sid in mgr._test_emitted)


def test_foreign_scope_is_busy_without_owner_details(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1")
    r = mgr.start("scope-b", "sess-9")
    assert r["status"] == "busy"
    # The refusal must not leak who holds the lease.
    assert "scope-a" not in json.dumps(r)


def test_admin_evicts_foreign_lease(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1")
    r = mgr.start("scope-b", "sess-9", is_admin=True)
    assert r["status"] == "active"
    assert mgr._lease.user_scope_id == "scope-b"


def test_stop_by_non_owner_refused_by_owner_allowed(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    r = mgr.start("scope-a", "sess-1")
    ticket = r["streamPath"].split("/t/")[1].split("/")[0]
    assert mgr.stop("user", requester_scope="scope-b")["status"] == "busy"
    assert mgr.validate_ticket(ticket) is not None
    assert mgr.stop("user", requester_scope="scope-a")["status"] == "stopped"
    assert mgr.validate_ticket(ticket) is None


# ── agent coordination ────────────────────────────────────────────────────

def test_agent_takeover_evicts_and_blocks_until_run_ends(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    r = mgr.start("scope-a", "sess-1")
    ticket = r["streamPath"].split("/t/")[1].split("/")[0]
    mgr.stop_for_agent_run()
    assert mgr.validate_ticket(ticket) is None
    assert mgr.start("scope-a", "sess-1")["status"] == "agent_active"
    mgr.agent_run_ended()
    assert mgr.start("scope-a", "sess-1")["status"] == "active"


def test_takeover_skips_tab_parking(mgr, monkeypatch):
    """Parking on takeover would close the very tabs the agent is about to use."""
    _no_ipc_tasks(monkeypatch)
    parked = []
    monkeypatch.setattr(bi, "park_browser_idle", lambda base: parked.append(base))
    mgr.start("scope-a", "sess-1")
    mgr.stop_for_agent_run()
    assert parked == []
    mgr.agent_run_ended()
    mgr.start("scope-a", "sess-1")
    mgr.stop("user", requester_scope="scope-a")
    assert len(parked) == 1


def test_spawned_child_task_counts_as_agent_active(mgr, monkeypatch):
    class _Task:
        agent_type = "browser_agent"

    class _FakeIPC:
        def get_active_tasks(self, session_id=None):
            return [_Task()]

        def get_pending_tasks(self, session_id=None):
            return []

    import vaf.core.subagent_ipc as ipc_mod
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIPC())
    assert mgr.is_agent_active() is True
    assert mgr.start("scope-a", "sess-1")["status"] == "agent_active"


def test_other_subagent_types_do_not_block(mgr, monkeypatch):
    class _Task:
        agent_type = "coding_agent"

    class _FakeIPC:
        def get_active_tasks(self, session_id=None):
            return [_Task()]

        def get_pending_tasks(self, session_id=None):
            return []

    import vaf.core.subagent_ipc as ipc_mod
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _FakeIPC())
    assert mgr.is_agent_active() is False


# ── cookie jar handover and the login lane ────────────────────────────────

def test_foreign_handover_clears_jar_before_loading(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1")
    mgr.stop("user", requester_scope="scope-a")
    mgr.start("scope-b", "sess-2")
    ops = [op for op, _ in mgr._test_cookie_calls]
    assert "clear" in ops, "a scope change must never inherit the previous user's cookies"


def test_save_exports_to_the_scope_file_on_stop(mgr, monkeypatch, tmp_path):
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1", save=True)
    mgr.stop("user", requester_scope="scope-a")
    path = bi.browser_storage_state_path("scope-a", "default")
    state = json.loads(open(path, encoding="utf-8").read())
    assert [c["name"] for c in state["cookies"]] == ["exported"]
    assert state["origins"] == []


def test_stop_without_save_exports_nothing(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1", save=False)
    mgr.stop("user", requester_scope="scope-a")
    assert not any(op == "get" for op, _ in mgr._test_cookie_calls)


# ── storage-state format round-trip (shared with the agent lane) ──────────

def test_cookie_roundtrip_preserves_the_playwright_shape(tmp_path):
    cdp_cookies = [
        {"name": "keep", "value": "v", "domain": ".example.com", "path": "/p",
         "expires": 1900000000.0, "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "session_cookie", "value": "s", "domain": "example.com", "path": "/"},
    ]
    path = str(tmp_path / "state.json")
    bi._export_storage_cookies(path, cdp_cookies)

    state = json.loads(open(path, encoding="utf-8").read())
    assert set(state.keys()) == {"cookies", "origins"}
    # Exactly the 8 fields the agent lane's export writes, defaults included.
    assert state["cookies"][1] == {
        "name": "session_cookie", "value": "s", "domain": "example.com", "path": "/",
        "expires": -1, "httpOnly": False, "secure": False, "sameSite": "Lax",
    }

    loaded = bi._load_storage_cookies(path)
    by_name = {c["name"]: c for c in loaded}
    # A stored session cookie (expires -1) must be loaded WITHOUT the expires
    # key: CDP rejects -1, which is why the agent lane's restore drops it too.
    assert "expires" not in by_name["session_cookie"]
    assert by_name["keep"]["expires"] == 1900000000.0
    assert by_name["keep"]["sameSite"] == "None"


def test_load_tolerates_missing_or_broken_file(tmp_path):
    assert bi._load_storage_cookies(str(tmp_path / "absent.json")) == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert bi._load_storage_cookies(str(broken)) == []


# ── stream presence bookkeeping ───────────────────────────────────────────

def test_stream_connection_counting(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    r = mgr.start("scope-a", "sess-1")
    ticket = r["streamPath"].split("/t/")[1].split("/")[0]
    assert mgr.stream_connected(ticket) is True
    assert mgr.stream_connected("forged") is False
    assert mgr._lease.stream_connections == 1
    before = mgr._lease.last_disconnect
    mgr.stream_disconnected(ticket)
    assert mgr._lease.stream_connections == 0
    assert mgr._lease.last_disconnect >= before


def test_helpers_never_raise_without_a_manager(monkeypatch):
    """The browser_agent hooks run on every tool call; they must be inert on error."""
    monkeypatch.setattr(bi, "get_interactive_manager",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    bi.stop_for_agent_run()
    bi.agent_stream_started("sess-1")
    bi.agent_run_ended()


# ── the run's watch-only stream grant ─────────────────────────────────────

def test_agent_watch_grant_lifecycle(mgr, monkeypatch):
    """The grant's ticket validates for the duration of the run and dies with it."""
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    mgr.stop_for_agent_run()
    mgr.agent_stream_started("sess-run")
    payload, sid = mgr._test_emitted[-1]
    assert sid == "sess-run"
    assert payload["status"] == "agent_active"
    assert "view_only=1" in payload["streamPath"]
    ticket = payload["streamPath"].split("/t/")[1].split("/")[0]
    assert mgr.validate_ticket(ticket) is not None
    mgr.agent_run_ended()
    assert mgr.validate_ticket(ticket) is None


def test_watch_stream_goes_only_to_the_runs_session(mgr, monkeypatch):
    """A start() during a run hands the stream to the run's own session and the
    bare refusal to everyone else - the ticket is the capability."""
    _no_ipc_tasks(monkeypatch)
    mgr.stop_for_agent_run()
    mgr.agent_stream_started("sess-run")
    own = mgr.start("scope-a", "sess-run")
    assert own["status"] == "agent_active" and "view_only=1" in own["streamPath"]
    other = mgr.start("scope-b", "sess-other")
    assert other["status"] == "agent_active" and other["streamPath"] == ""
    mgr.agent_run_ended()


def test_watch_grant_viewer_connected_waits_for_pixels(mgr, monkeypatch):
    """The connecting cover works for the watch grant exactly as for a lease."""
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    mgr.stop_for_agent_run()
    mgr.agent_stream_started("sess-run")
    assert mgr._test_emitted[-1][0]["viewerConnected"] is False
    ticket = mgr._test_emitted[-1][0]["streamPath"].split("/t/")[1].split("/")[0]
    assert mgr.stream_connected(ticket)
    assert mgr.stream_bytes(ticket, 45) is False           # handshake-sized
    assert mgr.stream_bytes(ticket, 49132) is True         # a real frame
    payload, sid = mgr._test_emitted[-1]
    assert sid == "sess-run" and payload["viewerConnected"] is True
    mgr.agent_run_ended()


def test_no_watch_grant_inside_a_spawned_child(mgr, monkeypatch):
    """In the child the singleton is not the one the proxy validates against:
    a grant made there would be a link to a 403, so the helper stands down."""
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    mgr.agent_stream_started("sess-run")
    assert mgr._agent_stream is None and mgr._test_emitted == []


# ── the give-back after an agent takeover ─────────────────────────────────

def test_takeover_remembers_the_holder_and_run_end_says_resumable(mgr, monkeypatch):
    """The run only borrows the browser: the evicted holder's session is told
    resumable on run end, exactly once."""
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1")
    mgr.stop_for_agent_run()
    mgr.agent_run_ended()
    resumable = [(p, sid) for p, sid in mgr._test_emitted
                 if p.get("reason") == "agent_done" and p.get("resumable")]
    assert resumable == [({"status": "stopped", "reason": "agent_done",
                           "saving": False, "streamPath": "", "resumable": True},
                          "sess-1")]
    # Consumed: a second run that evicted nobody offers nobody a give-back.
    mgr.stop_for_agent_run()
    mgr.agent_run_ended()
    assert [1 for p, _ in mgr._test_emitted if p.get("resumable")] == [1]


def test_no_lease_means_no_give_back(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    mgr.stop_for_agent_run()
    mgr.agent_run_ended()
    assert not any(p.get("resumable") for p, _ in mgr._test_emitted)


def test_fresh_lease_clears_a_pending_give_back(mgr, monkeypatch):
    """Spawn lane: the holder survives the marker return (notify=False), but a
    lease the person starts by hand supersedes it - the browser has an owner
    again, and the eventual run end must not re-offer it."""
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1")
    mgr.stop_for_agent_run()
    mgr.agent_run_ended(notify=False)      # marker returned, child still starting
    mgr.start("scope-a", "sess-1")         # person re-opens by hand mid-run
    mgr.stop("user", requester_scope="scope-a")
    mgr.agent_run_ended()
    assert not any(p.get("resumable") for p, _ in mgr._test_emitted)


def test_run_hook_evicts_lease_and_clears_flag_on_spawn(monkeypatch, tmp_path):
    """The spawn lane returns its marker immediately; the in-process flag must
    not outlive run() there, or the interactive browser stays blocked forever
    (the IPC scan of the spawned task is the truth from that point on). Pins
    the hook ORDER in BrowserAgentTool.run(): evict before the spawn branch,
    clear-without-notify on the marker return."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VAF_SPAWN_BROWSER_SUBAGENT", "1")
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    monkeypatch.setattr(bi, "resolve_cdp_ws_url", lambda base: "ws://stub")
    monkeypatch.setattr(bi, "park_browser_idle", lambda base: None)
    monkeypatch.setattr(bi, "_cookie_op", lambda *a, **k: None)
    _no_ipc_tasks(monkeypatch)
    # A fresh singleton for this test; run() reaches it via get_interactive_manager.
    mgr = bi.InteractiveBrowserManager()
    mgr._ensure_janitor = lambda: None
    mgr._emit = lambda payload, session_id: None
    monkeypatch.setattr(bi, "_manager", mgr)

    r = mgr.start("scope-a", "sess-1")
    ticket = r["streamPath"].split("/t/")[1].split("/")[0]

    class _Spawned:
        marker = "[SUBAGENT_ASYNC:stub]"

    import vaf.core.subagent_spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn_subagent", lambda *a, **k: _Spawned())

    from vaf.tools.browser_agent import BrowserAgentTool
    result = BrowserAgentTool().run(task="look something up")
    assert result == _Spawned.marker
    # The person's lease is gone (the agent won)...
    assert mgr.validate_ticket(ticket) is None
    # ...and the in-process flag did not outlive run(): with no IPC task either,
    # the browser is startable again.
    assert mgr.start("scope-a", "sess-1")["status"] == "active"
    mgr.stop("user", requester_scope="scope-a")


def test_snapshot_context_only_for_the_leaseholding_session(mgr, monkeypatch):
    """The browser context may only ride along for the chat that drives the
    browser; every other session gets None BEFORE any network is touched."""
    _no_ipc_tasks(monkeypatch)
    calls = []
    monkeypatch.setattr(bi, "resolve_cdp_ws_url",
                        lambda base: calls.append(base) or (_ for _ in ()).throw(RuntimeError("net")))
    assert mgr.snapshot_context("sess-1") is None          # no lease at all
    monkeypatch.setattr(bi, "resolve_cdp_ws_url", lambda base: "ws://stub")
    mgr.start("scope-a", "sess-1")
    monkeypatch.setattr(bi, "resolve_cdp_ws_url",
                        lambda base: calls.append(base) or (_ for _ in ()).throw(RuntimeError("net")))
    assert mgr.snapshot_context("sess-OTHER") is None      # foreign session
    assert calls == [], "the lease gate must refuse before any CDP resolution"
    # The leaseholder reaches the network lane; a dead network answers None, not a raise.
    assert mgr.snapshot_context("sess-1") is None
    assert calls, "the leaseholder is allowed through to the CDP resolution"
    mgr.stop("user", requester_scope="scope-a")


def test_storage_path_is_scope_segmented(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    p1 = bi.browser_storage_state_path("scope/../evil", "sess/../../name")
    assert ".." not in p1
    assert "browser_sessions" in p1
    p2 = bi.browser_storage_state_path("other", "default")
    assert p1 != p2


# ── the stream URL contract (three live defects came from this one string) ──

def test_stream_path_is_relative_and_carries_the_socket_path(mgr, monkeypatch):
    """Same-origin document + an explicit socket path, both load-bearing.

    RELATIVE: the document must ride the front door the page is already on. An
    absolute http:// URL was measured returning nothing at all, because the
    backend port speaks HTTPS whenever LAN hosting with TLS is on.

    ?path=: the KasmVNC client builds its socket url from SETTINGS
    (ws://<host>:<port>/ + the `path` setting), not relative to its own
    directory, so without this it dials a ticketless /websockify that exists
    nowhere and the viewer never connects.
    """
    _no_ipc_tasks(monkeypatch)
    sp = mgr.start("scope-a", "sess-1")["streamPath"]
    assert sp.startswith("/api/browser-vnc/t/"), "must be relative, not absolute"
    assert "://" not in sp, "no scheme may be baked in here"
    ticket = sp.split("/t/")[1].split("/")[0]
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(sp).query)
    assert q["path"] == [f"api/browser-vnc/t/{ticket}/websockify"]
    # No leading slash: the client appends "/" itself, so one here yields "//…".
    assert not q["path"][0].startswith("/")


def test_https_proxy_relays_the_stream_socket():
    """The LAN lane needs BOTH gates, and the second one hides behind the first.

    The path starts with /api, so the proxy's catch-all API route looks like it
    covers it - it does not, because that route matches HTTP scopes only, and a
    websocket upgrade came back as HTTP 403 with the backend never reached. So
    a WebSocketRoute must exist AND the allowlist must accept the path.
    """
    import secrets
    from starlette.routing import WebSocketRoute
    from vaf.network.https_proxy import _WS_ALLOWED, create_proxy_app

    ticket = secrets.token_urlsafe(24)
    good = f"/api/browser-vnc/t/{ticket}/websockify"
    assert _WS_ALLOWED.match(good), "allowlist must accept a real lease ticket"
    for bad in ("/api/browser-vnc/t/../../etc/websockify",
                "/api/browser-vnc/t//websockify",
                "/api/browser-vnc/t/short/websockify",
                f"/api/browser-vnc/t/{ticket}/other"):
        assert not _WS_ALLOWED.match(bad), f"allowlist must refuse {bad}"
    assert _WS_ALLOWED.match("/ws"), "the WebUI socket must keep working"
    assert _WS_ALLOWED.match("/ws/a2a/room-abc"), "rooms must keep working"

    app = create_proxy_app()
    inner = getattr(app, "app", app)          # unwrap the access-log middleware
    ws_routes = [r for r in inner.routes if isinstance(r, WebSocketRoute)]
    assert any("browser-vnc" in r.path for r in ws_routes), (
        "no WebSocketRoute for the stream: the /api catch-all is HTTP-only, so the "
        "upgrade is refused at the handshake and the allowlist is never consulted"
    )


def test_only_the_stream_viewer_may_be_framed():
    """X-Frame-Options: the viewer is framed BY the web UI, everything else is not.

    The blanket DENY on every backend response is why the interactive browser
    loaded (200 in the access log) and then showed nothing: the browser fetched
    the viewer and refused to paint it inside the window. SAMEORIGIN rather than
    no header at all - the viewer travels the same front door as the UI framing
    it, so a foreign embedder is still refused.
    """
    from vaf.core.web_server import _SecurityHeadersMiddleware as M

    assert M._FRAMEABLE_PREFIX == "/api/browser-vnc/"
    frameable = ["/api/browser-vnc/t/abc/index.html", "/api/browser-vnc/t/abc/assets/x.js"]
    denied = ["/api/version", "/api/sessions", "/", "/api/browser-vnc-other",
              "/login", "/api/memory/search"]
    for p in frameable:
        assert p.startswith(M._FRAMEABLE_PREFIX), p
    for p in denied:
        assert not p.startswith(M._FRAMEABLE_PREFIX), f"{p} must keep DENY"


def test_viewer_connected_waits_for_pixels_not_for_the_socket(mgr, monkeypatch):
    """An open socket is not a picture, and the difference was visible.

    The viewer opens its websocket immediately and then shows its OWN branded
    splash for the whole protocol handshake. Announcing the accept as "connected"
    therefore lifted the window's cover at the exact moment that splash appeared,
    and the person saw it - the cover was correct, its trigger was not.

    Measured against the container: the entire handshake is 45 bytes (greeting 12,
    security types 2, result 4, ServerInit 27); the first framebuffer update is
    49132. The threshold sits between them by two orders of magnitude.
    """
    _no_ipc_tasks(monkeypatch)
    r = mgr.start("scope-a", "sess-1")
    ticket = r["streamPath"].split("/t/")[1].split("/")[0]
    assert r["viewerConnected"] is False

    mgr._test_emitted.clear()
    assert mgr.stream_connected(ticket) is True
    assert mgr._test_emitted == [], (
        "accepting the socket must announce nothing: the splash starts here"
    )

    # The handshake: real sizes, and none of them may flip it.
    for n in (12, 2, 4, 27):
        assert mgr.stream_bytes(ticket, n) is False, f"{n} bytes is handshake, not a picture"
    assert mgr._test_emitted == []
    assert mgr._payload("active")["viewerConnected"] is False

    # The first framebuffer update does.
    assert mgr.stream_bytes(ticket, 49132) is True
    assert [p["viewerConnected"] for p, _ in mgr._test_emitted] == [True], (
        "the picture must announce itself exactly once"
    )

    mgr._test_emitted.clear()
    assert mgr.stream_bytes(ticket, 49132) is True, "already up: the caller may stop reporting"
    assert mgr._test_emitted == [], "a running stream is not a state change"

    mgr._test_emitted.clear()
    assert mgr.stream_bytes("forged", 49132) is True, "a foreign ticket reports nothing"
    assert mgr._test_emitted == []


def test_a_viewer_leaving_makes_the_next_one_wait_for_its_own_picture(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    r = mgr.start("scope-a", "sess-1")
    ticket = r["streamPath"].split("/t/")[1].split("/")[0]
    mgr.stream_connected(ticket)
    mgr.stream_bytes(ticket, 49132)
    assert mgr._payload("active")["viewerConnected"] is True

    mgr._test_emitted.clear()
    mgr.stream_connected(ticket)                 # a second viewer joins
    mgr.stream_disconnected(ticket)              # one of two leaves
    assert mgr._payload("active")["viewerConnected"] is True, "someone is still watching"

    mgr.stream_disconnected(ticket)              # the last one leaves
    assert mgr._payload("active")["viewerConnected"] is False, (
        "the next viewer draws its own picture and shows its own splash while doing so"
    )
    assert [p["viewerConnected"] for p, _ in mgr._test_emitted] == [False]


def test_disconnect_before_the_picture_announces_nothing(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    r = mgr.start("scope-a", "sess-1")
    ticket = r["streamPath"].split("/t/")[1].split("/")[0]
    mgr.stream_connected(ticket)
    mgr.stream_bytes(ticket, 45)                 # handshake only
    mgr._test_emitted.clear()
    mgr.stream_disconnected(ticket)
    assert [p["viewerConnected"] for p, _ in mgr._test_emitted] == [False], (
        "leaving before the picture is still a state report, and it says: not up"
    )
def test_parking_empties_the_window_instead_of_replacing_it(monkeypatch):
    """The browser window is launched in app mode - no tab strip, no toolbar - and
    ONLY that first window is one. A tab created through CDP comes up as an ordinary
    browser window with all its chrome, so parking must never create one: it did,
    and closed the app window along with it (its data: URL is not "about:blank"),
    which is how re-opening the browser showed a browser inside the browser."""
    calls = []
    navigated = []

    class _Resp:
        def __init__(self, body): self._b = body
        def read(self): return self._b.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    listing = json.dumps([
        {"type": "page", "id": "APP", "url": "https://example.com/",
         "webSocketDebuggerUrl": "ws://stub/app"},
        {"type": "page", "id": "EXTRA", "url": "https://other.test/",
         "webSocketDebuggerUrl": "ws://stub/extra"},
        {"type": "background_page", "id": "BG", "url": "chrome://bg"},
    ])

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        return _Resp(listing if url.endswith("/json/list") else "{}")

    import urllib.request as _req
    monkeypatch.setattr(_req, "urlopen", fake_urlopen)
    monkeypatch.setattr(bi, "_navigate_blank", lambda ws: navigated.append(ws))

    bi.park_browser_idle("http://stub:9222")

    assert not any("/json/new" in c for c in calls), (
        "parking must not create a tab - a CDP-made tab is an ordinary window"
    )
    assert any("/json/close/EXTRA" in c for c in calls), "extra pages must be closed"
    assert not any("/json/close/APP" in c for c in calls), "the kept window must survive"
    assert navigated == ["ws://stub/app"], "the kept window is emptied by navigating"


def test_parking_creates_a_tab_only_when_there_is_none(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, body): self._b = body
        def read(self): return self._b.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        return _Resp("[]" if url.endswith("/json/list") else "{}")

    import urllib.request as _req
    monkeypatch.setattr(_req, "urlopen", fake_urlopen)
    bi.park_browser_idle("http://stub:9222")
    assert any("/json/new" in c for c in calls), (
        "with no page at all the next run needs one to attach to"
    )


def test_the_browser_context_block_says_what_can_be_done_with_it():
    """State of the world plus the action it enables, in the SAME block.

    The per-turn injection named the page and the selection and stopped there.
    Measured live: asked to open a page and wait, the agent neither knew the
    person was in the browser nor that browser_agent drives that very browser -
    facts without an affordance taught it nothing. Kept per-turn rather than in
    the standing prompt, so it is present exactly when it is true.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core"
           / "headless_runner.py").read_text(encoding="utf-8")
    block_start = src.index("--- USER IS IN THE INTERACTIVE BROWSER ---")
    block = src[block_start:block_start + 2000]
    assert "browser_agent" in block, (
        "the block must name the tool that can act, or it is a fact with no affordance"
    )
    assert "takes over" in block and "hands control back" in block, (
        "the hand-over must be stated: the model has to know the user loses and regains "
        "control, or it cannot judge whether acting is appropriate"
    )


def test_reopening_waits_for_its_own_picture(mgr, monkeypatch):
    """Every handout starts the wait again, however the window was closed.

    The count was only reset when the LAST viewer disconnected. A lease that
    survived the close - same person, same chat, so start() reuses it - came
    back carrying the previous viewer's count, so the very first payload said
    "picture is up" before a pixel had arrived and the cover never went up. The
    foreign splash was then visible on every open after the first, which is
    exactly what the live test showed.
    """
    _no_ipc_tasks(monkeypatch)
    r = mgr.start("scope-a", "sess-1")
    ticket = r["streamPath"].split("/t/")[1].split("/")[0]
    mgr.stream_connected(ticket)
    mgr.stream_bytes(ticket, 49132)
    assert mgr._payload("active")["viewerConnected"] is True

    # The window is closed in a way that leaves the lease standing (no stop, no
    # disconnect - a chat switch, a dropped socket, a race on the way out).
    again = mgr.start("scope-a", "sess-1")
    assert again["status"] == "active"
    assert again["viewerConnected"] is False, (
        "a handed-out stream has shown nothing yet, whatever the last one managed"
    )
    assert mgr._payload("active")["viewerConnected"] is False


def test_a_new_window_of_the_same_person_also_waits(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    r = mgr.start("scope-a", "sess-1")
    ticket = r["streamPath"].split("/t/")[1].split("/")[0]
    mgr.stream_connected(ticket)
    mgr.stream_bytes(ticket, 49132)
    assert mgr._payload("active")["viewerConnected"] is True
    r2 = mgr.start("scope-a", "sess-2")          # same person, different chat
    assert r2["viewerConnected"] is False
