"""Tests for the research agent title generator.

Small reasoning models leaked their chain of thought ("Thinking Process: ...")
or echoed the few-shot example ("SpaceX Rocket Launch Overview") as the report
title. The generator now skips the LLM for clean short topics and rejects
leaked/echoed answers.
"""
import json
import pytest

from vaf.tools.research_agent import ResearchAgentTool


@pytest.fixture
def tool():
    return ResearchAgentTool()


def test_short_clean_topic_skips_llm(tool, monkeypatch):
    def _boom(**kwargs):
        raise AssertionError("query_llm must not be called for clean short topics")
    monkeypatch.setattr(tool, "query_llm", _boom)
    assert tool._generate_title("Geschichte des Brandenburger Tors") == "Geschichte des Brandenburger Tors"


def test_instruction_topic_uses_llm(tool, monkeypatch):
    monkeypatch.setattr(tool, "query_llm", lambda **kw: "Brandenburg Gate: History and Significance")
    title = tool._generate_title("Bitte recherchiere die Geschichte des Brandenburger Tors ausführlich")
    assert title == "Brandenburg Gate: History and Significance"


def test_reasoning_leak_is_rejected(tool, monkeypatch):
    monkeypatch.setattr(
        tool, "query_llm",
        lambda **kw: "Thinking Process: The user wants a title about the gate...",
    )
    title = tool._generate_title("Erstelle mir bitte einen Bericht über das Brandenburger Tor in Berlin")
    # Falls back to the raw topic instead of the leaked reasoning
    assert "thinking process" not in title.lower()


def test_fewshot_echo_is_rejected(tool, monkeypatch):
    monkeypatch.setattr(tool, "query_llm", lambda **kw: "SpaceX Rocket Launch Overview")
    title = tool._generate_title("Erstelle mir bitte einen Bericht über das Brandenburger Tor in Berlin")
    assert "spacex" not in title.lower()


def test_title_after_reasoning_marker_is_extracted(tool, monkeypatch):
    monkeypatch.setattr(
        tool, "query_llm",
        lambda **kw: "<think>The user asks about the gate, a good title would be...</think>\nTitle: Das Brandenburger Tor im Wandel der Zeit",
    )
    title = tool._generate_title("Bitte recherchiere alles über das Brandenburger Tor und seine Geschichte")
    assert title == "Das Brandenburger Tor im Wandel der Zeit"


# ─────────────────────────────────────────────────────────────────────────────
# Query generation (a Veyllo-strategy run once searched with the full
# 100-char topic sentence as the ONLY query and found 0 sources)
# ─────────────────────────────────────────────────────────────────────────────

LONG_TOPIC = "Strategie für Veyllo GmbH (Veyllo Labs) und VAF Framework - Marktanalyse, Positionierung, Vermarktung, Monetarisierung"


def test_compress_query_shortens_prompt_style_topics(tool):
    short = tool._compress_query(LONG_TOPIC)
    assert len(short.split()) <= 7
    assert "(" not in short and "-" not in short
    assert "Veyllo" in short


def test_augment_queries_fallback_returns_multiple_short_queries(tool, monkeypatch):
    # LLM fails completely -> still multiple searchable keyword queries
    monkeypatch.setattr(tool, "query_llm", lambda **kw: None)
    queries = tool._augment_queries(LONG_TOPIC, LONG_TOPIC, "de")
    assert len(queries) >= 2
    assert all(len(q) <= 90 for q in queries)
    assert queries[0] == tool._compress_query(LONG_TOPIC)


def test_augment_queries_parses_last_json_after_reasoning(tool, monkeypatch):
    monkeypatch.setattr(
        tool, "query_llm",
        lambda **kw: (
            "<think>Let me think of angles... maybe [not, json, here</think>\n"
            '["AI agent framework market analysis", "KI Framework Monetarisierung", "agent framework positioning"]'
        ),
    )
    queries = tool._augment_queries(LONG_TOPIC, LONG_TOPIC, "de")
    assert "AI agent framework market analysis" in queries
    assert len(queries) == 4  # short base + 3 variants


