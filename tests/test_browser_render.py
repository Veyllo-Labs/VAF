# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""render_check / render_page - the probe behind the build-run-inspect-fix loop.

Everything here runs without a browser: the protocol half sits behind the
`_render_via_cdp` seam, and these tests pin the orchestration around it - the
busy answer, the localhost rewrite, the workspace jail on file targets, the
mirror sync, and the report both agents read. The three wiring guarantees
(coder registration, advertised schema, host-gateway on both container lanes)
are pinned statically so they cannot drift apart one file at a time.
"""
import contextlib
import os
import re
import threading
import time
from pathlib import Path

import pytest

import vaf.core.browser_render as br

_REPO = Path(__file__).resolve().parents[1]


class _Manager:
    """Stands in for the manager's probe hold; the real one (handover, mutex,
    busy check) is pinned by the shared-browser tests below."""

    def __init__(self, busy=False):
        self.busy = busy
        self.synced = []

    def cdp_base(self):
        return "http://127.0.0.1:9222"

    def has_activity(self):
        return self.busy

    @contextlib.contextmanager
    def hold_for_probe(self, scope):
        yield not self.busy

    def sync_workspace(self, scope):
        self.synced.append(scope)
        return []


@pytest.fixture
def quiet_manager(monkeypatch):
    """The shared browser, idle, with the pool disabled - and the CDP half
    recording what it was asked to render instead of doing it."""
    import vaf.core.browser_interactive as bi
    import vaf.core.browser_pool as bp

    mgr = _Manager()
    monkeypatch.setattr(bi, "get_interactive_manager", lambda: mgr)

    class _NoPool:
        def resolve(self, scope):
            return None

    monkeypatch.setattr(bp, "get_browser_pool", lambda: _NoPool())

    rendered = {}

    def _fake_cdp(cdp_base, url, *, wait_ms, max_text):
        rendered.update(cdp_base=cdp_base, url=url, wait_ms=wait_ms)
        return {"ok": True, "url": url, "title": "T", "text": "hello",
                "console": [], "page_errors": [], "failed_requests": [],
                "screenshot_b64": "", "error": ""}

    monkeypatch.setattr(br, "_render_via_cdp", _fake_cdp)
    mgr.rendered = rendered
    return mgr


# ── render_page orchestration ─────────────────────────────────────────────────

def test_busy_browser_answers_busy_and_never_renders(monkeypatch):
    # The probe evicts no one: an interactive session or agent run owns the
    # browser, and the answer is busy - not a hijacked page.
    import vaf.core.browser_interactive as bi
    import vaf.core.browser_pool as bp
    monkeypatch.setattr(bi, "get_interactive_manager", lambda: _Manager(busy=True))
    monkeypatch.setattr(bp, "get_browser_pool", lambda: (_ for _ in ()).throw(RuntimeError))
    monkeypatch.setattr(br, "_render_via_cdp",
                        lambda *a, **k: pytest.fail("rendered into a busy browser"))
    out = br.render_page("https://example.com/", user_scope_id="s1")
    assert out["busy"] is True and out["ok"] is False
    assert "in use" in out["error"]


def test_localhost_is_rewritten_to_the_host_gateway_name(quiet_manager):
    # The container's localhost is the container; the tool must aim at the host.
    out = br.render_page("http://localhost:3000/app?x=1", user_scope_id="s1")
    assert out["ok"] is True and out["rewritten"] is True
    assert quiet_manager.rendered["url"] == "http://host.docker.internal:3000/app?x=1"


def test_public_urls_pass_through_unrewritten(quiet_manager):
    out = br.render_page("https://example.com/page", user_scope_id="s1")
    assert out["rewritten"] is False
    assert quiet_manager.rendered["url"] == "https://example.com/page"


def test_file_target_inside_the_jail_rides_the_mirror(quiet_manager, monkeypatch, tmp_path):
    # A project file is opened through the EXISTING workspace mirror, synced
    # first so the file just written is the file the browser opens.
    import vaf.core.session as sess
    (tmp_path / "site").mkdir()
    page = tmp_path / "site" / "index.html"
    page.write_bytes(b"<h1>hi</h1>")
    monkeypatch.setattr(sess, "get_user_projects_root", lambda scope: tmp_path)
    out = br.render_page(str(page), user_scope_id="s1")
    assert out["ok"] is True
    assert quiet_manager.rendered["url"] == "file:///home/browser/Workspace/site/index.html"
    assert quiet_manager.synced == ["s1"]


def test_file_target_outside_the_jail_is_refused(quiet_manager, monkeypatch, tmp_path):
    # The same boundary every file tool enforces: only the caller's own
    # project root is renderable. /etc/passwd is a file; it is not a page.
    import vaf.core.session as sess
    monkeypatch.setattr(sess, "get_user_projects_root", lambda scope: tmp_path / "root")
    (tmp_path / "root").mkdir()
    outside = tmp_path / "elsewhere.html"
    outside.write_bytes(b"x")
    out = br.render_page(str(outside), user_scope_id="s1")
    assert out["ok"] is False and "Not renderable" in out["error"]
    assert quiet_manager.synced == []


def test_missing_file_is_refused_not_rendered(quiet_manager, monkeypatch, tmp_path):
    import vaf.core.session as sess
    monkeypatch.setattr(sess, "get_user_projects_root", lambda scope: tmp_path)
    out = br.render_page(str(tmp_path / "ghost.html"), user_scope_id="s1")
    assert out["ok"] is False and "Not renderable" in out["error"]


def test_render_page_never_raises(monkeypatch):
    import vaf.core.browser_interactive as bi
    monkeypatch.setattr(bi, "get_interactive_manager",
                        lambda: (_ for _ in ()).throw(RuntimeError("no docker")))
    out = br.render_page("https://example.com/")
    assert out["ok"] is False and "render failed" in out["error"]


# ── the shared browser: the probe is a holder like any other ──────────────────

@pytest.fixture
def shared(monkeypatch, tmp_path):
    """A REAL shared manager (no dedicated scope, empty jar tracker: the state of
    every fresh VAF process), with docker and CDP replaced by recorders. The
    container's Workspace is a set the purge empties and the mirror adds to,
    the way `rm -rf Workspace/*` and `tar -xf` treat the real folder."""
    import vaf.core.browser_interactive as bi
    import vaf.core.browser_pool as bp
    import vaf.core.containers as containers
    import vaf.core.session as sess
    import vaf.core.subagent_ipc as ipc_mod

    monkeypatch.delenv("VAF_BROWSER_WORKSPACE_SYNC", raising=False)
    events = []
    workspace = set()
    state = {"wipe_ok": True}

    def _purge(name):
        events.append("purge")
        workspace.clear()

    def _wipe(name, cdp_base=""):
        events.append("wipe")
        return state["wipe_ok"]

    def _tar(name, tar_path):
        import tarfile
        with tarfile.open(tar_path) as tf:
            workspace.update(tf.getnames())
        events.append("mirror")
        return 0

    monkeypatch.setattr(bi, "_purge_container_downloads", _purge)
    monkeypatch.setattr(bi, "verified_profile_wipe", _wipe)
    monkeypatch.setattr(bi, "_tar_stream_to_container", _tar)
    monkeypatch.setattr(bi, "_cookie_op",
                        lambda base, op, cookies=None: events.append(f"cookie:{op}") or [])
    monkeypatch.setattr(bi, "resolve_cdp_ws_url", lambda base: "ws://stub/devtools/browser/x")
    monkeypatch.setattr(bi, "_set_download_behavior", lambda base, allow: None)
    monkeypatch.setattr(bi, "_sweep_container_downloads", lambda name, scope: [])
    monkeypatch.setattr(bi, "park_browser_idle", lambda base: None)
    monkeypatch.setattr(bi, "_current_page", lambda base: None)
    monkeypatch.setattr(containers, "docker",
                        lambda args, timeout=60: type("R", (), {"returncode": 0, "stdout": "",
                                                                "stderr": ""})())

    class _IPC:
        tasks = []

        def get_active_tasks(self, session_id=None):
            return list(self.tasks)

        def get_pending_tasks(self, session_id=None):
            return []

    ipc = _IPC()
    monkeypatch.setattr(ipc_mod, "get_ipc", lambda: ipc)

    class _NoPool:
        def resolve(self, scope):
            return None

    monkeypatch.setattr(bp, "get_browser_pool", lambda: _NoPool())
    roots = {}
    for scope, files in (("scope-a", {"a.txt": b"a"}),
                         ("scope-b", {"index.html": b"<p>b</p>",
                                      "b-private-notes.txt": b"notes of b"})):
        root = tmp_path / scope
        root.mkdir()
        for fn, data in files.items():
            (root / fn).write_bytes(data)
        roots[scope] = root
    monkeypatch.setattr(sess, "get_user_projects_root", lambda scope: roots.get(scope))

    m = bi.InteractiveBrowserManager()
    m._emit = lambda payload, session_id: None
    m._ensure_janitor = lambda: None
    monkeypatch.setattr(bi, "get_interactive_manager", lambda: m)

    def _cdp(cdp_base, url, *, wait_ms, max_text):
        events.append("render")
        m._test_seen = {"workspace": set(workspace), "mutex_held": m._handover_mutex.locked()}
        return {"ok": True, "url": url, "title": "", "text": "x", "console": [],
                "page_errors": [], "failed_requests": [], "screenshot_b64": "", "error": ""}

    monkeypatch.setattr(br, "_render_via_cdp", _cdp)
    m._test_events = events
    m._test_workspace = workspace
    m._test_state = state
    m._test_roots = roots
    m._test_ipc = ipc
    return m


def test_a_render_on_the_shared_browser_hands_the_jar_over_first(shared):
    """The probe navigates the shared browser's first page and returns its text
    and a screenshot. A fresh process does not know whose state the container
    holds (the container outlives a restart), so the first render is a change of
    hands like an interactive start or an agent run: purge, VERIFIED wipe, jar
    cleared - all before the page is opened - and the jar is the probe's then.
    Before, it rendered in whatever logins the container held.

    MUTATION: render without the handover (drop `_hand_jar_locked` from
    hold_for_probe) - red."""
    out = br.render_page("https://mail.example.com/", user_scope_id="scope-b")
    assert out["ok"] is True
    ev = shared._test_events
    assert ev[:3] == ["purge", "wipe", "cookie:clear_cookies"], ev
    assert ev.index("render") > ev.index("cookie:clear_cookies")
    assert shared._last_cookie_scope == "scope-b"

    # The same scope again: its own jar, no second wipe.
    ev.clear()
    assert br.render_page("https://mail.example.com/", user_scope_id="scope-b")["ok"] is True
    assert "wipe" not in ev and ev[-1] == "render"


def test_an_unconfirmed_wipe_never_renders(shared):
    """Fail-closed like the other lanes: a wipe that cannot be confirmed means the
    previous holder's state may still be in the profile, so nothing is opened,
    the answer is an error, and the jar does not change owner.

    MUTATION: swallow BrowserHandoverError in hold_for_probe and render anyway -
    red."""
    shared._test_state["wipe_ok"] = False
    out = br.render_page("https://mail.example.com/", user_scope_id="scope-b")
    assert out["ok"] is False and out["busy"] is False
    assert "handed over" in out["error"]
    assert "render" not in shared._test_events
    assert shared._last_cookie_scope is None


def test_a_file_render_leaves_nothing_for_the_next_holder(shared):
    """The reverse direction: a file render mirrors the caller's project tree into
    the container's Workspace, and the mirror only adds. When the render left the
    jar on the previous owner, that owner's next lease saw no change of hands,
    purged nothing, and the other scope's files sat in their file picker. Now the
    render takes the jar, and the next lease's handover purges the mirror.

    MUTATIONS: render without the handover (red: b's files during a's lease);
    sync the mirror before the handover (red: the file is not there to render)."""
    shared.start("scope-a", "sess-a", save=False)
    shared.stop("user", requester_scope="scope-a")
    page = shared._test_roots["scope-b"] / "index.html"
    out = br.render_page(str(page), user_scope_id="scope-b")
    assert out["ok"] is True
    assert "index.html" in shared._test_seen["workspace"], "the rendered file was not mirrored"
    assert "a.txt" not in shared._test_seen["workspace"], "a's files were in b's render"

    r = shared.start("scope-a", "sess-a2", save=False)
    assert r["status"] == "active"
    assert not {"index.html", "b-private-notes.txt"} & shared._test_workspace, (
        f"b's files reached a's lease: {sorted(shared._test_workspace)}")


def test_the_probe_holds_the_browser_until_it_has_rendered(shared):
    """Held from the handover to the parking: a lease or a run that started
    between the wipe and the navigation would have its own jar restored under the
    probe, which would then read that holder's page.

    MUTATION: release the mutex right after the handover - red."""
    assert br.render_page("https://example.com/", user_scope_id="scope-b")["ok"] is True
    assert shared._test_seen["mutex_held"] is True
    assert not shared._handover_mutex.locked(), "the probe did not let go of the browser"


def test_a_handover_in_progress_answers_busy_at_once(shared):
    """The probe waits for no one: while another handover holds the browser it
    answers busy instead of queueing behind it (and then rendering into the jar
    that handover installed).

    MUTATION: wait for the mutex instead of trying it - red (renders late)."""
    shared._handover_mutex.acquire()
    threading.Timer(1.0, shared._handover_mutex.release).start()
    t0 = time.monotonic()
    out = br.render_page("https://example.com/", user_scope_id="scope-b")
    assert out["busy"] is True and "render" not in shared._test_events
    assert time.monotonic() - t0 < 0.9
    time.sleep(1.1)


def test_an_agent_run_in_another_process_makes_the_shared_browser_busy(shared):
    """A spawned browser_agent child drives the shared container from its own
    process, where this process's lease state and mutex do not reach. The queue
    scan is how the interactive start sees it, and the probe asks the same.

    MUTATION: check only this process's activity - red."""
    shared._test_ipc.tasks = [type("T", (), {"agent_type": "browser_agent"})()]
    out = br.render_page("https://example.com/", user_scope_id="scope-b")
    assert out["busy"] is True
    assert "render" not in shared._test_events and "wipe" not in shared._test_events


