"""
Shared PDF -> Markdown extraction (single source of truth).

Turns a PDF into Markdown with detected **headings** and **tables** so every consumer benefits at once:
- the attachment RAG (hierarchical `_split_into_sections` keys on `## headings`),
- the Librarian read tool, and
- the filesystem read tool.

Engine: `pdfplumber` (MIT, lightweight, exposes per-word font sizes) + a font-size -> heading heuristic
(the concept pymupdf4llm uses internally). This keeps VAF clean-license and lightweight instead of
pulling AGPL (PyMuPDF) or a heavy ML stack (docling/torch). Robustness:
- on any pdfplumber failure -> graceful fallback to PyPDF2 per-page text (never regress),
- for scanned / image-only PDFs (almost no embedded text) -> OCR via pdf2image + pytesseract.

Image/diagram text (raster figures) is intentionally out of scope here -- that is a separate optional
feature (vision-model figure descriptions / page-image OCR).
"""
from __future__ import annotations

import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Heading thresholds relative to the body font size. Validated against slides / prose / contract /
# invoice in tmp/pdf_extract_compare.py (DIY vs pymupdf4llm): comparable section counts, real titles.
_H1, _H2, _H3 = 1.6, 1.28, 1.16
_HEADING_MAX_LEN = 120   # only short lines (titles) become headings, not large-font paragraphs
_LINE_TOL = 3.0          # px tolerance when grouping words into one visual line
_MIN_TEXT_CHARS = 50     # below this the PDF is treated as scanned -> OCR fallback


def _render_table(tbl) -> str:
    """Render a pdfplumber table as a GitHub markdown table (cells `|`-escaped). Returns "" for
    trivial 1xN / Nx1 detections (find_tables() flags text boxes as 1-cell/1-col "tables" -- those
    are false positives; we let their words flow as text/headings instead of `| title |`)."""
    rows = tbl.extract() or []
    rows = [
        [(c or "").strip().replace("\n", " ").replace("|", "\\|") for c in r]
        for r in rows
        if any((c or "").strip() for c in r)
    ]
    if len(rows) < 2:
        return ""
    ncol = max(len(r) for r in rows)
    if ncol < 2:
        return ""
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * ncol) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _word_in_bbox(word: Dict[str, Any], bbox: Tuple[float, float, float, float]) -> bool:
    """True if the word's center falls inside the table bbox (so table text isn't also dumped as prose)."""
    x0, top, x1, bottom = bbox
    cx = (word["x0"] + word["x1"]) / 2.0
    cy = (word["top"] + word["bottom"]) / 2.0
    return (x0 <= cx <= x1) and (top <= cy <= bottom)


def _heading_prefix(text: str, line_size: float, body_size: float, bold: bool) -> str:
    """Markdown heading prefix for a line based on its font size relative to the body size."""
    if len(text) > _HEADING_MAX_LEN or body_size <= 0:
        return ""
    ratio = line_size / body_size
    if ratio >= _H1:
        return "# "
    if ratio >= _H2:
        return "## "
    if ratio >= _H3:
        return "### "
    return ""


