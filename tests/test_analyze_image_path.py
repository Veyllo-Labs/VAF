# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""analyze_image image_path: inspect agent-produced workspace images (Fix 9).

The tool was attachment-only; once the agent could produce images itself
(python_sandbox export_files), it had no way to quality-check them and spiraled
into struct/identify/OCR detours until the user aborted (live: green778499).
image_path is jailed to the chat's workspace, keyed on the dispatcher-injected
session id - an arbitrary host path would let a remote user exfiltrate foreign
files through the vision model's description.
"""
import types

import pytest

import vaf.core.session as session_mod
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


# ── _image_from_workspace resolution + jail ──────────────────────────────────

def test_relative_path_resolves_into_workspace(ws):
    _png(ws / "chart.png")
    got = AnalyzeImageTool._image_from_workspace("chart.png", "chat1")
    assert isinstance(got, dict)
    assert got["name"] == "chart.png" and got["mime_type"] == "image/png"


def test_absolute_path_inside_workspace_ok(ws):
    p = _png(ws / "sub_dir_free.png")
    got = AnalyzeImageTool._image_from_workspace(str(p), "chat1")
    assert isinstance(got, dict)


def test_path_outside_workspace_denied(ws, tmp_path):
    p = _png(tmp_path / "outside" / "secret.png")
    got = AnalyzeImageTool._image_from_workspace(str(p), "chat1")
    assert isinstance(got, str) and "Access denied" in got


def test_traversal_out_of_workspace_denied(ws, tmp_path):
    _png(tmp_path / "outside" / "leak.png")
    got = AnalyzeImageTool._image_from_workspace("../outside/leak.png", "chat1")
    assert isinstance(got, str) and "Access denied" in got


def test_missing_file_reported(ws):
    got = AnalyzeImageTool._image_from_workspace("nope.png", "chat1")
    assert isinstance(got, str) and "not found" in got


def test_non_image_suffix_refused(ws):
    (ws / "data.txt").write_text("x")
    got = AnalyzeImageTool._image_from_workspace("data.txt", "chat1")
    assert isinstance(got, str) and "not an image" in got


def test_no_session_refused(ws):
    got = AnalyzeImageTool._image_from_workspace("chart.png", "")
    assert isinstance(got, str) and "session" in got.lower()


def test_no_workspace_yet_hints_creation(ws):
    got = AnalyzeImageTool._image_from_workspace("chart.png", "other-chat")
    assert isinstance(got, str) and "no workspace" in got.lower()


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