def test_a_dedicated_instance_renders_without_a_wipe(shared, monkeypatch):
    """Only its own user is routed to a per-user instance, so its profile is theirs
    to keep: no wipe, no scrub - and another user's agent run on THEIR instance
    (the queue scan cannot tell instances apart) does not make it busy.

    MUTATIONS: apply the queue scan on dedicated instances too (red: busy);
    hand over non-persistently (red: scrubbed)."""
    import vaf.core.browser_interactive as bi
    mine = bi.InteractiveBrowserManager(cdp_base_url="http://127.0.0.1:49222",
                                        container_name="vaf-browser-u-0123456789ab",
                                        dedicated_scope="scope-b")
    monkeypatch.setattr(br, "_endpoint_for", lambda scope: (mine.cdp_base(), mine))
    shared._test_ipc.tasks = [type("T", (), {"agent_type": "browser_agent"})()]
    out = br.render_page("https://example.com/", user_scope_id="scope-b")
    assert out["ok"] is True
    assert shared._test_events == ["render"], shared._test_events


# ── the tool face ─────────────────────────────────────────────────────────────

def test_tool_report_carries_what_a_developer_checks(quiet_manager, monkeypatch):
    from vaf.tools.render_check import RenderCheckTool

    def _cdp(cdp_base, url, *, wait_ms, max_text):
        return {"ok": True, "url": url, "title": "My App", "text": "Welcome",
                "console": ["[error] boom"], "page_errors": ["ReferenceError: x"],
                "failed_requests": ["HTTP 404: /style.css"],
                "screenshot_b64": "", "error": ""}

    monkeypatch.setattr(br, "_render_via_cdp", _cdp)
    out = RenderCheckTool().run(target="https://example.com/", user_scope_id="s1")
    assert "My App" in out and "Welcome" in out
    assert "ReferenceError: x" in out
    assert "HTTP 404: /style.css" in out
    assert "[error] boom" in out


