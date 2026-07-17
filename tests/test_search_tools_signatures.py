# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""search_tools parameter signatures (tool-friction audit, Fix 4).

search_tools returned only name + one description line, so a freshly discovered
tool had to be called with GUESSED parameters (observed live: document_writer
called with an invented 'document_content' arg -> schema error). The top matches
now carry a compact call signature. The output format and the execute_tool
post-hook parser share one function (extract_discovered_tool_names) and this
file round-trips them, so they can never drift apart silently.
"""
from vaf.tools.base import BaseTool, format_tool_signature
from vaf.tools.search_tools import SearchToolsTool, extract_discovered_tool_names


class _FakeTool(BaseTool):
    def run(self, **kwargs):  # pragma: no cover - never called
        return ""


def _mk(name, desc, params=None):
    t = _FakeTool.__new__(_FakeTool)
    t.name = name
    t.description = desc
    t.parameters = params or {"type": "object", "properties": {}}
    return t


DOC_WRITER = _mk(
    "document_writer",
    "Creates simple structured documents (contracts, letters, messages, templates).",
    {
        "type": "object",
        "properties": {
            "document_type": {"type": "string"},
            "content": {"type": "string"},
            "filename": {"type": "string"},
            "format": {"type": "string"},
        },
        "required": ["document_type", "content", "filename"],
    },
)
READ_FILE = _mk(
    "read_file",
    "Reads the content of a file. Supports text, PDF, Word (.docx), Excel (.xlsx).",
    {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)
NO_ARGS = _mk("list_workflows", "List available workflows.")


def _search(query, tools):
    st = SearchToolsTool()
    st.available_tools = {t.name: t for t in tools}
    return st.run(query=query), st.available_tools


# ── Renderer ─────────────────────────────────────────────────────────────────

def test_signature_required_first_optionals_bracketed():
    sig = format_tool_signature(DOC_WRITER)
    assert sig == "document_writer(document_type: string, content: string, filename: string, [format: string])"


def test_signature_empty_for_no_properties():
    assert format_tool_signature(NO_ARGS) == ""


def test_signature_caps_length():
    big = _mk("big", "d", {
        "type": "object",
        "properties": {f"param_{i}": {"type": "string"} for i in range(40)},
        "required": [],
    })
    sig = format_tool_signature(big, max_chars=100)
    assert len(sig) <= 100 and sig.endswith("...)")


def test_signature_never_raises_on_garbage():
    broken = _mk("broken", "d")
    broken.parameters = {"properties": "not-a-dict"}
    assert format_tool_signature(broken) == ""


# ── Output format ────────────────────────────────────────────────────────────

def test_top_matches_carry_signature_lines():
    out, _ = _search("document", [DOC_WRITER, READ_FILE])
    assert "document_writer: Creates simple structured documents" in out
    assert "document_writer(document_type: string, content: string, filename: string, [format: string])" in out


def test_query_echo_is_capped():
    out, _ = _search("document " * 40, [DOC_WRITER])
    header = out.splitlines()[0]
    assert len(header) < 120


def test_output_stays_under_execute_tool_truncation():
    tools = [
        _mk(f"tool_with_long_name_{i}", "A rather long description " * 6, {
            "type": "object",
            "properties": {f"p{j}": {"type": "string"} for j in range(12)},
            "required": [f"p{j}" for j in range(6)],
        })
        for i in range(10)
    ]
    # every tool matches the query token via its description
    for t in tools:
        t.description = "searchable " + t.description
    out, _ = _search("searchable", tools)
    assert len(out) <= 2000, f"output {len(out)} chars would be truncated mid-line"


def test_no_match_fallback_stays_signature_free():
    out, _ = _search("zzz_nothing_matches", [DOC_WRITER, READ_FILE])
    assert "No close matches" in out
    assert "(" not in out.split(":", 1)[1] or "document_writer(" not in out


# ── Round-trip guard: format <-> post-hook parser ────────────────────────────

def test_parser_roundtrip_discovers_match_names_only():
    out, registry = _search("document file", [DOC_WRITER, READ_FILE, NO_ARGS])
    discovered = extract_discovered_tool_names(out, registry)
    # every discovered name is real, and the signature lines added no phantoms
    assert set(discovered) <= set(registry)
    assert "document_writer" in discovered
    for cand in discovered:
        assert "(" not in cand


def test_parser_ignores_signature_lines():
    line_block = (
        "Tools matching 'x':\n"
        "  document_writer: Creates documents.\n"
        "      document_writer(document_type: string, content: string, filename: string)\n"
    )
    got = extract_discovered_tool_names(line_block, {"document_writer": object()})
    assert got == ["document_writer"], got


def test_parser_matches_historic_semantics_no_dedup():
    block = "a: x\na: y\n"
    assert extract_discovered_tool_names(block, {"a": 1}) == ["a", "a"]
