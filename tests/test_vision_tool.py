# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression tests for the vision-as-a-tool design (token-efficient multimodal memory).

The main reasoning model is text-only: an attached image is run through the vision
backend ONCE to produce a base description (persisted), which is injected as a
``[VISUAL CONTEXT …]`` text block on every turn; the model calls ``analyze_image`` to
inspect the stored image on demand. These tests pin each moving part with the vision
call mocked (no API key / network), plus the disk persistence round-trip.
"""
import tempfile

import pytest

from vaf.core.agent import Agent
from vaf.core.session import Message, SessionManager
from vaf.core import vision_infer as vinfer
from vaf.core.vision_infer import build_visual_context_text, select_vision_backend


@pytest.fixture(autouse=True)
def _clear_vision_desc_cache():
    """The process-wide description memo is shared across tests — clear it each test so a
    cached entry from one test doesn't suppress a vision_infer call expected by another."""
    vinfer._DESC_CACHE.clear()
    yield
    vinfer._DESC_CACHE.clear()


def _img(name="shot.png", data="QUJD", mime="image/png", desc=None):
    d = {"name": name, "mime_type": mime, "data": data}
    if desc is not None:
        d["base_description"] = desc
    return d


def _patch_config(monkeypatch, values: dict):
    """Back Config.get/Config.load by a plain dict (falls back to the given default)."""
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", staticmethod(lambda k, d=None: values.get(k, d)))
    monkeypatch.setattr(Config, "load", staticmethod(lambda: dict(values)))


# ---------------------------------------------------------------------------
# vision_infer backend selection
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# File-based image storage (path instead of inline base64)
# ---------------------------------------------------------------------------
def test_image_to_b64_reads_legacy_data():
    got = vinfer.image_to_b64({"data": "QUJD", "mime_type": "image/png"})
    assert got == ("QUJD", "image/png")


def test_image_to_b64_strips_data_uri_prefix():
    got = vinfer.image_to_b64({"data": "data:image/png;base64,QUJD"})
    assert got == ("QUJD", "image/jpeg")  # default mime when not given


def test_image_to_b64_reads_file_path(tmp_path):
    import base64
    f = tmp_path / "shot.png"
    f.write_bytes(b"HELLO")
    got = vinfer.image_to_b64({"path": str(f), "mime_type": "image/png"})
    assert got == (base64.b64encode(b"HELLO").decode(), "image/png")


def test_image_to_b64_none_when_missing(tmp_path):
    assert vinfer.image_to_b64({"path": str(tmp_path / "gone.png")}) is None
    assert vinfer.image_to_b64({}) is None
    assert vinfer.image_to_b64("nope") is None


def test_get_session_attachments_dir_is_user_siloed(monkeypatch, tmp_path):
    from vaf.core import session as sess_mod
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "documents_dir", staticmethod(lambda: tmp_path))
    d = sess_mod.get_session_attachments_dir("blue123", "cafe1234-0000-0000-0000-000000000000", create=True)
    assert d is not None and d.is_dir()
    assert d == tmp_path / "VAF_Projects" / "cafe1234" / "blue123" / "attachments"


def test_attachments_dirs_are_isolated_per_user(monkeypatch, tmp_path):
    # Two different users uploading in a SAME-named chat must land in DIFFERENT, non-overlapping
    # per-user roots (VAF_Projects/<uid8>/...). The silo key is the server-derived user_scope_id.
    from vaf.core import session as sess_mod
    from vaf.core.platform import Platform
    monkeypatch.setattr(Platform, "documents_dir", staticmethod(lambda: tmp_path))
    a = sess_mod.get_session_attachments_dir("chat1", "aaaaaaaa-0000-0000-0000-000000000000", create=False)
    b = sess_mod.get_session_attachments_dir("chat1", "bbbbbbbb-0000-0000-0000-000000000000", create=False)
    assert a != b
    assert "aaaaaaaa" in str(a) and "bbbbbbbb" in str(b)
    root_b = sess_mod.get_user_projects_root("bbbbbbbb-0000-0000-0000-000000000000")
    assert not str(a).startswith(str(root_b))  # user A's dir is NOT under user B's root
    # Same user + same chat is stable (so the path persists across turns/reloads).
    assert a == sess_mod.get_session_attachments_dir("chat1", "aaaaaaaa-0000-0000-0000-000000000000", create=False)