def _extract_pdfplumber(file_path: Path, max_pages: int) -> Tuple[str, int]:
    """Primary path: markdown with headings + tables. Returns (markdown, total_num_pages)."""
    import pdfplumber

    out: List[str] = []
    with pdfplumber.open(str(file_path)) as pdf:
        total_pages = len(pdf.pages)
        pages = pdf.pages[:max_pages]

        # Pass 1: body font size = the size carrying the most CHARACTERS (robust on heading-heavy slides).
        page_data: List[Tuple[Any, List[Dict[str, Any]], List[Any]]] = []
        size_mass: "Counter[float]" = Counter()
        for pg in pages:
            words = pg.extract_words(extra_attrs=["size", "fontname"])
            try:
                tables = pg.find_tables()
            except Exception:
                tables = []
            page_data.append((pg, words, tables))
            for w in words:
                s = round(float(w.get("size") or 0), 1)
                if s:
                    size_mass[s] += max(1, len(w.get("text") or ""))
        body = size_mass.most_common(1)[0][0] if size_mass else 10.0

        # Pass 2: per page, interleave heading/body lines and tables by vertical position.
        for pidx, (pg, words, tables) in enumerate(page_data, 1):
            out.append(f"--- Page {pidx} ---")
            # Keep only real tables (>=2 cols, >=2 rows); their words are pulled out of the text flow.
            real_tables = [(t, m) for t in tables if (m := _render_table(t))]
            tboxes = [t.bbox for t, _ in real_tables]
            free = [w for w in words if not any(_word_in_bbox(w, b) for b in tboxes)]
            free.sort(key=lambda w: (round(w["top"]), w["x0"]))

            lines: List[List[Dict[str, Any]]] = []
            cur: List[Dict[str, Any]] = []
            cur_top = None
            for w in free:
                if cur_top is None or abs(w["top"] - cur_top) <= _LINE_TOL:
                    cur.append(w)
                    cur_top = w["top"] if cur_top is None else cur_top
                else:
                    lines.append(cur)
                    cur = [w]
                    cur_top = w["top"]
            if cur:
                lines.append(cur)

            blocks: List[Tuple[float, str]] = []  # (top_y, rendered_text)
            for ln in lines:
                txt = " ".join(w["text"] for w in ln).strip()
                if not txt:
                    continue
                line_size = statistics.median([float(w.get("size") or body) for w in ln])
                bold = any("bold" in (w.get("fontname") or "").lower() for w in ln)
                top = min(w["top"] for w in ln)
                blocks.append((top, _heading_prefix(txt, line_size, body, bold) + txt))
            for t, md in real_tables:
                blocks.append((t.bbox[1], md))

            blocks.sort(key=lambda b: b[0])
            for _, b in blocks:
                if b.strip():
                    out.append(b)

        if total_pages > len(pages):
            out.append(f"\n... ({total_pages - len(pages)} more pages not shown)")

    return "\n\n".join(out), total_pages


def _extract_pypdf2(file_path: Path, max_pages: int) -> Tuple[str, int]:
    """Fallback path (no headings): the original per-page PyPDF2 text. Returns (text, total_num_pages)."""
    import PyPDF2

    out: List[str] = []
    with open(file_path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        total_pages = len(reader.pages)
        n = min(total_pages, max_pages)
        for i in range(n):
            page_text = reader.pages[i].extract_text() or ""
            if page_text.strip():
                out.append(f"--- Page {i + 1} ---\n{page_text}")
        if total_pages > n:
            out.append(f"\n... ({total_pages - n} more pages not shown)")
    return "\n\n".join(out), total_pages


def pdf_ocr_fallback(file_path: Path, max_pages: int) -> str:
    """Extract text from scanned (image-only) PDFs via OCR. Requires pdf2image + pytesseract (+ poppler,
    Tesseract). Returns "" if unavailable or empty. (Shared: Librarian delegates to this.)"""
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError:
        return ""
    try:
        images = convert_from_path(str(file_path), first_page=1, last_page=max_pages, dpi=200)
        for lang in ("deu+eng", "eng", None):
            try:
                lang_arg = {"lang": lang} if lang else {}
                parts = []
                for i, img in enumerate(images):
                    text = pytesseract.image_to_string(img, **lang_arg)
                    if text.strip():
                        parts.append(f"--- Page {i + 1} ---\n{text.strip()}")
                if parts:
                    return "\n\n".join(parts)
            except pytesseract.TesseractError:
                continue
        return ""
    except Exception:
        return ""


def extract_pdf_markdown(file_path, max_pages: int = 50, ocr_fallback: bool = True) -> Dict[str, Any]:
    """
    Extract a PDF as Markdown (headings + tables).

    Returns {"markdown": str, "num_pages": int, "used_ocr": bool, "method": "pdfplumber"|"pypdf2"|"ocr"}.
    Tries pdfplumber; on failure falls back to PyPDF2; for scanned PDFs (almost no text) tries OCR.
    """
    file_path = Path(file_path)
    method = "pdfplumber"
    try:
        markdown, num_pages = _extract_pdfplumber(file_path, max_pages)
    except Exception:
        markdown, num_pages = _extract_pypdf2(file_path, max_pages)
        method = "pypdf2"

    used_ocr = False
    if ocr_fallback and len((markdown or "").strip()) < _MIN_TEXT_CHARS and num_pages > 0:
        ocr_text = pdf_ocr_fallback(file_path, min(num_pages, max_pages))
        if ocr_text and len(ocr_text.strip()) > len((markdown or "").strip()):
            markdown, method, used_ocr = ocr_text, "ocr", True

    return {"markdown": markdown or "", "num_pages": num_pages, "used_ocr": used_ocr, "method": method}
