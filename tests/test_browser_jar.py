# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The shared browser across processes: one record of whose state the profile holds
and who holds the browser, read by every VAF process before it trusts its memory.

The shared container is driven from the web process (leases, chat runs, the main
agent's render_check), from a coder child (its render_check; the default in the web
UI) and from spawned browser_agent children. Each has its own manager, so these tests
stand two managers in for two processes: a fake process table gives each its own pid
and start time, and one fake container (jar, Workspace, the page a probe reads) is
what both drive. Docker and CDP are replaced by that fake.
"""
import contextlib
import json
import threading
import time

import pytest

import vaf.core.browser_interactive as bi
import vaf.core.browser_jar as bj
import vaf.core.browser_render as br

WEB, CHILD, OTHER = 1001, 1002, 1003


class _Procs:
    """pid -> start time; a pid missing from the table is a process that is gone."""

    def __init__(self, monkeypatch):
        self.table = {WEB: 100.0, CHILD: 200.0}
        self.current = WEB
        monkeypatch.setattr(bj, "_process_started", lambda pid: self.table.get(pid))
        monkeypatch.setattr(bj, "_own_identity", lambda: (self.current, self.table[self.current]))

    @contextlib.contextmanager
    def be(self, pid):
        previous, self.current = self.current, pid
        try:
            yield
        finally:
            self.current = previous


@pytest.fixture
def box(monkeypatch, tmp_path):
    import vaf.core.browser_pool as bp
    import vaf.core.containers as containers
    import vaf.core.session as sess
    import vaf.core.subagent_ipc as ipc_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VAF_USER_SCOPE_ID", raising=False)
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    monkeypatch.delenv("VAF_BROWSER_SCRUB", raising=False)
    monkeypatch.delenv("VAF_BROWSER_WORKSPACE_SYNC", raising=False)
    monkeypatch.delenv("VAF_BROWSER_DOWNLOADS", raising=False)
    procs = _Procs(monkeypatch)
    c = {"jar": set(), "workspace": set(), "events": [], "wipe_ok": True, "seen": []}

    def _purge(name):
        c["events"].append("purge")
        c["workspace"].clear()

    def _wipe(name, cdp_base=""):
        c["events"].append("wipe")
        if not c["wipe_ok"]:
            return False
        c["jar"].clear()
        c["workspace"].clear()
        return True

    def _cookie(base, op, cookies=None):
        if op in ("clear_cookies", "scrub"):
            c["jar"].clear()
        elif op == "set":
            c["jar"].update(k["name"] for k in cookies or [])
        elif op == "get":
            return [{"name": n, "value": "1", "domain": "example.com", "path": "/",
                     "expires": 123.0, "httpOnly": True, "secure": True,
                     "sameSite": "Strict"} for n in sorted(c["jar"])]
        return None

    def _tar(name, tar_path):
        import tarfile
        with tarfile.open(tar_path) as tf:
            c["workspace"].update(tf.getnames())
        return 0

    monkeypatch.setattr(bi, "_purge_container_downloads", _purge)
    monkeypatch.setattr(bi, "verified_profile_wipe", _wipe)
    monkeypatch.setattr(bi, "_cookie_op", _cookie)
    monkeypatch.setattr(bi, "_tar_stream_to_container", _tar)
    monkeypatch.setattr(bi, "resolve_cdp_ws_url", lambda base: "ws://stub/devtools/browser/x")
    monkeypatch.setattr(bi, "_set_download_behavior", lambda base, allow: None)
    monkeypatch.setattr(bi, "_sweep_container_downloads", lambda name, scope: [])
    monkeypatch.setattr(bi, "park_browser_idle", lambda base: c["events"].append("park"))
    monkeypatch.setattr(bi, "_current_page", lambda base: None)
    monkeypatch.setattr(containers, "docker",
                        lambda args, timeout=60: type("R", (), {"returncode": 0, "stdout": "",
                                                                "stderr": ""})())

    class _IPC:
        def get_active_tasks(self, session_id=None):
            return []

        def get_pending_tasks(self, session_id=None):
            return []

    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: _IPC())

    class _NoPool:
        def resolve(self, scope):
            return None

    monkeypatch.setattr(bp, "get_browser_pool", lambda: _NoPool())
    roots = {}
    for scope, files in (("scope-a", {"a.txt": b"a"}),
                         ("scope-b", {"index.html": b"<p>b</p>",
                                      "b-private-notes.txt": b"notes of b"}),
                         ("scope-x", {"x.txt": b"x"})):
        root = tmp_path / scope
        root.mkdir()
        for fn, data in files.items():
            (root / fn).write_bytes(data)
        roots[scope] = root
    monkeypatch.setattr(sess, "get_user_projects_root", lambda scope: roots.get(scope))

    managers = {}
    for pid in (WEB, CHILD):
        m = bi.InteractiveBrowserManager()
        m._emit = lambda payload, session_id: None
        m._ensure_janitor = lambda: None
        managers[pid] = m
    # Each "process" sees its own manager, as each real process has its own.
    monkeypatch.setattr(bi, "get_interactive_manager", lambda: managers[procs.current])

    def _cdp(cdp_base, url, *, wait_ms, max_text):
        # What the page sees: the jar at the moment it is opened. b's page sets a
        # cookie of its own, the way a real page does.
        c["seen"].append((url, sorted(c["jar"])))
        c["events"].append("render")
        c["jar"].add("set-by-b-page")
        return {"ok": True, "url": url, "title": "", "text": "x", "console": [],
                "page_errors": [], "failed_requests": [], "screenshot_b64": "", "error": ""}

    monkeypatch.setattr(br, "_render_via_cdp", _cdp)
    c["procs"] = procs
    c["web"] = managers[WEB]
    c["child"] = managers[CHILD]
    c["roots"] = roots
    return c


def _login(box, scope, name):
    """The person's saved login, in the per-scope storage-state file a lease loads."""
    bi._export_storage_cookies(bi.browser_storage_state_path(scope, "default"),
                               [{"name": name, "value": "1", "domain": "example.com",
                                 "path": "/", "expires": 123.0, "httpOnly": True,
                                 "secure": True, "sameSite": "Strict"}])


