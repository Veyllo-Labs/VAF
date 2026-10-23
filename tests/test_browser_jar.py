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


@pytest.mark.parametrize("content", [
    json.dumps({"owner": "x", "instance": "test-container@test-start", "holders": []}),
    json.dumps({"format": "browserjar-9-ffffff", "owner": "x", "holders": []}),
    "{not json",
])
def test_a_record_that_cannot_be_used_is_never_overwritten(box, content):
    """A record without this format's tag, with another one (a newer VAF during an
    update), or one that does not parse vouches for nobody - and its holder half
    is not "no holders" either: reading it as empty would grant the browser past
    the holders it lists and overwrite them. A probe answers busy, a lease is
    refused, the file stays as it is; once it is removed the browser works again,
    with the wipe an unknown owner gets.

    MUTATIONS: read an unusable file as empty for the holders (red: granted and
    overwritten); skip the tag check (red on the untagged case)."""
    path = bj._state_dir()
    path.mkdir(parents=True, exist_ok=True)
    record = path / "vaf-browser.json"
    record.write_bytes(content.encode("utf-8"))
    with box["procs"].be(CHILD):
        out = br.render_page("https://example.com/", user_scope_id="scope-b")
    assert out["busy"] is True and box["events"] == []
    res = box["web"].start("scope-a", "sess-a", save=False)
    assert res["status"] == "error" and res["reason"] == "handover_failed"
    assert record.read_bytes() == content.encode("utf-8"), "an unusable record was overwritten"

    record.unlink()
    assert box["web"].start("scope-a", "sess-a", save=False)["status"] == "active"
    assert "wipe" in box["events"]


@pytest.mark.skipif(not __import__("sys").platform.startswith("linux"),
                    reason="the boot clock seam is Linux's /proc")
def test_a_clock_step_does_not_make_a_live_holder_look_gone(monkeypatch):
    """psutil's create_time on Linux adds the boot time, which /proc/stat derives
    from the wall clock in whole seconds: a clock step (measured, 15 ms forward was
    enough) moved it by a second, every start a long-running process had recorded
    stopped matching, and its live lease read as gone to every process started
    after the step. The mark is the boot-relative tick count now, which no clock
    step moves. A REAL second process, its entry recorded, then the boot time
    shifted by a second.

    MUTATION: mark processes with psutil's create_time again - red."""
    import subprocess
    import sys

    import psutil._pslinux as pl

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        entry = {"pid": child.pid, "started": bj._process_started(child.pid)}
        assert bj._alive(entry)
        real_boot = pl.boot_time
        monkeypatch.setattr(pl, "boot_time", lambda: real_boot() + 1.0)
        assert bj._alive(entry), "a clock step made a live holder look gone"
        assert not bj._alive({"pid": child.pid, "started": entry["started"] + 1}), (
            "another start mark on the same pid must read as another process")
    finally:
        child.kill()
        child.wait()
    assert not bj._alive(entry), "a holder whose process is gone must read as gone"


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


def test_a_run_stopped_while_it_waits_leaves_no_holder(box):
    """The run's handover waits for a probe of another process in an executor
    thread, and a Stop does not wait for that thread: run() returned, its
    agent_run_ended ran, and the late claim then took the browser for a run that
    no longer existed and kept its entry for the life of the process - every
    coder render busy, every workflow browser run refused. A run carries the
    generation it began in, and a claim that lands after its end is released.

    MUTATION: drop the generation check after the claim - red."""
    web = box["web"]
    with box["procs"].be(CHILD):
        token, _ = bj.record_for("vaf-browser").claim(bj.KIND_PROBE, "scope-b")
    web.stop_for_agent_run()
    generation = web.run_generation()
    refused = []

    def _handover():
        try:
            web.hand_jar_to_run(user_scope_id="scope-c", persistent=False,
                                run_generation=generation)
        except bi.BrowserHandoverError as e:
            refused.append(e)

    worker = threading.Thread(target=_handover)
    worker.start()
    time.sleep(0.3)
    web.agent_run_ended()                       # Stop: run() returns, its finally runs
    bj.record_for("vaf-browser").release(token)  # the probe ends, the claim lands
    worker.join(10)
    assert refused, "the late handover took the browser for a run that had ended"
    data = json.loads((bj._state_dir() / "vaf-browser.json").read_bytes())
    assert not [h for h in data["holders"] if h["pid"] == WEB], data["holders"]
    assert web._run_token is None
    assert "wipe" not in box["events"]

    # The next run, a new generation, gets the browser as usual.
    web.stop_for_agent_run()
    web.hand_jar_to_run(user_scope_id="scope-c", persistent=False,
                        run_generation=web.run_generation())
    assert "wipe" in box["events"]
    web.agent_run_ended()


