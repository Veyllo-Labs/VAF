# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Research agent now saves reports as .docx (like document_agent): the HTML sections are
converted to clean Markdown, built into a DocumentModel and rendered to docx. Pins the
HTML->Markdown conversion and the end-to-end markdown->docx path.
"""
from vaf.tools.research_agent import ResearchAgentTool
from vaf.core.document_formatting import infer_document_model, save_document_model_as_docx


def test_section_html_to_markdown_blocks_and_citations():
    html = (
        '<h2>Marktüberblick</h2>'
        '<p>Der Markt wächst <strong>stark</strong> [1].</p>'
        '<h3>Details</h3>'
        '<ul><li>Punkt A</li><li>Punkt B [2]</li></ul>'
        '<p>Quelle <span class="cite">[3]</span> bestätigt das.</p>'
    )
    md = ResearchAgentTool._section_html_to_markdown(html)
    assert '## Marktüberblick' in md
    assert '### Details' in md
    assert '**stark** [1]' in md
    assert '- Punkt A' in md and '- Punkt B [2]' in md
    assert '[3]' in md                     # cite span -> kept [n]
    assert '<' not in md and '>' not in md  # no HTML tags survive


def test_section_html_to_markdown_unescapes_entities():
    md = ResearchAgentTool._section_html_to_markdown('<p>Tom &amp; Jerry &lt;3</p>')
    assert 'Tom & Jerry <3' in md


def test_section_html_to_markdown_empty():
    assert ResearchAgentTool._section_html_to_markdown('') == ''


def test_end_to_end_html_sections_to_docx(tmp_path):
    sections_html = [
        '<h2>Einleitung</h2><p>Überblick über das Thema [1].</p>',
        '<h2>Analyse</h2><p>Kernpunkte.</p><ul><li>Erstens</li><li>Zweitens</li></ul>',
    ]
    md_sections = [ResearchAgentTool._section_html_to_markdown(s) for s in sections_html]
    bare = ResearchAgentTool.__new__(ResearchAgentTool)   # no heavy __init__
    md = bare._assemble_markdown("Test Topic", md_sections, ["https://a.example", "https://b.example"])
    model = infer_document_model("Research Report: Test Topic", "report", md)
    titles = [s.title for s in model.sections]
    assert "Einleitung" in titles and "Analyse" in titles and "Sources" in titles

    out = tmp_path / "research_test.docx"
    save_document_model_as_docx(model, out)
    assert out.exists() and out.stat().st_size > 0
