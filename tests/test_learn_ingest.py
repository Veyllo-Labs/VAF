# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Tests for the shared section-based contextual ingestion (vaf/tools/learn_document.py).

Covers the pure helpers (JSON parse + fallback) and the orchestration of `ingest_document_knowledge`
with a mocked RagPipeline + fake DB session, so no real DB / embedding model is needed.
"""
import asyncio
import json

from vaf.tools import learn_document as ld


def test_strip_json_fences():
    assert ld._strip_json_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert ld._strip_json_fences('{"a":1}') == '{"a":1}'


def test_contextualize_returns_plaintext_summary():
    out = ld._contextualize_section_llm(
        "some section text", "Title", "Doc",
        lambda p: "This section explains the topic and its key facts.",
    )
    assert out == "This section explains the topic and its key facts."


def test_contextualize_fallback_is_clean_label_never_raw_text():
    # no generate_fn -> clean section label (NOT the raw section text)
    assert ld._contextualize_section_llm("raw section text", "Intro", "Doc", None) == "Intro — from Doc."
    # too-short model output -> fallback, not the noise
    assert ld._contextualize_section_llm("text", "Intro", "Doc", lambda p: "ok") == "Intro — from Doc."


def test_clean_title_strips_extensions():
    assert ld._clean_title("Report.pptx-compressed.pdf") == "Report"
    assert ld._clean_title("notes.md") == "notes"
    assert ld._clean_title("Plain Title") == "Plain Title"


def test_strip_think_removes_reasoning():
    # reasoning model: keep only the answer after </think>
    assert ld._strip_think("<think>we should summarize</think>\n\nThe summary.") == "The summary."
    # truncated mid-reasoning (no close) -> empty, so the caller falls back to a clean label
    assert ld._strip_think("<think>reasoning that never closed") == ""
    assert ld._strip_think("No think here.") == "No think here."


def test_contextualize_strips_think_from_summary():
    out = ld._contextualize_section_llm(
        "section text", "Heading", "Doc",
        lambda p: "<think>Let me think about this section...</think>\nThis section explains X and Y.",
    )
    assert out == "This section explains X and Y."


class _FakePipeline:
    records = []

    def __init__(self, db):
        pass

    async def ingest(self, content, metadata=None, auto_connect=True, user_scope_id=None):
        _FakePipeline.records.append({"content": content, "meta": metadata})
        return object()


class _FakeResult:
    def scalar_one_or_none(self):
        return None


class _FakeDB:
    async def execute(self, q):
        return _FakeResult()


def _stub_generate(prompt):
    if '"doc_summary"' in prompt:  # doc-level call -> JSON
        return json.dumps({"doc_summary": "Overall overview.", "doc_tags": ["topic1", "topic2"]})
    return "This section covers the topic with key facts."  # per-section call -> plain text


def test_ingest_document_knowledge_orchestration(monkeypatch):
    import vaf.memory.rag as ragmod
    _FakePipeline.records = []
    monkeypatch.setattr(ragmod, "RagPipeline", _FakePipeline)

    md = (
        "## Introduction\n" + ("intro text " * 80) + "\n\n"
        "## Methods\n" + ("methods text " * 80) + "\n\n"
        "## Results\n" + ("results text " * 80) + "\n"
    )
    res = asyncio.run(ld.ingest_document_knowledge(
        _FakeDB(), content_markdown=md, doc_title="MyDoc", doc_tag="doc-mydoc",
        source="test", mem_type="knowledge", generate_fn=_stub_generate,
        user_scope_id=None, extra_tags=["extra"],
    ))

    recs = _FakePipeline.records
    section_recs = [r for r in recs if r["meta"].get("type") == "knowledge"]
    index_recs = [r for r in recs if r["meta"].get("type") == "document_index"]

    assert res["created"] == 3
    assert len(section_recs) == 3
    assert len(index_recs) == 1  # exactly ONE document_index root, not one-per-section

    for r in section_recs:
        m = r["meta"]
        # title == the plain-text LLM context (drives the Memory embedding in RagPipeline.ingest)
        assert m["title"] == "This section covers the topic with key facts."
        assert m["title"] in r["content"]          # context is prepended to the body
        assert "section_index" in m
        # doc-level tags land on every section; no fragile per-section JSON tags
        for t in ("doc-mydoc", "knowledge", "extra", "topic1", "topic2"):
            assert t in m["tags"]
        # never the old raw-text / doc_summary noise
        assert "[Overall overview.]" not in r["content"]

    # doc_summary lives only on the index root
    assert index_recs[0]["meta"].get("doc_summary") == "Overall overview."
    assert res["doc_summary"] == "Overall overview."
