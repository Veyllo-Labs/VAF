# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""document_writer extension allowlist (blue378604 audit, Fix 5).

The tool declared .txt/.md/.docx but silently accepted ANY extension and rendered
it as "text": a raw .svg happened to survive the pipeline, an .html spec came out
as an rst-like text file. The gate now rejects non-document extensions with an
actionable redirect (write_file for raw files, coding_agent for code projects),
derives a missing extension from the format param instead of blanket .txt, treats
the extension as authoritative for the output format (format="word" no longer
writes DOCX bytes into a .txt), coerces non-string filenames at the boundary, and
returns failures with the "Tool Error:" prefix so the agent loop and the workflow
engine actually score them as errors.
"""
import pytest

import vaf.core.session as session_mod
from vaf.tools.document_writer import DocumentWriterTool


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    target = tmp_path / "docs_out"
    target.mkdir()
    monkeypatch.setattr(session_mod, "resolve_agent_output_dir",
                        lambda default, session_id=None: target)
    return target


def test_svg_rejected_with_redirect(out_dir):
    out = DocumentWriterTool().run(document_type="diagram",
                                   content="<svg xmlns='x'/>", filename="chart.svg")
    assert out.startswith("Tool Error:"), out
    assert "write_file" in out
    assert "coding_agent" in out
    assert not (out_dir / "chart.svg").exists()


def test_html_rejected_with_redirect(out_dir):
    out = DocumentWriterTool().run(document_type="other",
                                   content="<html></html>", filename="page.html")
    assert out.startswith("Tool Error:"), out
    assert not (out_dir / "page.html").exists()


def test_txt_md_docx_still_work(out_dir):
    r1 = DocumentWriterTool().run(document_type="letter", content="Hello", filename="a.txt")
    assert "saved successfully" in r1 and (out_dir / "a.txt").exists()
    r2 = DocumentWriterTool().run(document_type="letter", content="# H", filename="b.md")
    assert "saved successfully" in r2 and (out_dir / "b.md").exists()
    r3 = DocumentWriterTool().run(document_type="letter", content="Hello", filename="c.docx")
    assert "saved successfully" in r3 and (out_dir / "c.docx").exists()


def test_uppercase_extension_accepted(out_dir):
    out = DocumentWriterTool().run(document_type="letter", content="x", filename="REPORT.MD")
    assert "saved successfully" in out
    assert (out_dir / "REPORT.MD").exists()


def test_no_suffix_derives_from_format(out_dir):
    DocumentWriterTool().run(document_type="letter", content="x",
                             filename="brief", format="word")
    assert (out_dir / "brief.docx").exists(), "format=word must yield .docx, not .txt"


def test_no_suffix_defaults_to_txt(out_dir):
    DocumentWriterTool().run(document_type="letter", content="x", filename="notiz")
    assert (out_dir / "notiz.txt").exists()


def test_extension_beats_format_param(out_dir):
    # format="word" + report.txt used to write DOCX bytes into the .txt file.
    out = DocumentWriterTool().run(document_type="report", content="Hello",
                                   filename="report.txt", format="word")
    assert "Format:** Text" in out, out
    data = (out_dir / "report.txt").read_bytes()
    assert not data.startswith(b"PK"), "DOCX (zip) bytes written into a .txt file"


def test_non_string_filename_is_coerced(out_dir):
    out = DocumentWriterTool().run(document_type="letter", content="x", filename=None)
    assert "saved successfully" in out  # falls back to document.txt
    assert (out_dir / "document.txt").exists()


def test_empty_content_is_tool_error(out_dir):
    out = DocumentWriterTool().run(document_type="letter", content="", filename="a.txt")
    assert out.startswith("Tool Error:"), out
