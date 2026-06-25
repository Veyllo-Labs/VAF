# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Unit tests for vaf/core/pdf_extract.py.

Covers the deterministic logic (heading thresholds, table rendering incl. false-positive + pipe
escaping, bbox geometry) and the fallback orchestration (pdfplumber -> PyPDF2 -> OCR) via monkeypatch,
so no real PDF / no PDF-writer dependency is needed.
"""
import pytest

from vaf.core import pdf_extract
from vaf.core.pdf_extract import (
    _heading_prefix,
    _render_table,
    _word_in_bbox,
    extract_pdf_markdown,
)


class _FakeTable:
    def __init__(self, rows, bbox=(0, 0, 100, 50)):
        self._rows = rows
        self.bbox = bbox

    def extract(self):
        return self._rows


def test_heading_prefix_by_relative_size():
    body = 10.0
    assert _heading_prefix("Title", 16.0, body, False) == "# "    # >= 1.6x
    assert _heading_prefix("Title", 13.0, body, False) == "## "   # >= 1.28x
    assert _heading_prefix("Title", 11.7, body, False) == "### "  # >= 1.16x
    assert _heading_prefix("Title", 10.5, body, False) == ""      # body-ish, no heading
    # a long line (paragraph) in larger font must NOT become a heading
    assert _heading_prefix("x" * 200, 20.0, body, False) == ""


def test_render_table_real_vs_false_positive():
    md = _render_table(_FakeTable([["A", "B"], ["1", "2"]]))
    assert "| A | B |" in md
    assert "| --- | --- |" in md
    assert "| 1 | 2 |" in md
    # 1-cell / 1-column / single-row detections are find_tables() false positives -> "" (text flows)
    assert _render_table(_FakeTable([["only one cell"]])) == ""
    assert _render_table(_FakeTable([["A", "B"]])) == ""           # single row
    assert _render_table(_FakeTable([["a"], ["b"], ["c"]])) == ""  # single column
    # pipes inside cells are escaped so they don't break the markdown table
    assert "\\|" in _render_table(_FakeTable([["a|b", "c"], ["1", "2"]]))


def test_word_in_bbox():
    w = {"x0": 10, "x1": 20, "top": 10, "bottom": 20}  # center (15, 15)
    assert _word_in_bbox(w, (0, 0, 100, 100)) is True
    assert _word_in_bbox(w, (50, 50, 100, 100)) is False


def test_pypdf2_fallback_when_pdfplumber_fails(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("pdfplumber broken")

    monkeypatch.setattr(pdf_extract, "_extract_pdfplumber", _boom)
    monkeypatch.setattr(
        pdf_extract, "_extract_pypdf2",
        lambda p, m: ("--- Page 1 ---\nhello world from the pypdf2 fallback path", 1),
    )
    out = extract_pdf_markdown("/nonexistent.pdf")
    assert out["method"] == "pypdf2"
    assert out["used_ocr"] is False
    assert "pypdf2 fallback" in out["markdown"]


def test_ocr_fallback_when_text_is_sparse(monkeypatch):
    # pdfplumber returns near-empty (scanned PDF) -> OCR path takes over
    monkeypatch.setattr(pdf_extract, "_extract_pdfplumber", lambda p, m: ("   ", 3))
    monkeypatch.setattr(
        pdf_extract, "pdf_ocr_fallback",
        lambda p, m: "--- Page 1 ---\nOCR recovered text " * 3,
    )
    out = extract_pdf_markdown("/scanned.pdf", ocr_fallback=True)
    assert out["used_ocr"] is True
    assert out["method"] == "ocr"
    assert "OCR recovered" in out["markdown"]


def test_ocr_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(pdf_extract, "_extract_pdfplumber", lambda p, m: ("   ", 3))
    called = {"ocr": False}

    def _ocr(p, m):
        called["ocr"] = True
        return "should not be used"

    monkeypatch.setattr(pdf_extract, "pdf_ocr_fallback", _ocr)
    out = extract_pdf_markdown("/scanned.pdf", ocr_fallback=False)
    assert called["ocr"] is False
    assert out["used_ocr"] is False