class _Reached(Exception):
    """Ends a real BrowserAgentTool run right after its handover, before browser-use."""


def _two_runs_through_the_gate(box, monkeypatch, *, a_end_waits_for_b_claim):
    """Drive two REAL BrowserAgentTool.run calls on the shared browser through the
    real concurrency gate (one slot) and the real _run_browser up to its handover;
    the workspace mirror right after it ends each run. A holds the gate until the
    test lets it go; B queues behind it. Returns (result of B, B's view of the
    record at the moment B had its handover)."""
    pytest.importorskip("browser_use", exc_type=ImportError)
    import vaf.tools.browser_agent as ba

    monkeypatch.setitem(ba._BROWSER_SEMAPHORES, "vaf-browser", threading.Semaphore(1))
    web = box["web"]
    release_a = threading.Event()
    b_claimed = threading.Event()
    a_ended = threading.Event()
    b_view = {}
    real_hand = bi.hand_jar_to_run
    real_end = bi.agent_run_ended

    def hand(**kwargs):
        real_hand(**kwargs)
        if kwargs.get("user_scope_id") == "scope-b":
            b_claimed.set()

    def end(notify=True, container_name=None):
        if threading.current_thread().name == "run-a" and a_end_waits_for_b_claim:
            b_claimed.wait(1.0)
        real_end(notify=notify, container_name=container_name)
        if threading.current_thread().name == "run-a":
            a_ended.set()

    def mirror(user_scope_id=None, container_name=None):
        if user_scope_id == "scope-a":
            release_a.wait(10)
        else:
            a_ended.wait(3)
            data = json.loads((bj._state_dir() / "vaf-browser.json").read_bytes())
            b_view.update(token=web._run_token,
                          runs=[h for h in data["holders"] if h["kind"] == bj.KIND_RUN])
        raise _Reached(user_scope_id)

    monkeypatch.setattr(bi, "hand_jar_to_run", hand)
    monkeypatch.setattr(bi, "agent_run_ended", end)
    monkeypatch.setattr(bi, "sync_workspace_for_run", mirror)
    results = {}

    def run(scope):
        results[scope] = ba.BrowserAgentTool().run(task="look it up", user_scope_id=scope)

    a = threading.Thread(target=run, args=("scope-a",), name="run-a")
    a.start()
    for _ in range(100):                       # A holds the gate, past its handover
        if web._run_token is not None:
            break
        time.sleep(0.05)
    b = threading.Thread(target=run, args=("scope-b",), name="run-b")
    b.start()
    time.sleep(0.4)                             # B is queued at the gate now
    release_a.set()
    a.join(15)
    b.join(15)
    return results.get("scope-b", ""), b_view


def test_a_run_queued_at_the_gate_gets_the_browser_when_the_run_ahead_ends(box, monkeypatch):
    """With one slot (the default) a second run waits at the gate. The end of the
    run ahead of it moves the run generation on; a generation read before the gate
    then retired the queued run as if it had been stopped, and the person got "the
    browser could not be cleaned for this user change". It is read after the gate.

    MUTATION: read the generation in run() before the gate again - red."""
    out, _view = _two_runs_through_the_gate(box, monkeypatch, a_end_waits_for_b_claim=False)
    assert "_Reached" in out, f"the queued run was refused: {out}"