def test_tool_says_empty_out_loud(quiet_manager, monkeypatch):
    # A blank page is the failure this tool exists to catch; the report must
    # SAY empty, not trail off with nothing.
    from vaf.tools.render_check import RenderCheckTool

    def _blank(cdp_base, url, *, wait_ms, max_text):
        return {"ok": True, "url": url, "title": "", "text": "",
                "console": [], "page_errors": [], "failed_requests": [],
                "screenshot_b64": "", "error": ""}

    monkeypatch.setattr(br, "_render_via_cdp", _blank)
    out = RenderCheckTool().run(target="https://example.com/", user_scope_id="s1")
    assert "EMPTY" in out


def test_coder_relative_targets_resolve_against_base_dir(quiet_manager, monkeypatch, tmp_path):
    import vaf.core.session as sess
    from vaf.tools.render_check import RenderCheckTool
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "index.html").write_bytes(b"<p>x</p>")
    monkeypatch.setattr(sess, "get_user_projects_root", lambda scope: tmp_path)
    RenderCheckTool(base_dir=str(proj)).run(target="index.html", user_scope_id="s1")
    assert quiet_manager.rendered["url"] == "file:///home/browser/Workspace/proj/index.html"


def test_busy_report_is_labelled_busy(monkeypatch):
    import vaf.core.browser_interactive as bi
    import vaf.core.browser_pool as bp
    from vaf.tools.render_check import RenderCheckTool
    monkeypatch.setattr(bi, "get_interactive_manager", lambda: _Manager(busy=True))
    monkeypatch.setattr(bp, "get_browser_pool", lambda: (_ for _ in ()).throw(RuntimeError))
    out = RenderCheckTool().run(target="https://example.com/", user_scope_id="s1")
    assert out.startswith("Browser busy")


