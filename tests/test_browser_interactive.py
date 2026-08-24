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
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    monkeypatch.delenv("VAF_BROWSER_SCRUB", raising=False)
    monkeypatch.setattr(bi, "resolve_cdp_ws_url", lambda base: "ws://stub/devtools/browser/x")
    monkeypatch.setattr(bi, "park_browser_idle", lambda base: None)
    monkeypatch.setattr(bi, "_current_page", lambda base: None)
    monkeypatch.delenv("VAF_BROWSER_DOWNLOADS", raising=False)
    dl_policy = []
    monkeypatch.setattr(bi, "_set_download_behavior",
                        lambda base, allow: dl_policy.append(allow))
    dl_purges = []
    monkeypatch.setattr(bi, "_purge_container_downloads",
                        lambda name: dl_purges.append(name))
    dl_sweeps = []
    monkeypatch.setattr(bi, "_sweep_container_downloads",
                        lambda name, scope: (dl_sweeps.append((name, scope)) or []))
    ws_syncs = []
    monkeypatch.setattr(bi, "_sync_workspace_to_container",
                        lambda name, scope, prev: (ws_syncs.append((name, scope)) or (("sig",), [])))
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
    m._test_dl_policy = dl_policy
    m._test_dl_purges = dl_purges
    m._test_dl_sweeps = dl_sweeps
    m._test_ws_syncs = ws_syncs
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