def test_a_run_past_the_gate_is_not_retired_by_the_end_of_the_one_before(box, monkeypatch):
    """The run's end comes before the gate opens. With the gate released first, the
    queued run passed it and took the browser while the run ahead was still ending,
    and that end then released the new run's holder entry: other processes saw the
    browser free while it was being driven.

    MUTATION: release the gate before agent_run_ended in run()'s finally - red."""
    out, view = _two_runs_through_the_gate(box, monkeypatch, a_end_waits_for_b_claim=True)
    assert "_Reached" in out, f"the queued run was refused: {out}"
    assert view.get("token") is not None and len(view.get("runs") or []) == 1, (
        f"the run past the gate lost its holder entry to the end of the one before: {view}")


def test_the_run_reads_its_generation_after_the_gate_and_ends_before_opening_it():
    """The two orderings above, pinned in the source as well, so they hold where
    browser-use is not installed and the real-run tests skip.

    MUTATIONS: read the generation in run() again (red); release the gate before
    agent_run_ended (red)."""
    import inspect

    from vaf.tools.browser_agent import BrowserAgentTool

    run_src = inspect.getsource(BrowserAgentTool.run)
    assert "run_generation(" not in run_src, "run() reads the generation before the gate"
    assert run_src.count("agent_run_ended(container_name=_container)") == 1
    assert (run_src.index("agent_run_ended(container_name=_container)")
            < run_src.index("_browser_semaphore(_container).release()")), (
        "the gate opens before the run has ended")
    body = inspect.getsource(BrowserAgentTool._run_browser)
    read = body.index("_run_gen = run_generation(container_name=container_name)")
    assert read < body.index("hand_jar_to_run(user_scope_id=user_scope_id"), body[read - 200:read]
    assert "run_generation=_run_gen" in body


def _dedicated(monkeypatch, scope="scope-x", name="vaf-browser-u-0123456789ab"):
    """A per-user instance's manager, registered the way the pool registers it."""
    m = bi.InteractiveBrowserManager(cdp_base_url="http://127.0.0.1:49222",
                                     container_name=name, dedicated_scope=scope)
    m._emit = lambda payload, session_id: None
    m._ensure_janitor = lambda: None
    monkeypatch.setitem(bi._pool_managers, name, m)
    return m, name


def test_a_run_ending_on_another_browser_leaves_the_shared_run_alone(box, monkeypatch):
    """Run B drives the SHARED browser (it evicted Y's lease there); run P runs and
    ends on X's own instance. The module hook used to call the full
    agent_run_ended on every manager remembering an evicted lease - so P's end
    released B's holder entry, moved the shared generation on and cleared its
    flag while B still drove, and a coder child's render would have wiped under
    B. Other managers get the give-back only, and only for a lease THIS run
    evicted.

    MUTATION: call the full agent_run_ended on the other managers again - red."""
    web = box["web"]
    emitted = []
    web._emit = lambda payload, session_id: emitted.append((dict(payload), session_id))
    assert web.start("scope-y", "sess-y", save=False)["status"] == "active"
    bi.stop_for_agent_run(container_name="vaf-browser")
    bi.hand_jar_to_run(user_scope_id="scope-y", persistent=True, container_name="vaf-browser",
                       run_generation=bi.run_generation(container_name="vaf-browser"))
    generation = web.run_generation()

    _ded, name = _dedicated(monkeypatch)
    bi.stop_for_agent_run(container_name=name)
    bi.hand_jar_to_run(user_scope_id="scope-x", persistent=True, container_name=name,
                       run_generation=bi.run_generation(container_name=name))
    bi.agent_run_ended(container_name=name)

    data = json.loads((bj._state_dir() / "vaf-browser.json").read_bytes())
    assert [h["kind"] for h in data["holders"]] == [bj.KIND_RUN], data["holders"]
    assert web._run_token is not None and web._agent_active is True
    assert web.run_generation() == generation
    assert not [p for p, _ in emitted if p.get("resumable")], "Y was told to resume while B drives"

    bi.agent_run_ended(container_name="vaf-browser")           # B ends
    data = json.loads((bj._state_dir() / "vaf-browser.json").read_bytes())
    assert data["holders"] == []
    assert [sid for p, sid in emitted if p.get("resumable")] == ["sess-y"]