# ── wiring, pinned statically so it cannot drift one file at a time ───────────

def test_coder_advertises_and_registers_the_same_tool():
    # Rule 2: the schema list and local_tools are copies. A schema without a
    # registration is a tool the model calls into a KeyError; a registration
    # without a schema is a tool the model can never call.
    src = (_REPO / "vaf" / "tools" / "coder.py").read_bytes().decode("utf-8")
    assert 'self.local_tools["render_check"] = RenderCheckTool(base_dir, environment=_env_binding,' in src
    assert '"name": "render_check"' in src
    assert "`render_check(target)`" in src, "prompt guidance line missing"
    # The report closes with the rendered text; the coder's default 3000-char
    # head cut would drop exactly that part.
    assert '"read_file", "run_tests", "render_check"' in src, \
        "render_check lost the larger history char limit"


def test_both_container_lanes_carry_the_host_gateway_name():
    # render_page rewrites localhost to host.docker.internal unconditionally,
    # so BOTH browser lanes (compose shared browser, pooled instances) must
    # resolve that name - one without the other breaks per deployment shape.
    compose = (_REPO / "docker-compose.memory.yml").read_bytes().decode("utf-8")
    browser_block = compose.split("vaf-browser:", 1)[1].split("\n  vaf-", 1)[0]
    assert "host.docker.internal:host-gateway" in browser_block
    pool = (_REPO / "vaf" / "core" / "browser_pool.py").read_bytes().decode("utf-8")
    assert '"--add-host", "host.docker.internal:host-gateway"' in pool


def test_rewrite_covers_every_local_spelling():
    for host in ("localhost", "127.0.0.1", "0.0.0.0", "LOCALHOST"):
        url, rewritten = br._rewrite_local(f"http://{host}:8000/x")
        assert rewritten and url == "http://host.docker.internal:8000/x"
    url, rewritten = br._rewrite_local("http://192.168.1.5:8000/x")
    assert not rewritten and "192.168.1.5" in url