def _saved(scope):
    from vaf.core import data_files
    state = data_files.read_json(bi.browser_storage_state_path(scope, "default"))
    return [c["name"] for c in state["cookies"]]


# ── (a) a probe in another process meets a live lease ────────────────────────

def test_a_coder_render_in_another_process_does_not_touch_a_live_lease(box):
    """a's lease is live in the web process; b's coder renders in its child. The
    child's manager has no lease and no memory of a, so it used to purge, wipe
    (Chromium killed under a), mirror b's files and point a's tab at b's page - and
    a's stop then exported the cookie b's page set over a's saved login. The lease
    is in the record now, so the probe is busy and touches nothing.

    MUTATION: drop the probe's claim in hold_for_probe - red."""
    _login(box, "scope-a", "a-mail-login")
    assert box["web"].start("scope-a", "sess-a", save=True)["status"] == "active"
    box["events"].clear()
    with box["procs"].be(CHILD):
        out = br.render_page(str(box["roots"]["scope-b"] / "index.html"), user_scope_id="scope-b")
    assert out["busy"] is True and out["ok"] is False
    assert box["events"] == [], f"the probe touched a live lease of another process: {box['events']}"
    assert "b-private-notes.txt" not in box["workspace"]
    box["web"].stop("user", requester_scope="scope-a")
    assert _saved("scope-a") == ["a-mail-login"], "a's saved login was overwritten"

    # Once a has stopped, b's render gets the browser - wiped first.
    box["events"].clear()
    with box["procs"].be(CHILD):
        out = br.render_page("https://mail.example/", user_scope_id="scope-b")
    assert out["ok"] is True
    assert box["events"][:2] == ["purge", "wipe"]
    assert box["seen"][-1] == ("https://mail.example/", [])


# ── (b) the owner is the record's, not a process's memory ─────────────────────

def test_the_owner_a_child_left_is_seen_by_the_web_process_and_back(box):
    """In sequence, nothing overlapping. After b's coder render the web process
    still believed the jar was a's, so a's next lease got no purge and no wipe and
    a's file picker listed b's files; b's next render in the same child believed
    the jar was b's and loaded the page in a's login. Both read the record now.

    MUTATION: decide the owner from the manager's own memory
    (`_last_cookie_scope`) instead of the record - red."""
    _login(box, "scope-a", "a-mail-login")
    web = box["web"]
    assert web.start("scope-a", "sess-a", save=True)["status"] == "active"
    web.stop("user", requester_scope="scope-a")

    with box["procs"].be(CHILD):
        assert br.render_page(str(box["roots"]["scope-b"] / "index.html"),
                              user_scope_id="scope-b")["ok"] is True
    assert "b-private-notes.txt" in box["workspace"]

    box["events"].clear()
    assert web.start("scope-a", "sess-a2", save=True)["status"] == "active"
    assert box["events"][:2] == ["purge", "wipe"], (
        f"a's lease trusted the web process's memory: {box['events']}")
    assert not {"index.html", "b-private-notes.txt"} & box["workspace"], (
        f"b's files reached a's file picker: {sorted(box['workspace'])}")
    web.stop("user", requester_scope="scope-a")

    box["events"].clear()
    with box["procs"].be(CHILD):
        assert br.render_page("https://mail.example/", user_scope_id="scope-b")["ok"] is True
    assert "wipe" in box["events"]
    assert box["seen"][-1] == ("https://mail.example/", []), (
        f"b's page opened in a's login: {box['seen'][-1]}")


