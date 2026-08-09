# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""PDF page ranges are real, and PDF reads report honestly.

"Ask me to read pages 10-20 of <file>" was promised by the librarian's own tip
and five doc sites while ZERO implementations existed - the read tools always
took the first 50 pages, cut the result at a hand-rolled 15,000 chars (the same
three lines, byte-identical, in read_file AND the librarian) and appended a
bare "(truncated)". These tests pin the range plumbing on both consumers, the
fast-path phrase parsing, and that both now render through the ONE honest
formatter instead of private cuts.
"""
from pathlib import Path

import pytest

from vaf.core import pdf_extract
from vaf.tools.filesystem import ReadFileTool
from vaf.tools.librarian import LibrarianTool


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = (tmp_path / "home").resolve()
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.chdir(h)
    return h


@pytest.fixture
def pdf_file(home):
    p = home / "report.pdf"
    p.write_bytes(b"%PDF-fake")
    return p


@pytest.fixture
def extractor_spy(monkeypatch):
    """Capture the kwargs both consumers pass to the shared extractor."""
    calls = []

    def _fake(file_path, max_pages=None, ocr_fallback=True, *, first_page=1, cancel=None):
        calls.append({"max_pages": max_pages, "first_page": first_page,
                      "ocr_fallback": ocr_fallback})
        pages_read = max_pages if max_pages is not None else 3
        return {"markdown": f"--- Page {first_page} ---\ncontent",
                "num_pages": 1000, "total_pages": 1000, "pages_read": pages_read,
                "first_page": first_page, "truncated": True,
                "used_ocr": False, "method": "pdfplumber",
                "ocr_unavailable_reason": ""}

    # Both consumers import the symbol lazily from vaf.core.pdf_extract, so
    # patching the module attribute covers read_file AND the librarian.
    monkeypatch.setattr(pdf_extract, "extract_pdf_markdown", _fake)
    return calls


def test_read_file_pdf_accepts_a_page_range(pdf_file, extractor_spy):
    out = ReadFileTool().run(path=str(pdf_file), first_page=100, last_page=120)
    assert extractor_spy[-1] == {"max_pages": 21, "first_page": 100, "ocr_fallback": True}
    assert "**Pages:** 100-120 of 1000" in out


def test_read_file_default_window_is_named_not_silent(pdf_file, extractor_spy):
    out = ReadFileTool().run(path=str(pdf_file))
    assert extractor_spy[-1]["max_pages"] == ReadFileTool._PDF_READ_DEFAULT_PAGES
    assert extractor_spy[-1]["first_page"] == 1
    # The honest continuation replaces the old bare "(truncated)" tail.
    assert "first_page=51" in out
    assert "... (truncated)" not in out


def test_librarian_read_uses_the_shared_formatter(pdf_file, extractor_spy):
    lib = LibrarianTool()
    out = lib._read_file(pdf_file, first_page=7, last_page=9)
    assert extractor_spy[-1] == {"max_pages": 3, "first_page": 7, "ocr_fallback": True}
    assert "**Pages:** 7-9 of 1000" in out


def test_librarian_explicit_range_bypasses_the_size_gate(home, extractor_spy, monkeypatch):
    """"Read pages 100-120 of an 85 MB PDF" must extract that slice - the
    extractor streams pages now, so the whole-file size gate only applies to
    un-ranged reads."""
    big = home / "huge.pdf"
    # Sparse file: st_size 85 MB without writing 85 MB.
    with open(big, "wb") as f:
        f.write(b"%PDF-fake")
        f.seek(85 * 1024 * 1024 - 1)
        f.write(b"\0")
    lib = LibrarianTool()
    out = lib._read_file(big, first_page=100, last_page=120)
    assert extractor_spy, "the size gate swallowed an explicit page range"
    assert "**Pages:** 100-120" in out
    # Without a range the gate still stands.
    out2 = lib._read_file(big)
    assert "large" in out2.lower() or "too large" in out2.lower()


@pytest.mark.parametrize("task,first,last", [
    ("read pages 10-20 of report.pdf", 10, 20),
    ("read pages 10 to 20 of report.pdf", 10, 20),
    ("lies seiten 10 bis 20 aus report.pdf", 10, 20),
    ("read page 7 of report.pdf", 7, 7),
])
def test_fast_path_parses_page_ranges(home, pdf_file, task, first, last, monkeypatch):
    """The direct-execution lane must parse the documented phrasings and hand
    the range to _read_file - without it "read pages 10-20" matched NO read
    pattern at all and fell to the LLM loop."""
    seen = {}

    def _spy(self, file_path, enable_chunking=True, first_page=None, last_page=None):
        seen.update(first_page=first_page, last_page=last_page, path=file_path)
        return "ok"

    monkeypatch.setattr(LibrarianTool, "_read_file", _spy)
    monkeypatch.setattr(LibrarianTool, "_extract_file_path", lambda self, t: pdf_file)
    lib = LibrarianTool()
    result = lib._try_direct_execution(task, caller=None)
    assert result == "ok", f"fast path did not fire for {task!r}"
    assert seen.get("first_page") == first and seen.get("last_page") == last, \
        f"fast path did not parse the range from {task!r}: {seen}"
