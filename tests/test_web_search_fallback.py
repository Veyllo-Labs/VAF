# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The internal-knowledge (RAG) fallback of web_search must stay honest.

Live incident 2026-07-15 18:25: a weather query with trusted_sources_only hit
a 12-domain site: filter with zero web hits; the RAG fallback made `results`
non-empty, which skipped the retry-without-filter, and the hard-coded banner
told the model "the web is unreachable" on a healthy network - which the model
dutifully repeated to the user.
"""
import types

import vaf.tools.search as search
from vaf.tools.search import WebSearchTool
from vaf.core.config import Config

RAG = ([{"title": "Memory note", "href": "memory://1", "body": "old note",
         "internal_knowledge": True}], "Internal Knowledge (RAG)", "memory")
WEB = ([{"title": "Weather tomorrow", "href": "https://example.org/w",
         "body": "Sunny, 25 C"}], "DuckDuckGo", None)
EMPTY = ([], "DuckDuckGo", "Google: keine Treffer.")


def _intent(monkeypatch):
    """Force a site: filter so query_with_filter != query (no net/LLM)."""
    import vaf.core.query_analyzer as qa
    monkeypatch.setattr(qa, "analyze_query", lambda q: types.SimpleNamespace(
        suggested_sources=["acm.org", "bbc.com"], confidence=0.9,
        intent_type="news"))


def test_rag_fallback_does_not_defeat_the_unfiltered_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    _intent(monkeypatch)
    seen = []

    def scripted(q, n):
        seen.append(q)
        # Filtered pass: zero web hits, RAG fallback kicks in.
        # Plain pass: the real web answers.
        return RAG if "site:" in q else WEB

    monkeypatch.setattr(search, "get_web_search_results", scripted)
    out = WebSearchTool().run(query="wetter morgen retrytest", deep=False)

    assert len(seen) == 2 and "site:" in seen[0] and "site:" not in seen[1]
    assert "Web Search Results" in out
    assert "INTERNAL KNOWLEDGE" not in out


def test_rag_results_kept_when_plain_query_also_finds_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    _intent(monkeypatch)

    def scripted(q, n):
        return RAG if "site:" in q else EMPTY

    monkeypatch.setattr(search, "get_web_search_results", scripted)
    out = WebSearchTool().run(query="wetter morgen keeptest", deep=False)
    assert "INTERNAL KNOWLEDGE" in out


def test_rag_banner_says_no_results_without_provider_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    _intent(monkeypatch)
    monkeypatch.setattr(search, "get_web_search_results", lambda q, n: RAG)

    out = WebSearchTool().run(query="wetter morgen honesttest", deep=False)
    assert "NO results" in out
    assert "unreachable" not in out


def test_rag_banner_claims_outage_only_on_recorded_provider_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    _intent(monkeypatch)

    def scripted(q, n):
        # A genuine outage records provider errors (run() resets them first).
        search._note_provider_error("DuckDuckGo failed after 3 attempts (HTTP timeout)")
        return RAG

    monkeypatch.setattr(search, "get_web_search_results", scripted)
    out = WebSearchTool().run(query="wetter morgen outagetest", deep=False)
    assert "unreachable" in out
    assert "DuckDuckGo failed" in out
