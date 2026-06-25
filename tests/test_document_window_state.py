# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Document-agent live-window state: section HTML rendering, placeholder resolution,
and the document_state emit payload that drives the SubAgent document view.
"""
import pytest

from vaf.core.document_formatting import DocumentSection, DocumentBlock, render_section_html
from vaf.tools.document_agent import DocumentAgentTool


def _bare() -> DocumentAgentTool:
    return DocumentAgentTool.__new__(DocumentAgentTool)


def test_render_section_html_blocks_and_keeps_placeholders():
    sec = DocumentSection(title="Vertragsparteien", heading_level=2, blocks=[
        DocumentBlock(type="paragraph", text="Zwischen {{VERKÄUFER_NAME}} und {{KÄUFER_NAME}}."),
        DocumentBlock(type="bullet_list", items=["Punkt A", "Punkt B"]),
        DocumentBlock(type="numbered_list", items=["Erstens", "Zweitens"]),
    ])
    html = render_section_html(sec)
    assert "<h2>Vertragsparteien</h2>" in html
    assert "<p>Zwischen {{VERKÄUFER_NAME}} und {{KÄUFER_NAME}}.</p>" in html   # placeholders verbatim
    assert "<ul><li>Punkt A</li><li>Punkt B</li></ul>" in html
    assert "<ol><li>Erstens</li><li>Zweitens</li></ol>" in html


def test_render_section_html_escapes_angle_brackets():
    sec = DocumentSection(title="T", blocks=[DocumentBlock(type="paragraph", text="a < b > c & d")])
    html = render_section_html(sec)
    assert "&lt; b &gt;" in html and "&amp; d" in html


def test_section_word_count_counts_body():
    sec = DocumentSection(title="X", blocks=[DocumentBlock(type="paragraph", text="one two three four five")])
    assert DocumentAgentTool._section_word_count(sec) >= 5


def test_resolve_placeholders_memory_chat_open(monkeypatch):
    t = _bare()
    t._doc_state = {"sectionsHtml": [
        "<p>{{VERKÄUFER_NAME}} verkauft an {{KÄUFER_NAME}}.</p>",
        "<p>E-Mail {{VERKÄUFER_EMAIL}}, Marke {{MARKE}}, FIN {{FIN}}.</p>",
    ]}
    # memory provides the user's own identity; counterparty fields must NOT be filled from it
    monkeypatch.setattr(t, "_memory_identity", lambda: {"NAME": "Mert Can", "EMAIL": "m@example.de"})
    out = t._resolve_placeholders("Marke: VW\nModell: Golf VII")
    by = {p["name"]: p for p in out}

    assert by["VERKÄUFER_NAME"]["value"] == "Mert Can" and by["VERKÄUFER_NAME"]["source"] == "memory"
    assert by["VERKÄUFER_EMAIL"]["value"] == "m@example.de" and by["VERKÄUFER_EMAIL"]["source"] == "memory"
    assert by["KÄUFER_NAME"]["source"] == "open"          # counterparty -> stays open
    assert by["MARKE"]["value"] == "VW" and by["MARKE"]["source"] == "chat"
    assert by["FIN"]["source"] == "open"                  # unknown -> open
    # document order preserved, no duplicates
    assert [p["name"] for p in out] == ["VERKÄUFER_NAME", "KÄUFER_NAME", "VERKÄUFER_EMAIL", "MARKE", "FIN"]


def test_resolve_placeholders_empty_when_none():
    t = _bare()
    t._doc_state = {"sectionsHtml": ["<p>Kein Platzhalter hier.</p>"]}
    assert t._resolve_placeholders("task") == []


def test_emit_document_state_payload_shape(monkeypatch):
    from vaf.core.web_interface import get_web_interface
    wi = get_web_interface()
    captured = {}
    monkeypatch.setattr(wi, "_push_session_update", lambda sid, payload: captured.update({"sid": sid, "p": payload}))
    wi.emit_document_state({"title": "Kaufvertrag", "format": "docx", "sections": []}, session_id="s1")
    assert captured["sid"] == "s1"
    assert captured["p"]["type"] == "document_state"
    assert captured["p"]["title"] == "Kaufvertrag"
    assert captured["p"]["format"] == "docx"
