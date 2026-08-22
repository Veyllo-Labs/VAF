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
import os
import re
from pathlib import Path

import pytest

import vaf.core.browser_render as br

_REPO = Path(__file__).resolve().parents[1]


class _Manager:
    def __init__(self, busy=False):
        self.busy = busy
        self.synced = []

    def cdp_base(self):
        return "http://127.0.0.1:9222"

    def has_activity(self):
        return self.busy

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
    assert 'self.local_tools["render_check"] = RenderCheckTool(base_dir)' in src
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
