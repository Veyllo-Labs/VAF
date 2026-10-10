# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Looking at what a sandbox environment serves.

The page is opened INSIDE the environment by chromium-headless-shell: localhost is the
environment, nothing joins its network, no port is published, no CDP port exists. The
personal browser container is never used, because its CDP listens without authentication
on its network and code in the environment could drive it (browser_pool.py)."""
import base64
import types

import pytest

from vaf.core import containers
from vaf.core import environments as envmod

PNG = b"\x89PNG\r\n\x1a\nfake"
STDERR = (
    '[1010/065518.768008:INFO:CONSOLE:1] "boom from page", source: http://localhost:8000/ (1)\n'
    '[1010/065518.768042:INFO:CONSOLE:1] "info line", source: http://localhost:8000/ (1)\n'
    '[1010/065518.768808:INFO:CONSOLE:1] "Uncaught Error: uncaught here", source: http://localhost:8000/ (1)\n'
    "[1010/065518.9:WARNING:sandbox] unrelated\n")
DOM = ("<html><head><title>My App</title><style>.x{}</style></head><body><h1>Hello preview</h1>"
       "<script>console.log(1)</script><p>added by js</p></body></html>")


@pytest.fixture
def mgr(monkeypatch, tmp_path):
    m = envmod.EnvironmentManager(state_dir=tmp_path / "s")
    env = envmod.Environment(id="0a1b2c3d", kind="project", network="none", owner="h",
                             container="vaf-env-h-0a1b2c3d", volume="", net="n", state="running")
    monkeypatch.setattr(m, "get", lambda owner, env_id, admin=False: env)
    m.calls = []

    def _exec_in(e, argv, **kw):
        m.calls.append(argv)
        if argv[0] == "chromium-headless-shell":
            if "--dump-dom" in argv:
                return envmod.ExecResult(0, DOM, "")
            return envmod.ExecResult(0, "", STDERR)
        return envmod.ExecResult(0, "", "")

    monkeypatch.setattr(m, "exec_in", _exec_in)
    monkeypatch.setattr(containers, "docker",
                        lambda args, timeout=60, **kw: types.SimpleNamespace(returncode=0, stdout=PNG, stderr=b""))
    return m


def test_a_page_is_rendered_inside_the_environment(mgr):
    r = mgr.render("s", "0a1b2c3d", "http://localhost:8000/", wait_ms=99999)
    assert r["ok"] and base64.b64decode(r["screenshot_b64"]) == PNG and r["screenshot_ext"] == "png"
    assert r["url"] == "http://localhost:8000/"
    assert r["title"] == "My App"
    assert r["text"] == "Hello preview\nadded by js"            # no script, no style
    assert r["page_errors"] == ['"Uncaught Error: uncaught here", source: http://localhost:8000/ (1)']
    assert len(r["console"]) == 2 and "boom from page" in r["console"][0]
    assert r["failed_requests"] is None                         # not measured this way, said so
    shot = next(a for a in mgr.calls if a[0] == "chromium-headless-shell" and "--dump-dom" not in a)
    assert "--virtual-time-budget=10000" in shot and shot[-1] == "http://localhost:8000/"
    assert mgr.calls[-1][:2] == ["rm", "-rf"]                   # the shot is cleaned up


def test_a_page_that_logs_not_found_is_still_rendered(mgr, monkeypatch):
    """MUTATION: match "not found" in the browser's stderr again - red: a page whose console
    said "404 (Not Found)" was answered with "this environment has no browser"."""
    real = mgr.exec_in

    def _exec_in(e, argv, **kw):
        if argv[0] == "chromium-headless-shell" and "--dump-dom" not in argv:
            return envmod.ExecResult(0, "", '[1:1:CONSOLE(1)] "Failed to load resource: 404 (Not Found)"')
        return real(e, argv, **kw)

    monkeypatch.setattr(mgr, "exec_in", _exec_in)
    assert mgr.render("s", "0a1b2c3d", "http://localhost:8000/")["ok"]


def test_a_path_is_a_file_under_the_workspace():
    assert envmod.EnvironmentManager._render_url("index.html") == "file:///workspace/index.html"
    assert envmod.EnvironmentManager._render_url("/workspace/dist/a.html") == "file:///workspace/dist/a.html"
    with pytest.raises(envmod.EnvironmentRefused):
        envmod.EnvironmentManager._render_url("")


def test_the_fallback_image_says_it_has_no_browser(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "exec_in", lambda e, argv, **kw: envmod.ExecResult(
        127 if argv[0] == "chromium-headless-shell" else 0, "", "timeout: failed to run command"))
    with pytest.raises(envmod.EnvironmentRefused, match="no browser"):
        mgr.render("s", "0a1b2c3d", "index.html")


def test_the_coders_render_check_maps_project_files_and_refuses_the_rest(monkeypatch, tmp_path):
    from vaf.tools.render_check import RenderCheckTool
    seen = []

    class _M:
        def render(self, owner, env_id, target, wait_ms=1500):
            seen.append((owner, env_id, target))
            return {"ok": False, "error": "stop"}

    monkeypatch.setattr(envmod, "get_environment_manager", lambda: _M())
    proj = tmp_path / "proj"
    (proj / "dist").mkdir(parents=True)
    env = types.SimpleNamespace(id="0a1b2c3d")
    tool = RenderCheckTool(str(proj), environment=env, owner_scope="scope-alice")
    tool.run(target="dist/index.html")
    tool.run(target="http://localhost:5173/")
    assert seen == [("scope-alice", "0a1b2c3d", "/workspace/dist/index.html"),
                    ("scope-alice", "0a1b2c3d", "http://localhost:5173/")]
    assert "only files inside the project" in tool.run(target="../secrets.html")
    assert "only files inside the project" in tool.run(target="..")


def test_a_file_named_with_two_dots_is_still_in_the_project(monkeypatch, tmp_path):
    """MUTATION: back to rel.startswith("..") - red: `..draft.html` was refused as a climb."""
    from vaf.tools.render_check import RenderCheckTool
    seen = []

    class _M:
        def render(self, owner, env_id, target, wait_ms=1500):
            seen.append(target)
            return {"ok": False, "error": "stop"}

    monkeypatch.setattr(envmod, "get_environment_manager", lambda: _M())
    proj = tmp_path / "proj"
    proj.mkdir()
    tool = RenderCheckTool(str(proj), environment=types.SimpleNamespace(id="0a1b2c3d"),
                           owner_scope="scope-alice")
    assert "only files inside" not in tool.run(target="..draft.html")
    assert seen == ["/workspace/..draft.html"]


def test_a_path_on_another_drive_is_refused_not_raised(monkeypatch, tmp_path):
    """os.path.relpath raises ValueError across Windows drives. MUTATION: drop the
    except - red: the tool raised instead of refusing."""
    from vaf.tools import render_check as rc
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: None)

    def _other_drive(path, start=None):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(rc.os.path, "relpath", _other_drive)
    tool = rc.RenderCheckTool(str(tmp_path), environment=types.SimpleNamespace(id="0a1b2c3d"),
                              owner_scope="scope-alice")
    assert "only files inside the project" in tool.run(target="D:/elsewhere/page.html")


def test_sandbox_preview_reports_like_render_check(mgr, monkeypatch, tmp_path):
    from vaf.tools.environments import SandboxPreviewTool
    monkeypatch.setattr(envmod, "get_environment_manager", lambda: mgr)
    import vaf.core.session as session_mod
    import vaf.core.subagent_ipc as ipc
    import vaf.core.web_interface as wi
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: "chat-1")
    monkeypatch.setattr(session_mod, "get_session_workspace_dir", lambda sid, create=False, **kw: tmp_path)
    chips = []
    monkeypatch.setattr(wi, "notify_file_created", lambda sid, path, title=None: chips.append(path))
    out = SandboxPreviewTool().run(environment="0a1b2c3d", target="http://localhost:8000/",
                                   user_scope_id="s")
    assert "Rendered: http://localhost:8000/" in out and "Title: My App" in out
    assert "Uncaught Error" in out and "Failed requests: not measured in this mode" in out
    [png] = list(tmp_path.glob("render_check_*.png"))
    assert png.read_bytes() == PNG and chips == [str(png)]
