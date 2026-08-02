# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The six hand-rolled one-shot completions consume the shared primitive.

Each pin here is either a live defect the conversion fixed (the git commit-message
metadata frame, the RAG stream crash, the dead attachment-RAG timeout) or the wiring
that keeps a site from quietly hand-rolling again (AST: the three deleted
call_local_llm copies must not return; every converted module calls complete).
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CONVERTED_CLIS = (
    "vaf/cli/cmd/git.py",
    "vaf/cli/cmd/debug.py",
    "vaf/cli/cmd/generate.py",
)


# ── the deleted copies stay deleted (AST, not substring) ─────────────────────────────

@pytest.mark.parametrize("rel", CONVERTED_CLIS)
def test_the_hand_rolled_copy_is_gone(rel):
    tree = ast.parse((ROOT / rel).read_bytes())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "call_local_llm":
            pytest.fail(f"{rel}: call_local_llm returned - one-shot completions route "
                        f"through vaf.core.completion.complete")


@pytest.mark.parametrize("rel", CONVERTED_CLIS + (
    "vaf/tools/coder_templates/__init__.py",
    "vaf/memory/rag.py",
    "vaf/memory/attachment_rag.py",
))
def test_the_converted_module_calls_the_primitive(rel):
    tree = ast.parse((ROOT / rel).read_bytes())
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "complete"
    ]
    assert calls, f"{rel}: no complete(...) call - the conversion was reverted"


# ── the git bug pin, driven through the module's imported seam ───────────────────────

def test_git_commit_messages_carry_no_metadata_frame(monkeypatch):
    """THE bug: the deleted collector concatenated '{"finish_reason": "stop"}' into
    commit messages. Driven through the primitive the git module now calls."""
    import vaf.core.completion as comp
    import vaf.cli.cmd.git as git_mod

    monkeypatch.setattr(
        comp, "_api_complete",
        lambda *a, **k: comp.collect_stream(["feat: add parser", '{"finish_reason": "stop"}']),
    )
    monkeypatch.setattr(comp, "_local_complete", lambda *a, **k: None)
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, key, default=None: "openai" if key == "provider" else default))

    msg = git_mod._generate_commit_message_llm("write a commit message")
    assert msg == "feat: add parser"
    assert "finish_reason" not in msg


def test_debug_and_generate_keep_their_error_string_shape(monkeypatch):
    import vaf.cli.cmd.debug as debug_mod
    import vaf.cli.cmd.generate as gen_mod
    import vaf.core.completion as comp

    monkeypatch.setattr(comp, "complete", lambda *a, **k: None)
    assert debug_mod._explain_llm("x").startswith("Error:")
    assert gen_mod._generate_llm("x").startswith("Error:")


# ── coder_templates classification ───────────────────────────────────────────────────

def test_template_detection_cleans_and_falls_back(monkeypatch):
    from vaf.tools.coder_templates import TemplateManager
    import vaf.core.completion as comp

    tm = TemplateManager.__new__(TemplateManager)

    monkeypatch.setattr(comp, "complete", lambda *a, **k: '"python_script"')
    result, _info = tm.detect_template_type_with_llm("build a scraper")
    assert result == "python_script"

    monkeypatch.setattr(comp, "complete", lambda *a, **k: None)
    result, info = tm.detect_template_type_with_llm("build a scraper")
    assert result is None
    assert "empty response" in info


# ── attachment_rag: the dead timeout parameter now binds ─────────────────────────────

def test_attachment_summary_forwards_its_timeout(monkeypatch):
    import vaf.core.completion as comp
    from vaf.memory.attachment_rag import _summarize_section_llm

    seen = {}

    def fake_complete(messages, **kwargs):
        seen.update(kwargs)
        return "a fine summary"

    monkeypatch.setattr(comp, "complete", fake_complete)
    out = _summarize_section_llm("body text", "Title", timeout_sec=7)
    assert out == "a fine summary"
    assert seen["timeout"] == 7, (
        "timeout_sec was declared and never used for weeks - it must reach the call"
    )

    monkeypatch.setattr(comp, "complete", lambda *a, **k: None)
    assert _summarize_section_llm("body text " * 50, "Title").startswith("body text")


# ── rag: answer error prefix + the stream regression ─────────────────────────────────

def test_rag_answer_keeps_its_error_prefix(monkeypatch):
    import asyncio

    import vaf.core.completion as comp
    from vaf.memory.rag import RagPipeline

    monkeypatch.setattr(comp, "complete", lambda *a, **k: None)
    pipeline = RagPipeline.__new__(RagPipeline)
    out = asyncio.run(RagPipeline._generate_answer(pipeline, "question"))
    assert out.startswith("Error generating answer:")


def test_rag_stream_yields_text_not_an_attribute_error(monkeypatch):
    """chat_completion_stream yields STRINGS; the old code called .get() on them and
    crashed on the first chunk, so every streamed answer was an error string."""
    import asyncio

    from vaf.memory.rag import RagPipeline

    class _FakeBackend:
        def __init__(self, provider, **kw):
            pass

        def chat_completion_stream(self, messages, temperature, max_tokens):
            yield "Hel"
            yield '{"finish_reason": "stop"}'
            yield "lo"

    import vaf.core.api_backend as ab
    monkeypatch.setattr(ab, "APIBackendManager", _FakeBackend)

    pipeline = RagPipeline.__new__(RagPipeline)

    async def collect():
        parts = []
        async for token in RagPipeline._stream_answer(pipeline, "q"):
            parts.append(token)
        return parts

    parts = asyncio.run(collect())
    assert "".join(parts) == "Hello"
    assert not any(p.startswith("Error:") for p in parts)


def test_rag_stream_surfaces_a_backend_error_once(monkeypatch):
    import asyncio

    from vaf.memory.rag import RagPipeline

    class _ErrBackend:
        def __init__(self, provider, **kw):
            pass

        def chat_completion_stream(self, messages, temperature, max_tokens):
            yield "[API Error from openai: boom]"
            yield "should never follow"

    import vaf.core.api_backend as ab
    monkeypatch.setattr(ab, "APIBackendManager", _ErrBackend)
    pipeline = RagPipeline.__new__(RagPipeline)

    async def collect():
        return [t async for t in RagPipeline._stream_answer(pipeline, "q")]

    parts = asyncio.run(collect())
    assert parts == ["Error: model backend unavailable"]