def test_persist_attached_images_writes_file_not_base64(monkeypatch, tmp_path):
    import vaf.core.session as sess_mod
    from vaf.core.web_server import _persist_attached_images_to_files
    monkeypatch.setattr(sess_mod, "get_session_attachments_dir", lambda sid, uid, create=True: tmp_path)
    out = _persist_attached_images_to_files(
        [{"data": "UE5HREFUQQ==", "mime_type": "image/png", "name": "shot.png"}], "s1", "uid")
    assert "data" not in out[0] and out[0]["path"]
    assert out[0]["name"] == "shot.png" and out[0]["mime_type"] == "image/png"
    from pathlib import Path
    assert Path(out[0]["path"]).exists()  # file actually written


def test_persist_keeps_non_image_as_is(monkeypatch, tmp_path):
    import vaf.core.session as sess_mod
    from vaf.core.web_server import _persist_attached_images_to_files
    monkeypatch.setattr(sess_mod, "get_session_attachments_dir", lambda sid, uid, create=True: tmp_path)
    entry = {"data": "QUJD", "mime_type": "application/pdf", "name": "doc.pdf"}
    out = _persist_attached_images_to_files([entry], "s1", "uid")
    assert out[0] == entry  # untouched (not an image)


def test_persist_uses_mime_extension_not_filename(monkeypatch, tmp_path):
    # Hardening: a crafted name (evil.svg) with mime image/png must be written with a .png
    # extension (derived from the validated mime), so /api/file serves an image Content-Type.
    import vaf.core.session as sess_mod
    from vaf.core.web_server import _persist_attached_images_to_files
    monkeypatch.setattr(sess_mod, "get_session_attachments_dir", lambda sid, uid, create=True: tmp_path)
    out = _persist_attached_images_to_files(
        [{"data": "QUJD", "mime_type": "image/png", "name": "evil.svg"}], "s1", "uid")
    assert out[0]["path"].endswith(".png")  # from mime, not the .svg in the name
    assert out[0]["name"] == "evil.svg"     # display name preserved


def test_persist_unique_filenames_no_collision(monkeypatch, tmp_path):
    # Two same-named images in the same second must not overwrite each other.
    import vaf.core.session as sess_mod
    from vaf.core.web_server import _persist_attached_images_to_files
    from pathlib import Path
    monkeypatch.setattr(sess_mod, "get_session_attachments_dir", lambda sid, uid, create=True: tmp_path)
    p1 = _persist_attached_images_to_files([{"data": "QUJD", "mime_type": "image/png", "name": "image.png"}], "s", "u")[0]["path"]
    p2 = _persist_attached_images_to_files([{"data": "REVG", "mime_type": "image/png", "name": "image.png"}], "s", "u")[0]["path"]
    assert p1 != p2 and Path(p1).exists() and Path(p2).exists()


def test_vision_infer_reads_path_image(monkeypatch, tmp_path):
    f = tmp_path / "img.png"
    f.write_bytes(b"PNGBYTES")
    _patch_config(monkeypatch, {"vision_provider": "veyllo", "vision_model": "veyllo-chat"})
    _fake_api_backend(monkeypatch, ["a path-based view"])
    assert vinfer.vision_infer([{"path": str(f), "mime_type": "image/png", "name": "img.png"}], "describe") == "a path-based view"


def test_describe_image_cached_memoizes_by_path(monkeypatch):
    calls = {"n": 0}

    def fake_infer(images, prompt, **kw):
        calls["n"] += 1
        return f"desc-{calls['n']}"

    monkeypatch.setattr(vinfer, "vision_infer", fake_infer)
    _patch_config(monkeypatch, {"vision_description_max_tokens": 1024})
    vinfer._DESC_CACHE.clear()

    img = {"path": "/tmp/vaf-x/shot.png", "mime_type": "image/png", "name": "shot.png"}
    d1 = vinfer.describe_image_cached(img)
    d2 = vinfer.describe_image_cached(dict(img))  # same path, different dict instance
    assert d1 == d2 and calls["n"] == 1, "the same image must be described only once"

    # A different image triggers a fresh (billed) call.
    vinfer.describe_image_cached({"path": "/tmp/vaf-x/other.png", "mime_type": "image/png", "name": "other.png"})
    assert calls["n"] == 2