def test_augment_queries_rejects_overlong_variants(tool, monkeypatch):
    monkeypatch.setattr(
        tool, "query_llm",
        lambda **kw: json.dumps([LONG_TOPIC + " even longer variant that no search engine likes", "short query"]),
    )
    queries = tool._augment_queries(LONG_TOPIC, LONG_TOPIC, "de")
    assert "short query" in queries
    assert all(len(q) <= 90 for q in queries)


def test_augment_queries_rejects_placeholder_echoes(tool, monkeypatch):
    # A live run once produced ["Query 1", "Query 2", "Query 3"] — the model
    # echoed the example format. Those must never reach the search engine.
    monkeypatch.setattr(tool, "query_llm", lambda **kw: '["Query 1", "query2", "real framework comparison"]')
    queries = tool._augment_queries(LONG_TOPIC, LONG_TOPIC, "de")
    assert "real framework comparison" in queries
    assert not any(q.lower().startswith("query") for q in queries if q != queries[0])


def test_sanitize_section_rejects_pure_reasoning(tool):
    leaked = (
        "Thinking Process: 1. **Analyze the Request:** * **Task:** Write ONE section "
        "of an HTML research report. * **Topic:** Veyllo GmbH VAF Framework..."
    )
    assert tool._sanitize_section_output(leaked, "Overview") == ""


def test_sanitize_section_cuts_reasoning_before_html(tool):
    raw = (
        "Okay, the user wants an overview section. Let me draft it...\n"
        "<h2>Overview</h2><p>VAF is an agent framework developed by Veyllo Labs.</p>"
    )
    out = tool._sanitize_section_output(raw, "Overview")
    assert out.startswith("<h2>")
    assert "Okay, the user" not in out
    assert "agent framework" in out


def test_sanitize_section_ignores_backtick_quoted_tags(tool):
    # A live run leaked reasoning that QUOTED the prompt's structure examples —
    # the sanitizer must not treat `<h2>Section title</h2>` inside backticks as
    # the start of the real answer.
    leaked = (
        "1. **Analyze the Request:** Structure: `<h2>Section title</h2>`, 3-6 paragraphs, "
        "`<ul>` with 6-10 key bullets, Sources paragraph at the end. No real answer follows."
    )
    assert tool._sanitize_section_output(leaked, "Overview") == ""


def test_sanitize_section_strips_markdown_fences(tool):
    out = tool._sanitize_section_output("```html\n<h2>Overview</h2><p>Real content.</p>\n```", "Overview")
    assert out.startswith("<h2>Overview</h2>")
    assert "```" not in out


def test_sanitize_section_finds_real_html_after_quoted_tags(tool):
    raw = (
        "Plan: use `<h2>` for the heading.\n"
        "<h2>Overview</h2><p>VAF is an agent framework by Veyllo Labs.</p>"
    )
    out = tool._sanitize_section_output(raw, "Overview")
    assert out.startswith("<h2>Overview</h2>")
    assert "Plan: use" not in out


def test_sanitize_section_adds_heading_and_wraps_prose(tool):
    out = tool._sanitize_section_output("VAF is an agent framework with sub-agents.", "Overview")
    assert out.startswith("<h2>Overview</h2>")
    assert "<p>VAF is an agent framework" in out

    out2 = tool._sanitize_section_output("<p>Body without heading.</p>", "Overview")
    assert out2.startswith("<h2>Overview</h2>")