def test_a_lease_evicted_for_a_run_elsewhere_gets_the_give_back_and_nothing_else(box, monkeypatch):
    """The case the cross-manager loop exists for: the person's lease sits on the
    shared browser while their run pinned their own instance. The lease is evicted
    and handed back at the run's end; the shared browser is never marked as driven
    by an agent (none drives it), so it is not left refusing every start.

    MUTATION: evict through the full stop_for_agent_run again - red."""
    web = box["web"]
    emitted = []
    web._emit = lambda payload, session_id: emitted.append((dict(payload), session_id))
    assert web.start("scope-x", "sess-x", save=False)["status"] == "active"
    _ded, name = _dedicated(monkeypatch)
    bi.stop_for_agent_run(container_name=name)
    assert web._lease is None and web._agent_active is False
    bi.agent_run_ended(container_name=name)
    assert [sid for p, sid in emitted if p.get("resumable")] == ["sess-x"]
    assert web.start("scope-x", "sess-x", save=False)["status"] == "active"


@pytest.mark.skipif(not __import__("sys").platform.startswith("linux"),
                    reason="the /proc read is Linux's")
def test_a_read_error_is_not_taken_for_a_gone_holder(monkeypatch):
    """Only "this pid is not in /proc" means gone. With the file-descriptor table
    full (EMFILE) the read fails although the holder lives, and reading it as gone
    wrote it out of the record. Without /proc at all, existence is asked of the
    OS instead.

    MUTATIONS: answer gone for any OSError (red on EMFILE); answer gone when /proc
    is missing (red on the second half)."""
    import errno
    import os
    import pathlib

    me = {"pid": os.getpid(), "started": bj._process_started(os.getpid())}
    real_read = pathlib.Path.read_bytes

    def emfile(self):
        if str(self).startswith("/proc/"):
            raise OSError(errno.EMFILE, "Too many open files")
        return real_read(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", emfile)
    assert bj._alive(me), "a read error made a live holder look gone"

    def no_proc(self):
        if str(self).startswith("/proc/"):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")
        return real_read(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", no_proc)
    real_isdir = bj.os.path.isdir
    monkeypatch.setattr(bj.os.path, "isdir",
                        lambda p: False if str(p).startswith("/proc") else real_isdir(p))
    assert bj._alive(me), "a missing /proc made a live holder look gone"
    assert not bj._alive({"pid": 4194304 + 7, "started": 5.0}), "a pid that cannot exist lived"


@pytest.mark.parametrize("moment", ["after its handover", "before its handover"])
def test_a_queued_run_keeps_the_browser_marked_as_driven(box, monkeypatch, moment):
    """Y holds a lease; run A evicts it and drives; run B evicts too and waits at
    the gate. A's end used to clear the single in-process flag although B was about
    to drive, and nothing set it again: Y's window, resuming on the give-back,
    got `active` and B ran on Y's restored logins (or B wiped under Y's live
    lease), and an in-process probe rendered under B. The flag is a count of the
    browser's runs, so it holds from B's eviction to B's end.

    MUTATION: let agent_run_ended clear the count instead of taking one back - red."""
    pytest.importorskip("browser_use", exc_type=ImportError)
    import vaf.tools.browser_agent as ba

    monkeypatch.setitem(ba._BROWSER_SEMAPHORES, "vaf-browser", threading.Semaphore(1))
    web = box["web"]
    assert web.start("scope-y", "sess-y", save=False)["status"] == "active"
    release_a, b_here, release_b = threading.Event(), threading.Event(), threading.Event()
    real_take = bi.take_agent_handover

    def take(user_scope_id=None, container_name=None):
        if user_scope_id == "scope-b" and moment == "before its handover":
            b_here.set()
            release_b.wait(10)
        return real_take(user_scope_id=user_scope_id, container_name=container_name)

    def mirror(user_scope_id=None, container_name=None):
        if user_scope_id == "scope-a":
            release_a.wait(10)
        elif moment == "after its handover":
            b_here.set()
            release_b.wait(10)
        raise _Reached(user_scope_id)

    monkeypatch.setattr(bi, "take_agent_handover", take)
    monkeypatch.setattr(bi, "sync_workspace_for_run", mirror)
    runs = [threading.Thread(target=lambda s=s: ba.BrowserAgentTool().run(task="look it up",
                                                                         user_scope_id=s))
            for s in ("scope-a", "scope-b")]
    runs[0].start()
    for _ in range(100):
        if web._run_token is not None:
            break
        time.sleep(0.05)
    runs[1].start()
    time.sleep(0.4)                              # B waits at the gate
    release_a.set()
    assert b_here.wait(10), "B never got past the gate"

    res = web.start("scope-y", "sess-y", save=False)
    probe = br.render_page("https://example.com/", user_scope_id="scope-z")
    release_b.set()
    for t in runs:
        t.join(15)
    assert res["status"] == "agent_active", f"another user got the browser under run B: {res}"
    assert probe["busy"] is True, "a probe rendered under run B"
    assert web._agent_runs == 0 and not web.has_activity()
    assert web.start("scope-y", "sess-y", save=False)["status"] == "active"


def test_a_run_whose_gate_times_out_gives_its_count_back(box, monkeypatch):
    """A run that evicted and then gave up at the gate never drives: it gives back
    its place in the count, and nothing else - the run ahead still drives, so its
    generation and its holder entry stay. Without the give-back the browser
    counted as driven for the life of the process and refused every start.

    MUTATIONS: no give-back on the timeout (red: the count stays up); end the run
    instead (red: the run ahead's generation and entry are gone)."""
    import vaf.tools.browser_agent as ba

    gate = threading.Semaphore(1)
    monkeypatch.setitem(ba._BROWSER_SEMAPHORES, "vaf-browser", gate)
    monkeypatch.setattr(ba, "_BROWSER_QUEUE_TIMEOUT", 0.2)
    web = box["web"]
    gate.acquire()                               # the run ahead holds the gate...
    bi.stop_for_agent_run(container_name="vaf-browser")
    bi.hand_jar_to_run(user_scope_id="scope-a", persistent=True, container_name="vaf-browser",
                       run_generation=bi.run_generation(container_name="vaf-browser"))
    token, generation = web._run_token, web.run_generation()
    assert web._agent_runs == 1

    out = ba.BrowserAgentTool().run(task="look it up", user_scope_id="scope-b")
    assert out.startswith("Browser agent is busy"), out
    assert web._agent_runs == 1, "the timed-out run kept its place in the count"
    assert web._run_token == token and web.run_generation() == generation
    data = json.loads((bj._state_dir() / "vaf-browser.json").read_bytes())
    assert [h["kind"] for h in data["holders"]] == [bj.KIND_RUN]

    bi.agent_run_ended(container_name="vaf-browser")   # ...and ends
    gate.release()
    assert web._agent_runs == 0 and not web.has_activity()


def test_an_argument_that_does_not_parse_leaves_no_run_counted(box):
    """The arguments are parsed before the eviction: one that raises leaves no run
    counted behind (the eviction would otherwise never be ended).

    MUTATION: parse after stop_for_agent_run again - red."""
    from vaf.tools.browser_agent import BrowserAgentTool

    with pytest.raises(ValueError):
        BrowserAgentTool().run(task="look it up", user_scope_id="scope-a", max_steps="many")
    assert box["web"]._agent_runs == 0


_X_BROWSER = "vaf-browser-u-0123456789ab"


def _pool_with_x(monkeypatch):
    """The pool gives scope-x its own instance; nothing in this process has created
    that instance's manager yet (nobody opened it since the start)."""
    import vaf.core.browser_pool as bp

    inst = bp.BrowserInstance(user_scope_id="scope-x", container_name=_X_BROWSER,
                              cdp_base="http://127.0.0.1:49222", vnc_base="http://127.0.0.1:46901",
                              last_used=time.time())

    class _Pool:
        def resolve(self, scope):
            return inst if scope == "scope-x" else None

    monkeypatch.setattr(bp, "get_browser_pool", lambda: _Pool())
    monkeypatch.setattr(bi, "_pool_managers", {})
    monkeypatch.setattr(bi.InteractiveBrowserManager, "_emit", lambda self, p, s: None)
    monkeypatch.setattr(bi.InteractiveBrowserManager, "_ensure_janitor", lambda self: None)
    return inst


def _drive_and_look(seen, web):
    """A stand-in for _run_browser: the same handover call, then a look at the
    browsers while the run drives."""
    async def _drive(self, **kw):
        container = kw["container_name"]
        bi.hand_jar_to_run(user_scope_id=kw["user_scope_id"], persistent=True,
                           container_name=container,
                           run_generation=bi.run_generation(container_name=container))
        pooled = bi._pool_managers.get(container)
        seen.update(container=container, shared_runs=web._agent_runs,
                    pooled_runs=pooled._agent_runs if pooled else None,
                    x_start=bi.get_manager_for_scope("scope-x").start("scope-x", "sess-x")["status"])
        return "done"
    return _drive


def test_a_run_on_a_pooled_browser_nobody_opened_lands_on_that_browser(box, monkeypatch):
    """X's run drives X's own instance, but no manager for it existed in this
    process, so every hook fell back to the SHARED manager: it counted, evicted,
    wiped and mirrored there, and once X opened the browser mid-run (creating the
    pooled manager) the run's end went elsewhere and the shared count stayed at 1
    for the life of the process. The run registers its browser's manager first.

    MUTATION: drop the registration in run() - red."""
    from vaf.tools.browser_agent import BrowserAgentTool

    _pool_with_x(monkeypatch)
    web = box["web"]
    seen = {}
    monkeypatch.setattr(BrowserAgentTool, "_run_browser", _drive_and_look(seen, web))
    assert BrowserAgentTool().run(task="look it up", user_scope_id="scope-x") == "done"
    assert seen["container"] == _X_BROWSER
    assert seen["pooled_runs"] == 1 and seen["shared_runs"] == 0, seen
    assert seen["x_start"] == "agent_active", "X's own browser opened under X's run"
    assert web._agent_runs == 0 and bi._pool_managers[_X_BROWSER]._agent_runs == 0
    path = bj._state_dir() / "vaf-browser.json"
    assert not path.exists() or json.loads(path.read_bytes())["holders"] == []


def test_a_spawned_step_on_its_own_browser_is_not_refused_for_a_shared_lease(box, monkeypatch):
    """A workflow's browser step runs in a child process on the person's own
    instance. With the hooks on the shared manager it claimed the SHARED browser
    in the record and was refused whenever someone else held a lease there. The
    child registers its browser's manager like any run.

    MUTATION: drop the registration in run() - red."""
    from vaf.tools.browser_agent import BrowserAgentTool

    _pool_with_x(monkeypatch)
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    box["procs"].table[OTHER] = 300.0
    with box["procs"].be(OTHER):                 # Y's lease, in the web process
        bj.record_for("vaf-browser").claim(bj.KIND_LEASE, "scope-y")
    seen = {}
    monkeypatch.setattr(BrowserAgentTool, "_run_browser", _drive_and_look(seen, box["web"]))
    out = BrowserAgentTool().run(task="look it up", user_scope_id="scope-x")
    assert out == "done", f"the step on its own browser was refused: {out}"
    assert seen["container"] == _X_BROWSER and seen["shared_runs"] == 0


def test_a_start_still_in_its_handover_when_a_run_begins_is_refused(box):
    """start() checked for a run only at its top. A run that began while the start
    was in its wipe found no lease to evict, waited for the start's handover, and
    then drove with the person's lease live under it - a valid ticket on the run's
    browser. The count is checked again in the lock step that installs the lease.

    MUTATION: drop the check at the install - red."""
    web = box["web"]
    wiping, done = threading.Event(), threading.Event()
    real_wipe = bi.verified_profile_wipe

    def slow_wipe(name, cdp_base=""):
        wiping.set()
        time.sleep(0.5)
        return real_wipe(name, cdp_base)

    bi.verified_profile_wipe = slow_wipe
    result = {}
    try:
        def y_start():
            result.update(web.start("scope-y", "sess-y", save=False))
            done.set()

        t = threading.Thread(target=y_start)
        t.start()
        assert wiping.wait(5)
        web.stop_for_agent_run()                  # run B begins mid-handover
        bi.verified_profile_wipe = real_wipe
        web.hand_jar_to_run(user_scope_id="scope-b", persistent=True,
                            run_generation=web.run_generation())
        t.join(5)
        assert result.get("status") == "agent_active", result
        assert web._lease is None and web._lease_token is None
        data = json.loads((bj._state_dir() / "vaf-browser.json").read_bytes())
        assert [h["kind"] for h in data["holders"]] == [bj.KIND_RUN], data["holders"]
        web.agent_run_ended()
    finally:
        bi.verified_profile_wipe = real_wipe


def test_without_a_cross_process_lock_the_record_is_unavailable(monkeypatch):
    """filelock is a hard dependency; should it ever be missing the record must not
    be read and written with no exclusion at all, so every caller fails closed.

    MUTATION: go on without the lock - red."""
    import vaf.core.secure_store as ss

    monkeypatch.setattr(ss, "_get_filelock_cls", lambda: None)
    with pytest.raises(bj.JarRecordUnavailable):
        bj.record_for("vaf-browser").claim(bj.KIND_PROBE, "scope-a")


@pytest.mark.parametrize("path", ["refused at the install", "handover refused"])
def test_a_refresh_start_overtaken_by_a_run_leaves_no_entry(box, monkeypatch, path):
    """Y's window starts again (a refresh, same session) while Y's lease stands, and
    a run's eviction lands between that start's look at its lease and its claim:
    the eviction releases the old entry, the claim makes a new one, and the start is
    then refused - at the install (a run is counted) or at the handover (the wipe
    fails). Whether the entry goes back is decided at that point, from the lease
    actually installed; a lease seen before the claim kept the new entry in the
    record with no lease behind it, and other processes saw the browser held.

    MUTATION: release only when no lease was seen before the claim - red."""
    import vaf.core.browser_jar as bj_mod

    web = box["web"]
    assert web.start("scope-y", "sess-y", save=False)["status"] == "active"
    evicted, holding = threading.Event(), threading.Event()
    real_claim = web._claim_shared

    def slow_claim(kind, scope, *, wait_s):
        if kind == bj.KIND_LEASE and not holding.is_set():
            holding.set()
            evicted.wait(5)
        return real_claim(kind, scope, wait_s=wait_s)

    monkeypatch.setattr(web, "_claim_shared", slow_claim)
    if path == "handover refused":
        box["wipe_ok"] = False
        monkeypatch.setattr(bj_mod, "container_instance", lambda c: "test-container@restarted")
    result = {}
    t = threading.Thread(target=lambda: result.update(web.start("scope-y", "sess-y", save=False)))
    t.start()
    assert holding.wait(5)
    web.stop_for_agent_run()                      # the run's eviction lands now
    evicted.set()
    t.join(5)
    expected = "agent_active" if path == "refused at the install" else "error"
    assert result.get("status") == expected, result
    assert web._lease is None and web._lease_token is None
    data = json.loads((bj._state_dir() / "vaf-browser.json").read_bytes())
    assert data["holders"] == [], f"an entry stayed with no lease behind it: {data['holders']}"
    web.agent_run_ended()