def test_foreign_handover_scrubs_jar_before_loading(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1")
    mgr.stop("user", requester_scope="scope-a")
    before = len(mgr._test_cookie_calls)
    mgr.start("scope-b", "sess-2")
    ops = [op for op, _ in mgr._test_cookie_calls[before:]]
    # Scrub, not just a cookie clear: localStorage/IndexedDB carry logins on
    # token-based sites exactly as cookies do.
    assert "scrub" in ops, "a scope change must never inherit the previous user's state"


def test_first_start_after_process_boot_scrubs_the_unknown_jar(mgr, monkeypatch):
    """A fresh manager knows nothing about the jar while the container may
    still hold anyone's state; trusting that gap is how residue crosses
    users. The same scope refreshing afterwards keeps its state."""
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1")
    assert [op for op, _ in mgr._test_cookie_calls].count("scrub") == 1
    mgr.start("scope-a", "sess-1")
    assert [op for op, _ in mgr._test_cookie_calls].count("scrub") == 1


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


def test_the_takeover_hook_touches_no_jar(mgr, monkeypatch):
    """It fires at the top of run(), before the concurrency gate, so a scrub in
    there lands on a browser another run may still be driving: that run would
    be logged out of every site mid-task, and in full mode its Chromium killed.
    The eviction (and its cookie EXPORT) is all this hook may do."""
    _no_ipc_tasks(monkeypatch)
    monkeypatch.setenv("VAF_BROWSER_SCRUB", "full")
    wipes = []
    monkeypatch.setattr(bi, "request_profile_wipe", lambda *a: wipes.append(a))
    mgr.start("scope-a", "sess-1", save=True)
    before = len(mgr._test_cookie_calls)
    wipes.clear()                    # the lease start did its own handover
    mgr.stop_for_agent_run()
    ops = [op for op, _ in mgr._test_cookie_calls[before:]]
    assert "scrub" not in ops and wipes == []
    assert "get" in ops              # the evicted holder's cookies are saved
    mgr.agent_run_ended()


def test_agent_run_hands_the_jar_to_the_runs_scope(mgr, monkeypatch):
    """The agent lane shares the one jar (browser_use works in the default
    context, measured), so a run of a different scope must scrub the previous
    holder's residue before it browses - after the gate, via this hook."""
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1", save=True)
    mgr.stop_for_agent_run()                     # exports scope-a's cookies
    before = len(mgr._test_cookie_calls)
    mgr.hand_jar_to_run(user_scope_id="scope-b", persistent=True)
    assert "scrub" in [op for op, _ in mgr._test_cookie_calls[before:]]
    assert mgr._last_cookie_scope == "scope-b"
    # Ordering across the two hooks: the export happened before the scrub.
    all_ops = [op for op, _ in mgr._test_cookie_calls]
    assert all_ops.index("get") < len(all_ops) - 1
    mgr.agent_run_ended()


def test_non_persistent_run_starts_clean_even_for_the_same_scope(mgr, monkeypatch):
    """"Starts with a clean browser" is the tool's documented promise; a
    persistent run of the same scope keeps its own state instead."""
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1")
    mgr.stop("user", requester_scope="scope-a")
    before = len(mgr._test_cookie_calls)
    mgr.hand_jar_to_run(user_scope_id="scope-a", persistent=False)
    assert "scrub" in [op for op, _ in mgr._test_cookie_calls[before:]]
    before = len(mgr._test_cookie_calls)
    mgr.hand_jar_to_run(user_scope_id="scope-a", persistent=True)
    assert "scrub" not in [op for op, _ in mgr._test_cookie_calls[before:]]


def test_full_mode_wipes_the_profile_only_on_a_scope_change(mgr, monkeypatch):
    """VAF_BROWSER_SCRUB=full adds the profile wipe (history, saved passwords,
    downloads) on a change of hands; a same-scope clean start stays quick."""
    _no_ipc_tasks(monkeypatch)
    monkeypatch.setenv("VAF_BROWSER_SCRUB", "full")
    wipes = []
    monkeypatch.setattr(bi, "request_profile_wipe", lambda *a: wipes.append(a))
    mgr.start("scope-a", "sess-1")
    assert len(wipes) == 1                       # unknown jar counts as a change
    mgr.stop("user", requester_scope="scope-a")
    mgr.hand_jar_to_run(user_scope_id="scope-b", persistent=True)
    assert len(wipes) == 2
    mgr.hand_jar_to_run(user_scope_id="scope-b", persistent=False)
    assert len(wipes) == 2                       # same scope: quick scrub only


def test_takeover_hands_the_persons_page_to_the_runs_scope(mgr, monkeypatch):
    """A takeover of a live session is a HANDOVER: the run learns where the
    person was, once, and only the run of the same scope."""
    _no_ipc_tasks(monkeypatch)
    monkeypatch.setattr(bi, "_current_page",
                        lambda base: {"url": "https://shop.example/cart", "title": "Cart"})
    mgr.start("scope-a", "sess-1")
    mgr.stop_for_agent_run()
    # A foreign scope learns nothing - and does not consume it either.
    assert mgr.take_agent_handover("scope-b") is None
    h = mgr.take_agent_handover("scope-a")
    assert h is not None and h["url"] == "https://shop.example/cart"
    # Consume-once: a later unrelated run must not inherit "continue here".
    assert mgr.take_agent_handover("scope-a") is None
    mgr.agent_run_ended()


def test_handover_expires_and_an_idle_takeover_has_none(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    # No lease: nobody handed anything over.
    mgr.stop_for_agent_run()
    assert mgr.take_agent_handover("scope-a") is None
    mgr.agent_run_ended()
    monkeypatch.setattr(bi, "_current_page",
                        lambda base: {"url": "https://shop.example/", "title": ""})
    mgr.start("scope-a", "sess-1")
    mgr.stop_for_agent_run()
    with mgr._lock:
        mgr._handover["at"] -= mgr.HANDOVER_TTL_S + 1
    assert mgr.take_agent_handover("scope-a") is None
    mgr.agent_run_ended()


def test_the_person_returning_outdates_the_handover(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    monkeypatch.setattr(bi, "_current_page",
                        lambda base: {"url": "https://shop.example/", "title": ""})
    mgr.start("scope-a", "sess-1")
    mgr.stop_for_agent_run()
    mgr.agent_run_ended()
    mgr.start("scope-a", "sess-1")      # the person came back themselves
    mgr.stop("user", requester_scope="scope-a")
    mgr.stop_for_agent_run()            # an idle takeover much later
    assert mgr.take_agent_handover("scope-a") is None
    mgr.agent_run_ended()


def test_a_continuing_run_never_scrubs_the_session_it_was_handed(mgr, monkeypatch):
    """The clean-start promise yields to the handover: a run that takes over a
    live session must not log out the very session it is continuing."""
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1")
    mgr.stop_for_agent_run()
    before = len(mgr._test_cookie_calls)
    mgr.hand_jar_to_run(user_scope_id="scope-a", persistent=False, continuing=True)
    assert "scrub" not in [op for op, _ in mgr._test_cookie_calls[before:]]
    mgr.agent_run_ended()


def test_give_back_keeps_the_browser_unparked_until_claimed(mgr, monkeypatch):
    """The person continues where the agent stopped, in BOTH directions: the
    run's end-of-task parking stands down while a give-back is pending, and
    the fallback parker fires only if nobody claims the browser."""
    _no_ipc_tasks(monkeypatch)
    parked = []
    monkeypatch.setattr(bi, "park_browser_idle", lambda base: parked.append(base))
    mgr._arm_unclaimed_parker = lambda: None      # no sleeping threads in unit tests
    mgr.start("scope-a", "sess-1")
    mgr.stop_for_agent_run()
    assert mgr.give_back_pending() is True        # the run's parking must skip
    mgr.agent_run_ended()
    assert mgr.give_back_pending() is False       # consumed with the give-back
    # Unclaimed: nobody took the browser back -> the fallback parks it.
    assert mgr._park_if_unclaimed() is True and len(parked) == 1
    # Claimed: the person's window resumed -> their state stays untouched.
    mgr.start("scope-a", "sess-1")
    assert mgr._park_if_unclaimed() is False and len(parked) == 1
    mgr.stop("user", requester_scope="scope-a")


# ── downloads: the container is a hand-off point, not storage ─────────────

def test_downloads_flow_to_the_holder_and_die_on_a_scope_change(mgr, monkeypatch):
    """The person's finished downloads leave with THEM (janitor tick, lease
    end, takeover), and a scope change purges the folder instead of delivering
    a previous holder's files into the next person's workspace."""
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1")
    assert mgr._test_dl_policy[-1] is True          # downloads allowed by default
    mgr.stop("user", requester_scope="scope-a")
    assert ("vaf-browser", "scope-a") in mgr._test_dl_sweeps
    before = len(mgr._test_dl_purges)
    mgr.start("scope-b", "sess-2")                  # different scope takes over
    assert len(mgr._test_dl_purges) == before + 1
    mgr.stop("user", requester_scope="scope-b")
    # Same-scope refresh purges nothing - their own files are on their way.
    before = len(mgr._test_dl_purges)
    mgr.start("scope-b", "sess-2")
    assert len(mgr._test_dl_purges) == before
    mgr.stop("user", requester_scope="scope-b")


def test_a_runs_downloads_reach_the_runs_own_scope(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    mgr.stop_for_agent_run()
    mgr.hand_jar_to_run(user_scope_id="scope-b", persistent=True)
    assert mgr._test_dl_policy[-1] is True
    mgr.agent_run_ended()
    assert ("vaf-browser", "scope-b") in mgr._test_dl_sweeps


def test_downloads_off_denies_in_the_browser_and_never_sweeps(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    monkeypatch.setenv("VAF_BROWSER_DOWNLOADS", "off")
    mgr.start("scope-a", "sess-1")
    assert mgr._test_dl_policy[-1] is False         # deny, enforced browser-side
    mgr.stop("user", requester_scope="scope-a")
    assert mgr._test_dl_sweeps == []


def test_workspace_mirrors_in_at_lease_start_and_resets_on_scope_change(mgr, monkeypatch):
    """The reverse lane: the holder's files appear in the browser from the
    first moment, the mirror belongs to the jar owner, and a scope change
    starts it over (after the purge) instead of trusting a stale signature."""
    _no_ipc_tasks(monkeypatch)
    mgr.start("scope-a", "sess-1")
    assert ("vaf-browser", "scope-a") in mgr._test_ws_syncs
    assert mgr._ws_sig == ("sig",)
    mgr.stop("user", requester_scope="scope-a")
    mgr.start("scope-b", "sess-2")                  # change of hands
    # The reset happened before the new sync stored its own signature.
    assert ("vaf-browser", "scope-b") in mgr._test_ws_syncs
    mgr.stop("user", requester_scope="scope-b")


def test_workspace_sync_off_mirrors_nothing(mgr, monkeypatch):
    _no_ipc_tasks(monkeypatch)
    monkeypatch.setenv("VAF_BROWSER_WORKSPACE_SYNC", "off")
    mgr.start("scope-a", "sess-1")
    assert mgr._test_ws_syncs == []
    mgr.stop("user", requester_scope="scope-a")


def test_workspace_mirror_walk_caps_and_signature(monkeypatch, tmp_path):
    """The module half: hidden files stay out, oversized files stay out, the
    total cap stops the walk, an unchanged tree costs no copy, and the paths
    list (the agent's upload whitelist) is answered either way. Without the
    mgr fixture - it stubs this very function."""
    monkeypatch.setenv("HOME", str(tmp_path))
    import types as _types
    import vaf.core.browser_pool as bp
    import vaf.core.session as session_mod

    root = tmp_path / "P" / "ab12cd34"
    (root / "sub").mkdir(parents=True)
    (root / "report.pdf").write_bytes(b"x" * 100)
    (root / "sub" / "notes.txt").write_bytes(b"y" * 50)
    (root / ".hidden").write_bytes(b"z")
    big = root / "huge.bin"
    big.write_bytes(b"0")
    import os as _os
    _os.truncate(big, bi._WS_SYNC_FILE_MAX + 1)

    calls = []

    def fake_docker(args, timeout=60):
        calls.append(list(args))
        return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bp, "_docker", fake_docker)
    monkeypatch.setattr(session_mod, "get_user_projects_root", lambda scope: root)

    sig, paths = bi._sync_workspace_to_container("vaf-browser", "scope-a", None)
    assert sorted(paths) == ["/home/browser/Workspace/report.pdf",
                             "/home/browser/Workspace/sub/notes.txt"]
    assert any(c[0] == "cp" for c in calls)
    # Unchanged tree: the signature short-circuits, no second copy.
    calls.clear()
    sig2, paths2 = bi._sync_workspace_to_container("vaf-browser", "scope-a", sig)
    assert sig2 == sig and sorted(paths2) == sorted(paths)
    assert not any(c[0] == "cp" for c in calls)
    # A new file changes the signature and triggers a copy again.
    (root / "new.md").write_bytes(b"n" * 10)
    sig3, paths3 = bi._sync_workspace_to_container("vaf-browser", "scope-a", sig)
    assert sig3 != sig and "/home/browser/Workspace/new.md" in paths3
    assert any(c[0] == "cp" for c in calls)


def test_mirror_relatives_are_posix_even_on_a_windows_host(monkeypatch, tmp_path):
    """The pinned str(PurePath) serialization class, at this lane's seam:
    os.path.relpath answers with backslashes on a Windows host, and these
    relatives become CONTAINER paths (the upload whitelist) - a Linux run can
    never reproduce it because os.sep is already the slash, so the Windows
    shape is injected through the seam (the Windows CI leg caught the live
    instance; this makes the class fail here instead)."""
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "notes.txt").write_bytes(b"x")
    real_relpath = bi.os.path.relpath
    monkeypatch.setattr(bi.os.path, "relpath",
                        lambda full, base: real_relpath(full, base).replace("/", "\\"))
    monkeypatch.setattr(bi.os, "sep", "\\", raising=False)
    files = bi._eligible_workspace_files(root)
    rels = [rel for rel, _s, _m in files]
    assert rels == ["sub/notes.txt"], rels


def test_the_run_hands_browser_use_the_upload_whitelist():
    """Static wiring: the run mirrors the owner's files and hands their
    container paths to browser-use as available_file_paths - upload_file
    refuses everything else, so a missing wire means no uploads at all."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "tools"
           / "browser_agent.py").read_text(encoding="utf-8")
    body = src.split("async def _run_browser", 1)[1]
    assert "sync_workspace_for_run(" in body
    assert body.index("sync_workspace_for_run(") < body.index("agent = Agent(")
    assert "available_file_paths=_ws_files or None" in body


def test_sweep_delivers_through_the_threat_funnel(monkeypatch, tmp_path):
    """The module half, with the docker seam faked: a finished file is copied
    out, asked past inspect_upload_file, and lands sanitized in the owner's
    VAF_Projects Downloads; a blocked file goes nowhere - and in BOTH cases
    the container copy is deleted. Deliberately WITHOUT the mgr fixture: it
    stubs this very function for the manager-level tests."""
    monkeypatch.setenv("HOME", str(tmp_path))
    import types as _types
    import vaf.core.browser_pool as bp
    import vaf.core.session as session_mod
    import vaf.core.threat_db as threat_mod

    calls = []
    payload = b"%PDF-1.4 test"

    def fake_docker(args, timeout=60):
        calls.append(list(args))
        if args[0] == "exec" and "find" in args[-1]:
            return _types.SimpleNamespace(returncode=0, stdout=(
                f"{len(payload)}\t/home/browser/Downloads/böse datei?.pdf\n"
                "999\t/home/browser/Downloads/loading.crdownload-not-really\n"), stderr="")
        if args[0] == "cp":
            with open(args[-1], "wb") as f:
                f.write(payload)
            return _types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bp, "_docker", fake_docker)
    monkeypatch.setattr(session_mod, "get_user_projects_root",
                        lambda scope: tmp_path / "VAF_Projects" / "ab12cd34")
    blocked = {"value": False}
    monkeypatch.setattr(threat_mod, "inspect_upload_file",
                        lambda p, **kw: _types.SimpleNamespace(blocked=blocked["value"]))

    delivered = bi._sweep_container_downloads("vaf-browser", "scope-a")
    target = tmp_path / "VAF_Projects" / "ab12cd34" / "Downloads"
    assert delivered and (target / delivered[0]).exists()
    assert "?" not in delivered[0]                  # sanitized name
    find_arg = next(c[-1] for c in calls if c[0] == "exec" and "find" in c[-1])
    assert "*.crdownload" in find_arg               # in-progress files excluded
    assert any(c[:3] == ["exec", "vaf-browser", "rm"] for c in calls)

    # Second file of the same name gets a collision suffix, not an overwrite.
    delivered2 = bi._sweep_container_downloads("vaf-browser", "scope-a")
    assert delivered2 and delivered2[0] != delivered[0]

    # Blocked verdict: nothing lands, the container copy still dies.
    calls.clear()
    blocked["value"] = True
    existing = sorted(target.iterdir())
    assert bi._sweep_container_downloads("vaf-browser", "scope-a") == []
    assert sorted(target.iterdir()) == existing
    assert any(c[:3] == ["exec", "vaf-browser", "rm"] for c in calls)


def test_the_run_wires_the_handover_into_its_task_and_its_jar_decision():
    """The manager half above is useless unless the tool consumes it: the run
    must take the handover, let it veto the clean-start scrub (continuing=),
    and hand the enriched task to the AGENT while the live view keeps showing
    the person's own words. Static, because the wiring lives deep inside the
    async run path; each assert names the revert it catches."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "tools"
           / "browser_agent.py").read_text(encoding="utf-8")
    assert "take_agent_handover(" in src
    assert "continuing=_handover is not None" in src
    body = src.split("async def _run_browser", 1)[1]
    # consume BEFORE the jar decision, or continuing can never be true
    assert body.index("take_agent_handover(") < body.index("hand_jar_to_run(")
    assert "[Session handover]" in body
    assert "task=_agent_task" in body, (
        "the enriched task must reach the Agent; the plain task stays for the live view"
    )
    # The return direction: end-of-run parking must ask give_back_pending and
    # stand down, or the person resumes into a blanked browser.
    assert "give_back_pending(" in body
    assert body.index("give_back_pending(") < body.index("self._park_browser_idle")
    assert "if not _skip_park:" in body


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
    assert "CONTINUES" in block, (
        "the block must say a takeover continues the user's session on their page - "
        "without it the model phrases tasks as opening the site fresh, and the run "
        "ignores the tab the person handed over (measured live on a marketplace session)"
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


def _second_manager(monkeypatch, container_name):
    """A second manager, registered under a pool container name.

    The pool resolves a scope to an instance TWICE and at different moments: once
    when the person opens the browser (web_server -> get_manager_for_scope) and
    again when a run starts (browser_agent -> get_browser_pool().resolve). Between
    those two the answer can change - the instance was idle-stopped or reaped, the
    capacity gate sent the run elsewhere - and then the run pins a different
    manager than the one holding the person's lease. This builds that second
    manager so the divergence can be reproduced without docker.
    """
    other = bi.InteractiveBrowserManager()
    other._emit = lambda payload, session_id: other._test_emitted.append((dict(payload), session_id))
    other._test_emitted = []
    other._ensure_janitor = lambda: None
    monkeypatch.setitem(bi._pool_managers, container_name, other)
    return other


def test_a_lease_on_another_manager_still_gets_its_give_back(mgr, monkeypatch):
    """The person opened the browser, so SOMEBODY holds a lease. When the run then
    pins a different manager than the lease sits on, the give-back must still reach
    the person.

    Before the fix nothing reached them at all, and silently: the run's manager has
    no lease, so `_pre_agent_holder` is None (browser_interactive.py:835), and the
    fallback branch needs `_last_session_id`, which is "" on a manager that never
    served an interactive start. Both branches fall through, no event is emitted,
    and the person's window waits forever in the agent view - the reported symptom,
    on macOS and Linux alike.

    "One person drives at a time" is a statement about the browser as a whole, not
    about one manager instance, so the lease has to be found wherever it lives.
    """
    _no_ipc_tasks(monkeypatch)
    monkeypatch.setattr(bi, "get_interactive_manager", lambda: mgr)

    # The person opens the browser: the lease lands on the shared manager.
    mgr.start("scope-a", "sess-1")
    assert mgr._lease is not None

    # The run resolves the pool differently and pins a POOL instance instead.
    other = _second_manager(monkeypatch, "vaf-browser-u-deadbeef")
    bi.stop_for_agent_run(container_name="vaf-browser-u-deadbeef")
    bi.agent_run_ended(container_name="vaf-browser-u-deadbeef")

    # Wherever it was recorded, the person must be told they can resume.
    resumable = [(p, sid)
                 for p, sid in (mgr._test_emitted + other._test_emitted)
                 if p.get("reason") == "agent_done" and p.get("resumable")]
    assert resumable, (
        "no give-back reached the lease holder: the run pinned a manager without "
        "the lease, so neither the holder branch nor the last-session fallback fired"
    )
    assert resumable[0][1] == "sess-1", f"give-back went to the wrong session: {resumable}"
