# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Tests for the internal-knowledge (RAG) fallback of the web search chain.

When every web provider fails (rate limit, no API keys, network down) or the
topic is internal, the search falls back to VAF's long-term memory and labels
the hits honestly as memory snippets (memory:// hrefs), never as web sources.
"""
import pytest

import vaf.tools.search as search_mod
from vaf.tools.search import _search_internal_knowledge


RAG_RAW = (
    "[Source 1] (Relevance: 91%)\n"
    "VAF ist Mert's Agenten-Framework mit eigenem IPC und Sub-Agents.\n"
    "Es laeuft lokal mit Qwen-Modellen."
    "\n\n---\n\n"
    "[Source 2] (Relevance: 74%)\n"
    "Veyllo Labs sitzt in Berlin und entwickelt VAF."
)


def test_internal_knowledge_shapes_results_like_web_hits(monkeypatch):
    import vaf.memory.rag as rag_mod
    monkeypatch.setattr(rag_mod, "run_memory_search_sync", lambda **kw: RAG_RAW)

    results = _search_internal_knowledge("VAF Framework", max_results=5)
    assert len(results) == 2
    first = results[0]
    assert first["href"] == "memory://internal/1"
    assert first["source"] == "internal_knowledge"
    assert "Internes Wissen" in first["title"]
    assert "91% relevant" in first["title"]
    assert "Agenten-Framework" in first["body"]


def test_internal_knowledge_empty_memory_returns_nothing(monkeypatch):
    import vaf.memory.rag as rag_mod
    monkeypatch.setattr(rag_mod, "run_memory_search_sync", lambda **kw: "")
    assert _search_internal_knowledge("anything", max_results=5) == []


def test_internal_results_get_unmistakable_header(monkeypatch):
    # A user once read the fallback as a working web search — the header must
    # scream "NOT WEB RESULTS" and instruct the agent to say so.
    monkeypatch.setattr(
        search_mod, "get_web_search_results",
        lambda q, m: (
            [{"title": "Internes Wissen: VAF Framework", "href": "memory://internal/1", "body": "snippet"}],
            "Internal Knowledge (RAG)",
            "hint",
        ),
    )
    out = search_mod.WebSearchTool().run(query="VAF", max_results=2, open_in_browser=False)
    assert out.startswith("### INTERNAL KNOWLEDGE — NOT WEB RESULTS")
    assert "long-term memory" in out
    assert "Tell the user explicitly" in out


def test_internal_knowledge_error_is_collected_not_raised(monkeypatch):
    import vaf.memory.rag as rag_mod

    def _boom(**kw):
        raise RuntimeError("rag db down")
    monkeypatch.setattr(rag_mod, "run_memory_search_sync", _boom)
    search_mod.reset_search_provider_errors()

    assert _search_internal_knowledge("anything", max_results=5) == []
    errors = search_mod.get_search_provider_errors()
    assert any("internal knowledge" in e for e in errors)