def test_describe_image_cached_memoizes_inline_data(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(vinfer, "vision_infer", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or "d"))
    _patch_config(monkeypatch, {"vision_description_max_tokens": 1024})
    vinfer._DESC_CACHE.clear()
    img = {"data": "QUJD", "mime_type": "image/png"}
    vinfer.describe_image_cached(img)
    vinfer.describe_image_cached(dict(img))
    assert calls["n"] == 1  # cached by content hash


def test_visual_context_names_file_location():
    out = build_visual_context_text(
        [{"name": "shot.png", "path": "/u/VAF_Projects/ab/sess/attachments/171_0_shot.png", "base_description": "d"}], "")
    assert "attachments/171_0_shot.png" in out


def test_analyze_image_uses_path_from_live_history(monkeypatch, tmp_path):
    from vaf.tools.vision import AnalyzeImageTool
    f = tmp_path / "live.png"
    f.write_bytes(b"X")
    monkeypatch.setattr(vinfer, "vision_infer", lambda images, prompt, **kw: f"PATH:{images[0].get('path')}")
    _patch_config(monkeypatch, {"vision_description_max_tokens": 1024})
    agent = type("A", (), {"history": [
        {"role": "user", "content": "look", "images": [{"name": "live.png", "path": str(f), "mime_type": "image/png"}]},
    ]})()
    out = AnalyzeImageTool().run(prompt="?", session_id="s1", _agent=agent)
    assert str(f) in out  # tool found the path-based image and passed it through


def _fake_api_backend(monkeypatch, chunks):
    """Make APIBackendManager(...).chat_completion() yield a fixed chunk sequence."""
    import vaf.core.api_backend as ab

    class _FakeBackend:
        def __init__(self, *a, **k):
            pass

        def chat_completion(self, *a, **k):
            for c in chunks:
                yield c

    monkeypatch.setattr(ab, "APIBackendManager", _FakeBackend)


def test_vision_infer_rejects_api_error_sentinel(monkeypatch):
    # Providers don't raise — they yield "[API Error from ...]". Must NOT become a description.
    _patch_config(monkeypatch, {"vision_provider": "veyllo", "vision_model": "veyllo-chat"})
    _fake_api_backend(monkeypatch, ["[API Error from veyllo: rate limit exceeded]"])
    assert vinfer.vision_infer([_img()], "describe") is None


def test_vision_infer_strips_think_and_control_json(monkeypatch):
    # Reasoning <think> markers and the trailing finish_reason JSON chunk must be filtered.
    _patch_config(monkeypatch, {"vision_provider": "veyllo", "vision_model": "veyllo-chat"})
    _fake_api_backend(monkeypatch, ["<think>", "planning", "</think>\n\n", "A red ball.", '{"finish_reason": "stop"}'])
    assert vinfer.vision_infer([_img()], "describe") == "A red ball."


def test_select_backend_explicit_vision_provider(monkeypatch):
    _patch_config(monkeypatch, {"vision_provider": "google", "vision_model": "gemini-2.5-flash"})
    assert select_vision_backend() == ("google", "gemini-2.5-flash")


def test_select_backend_falls_back_to_multimodal_main_provider(monkeypatch):
    # No explicit vision provider, main provider is multimodal (veyllo) → use it.
    _patch_config(monkeypatch, {"vision_provider": "", "provider": "veyllo", "api_model_veyllo": "veyllo-chat"})
    assert select_vision_backend() == ("veyllo", "veyllo-chat")


def test_select_backend_none_when_main_provider_is_not_vision(monkeypatch):
    # DeepSeek can't see images and no vision provider configured → no backend.
    _patch_config(monkeypatch, {"vision_provider": "", "provider": "deepseek", "api_model_deepseek": "deepseek-v4-flash"})
    assert select_vision_backend() == (None, None)


