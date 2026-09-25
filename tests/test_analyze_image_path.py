# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""analyze_image image_path: the agent looks at images it made, under the READ boundary.

The tool was attachment-only; once the agent could produce images itself (python_sandbox
export_files, a screenshot a command took), it had no way to look at them and spiraled into
struct/identify/OCR detours until the user aborted (live incident). The first fix held
image_path to the chat's workspace, keyed on the session: an arbitrary host path would let a
remote user exfiltrate foreign files through the vision model's description. That rule was
right about the danger and too narrow about the answer - a screenshot saved anywhere else in
the person's own files could not be looked at, while `read_file` could read the same file.

The boundary is now the one `read_file` has: the per-user READ jail BaseTool installs around
run() (`file_access = "read"`). What the person may read, the agent may look at; nothing else.
A relative path still means the chat's workspace.

MUTATION: drop `file_access = "read"` from the tool and the jail test goes red (no boundary is
installed, the foreign file is served); resolve relative paths against the process instead of
the workspace and the relative test goes red.
"""
import types

import pytest

import vaf.core.session as session_mod
import vaf.tools.filesystem as fs
from vaf.tools.vision import AnalyzeImageTool


@pytest.fixture
def ws(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.setattr(session_mod, "get_session_workspace_dir",
                        lambda sid, create=False: workspace if sid == "chat1" else None)
    return workspace


def _png(path):
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16)
    return path


def _jailed_to(root):
    """The read jail a user whose own tree is `root` would get (compute_user_jail's shape)."""
    return {"is_admin": False, "uid8": "ab12cd34", "allowed_roots": [root]}


# ── resolution ────────────────────────────────────────────────────────────────

def test_relative_path_resolves_into_workspace(ws):
    _png(ws / "chart.png")
    got = AnalyzeImageTool._image_from_path("chart.png", "chat1")
    assert isinstance(got, dict)
    assert got["name"] == "chart.png" and got["mime_type"] == "image/png"


def test_an_image_outside_the_workspace_is_reachable_when_it_may_be_read(ws, tmp_path):
    """The screenshot case: saved in the person's own files, not in the chat's workspace."""
    p = _png(tmp_path / "outside" / "screenshot.png")
    token = fs.set_librarian_scope(_jailed_to(tmp_path))
    try:
        got = AnalyzeImageTool._image_from_path(str(p), "chat1")
    finally:
        fs.reset_librarian_scope(token)
    assert isinstance(got, dict) and got["path"] == str(p.resolve())


def test_the_read_jail_decides(ws, tmp_path, monkeypatch):
    """A file outside the caller's own tree is refused, however it is named - through run(),
    so it is the tool's own declaration that installs the jail, not the test."""
    p = _png(tmp_path / "outside" / "secret.png")
    modes = []

    def fake_jail(scope, role, *, mode="write"):
        modes.append(mode)
        return _jailed_to(ws)

    monkeypatch.setattr(fs, "compute_user_jail", fake_jail)
    import vaf.core.vision_infer as vi_mod
    monkeypatch.setattr(vi_mod, "vision_infer", lambda *a, **k: "SERVED")
    for path in (str(p), "../outside/secret.png"):
        out = AnalyzeImageTool().run(prompt="x", image_path=path, session_id="chat1",
                                     user_scope_id="ab12cd34-0000", user_role="user")
        assert "SERVED" not in out and "denied" in out.lower(), out
    assert modes and set(modes) == {"read"}


def test_the_tool_declares_the_read_boundary():
    """What installs the jail around run() on every lane (dispatcher, coder, workflows). The
    chat (`session_id`) is declared too: it names the images attached to the conversation."""
    assert AnalyzeImageTool.file_access == "read"
    assert set(AnalyzeImageTool.identity_kwargs) == {"user_role", "user_scope_id", "session_id"}


def test_missing_file_reported(ws):
    got = AnalyzeImageTool._image_from_path("nope.png", "chat1")
    assert isinstance(got, str) and "not found" in got


def test_non_image_suffix_refused(ws):
    (ws / "data.txt").write_text("x")
    got = AnalyzeImageTool._image_from_path("data.txt", "chat1")
    assert isinstance(got, str) and "not an image" in got


def test_a_relative_path_without_a_workspace_asks_for_an_absolute_one(ws):
    for sid in ("", "other-chat"):
        got = AnalyzeImageTool._image_from_path("chart.png", sid)
        assert isinstance(got, str) and "absolute path" in got


# ── run() wiring ─────────────────────────────────────────────────────────────

def test_run_with_image_path_calls_vision(ws, monkeypatch):
    _png(ws / "chart.png")
    seen = {}

    def fake_infer(images, prompt, max_tokens=1024):
        seen["image"] = images[0]
        seen["prompt"] = prompt
        return "three labeled lines visible"

    import vaf.core.vision_infer as vi_mod
    monkeypatch.setattr(vi_mod, "vision_infer", fake_infer)
    out = AnalyzeImageTool().run(prompt="labels readable?", image_path="chart.png",
                                 session_id="chat1")
    assert "three labeled lines visible" in out
    assert seen["image"]["path"].endswith("chart.png")


def test_run_without_image_path_keeps_attachment_behavior(monkeypatch):
    # No attachments, no session -> the historical error message, unchanged.
    out = AnalyzeImageTool().run(prompt="x", session_id="", _agent=types.SimpleNamespace(history=[]))
    assert "no active session" in out.lower()


def test_read_file_points_an_image_at_analyze_image(tmp_path):
    """read_file used to hand the model an image's raw bytes. MUTATION: drop the image branch
    in ReadFileTool._read - red."""
    from vaf.tools.filesystem import ReadFileTool
    p = _png(tmp_path / "shot.png")
    out = ReadFileTool().run(path=str(p))
    assert out.startswith("shot.png is an image (PNG")
    assert f'analyze_image(image_path="{p.resolve()}"' in out and "\x89" not in out