# ── (c) a spawned run in another process ─────────────────────────────────────

def test_a_spawned_run_leaves_its_owner_for_every_process(box):
    """A browser_agent child for scope-x took the jar in its own process and left
    the web process's tracker on scope-b, whose next lease then browsed with x's
    login - and a web-process render for b loaded x's bank page.

    The other direction is pinned too: the owner the child named is trusted by the
    web process, so a lane of the SAME scope there does not wipe x's own state.

    MUTATION: skip naming the new owner after the confirmed wipe - red (the web
    process's render for x wipes)."""
    web, child = box["web"], box["child"]
    assert web.start("scope-b", "sess-b", save=False)["status"] == "active"
    web.stop("user", requester_scope="scope-b")

    with box["procs"].be(CHILD):
        child.stop_for_agent_run()
        child.hand_jar_to_run(user_scope_id="scope-x", persistent=True)
        box["jar"].add("x-bank-login")          # the run logs in
        child.agent_run_ended()

    box["events"].clear()
    assert br.render_page("https://bank.example/", user_scope_id="scope-x")["ok"] is True
    assert "wipe" not in box["events"], "the owner the child named was not trusted"

    box["events"].clear()
    assert br.render_page("https://bank.example/", user_scope_id="scope-b")["ok"] is True
    assert "x-bank-login" not in box["seen"][-1][1], f"b's render used x's login: {box['seen'][-1]}"
    assert "wipe" in box["events"]

    with box["procs"].be(CHILD):
        child.stop_for_agent_run()
        child.hand_jar_to_run(user_scope_id="scope-x", persistent=True)
        box["jar"].add("x-bank-login")
        child.agent_run_ended()
    box["events"].clear()
    assert web.start("scope-b", "sess-b2", save=False)["status"] == "active"
    assert "wipe" in box["events"] and "x-bank-login" not in box["jar"], (
        f"b's lease browsed with x's login: {sorted(box['jar'])}")


def test_a_run_meets_a_live_lease_of_another_process_and_is_refused(box):
    """The per-process gate never saw a lease of another process; the record does.
    A run is refused (not waited: a lease is long) with BrowserHeldElsewhere, which
    every lane that refuses on a failed handover already handles.

    MUTATION: skip the run's claim in hand_jar_to_run - red."""
    assert box["web"].start("scope-a", "sess-a", save=False)["status"] == "active"
    box["events"].clear()
    with box["procs"].be(CHILD):
        with pytest.raises(bi.BrowserHeldElsewhere):
            box["child"].hand_jar_to_run(user_scope_id="scope-x", persistent=True)
    assert box["events"] == []


def test_a_lease_waits_out_a_probe_of_another_process(box, monkeypatch):
    """A probe is short, so a lease waits for one of another process as it waits for
    one of its own (the handover mutex), and past the budget answers busy instead
    of hanging the window.

    MUTATION: do not wait (claim once) - red on the first half."""
    with box["procs"].be(CHILD):
        token, _ = bj.record_for("vaf-browser").claim(bj.KIND_PROBE, "scope-b")
    timer = threading.Timer(0.4, lambda: bj.record_for("vaf-browser").release(token))
    timer.start()
    t0 = time.monotonic()
    assert box["web"].start("scope-a", "sess-a", save=False)["status"] == "active"
    assert time.monotonic() - t0 >= 0.3
    box["web"].stop("user", requester_scope="scope-a")
    timer.join()

    monkeypatch.setattr(bi.InteractiveBrowserManager, "PROBE_WAIT_S", 0.3)
    with box["procs"].be(CHILD):
        bj.record_for("vaf-browser").claim(bj.KIND_PROBE, "scope-b")
    res = box["web"].start("scope-a", "sess-a2", save=False)
    assert res["status"] == "busy" and res["reason"] == "in_use"
    assert "scope-b" not in json.dumps(res)


# ── the record never locks the browser for a process that is gone ─────────────

@pytest.mark.parametrize("how", ["gone", "pid reused"])
def test_a_holder_whose_process_is_gone_does_not_lock_the_browser(box, how):
    """A crashed process never releases its entry. Its pid is checked with its start
    time, so an entry whose process is gone - or whose pid now belongs to a
    different process - is dropped instead of answering busy forever.

    MUTATIONS: treat any recorded pid as alive (red on both); compare the pid only,
    not its start time (red on "pid reused")."""
    procs = box["procs"]
    procs.table[OTHER] = 300.0
    with procs.be(OTHER):
        bj.record_for("vaf-browser").claim(bj.KIND_LEASE, "scope-a")
    if how == "gone":
        del procs.table[OTHER]
    else:
        procs.table[OTHER] = 999.0           # same pid, another process
    with procs.be(CHILD):
        out = br.render_page("https://example.com/", user_scope_id="scope-b")
    assert out["ok"] is True, out
    data = json.loads((bj._state_dir() / "vaf-browser.json").read_bytes())
    assert all(h["pid"] != OTHER for h in data["holders"])