# ---------------------------------------------------------------------------
# build_visual_context_text — the text that replaces the raw image
# ---------------------------------------------------------------------------
def test_visual_context_uses_base_description_and_invites_tool():
    out = build_visual_context_text([_img(desc="A centered logo over a chat input.")], "do point 2")
    assert "VISUAL CONTEXT" in out and "centered logo" in out
    assert "analyze_image" in out          # invites the on-demand tool
    assert "ground truth" in out           # anti-"I guessed" framing
    assert out.rstrip().endswith("do point 2")  # original user text preserved
    assert "data:image" not in out and "QUJD" not in out  # NO raw bytes


def test_visual_context_marks_missing_description():
    out = build_visual_context_text([_img(desc=None)], "")
    assert "automatic analysis is unavailable" in out
    assert "analyze_image" in out


# ---------------------------------------------------------------------------
# _prepare_messages: text-only main model (Option A / vision_mode=description_tool)
# ---------------------------------------------------------------------------
class _Stub:
    _thinking_reply_context = None
    provider = "local"          # vision-capable fall-through, but text path ignores it
    filename = "local"
    model_display_name = ""
    config = {}
    history = []

    def _consolidate_system_messages(self, messages):
        return messages


def test_prepare_messages_injects_description_not_image_block(monkeypatch):
    # default vision_mode = "description_tool"
    _patch_config(monkeypatch, {"vision_mode": "description_tool",
                                "vision_image_max_edge": 2000, "vision_image_jpeg_quality": 85})
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "benenne die UI-Probleme",
         "images": [_img(desc="A chat UI: centered logo, small RAG status line, large gaps.")]},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "2 bitte"},
    ]
    out = Agent._prepare_messages(_Stub(), msgs)
    img_user = next(m for m in out if m["role"] == "user" and "RAG status line" in str(m.get("content", "")))
    # content is a STRING (VISUAL CONTEXT text), never a multimodal list with image_url blocks
    assert isinstance(img_user["content"], str)
    assert "VISUAL CONTEXT" in img_user["content"]
    assert not any(isinstance(m.get("content"), list) for m in out), "no raw image_url blocks to the main model"


# ---------------------------------------------------------------------------
# chat_step base-description generation: once, idempotent, mode-gated
# ---------------------------------------------------------------------------
def test_ensure_base_descriptions_generates_once(monkeypatch):
    calls = {"n": 0}

    def fake_infer(images, prompt, **kw):
        calls["n"] += 1
        return "A detailed objective description."

    monkeypatch.setattr(vinfer, "vision_infer", fake_infer)
    _patch_config(monkeypatch, {"vision_mode": "description_tool", "vision_description_max_tokens": 1024})

    images = [_img(name="one.png", desc=None)]
    Agent._ensure_image_base_descriptions(object(), images)
    assert images[0]["base_description"] == "A detailed objective description."
    assert calls["n"] == 1

    # Idempotent: an image that already has a description is not re-analysed.
    Agent._ensure_image_base_descriptions(object(), images)
    assert calls["n"] == 1


def test_ensure_base_descriptions_skipped_in_inline_mode(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(vinfer, "vision_infer", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or "x"))
    _patch_config(monkeypatch, {"vision_mode": "inline_multimodal"})
    images = [_img(desc=None)]
    Agent._ensure_image_base_descriptions(object(), images)
    assert calls["n"] == 0 and "base_description" not in images[0]


# ---------------------------------------------------------------------------
# Persistence + reload: base_description rides metadata["images"] to disk and back
# ---------------------------------------------------------------------------
def test_base_description_persists_and_get_history_restores_it():
    with tempfile.TemporaryDirectory() as tmp:
        sm = SessionManager(storage_dir=tmp)
        sess = sm.new(name="vision")
        sess.add_message(role="user", content="analyse this",
                         metadata={"images": [_img(desc="A red ball on a table.")]})
        sess.add_message(role="assistant", content="done")
        sm.save(sess)

        loaded = sm.load(sess.id)
        umsg = next(m for m in loaded.messages if m.role == "user")
        assert umsg.metadata["images"][0]["base_description"] == "A red ball on a table."

        hist = loaded.get_history()
        user = next(h for h in hist if h["role"] == "user")
        asst = next(h for h in hist if h["role"] == "assistant")
        assert user["images"][0]["base_description"] == "A red ball on a table."
        assert "images" not in asst  # only user turns carry images


