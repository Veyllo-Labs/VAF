# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
from pathlib import Path

from docx import Document

from vaf.core.document_formatting import (
    build_document_model,
    coerce_section,
    infer_document_model,
    render_markdown,
    save_document_model_as_docx,
)
from vaf.tools.document_writer import DocumentWriterTool


def test_coerce_section_normalizes_titles_levels_and_inline_formatting():
    section = coerce_section(
        {
            "title": "**Scope**",
            "heading_level": 1,
            "blocks": [
                {"type": "paragraph", "text": "**Intro** text."},
                {"type": "bullet_list", "items": ["**One**", "Two"]},
            ],
        },
        fallback_title="Fallback",
    )

    assert section.title == "Scope"
    assert section.heading_level == 2
    assert section.blocks[0].type == "paragraph"
    assert section.blocks[0].text == "Intro text."
    assert section.blocks[1].type == "bullet_list"
    assert section.blocks[1].items == ["One", "Two"]


def test_infer_document_model_parses_markdown_hierarchy_and_lists():
    model = infer_document_model(
        title="Ignored",
        document_type="report",
        content=(
            "# Quarterly Report\n\n"
            "## Summary\n\n"
            "First paragraph.\n\n"
            "- Item one\n"
            "- Item two\n\n"
            "### Risks\n\n"
            "1. Risk a\n"
            "2. Risk b\n"
        ),
    )

    assert model.title == "Quarterly Report"
    assert [section.title for section in model.sections] == ["Summary", "Risks"]
    assert model.sections[0].blocks[0].text == "First paragraph."
    assert model.sections[0].blocks[1].type == "bullet_list"
    assert model.sections[0].blocks[1].items == ["Item one", "Item two"]
    assert model.sections[1].heading_level == 3
    assert model.sections[1].blocks[0].type == "numbered_list"
    assert model.sections[1].blocks[0].items == ["Risk a", "Risk b"]


def test_render_markdown_uses_canonical_heading_levels():
    model = build_document_model(
        title="Project Plan",
        document_type="report",
        sections=[
            {
                "title": "Overview",
                "heading_level": 2,
                "blocks": [
                    {"type": "paragraph", "text": "Plan summary."},
                    {"type": "numbered_list", "items": ["Step A", "Step B"]},
                ],
            }
        ],
    )

    markdown = render_markdown(model)

    assert markdown.startswith("# Project Plan\n")
    assert "\n## Overview\n" in markdown
    assert "Plan summary." in markdown
    assert "1. Step A" in markdown
    assert "2. Step B" in markdown
    assert "**" not in markdown


def test_save_document_model_as_docx_uses_heading_and_list_styles(tmp_path: Path):
    model = build_document_model(
        title="Architecture Notes",
        document_type="manual",
        sections=[
            {
                "title": "Decisions",
                "heading_level": 2,
                "blocks": [
                    {"type": "paragraph", "text": "Keep rendering deterministic."},
                    {"type": "bullet_list", "items": ["One renderer", "No bold heuristics"]},
                ],
            }
        ],
    )

    file_path = tmp_path / "architecture_notes.docx"
    save_document_model_as_docx(model, file_path)

    saved = Document(str(file_path))
    paragraphs = [(paragraph.text, paragraph.style.name) for paragraph in saved.paragraphs]

    assert paragraphs[0] == ("Architecture Notes", "Title")
    assert paragraphs[1] == ("Decisions", "Heading 1")
    assert paragraphs[2] == ("Keep rendering deterministic.", "Normal")
    assert paragraphs[3] == ("One renderer", "List Bullet")
    assert paragraphs[4] == ("No bold heuristics", "List Bullet")


def test_document_writer_markdown_output_is_normalized(tmp_path: Path):
    tool = DocumentWriterTool()
    file_path = tmp_path / "normalized.md"

    result = tool._create_markdown_document(
        file_path=file_path,
        content=(
            "Quarterly Update\n"
            "================\n\n"
            "Key Points\n"
            "----------\n\n"
            "**Clean** output matters.\n\n"
            "- First\n"
            "- Second\n"
        ),
        doc_type="report",
    )

    saved = file_path.read_text(encoding="utf-8")

    assert "Markdown document saved successfully." in result
    assert saved.startswith("# Quarterly Update\n")
    assert "\n## Key Points\n" in saved
    assert "**" not in saved
    assert "- First" in saved
    assert "- Second" in saved