# ── the owner record itself ───────────────────────────────────────────────────

def test_the_record_is_tagged_and_names_a_hash_never_the_scope(box):
    """A persisted format with its tag; the owner and the holders are scope
    HASHES, so the file says nothing about who uses the machine."""
    assert box["web"].start("scope-a", "sess-a", save=False)["status"] == "active"
    raw = (bj._state_dir() / "vaf-browser.json").read_bytes()
    data = json.loads(raw)
    assert data["format"] == "browserjar-1-6e0d93"
    assert data["owner"] == bj.scope_key("scope-a")
    assert b"scope-a" not in raw


def test_an_untagged_record_vouches_for_nobody(box):
    """A file without the format tag (a foreign or hand-made one) is not read as a
    record: the owner is unknown and the next holder is wiped.

    MUTATION: read the file without checking the tag - red."""
    path = bj._state_dir()
    path.mkdir(parents=True, exist_ok=True)
    (path / "vaf-browser.json").write_bytes(json.dumps(
        {"owner": bj.scope_key("scope-a"), "instance": "test-container@test-start",
         "holders": []}).encode("utf-8"))
    box["events"].clear()
    assert box["web"].start("scope-a", "sess-a", save=False)["status"] == "active"
    assert "wipe" in box["events"]


def test_an_owner_recorded_on_another_container_start_is_unknown(box, monkeypatch):
    """The owner is vouched for only on the container instance and the start it was
    recorded on: a restarted or recreated container starts unknown, as a restarted
    stack always did.

    MUTATION: compare the owner without the instance - red."""
    assert box["web"].start("scope-a", "sess-a", save=False)["status"] == "active"
    box["web"].stop("user", requester_scope="scope-a")
    monkeypatch.setattr(bj, "container_instance", lambda container: "test-container@later")
    box["events"].clear()
    with box["procs"].be(CHILD):
        assert box["child"].start("scope-a", "sess-a2", save=False)["status"] == "active"
    assert "wipe" in box["events"]


def test_a_handover_cut_short_leaves_the_owner_unknown(box):
    """The owner is cleared before the wipe and named only after it is confirmed. A
    wipe that fails mid-handover therefore leaves "unknown" - never the previous
    owner, whose state may be half gone and half the new holder's.

    MUTATION: do not clear the owner before the wipe - red."""
    web = box["web"]
    assert web.start("scope-a", "sess-a", save=False)["status"] == "active"
    web.stop("user", requester_scope="scope-a")
    box["wipe_ok"] = False
    with box["procs"].be(CHILD):
        out = br.render_page("https://example.com/", user_scope_id="scope-b")
    assert out["ok"] is False and "handed over" in out["error"] and "admin" in out["error"]
    box["wipe_ok"] = True
    box["events"].clear()
    assert web.start("scope-a", "sess-a2", save=False)["status"] == "active"
    assert "wipe" in box["events"], "a's lease trusted an owner a failed handover left behind"


def test_the_give_back_parker_stands_down_for_a_holder_elsewhere(box):
    """The parker blanks the page an evicted person did not come back to. A probe of
    this process (the handover mutex) or a holder of another process (the record)
    is about to read that page, so it stands down for both.

    MUTATION: drop either check - red."""
    web = box["web"]
    with box["procs"].be(CHILD):
        token, _ = bj.record_for("vaf-browser").claim(bj.KIND_PROBE, "scope-b")
    assert web._park_if_unclaimed() is False
    bj.record_for("vaf-browser").release(token)
    with web._handover_mutex:
        assert web._park_if_unclaimed() is False
    box["events"].clear()
    assert web._park_if_unclaimed() is True and box["events"] == ["park"]


def test_a_run_refused_for_a_holder_elsewhere_says_busy(box, monkeypatch):
    """The tool's answer names the real reason: busy elsewhere in VAF, not the
    wipe failure a BrowserHandoverError otherwise reports.

    MUTATION: drop the BrowserHeldElsewhere branch in BrowserAgentTool.run - red."""
    from vaf.tools.browser_agent import BrowserAgentTool

    async def _held(self, **kwargs):
        raise bi.BrowserHeldElsewhere("shared browser in use by another VAF process (lease)")

    monkeypatch.setattr(BrowserAgentTool, "_run_browser", _held)
    out = BrowserAgentTool().run(task="look up the opening hours", user_scope_id="scope-a")
    assert out.startswith("Browser agent is busy"), out
    assert "could not be cleaned" not in out
