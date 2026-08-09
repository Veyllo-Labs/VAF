# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Unit tests for vaf/core/pdf_extract.py.

Covers the deterministic logic (heading thresholds, table rendering incl. false-positive + pipe
escaping, bbox geometry), the fallback orchestration (pdfplumber -> PyPDF2 -> OCR) via monkeypatch,
and the contracts that exist because their absence caused real damage:

- streaming: every page is CLOSED while extraction runs (retaining pages measured
  9 GB peak RSS on a 1000-page book; streaming measures ~0.7 GB, SHA-identical),
- the OCR gate counts CONTENT, not the `--- Page N ---` scaffolding the module
  itself emits (a scanned PDF of 4+ pages never OCRed and its bare page markers
  were stored as knowledge),
- OCR unavailability is NAMED (`OCR unavailable: <reason>`), never a silent ""
  (TesseractNotFoundError subclasses OSError and slipped past `except
  TesseractError` into the blanket handler),
- truncation is DATA (`pages_read`/`total_pages`/`truncated`), not a prose tail
  that every downstream cut destroyed,
- `first_page` slices with ABSOLUTE page markers ("read pages 10-20" was
  promised in six places and implemented in none).

No real PDF / no PDF-writer dependency: fake pdfplumber page objects where the
behavior needs observing, monkeypatched extractors elsewhere.
"""
import pytest

from vaf.core import pdf_extract
from vaf.core.pdf_extract import (
    OCR_UNAVAILABLE_PREFIX,
    _content_chars,
    _heading_prefix,
    _render_table,
    _word_in_bbox,
    extract_pdf_markdown,
    format_pdf_read_result,
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
        lambda p, m, first_page=1, cancel=None: (
            "--- Page 1 ---\nhello world from the pypdf2 fallback path", 1, 1),
    )
    out = extract_pdf_markdown("/nonexistent.pdf")
    assert out["method"] == "pypdf2"
    assert out["used_ocr"] is False
    assert "pypdf2 fallback" in out["markdown"]


def test_ocr_fallback_when_text_is_sparse(monkeypatch):
    # pdfplumber returns near-empty (scanned PDF) -> OCR path takes over
    monkeypatch.setattr(pdf_extract, "_extract_pdfplumber",
                        lambda p, m, first_page=1, cancel=None: ("   ", 3, 3))
    monkeypatch.setattr(
        pdf_extract, "pdf_ocr_fallback",
        lambda p, m, first_page=1, cancel=None: "--- Page 1 ---\nOCR recovered text " * 3,
    )
    out = extract_pdf_markdown("/scanned.pdf", ocr_fallback=True)
    assert out["used_ocr"] is True
    assert out["method"] == "ocr"
    assert "OCR recovered" in out["markdown"]


def test_ocr_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(pdf_extract, "_extract_pdfplumber",
                        lambda p, m, first_page=1, cancel=None: ("   ", 3, 3))
    called = {"ocr": False}

    def _ocr(p, m, first_page=1, cancel=None):
        called["ocr"] = True
        return "should not be used"

    monkeypatch.setattr(pdf_extract, "pdf_ocr_fallback", _ocr)
    out = extract_pdf_markdown("/scanned.pdf", ocr_fallback=False)
    assert called["ocr"] is False
    assert out["used_ocr"] is False


# ---------------------------------------------------------------------------
# The OCR gate counts CONTENT, not scaffolding (the memory-corruption pin)
# ---------------------------------------------------------------------------

def test_ocr_gate_ignores_page_marker_scaffolding(monkeypatch):
    """A 10-page scan yields >=120 chars of pure `--- Page N ---` markers. The
    old gate counted those as text, never fired OCR, and the bare skeleton was
    STORED AS KNOWLEDGE. Content chars here are 0 -> OCR must fire."""
    skeleton = "\n\n".join(f"--- Page {i} ---" for i in range(1, 11))
    assert len(skeleton.strip()) > pdf_extract._MIN_TEXT_CHARS  # the old gate saw "enough text"
    assert _content_chars(skeleton) == 0

    monkeypatch.setattr(pdf_extract, "_extract_pdfplumber",
                        lambda p, m, first_page=1, cancel=None: (skeleton, 10, 10))
    monkeypatch.setattr(pdf_extract, "pdf_ocr_fallback",
                        lambda p, m, first_page=1, cancel=None: "--- Page 1 ---\nreal recovered words " * 5)
    out = extract_pdf_markdown("/scan.pdf", ocr_fallback=True, cancel=lambda: False)
    assert out["used_ocr"] is True, "the scaffolding defeated the OCR gate again"


def test_ocr_acceptance_compares_content_not_markers(monkeypatch):
    """The acceptance compare had the same flaw: marker-only OCR output (~14
    chars/page) must not beat 30 chars of real embedded text."""
    embedded = "--- Page 1 ---\n" + "x" * 30
    marker_only_ocr = "\n\n".join(f"--- Page {i} ---" for i in range(1, 50))
    monkeypatch.setattr(pdf_extract, "_extract_pdfplumber",
                        lambda p, m, first_page=1, cancel=None: (embedded, 49, 49))
    monkeypatch.setattr(pdf_extract, "pdf_ocr_fallback",
                        lambda p, m, first_page=1, cancel=None: marker_only_ocr)
    out = extract_pdf_markdown("/scan.pdf", ocr_fallback=True, cancel=lambda: False)
    assert out["used_ocr"] is False
    assert out["markdown"] == embedded


def test_marker_regex_matches_what_the_module_emits():
    """_content_chars strips exactly the marker THIS module writes. If the
    marker format changes without the regex, the gate silently regresses."""
    assert _content_chars("--- Page 7 ---") == 0
    assert _content_chars("--- Page 7 ---\nreal text") == len("real text")
    # A prose line mentioning a page is NOT scaffolding
    assert _content_chars("see --- Page 7 --- above") > 0


# ---------------------------------------------------------------------------
# Truncation is data; first_page slices with absolute markers
# ---------------------------------------------------------------------------

class _FakePage:
    """Minimal pdfplumber Page stand-in: words, no tables, close() observable."""

    def __init__(self, text, log, idx):
        self._text = text
        self._log = log
        self._idx = idx
        self.closed = False

    def extract_words(self, extra_attrs=None):
        self._log.append(("read", self._idx))
        if self.closed:
            raise AssertionError("extract_words on a closed page")
        return [{"text": self._text, "x0": 0, "x1": 10, "top": 0, "bottom": 5,
                 "size": 10.0, "fontname": "Helvetica"}]

    def find_tables(self):
        return []

    def close(self):
        self.closed = True
        self._log.append(("close", self._idx))


class _FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_pdfplumber(monkeypatch, n_pages, log):
    pages = [_FakePage(f"content of page {i}", log, i) for i in range(1, n_pages + 1)]
    import types
    fake = types.SimpleNamespace(open=lambda path: _FakePdf(pages))
    monkeypatch.setitem(__import__("sys").modules, "pdfplumber", fake)
    return pages


def test_first_page_slices_with_absolute_markers(monkeypatch):
    log = []
    _fake_pdfplumber(monkeypatch, 5, log)
    out = extract_pdf_markdown("/x.pdf", max_pages=2, first_page=3,
                               ocr_fallback=False, cancel=lambda: False)
    assert "--- Page 3 ---" in out["markdown"]
    assert "--- Page 4 ---" in out["markdown"]
    assert "--- Page 2 ---" not in out["markdown"] and "--- Page 5 ---" not in out["markdown"]
    # The marker must sit on the RIGHT content: a slice that reads pages 1-2 but
    # renumbers them 3-4 satisfies the marker asserts and is still wrong.
    assert "content of page 3" in out["markdown"]
    assert "content of page 1" not in out["markdown"]
    assert out["total_pages"] == 5 and out["pages_read"] == 2
    assert out["first_page"] == 3 and out["truncated"] is True
    # The deleted prose tail must never come back: truncation is DATA now.
    assert "more pages not shown" not in out["markdown"]


def test_whole_document_is_not_truncated(monkeypatch):
    log = []
    _fake_pdfplumber(monkeypatch, 3, log)
    out = extract_pdf_markdown("/x.pdf", ocr_fallback=False, cancel=lambda: False)
    assert out["pages_read"] == 3 and out["truncated"] is False


def test_pages_are_closed_while_streaming(monkeypatch):
    """The streaming contract: page N is closed before page N+1 is read.
    Retaining open pages measured 9 GB peak RSS over 1000 pages."""
    log = []
    _fake_pdfplumber(monkeypatch, 3, log)
    extract_pdf_markdown("/x.pdf", ocr_fallback=False, cancel=lambda: False)
    assert ("close", 1) in log and ("read", 2) in log
    assert log.index(("close", 1)) < log.index(("read", 2)), \
        "page 1 was still open while page 2 was read - streaming regressed"


def test_cancel_is_polled_per_page(monkeypatch):
    log = []
    _fake_pdfplumber(monkeypatch, 10, log)
    polls = {"n": 0}

    def _cancel():
        polls["n"] += 1
        return polls["n"] > 2  # allow two pages, then cancel

    out = extract_pdf_markdown("/x.pdf", ocr_fallback=False, cancel=_cancel)
    assert out["pages_read"] == 2
    assert out["truncated"] is True
    assert "--- Page 2 ---" in out["markdown"] and "--- Page 3 ---" not in out["markdown"]


# ---------------------------------------------------------------------------
# OCR engines: resolution (auto/tesseract/vision), page images, named reasons
# ---------------------------------------------------------------------------

def _cfg(monkeypatch, mapping):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get",
                        classmethod(lambda cls, k, d=None: mapping.get(k, d)))


def _fake_pytesseract(monkeypatch, *, binary_ok=True, text="words"):
    import sys as _sys
    import types

    class _NotFound(OSError):
        pass

    def _version_fail():
        raise _NotFound("tesseract is not installed")

    fake = types.SimpleNamespace(
        TesseractNotFoundError=_NotFound,
        TesseractError=type("TesseractError", (RuntimeError,), {}),
        get_tesseract_version=(lambda: "5.3") if binary_ok else _version_fail,
        image_to_string=lambda img, **kw: text,
    )
    monkeypatch.setitem(_sys.modules, "pytesseract", fake)
    return fake


def test_resolver_auto_prefers_tesseract(monkeypatch):
    _cfg(monkeypatch, {"ocr_engine": "auto"})
    _fake_pytesseract(monkeypatch, binary_ok=True)
    engine, reason = pdf_extract.resolve_ocr_engine()
    assert engine == "tesseract" and reason == ""


def test_resolver_auto_falls_to_vision_when_binary_missing(monkeypatch):
    """The out-of-the-box case: no Tesseract installed, but the vision lane
    resolves (Veyllo default / local Gemma) - scans work with zero system
    packages."""
    _cfg(monkeypatch, {"ocr_engine": "auto"})
    _fake_pytesseract(monkeypatch, binary_ok=False)
    monkeypatch.setattr("vaf.core.vision_infer.select_vision_backend",
                        lambda: ("veyllo", "veyllo-chat"))
    engine, reason = pdf_extract.resolve_ocr_engine()
    assert engine == "vision" and reason == ""


def test_resolver_names_both_remedies_when_nothing_can_run(monkeypatch):
    _cfg(monkeypatch, {"ocr_engine": "auto"})
    _fake_pytesseract(monkeypatch, binary_ok=False)
    monkeypatch.setattr("vaf.core.vision_infer.select_vision_backend",
                        lambda: (None, None))
    engine, reason = pdf_extract.resolve_ocr_engine()
    assert engine is None
    assert "Tesseract" in reason and "vision" in reason, \
        "the reason must name BOTH ways out"


def test_explicit_pick_never_silently_falls_elsewhere(monkeypatch):
    """An explicit choice that quietly runs the other engine is the settings
    lie this codebase keeps paying for."""
    _cfg(monkeypatch, {"ocr_engine": "vision"})
    _fake_pytesseract(monkeypatch, binary_ok=True)  # tesseract WOULD work
    monkeypatch.setattr("vaf.core.vision_infer.select_vision_backend",
                        lambda: (None, None))
    engine, reason = pdf_extract.resolve_ocr_engine()
    assert engine is None
    assert "ocr_engine=vision" in reason


def test_vision_engine_transcribes_pages_and_caps_per_call(monkeypatch):
    """One model call per page is instance spend: the per-call budget cuts and
    NAMES itself with the continuation page."""
    calls = []

    def _fake_vision(images, prompt, **kw):
        calls.append(images[0]["name"])
        assert "Transcribe" in prompt
        return f"text of {images[0]['name']}"

    monkeypatch.setattr("vaf.core.vision_infer.vision_infer", _fake_vision)
    pages = [(n, b"jpegbytes", "image/jpeg") for n in range(1, 6)]
    out = pdf_extract._ocr_pages_vision(iter(pages), None, max_pages_budget=3)
    assert calls == ["page-1", "page-2", "page-3"]
    assert "--- Page 3 ---" in out
    assert "ocr_vision_max_pages_per_call" in out and "first_page=4" in out


def test_tesseract_engine_reads_page_images(monkeypatch):
    _fake_pytesseract(monkeypatch, binary_ok=True, text="erkannter text")
    import sys as _sys
    import types
    fake_img = object()
    monkeypatch.setitem(_sys.modules, "PIL", types.SimpleNamespace(
        Image=types.SimpleNamespace(open=lambda buf: fake_img)))
    monkeypatch.setitem(_sys.modules, "PIL.Image",
                        types.SimpleNamespace(open=lambda buf: fake_img))
    pages = [(7, b"jpegbytes", "image/jpeg")]
    out = pdf_extract._ocr_pages_tesseract(iter(pages), None)
    assert "--- Page 7 ---" in out and "erkannter text" in out


def test_fallback_reports_the_resolver_reason(monkeypatch):
    _cfg(monkeypatch, {"ocr_engine": "tesseract"})
    _fake_pytesseract(monkeypatch, binary_ok=False)
    res = pdf_extract.pdf_ocr_fallback("/scan.pdf", 3)
    assert res.startswith(OCR_UNAVAILABLE_PREFIX)
    assert "Tesseract binary not found" in res



def test_unavailable_ocr_is_reported_not_stored(monkeypatch):
    """The sentinel must land in ocr_unavailable_reason - never in the markdown
    (it would be ingested as document content, the exact corruption class)."""
    monkeypatch.setattr(pdf_extract, "_extract_pdfplumber",
                        lambda p, m, first_page=1, cancel=None: ("", 6, 6))
    monkeypatch.setattr(pdf_extract, "pdf_ocr_fallback",
                        lambda p, m, first_page=1, cancel=None: OCR_UNAVAILABLE_PREFIX + "Tesseract binary not found")
    out = extract_pdf_markdown("/scan.pdf", ocr_fallback=True, cancel=lambda: False)
    assert out["used_ocr"] is False
    assert out["markdown"] == ""
    assert "Tesseract binary not found" in out["ocr_unavailable_reason"]


# ---------------------------------------------------------------------------
# format_pdf_read_result: the ONE honest rendering
# ---------------------------------------------------------------------------

def _res(**kw):
    base = {"markdown": "", "num_pages": 0, "used_ocr": False, "method": "pdfplumber",
            "total_pages": 0, "pages_read": 0, "first_page": 1, "truncated": False,
            "ocr_unavailable_reason": ""}
    base.update(kw)
    return base


def test_format_header_and_continuation_come_first():
    body = "--- Page 1 ---\n" + "text " * 10
    out = format_pdf_read_result(
        _res(markdown=body, total_pages=1000, pages_read=50, first_page=1, truncated=True),
        file_name="report.pdf")
    lines = out.split("\n")
    assert lines[0] == "### PDF: report.pdf"
    assert lines[1] == "**Pages:** 1-50 of 1000"
    # The continuation fact precedes the body so it survives the 2000-char chat cut.
    assert "first_page=51" in lines[2]
    assert out.index("first_page=51") < out.index("text text")


def test_format_char_cap_names_the_cut_page():
    pages = "\n\n".join(f"--- Page {i} ---\n" + ("w" * 500) for i in range(1, 60))
    out = format_pdf_read_result(
        _res(markdown=pages, total_pages=59, pages_read=59, first_page=1),
        file_name="big.pdf", char_cap=5000)
    assert "Output capped at 5,000 characters inside page" in out
    # The named page must be the last marker that made it into the kept prefix
    kept = out[: out.index("[Output capped")]
    import re as _re
    last_marker = _re.findall(r"--- Page (\d+) ---", kept)[-1]
    assert f"inside page {last_marker} " in out
    assert f"first_page={last_marker}" in out


def test_format_empty_names_the_ocr_reason():
    out = format_pdf_read_result(
        _res(total_pages=12, pages_read=12, truncated=False,
             ocr_unavailable_reason="Tesseract binary not found (install tesseract-ocr)"),
        file_name="scan.pdf")
    assert "No text layer detected" in out
    assert "Tesseract binary not found" in out


def test_format_out_of_range_start_is_honest():
    out = format_pdf_read_result(
        _res(total_pages=10, pages_read=0, first_page=50, truncated=True),
        file_name="small.pdf")
    assert "start at 50" in out and "only 10 pages" in out


def test_format_ocr_marker_in_header():
    out = format_pdf_read_result(
        _res(markdown="--- Page 1 ---\nrecovered", total_pages=3, pages_read=3,
             used_ocr=True),
        file_name="scan.pdf")
    assert "**Pages:** 1-3 of 3 (OCR)" in out