# ---------------------------------------------------------------------------
# analyze_image tool
# ---------------------------------------------------------------------------
def _session_with_images(imgs):
    s = type("S", (), {})()
    s.messages = [Message(role="user", content="x", metadata={"images": imgs})]
    return s


def _patch_sessionmanager(monkeypatch, session):
    import vaf.core.session as sess_mod

    class _FakeSM:
        def __init__(self, *a, **k):
            pass

        def load(self, sid):
            return session

    monkeypatch.setattr(sess_mod, "SessionManager", _FakeSM)


def test_analyze_image_runs_vision_on_session_image(monkeypatch):
    from vaf.tools.vision import AnalyzeImageTool
    _patch_sessionmanager(monkeypatch, _session_with_images([_img(name="ui.png", desc="d")]))
    monkeypatch.setattr(vinfer, "vision_infer", lambda images, prompt, **kw: f"SEEN:{prompt}")
    _patch_config(monkeypatch, {"vision_description_max_tokens": 1024})

    out = AnalyzeImageTool().run(prompt="what color is the button?", session_id="s1")
    assert "SEEN:what color is the button?" in out
    assert "ui.png" in out


def test_analyze_image_uses_live_agent_history_on_upload_turn(monkeypatch):
    # On the upload turn the image lives in agent.history but is NOT on disk yet.
    # The tool must find it via the live agent, not only via SessionManager.load().
    from vaf.tools.vision import AnalyzeImageTool
    _patch_sessionmanager(monkeypatch, _session_with_images([]))  # disk is empty
    monkeypatch.setattr(vinfer, "vision_infer", lambda images, prompt, **kw: f"SEEN:{images[0]['name']}")
    _patch_config(monkeypatch, {"vision_description_max_tokens": 1024})
    agent = type("A", (), {"history": [
        {"role": "user", "content": "look", "images": [_img(name="live.png", desc="d")]},
    ]})()
    out = AnalyzeImageTool().run(prompt="what is this?", session_id="s1", _agent=agent)
    assert "SEEN:live.png" in out  # came from live history despite empty disk


def test_analyze_image_no_image_attached(monkeypatch):
    from vaf.tools.vision import AnalyzeImageTool
    _patch_sessionmanager(monkeypatch, _session_with_images([]))
    out = AnalyzeImageTool().run(prompt="x", session_id="s1")
    assert "No image" in out


def test_analyze_image_reports_when_vision_unavailable(monkeypatch):
    from vaf.tools.vision import AnalyzeImageTool
    _patch_sessionmanager(monkeypatch, _session_with_images([_img()]))
    monkeypatch.setattr(vinfer, "vision_infer", lambda *a, **k: None)  # no vision backend
    _patch_config(monkeypatch, {"vision_description_max_tokens": 1024})
    out = AnalyzeImageTool().run(prompt="x", session_id="s1")
    assert "unavailable" in out.lower()


def test_analyze_image_requires_prompt():
    from vaf.tools.vision import AnalyzeImageTool
    assert "prompt" in AnalyzeImageTool().run(prompt="  ", session_id="s1").lower()


def test_analyze_image_selects_by_index_and_name():
    from vaf.tools.vision import AnalyzeImageTool
    imgs = [_img(name="first.png"), _img(name="second.png")]
    assert AnalyzeImageTool._select_image(imgs, None)["name"] == "second.png"   # default = most recent
    assert AnalyzeImageTool._select_image(imgs, "0")["name"] == "first.png"     # by index
    assert AnalyzeImageTool._select_image(imgs, "second")["name"] == "second.png"  # by name substring
    assert AnalyzeImageTool._select_image(imgs, "nope") is None