def test_stream_section_collects_content_and_ignores_reasoning(tool, monkeypatch):
    # The section writer streams from the local server with an idle timeout:
    # reasoning deltas keep the connection alive but only content is returned.
    # Lines arrive as raw BYTES (llama-server declares no charset, so requests
    # must not be allowed to decode them as ISO-8859-1): umlauts must survive.
    lines = [
        b'data: {"choices":[{"delta":{"reasoning_content":"let me think about the structure..."}}]}',
        'data: {"choices":[{"delta":{"content":"<h2>Overview</h2>"}}]}',
        'data: {"choices":[{"delta":{"content":"<p>VAF ist ein Framework für Größe und Türöffner.</p>"}}]}'.encode("utf-8"),
        b'data: [DONE]',
    ]

    class FakeResp:
        def raise_for_status(self): pass
        def iter_lines(self, decode_unicode=False): return iter(lines)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import vaf.tools.research_agent as ra
    monkeypatch.setattr(ra.requests, "post", lambda *a, **kw: FakeResp())

    progress_snapshots = []
    out = tool._stream_section_completion(
        messages=[{"role": "user", "content": "x"}],
        max_tokens=100, temperature=0.2,
        on_progress=progress_snapshots.append,
    )
    assert out == "<h2>Overview</h2><p>VAF ist ein Framework für Größe und Türöffner.</p>"
    assert "Ã" not in out  # no latin-1 mojibake
    assert "let me think" not in out
    assert progress_snapshots  # live progress was reported


def test_stream_section_returns_partial_on_connection_error(tool, monkeypatch):
    import vaf.tools.research_agent as ra

    def _boom(*a, **kw):
        raise ConnectionError("server down")
    monkeypatch.setattr(ra.requests, "post", _boom)
    out = tool._stream_section_completion(
        messages=[{"role": "user", "content": "x"}], max_tokens=100, temperature=0.2,
    )
    assert out == ""


def test_sanitize_strips_per_section_source_blocks(tool):
    # A live report ended sections with messy source blocks in changing formats:
    # "Die Quellen ... sind:" + raw URLs, or <p><strong>Sources:</strong></p> + <ul> of URLs.
    raw = (
        "<h2>Impact</h2><p>Echter Inhalt mit Aussage [2].</p>"
        "<p>Die Quellen für die Informationen in diesem Bericht sind wie folgt:</p>"
        "<p><strong>Sources:</strong></p>"
        "<ul><li>https://example.org/a</li><li>https://example.org/b</li></ul>"
    )
    out = tool._sanitize_section_output(raw, "Impact")
    assert "Echter Inhalt" in out
    assert "Quellen für die Informationen" not in out
    assert "Sources:" not in out
    assert "https://example.org/a" not in out


def test_sanitize_keeps_content_lists(tool):
    raw = "<h2>Impact</h2><p>Inhalt.</p><ul><li>Kernaussage eins</li><li>Kernaussage zwei</li></ul>"
    out = tool._sanitize_section_output(raw, "Impact")
    assert "Kernaussage eins" in out and "<ul>" in out


def test_sanitize_wraps_loose_text_after_heading(tool):
    # Models emit "<h2>..</h2>\n\nPlain text" — the loose text must become a <p>
    # so it renders with paragraph typography instead of the viewer default.
    raw = "<h2>Overview</h2>\n\nDie strategische Einordnung des VAF erfolgt im EU-Kontext."
    out = tool._sanitize_section_output(raw, "Overview")
    assert "<p>Die strategische Einordnung" in out


def test_sanitize_clamps_unclosed_heading(tool):
    # An unclosed <h2> swallowed whole sections (everything rendered big and bold).
    raw = "<h2>Overview Die strategische Einordnung des VAF erfolgt im Kontext der EU und vieler weiterer Programme ohne schliessendes Tag"
    out = tool._sanitize_section_output(raw, "Overview")
    assert out.startswith("<h2>Overview</h2>")
    assert "</h2><p>" in out.replace("\n", "") or "<p>" in out
    assert "strategische Einordnung" in out


def test_numbered_source_list_in_markdown(tool):
    md = tool._assemble_markdown("Topic", ["<h2>S</h2><p>Text [2].</p>"], ["https://a.de", "https://b.de"])
    assert "1. https://a.de" in md
    assert "2. https://b.de" in md


def test_search_provider_error_collector():
    from vaf.tools.search import (
        _note_provider_error,
        get_search_provider_errors,
        reset_search_provider_errors,
    )
    reset_search_provider_errors()
    assert get_search_provider_errors() == []
    _note_provider_error("DuckDuckGo: Max retries exceeded")
    assert get_search_provider_errors() == ["DuckDuckGo: Max retries exceeded"]
    reset_search_provider_errors()
    assert get_search_provider_errors() == []



