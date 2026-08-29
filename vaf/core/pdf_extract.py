# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
- for scanned / image-only PDFs (almost no embedded text) -> OCR via the resolved
  engine (`resolve_ocr_engine`): Tesseract (free, local) or the VISION MODEL
  (one call per page; uses the vision lane's provider choice). Page images come
  from the embedded streams (instant, original quality) or a pypdfium2 render -
  poppler is gone: it is GPL, has no official Windows build, and rendering was
  all it did.

Contract points that exist because their absence caused real damage:

- **Pages are streamed, never retained.** Holding every parsed page until the end
  measured 9 GB peak RSS on a 1000-page book; closing each page after its pass-1
  read brings the same run to ~0.7 GB with byte-identical output.
- **Truncation is DATA, not prose.** The result dict reports `pages_read`,
  `total_pages` and `truncated`; a notice appended to the end of the text was
  destroyed by every downstream cut and, worse, counted as "content" by the OCR
  gate. Rendering a truncation message is the consumer's job
  (`format_pdf_read_result`).
- **The OCR gate counts content, not scaffolding.** The extractor emits a
  `--- Page N ---` marker per page; counting those against `_MIN_TEXT_CHARS`
  meant a scanned PDF of 4+ pages never triggered OCR and its bare markers were
  stored as knowledge. `_content_chars` strips the markers this module itself
  emits before measuring.
- **OCR failures are NAMED.** A missing Tesseract binary raises
  `TesseractNotFoundError`, which subclasses OSError - the old
  `except TesseractError` could not catch it, so the user saw the same silent ""
  as for a genuinely empty page. Unavailability is returned as an
  `OCR unavailable: <reason>` sentinel (never raised: the text lane must not die
  because OCR cannot run).

Image/diagram text (raster figures) is intentionally out of scope here -- that is a separate optional
feature (vision-model figure descriptions / page-image OCR).
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Heading thresholds relative to the body font size. Validated against slides / prose / contract /
# invoice in tmp/pdf_extract_compare.py (DIY vs pymupdf4llm): comparable section counts, real titles.
_H1, _H2, _H3 = 1.6, 1.28, 1.16
_HEADING_MAX_LEN = 120   # only short lines (titles) become headings, not large-font paragraphs
_LINE_TOL = 3.0          # px tolerance when grouping words into one visual line
_MIN_TEXT_CHARS = 50     # below this CONTENT length the PDF is treated as scanned -> OCR fallback

# The one page-marker format this module emits (all three extractors). _content_chars
# strips exactly this before measuring, so the marker format and the regex must move
# together - a test pins the coupling.
_PAGE_MARKER_RE = re.compile(r"^--- Page \d+ ---$", re.MULTILINE)

# Sentinel prefix for "OCR could not run" (vs "" = ran fine, found nothing).
# String-prefix pattern per bounded_run.TIMEOUT_PREFIX precedent: the OCR lane is
# best-effort inside a text pipeline and must return, not raise.
OCR_UNAVAILABLE_PREFIX = "OCR unavailable: "

# Default char cap for the human/model-facing rendering of a read result. Context
# protection, NOT an extraction limit - the cap and what it cut are named in the
# output instead of a bare "(truncated)".
_PDF_RESULT_CHAR_CAP = 15000


def _content_chars(markdown: str) -> int:
    """Chars of rendered content, excluding the `--- Page N ---` scaffolding this
    module itself emits. The OCR gate and the OCR acceptance compare both use
    this: raw `len()` counted ~12 marker chars per page, so a scanned PDF of 4+
    pages could never look "sparse" and OCR never fired (its page skeleton was
    then stored as knowledge). A document whose TEXT contains a literal marker
    line undercounts by one marker length - harmless at a threshold of 50."""
    return len(_PAGE_MARKER_RE.sub("", markdown or "").strip())


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


def _extract_pdfplumber(
    file_path: Path,
    max_pages: Optional[int],
    first_page: int = 1,
    cancel: Optional[Callable[[], bool]] = None,
) -> Tuple[str, int, int]:
    """Primary path: markdown with headings + tables.

    Returns (markdown, total_pages, pages_read). Page markers carry ABSOLUTE
    page numbers so a slice read ("pages 100-120") cites correctly.

    Streaming contract: each page is fully consumed (words + tables RENDERED)
    while it is open, then closed and dropped - pass 2 works on plain lists.
    Retaining the Page objects kept pdfplumber's per-page caches alive and
    measured 9 GB peak RSS over 1000 pages; streaming measures ~0.7 GB with
    byte-identical output (tables do not depend on the body font size, so
    rendering them in pass 1 changes nothing).
    """
    import pdfplumber

    out: List[str] = []
    start = max(0, first_page - 1)
    with pdfplumber.open(str(file_path)) as pdf:
        total_pages = len(pdf.pages)
        stop = None if max_pages is None else start + max_pages
        pages = pdf.pages[start:stop]

        # Pass 1: per page, read words, RENDER tables, then close the page.
        # Body font size = the size carrying the most CHARACTERS (robust on
        # heading-heavy slides) - known only after all pages are read, which is
        # why rendering waits for pass 2 but reading cannot.
        page_data: List[Tuple[List[Dict[str, Any]], List[Tuple[tuple, str]]]] = []
        size_mass: "Counter[float]" = Counter()
        pages_read = 0
        for pg in pages:
            if cancel is not None and cancel():
                break
            words = pg.extract_words(extra_attrs=["size", "fontname"])
            try:
                tables = pg.find_tables()
            except Exception:
                tables = []
            # Keep only real tables (>=2 cols, >=2 rows) and only their RENDERED
            # form + bbox - the live table object dies with its page.
            real_tables = [(t.bbox, m) for t in tables if (m := _render_table(t))]
            page_data.append((words, real_tables))
            for w in words:
                s = round(float(w.get("size") or 0), 1)
                if s:
                    size_mass[s] += max(1, len(w.get("text") or ""))
            try:
                pg.close()
            except Exception:
                pass
            pages_read += 1
        body = size_mass.most_common(1)[0][0] if size_mass else 10.0

        # Pass 2: per page, interleave heading/body lines and tables by vertical position.
        for pidx, (words, real_tables) in enumerate(page_data, first_page):
            out.append(f"--- Page {pidx} ---")
            tboxes = [b for b, _ in real_tables]
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
            for bbox, md in real_tables:
                blocks.append((bbox[1], md))

            blocks.sort(key=lambda b: b[0])
            for _, b in blocks:
                if b.strip():
                    out.append(b)

    return "\n\n".join(out), total_pages, pages_read


def _extract_pypdf2(
    file_path: Path,
    max_pages: Optional[int],
    first_page: int = 1,
    cancel: Optional[Callable[[], bool]] = None,
) -> Tuple[str, int, int]:
    """Fallback path (no headings): per-page PyPDF2 text.

    Returns (text, total_pages, pages_read); markers are absolute. Unlike the
    pdfplumber path this one never emits a marker for an empty page (original
    behavior, kept - it makes fully scanned PDFs come back truly empty here)."""
    import PyPDF2

    out: List[str] = []
    start = max(0, first_page - 1)
    with open(file_path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        total_pages = len(reader.pages)
        end = total_pages if max_pages is None else min(total_pages, start + max_pages)
        pages_read = 0
        for i in range(start, end):
            if cancel is not None and cancel():
                break
            page_text = reader.pages[i].extract_text() or ""
            if page_text.strip():
                out.append(f"--- Page {i + 1} ---\n{page_text}")
            pages_read += 1
    return "\n\n".join(out), total_pages, pages_read


def resolve_ocr_engine() -> Tuple[Optional[str], str]:
    """Which engine reads scanned pages: ("tesseract"|"vision"|None, reason).

    The speech-lane resolver pattern: ONE place decides, every consumer asks it.
    `ocr_engine` config (default "auto"):

    - "auto": tesseract when its binary answers, else the vision model when the
      vision lane resolves (`vision_infer.select_vision_backend` - explicit
      choice, else the main agent if it can see), else None with a reason that
      names BOTH remedies. Tesseract first because it is free and local; the
      vision engine costs one model call per page.
    - "tesseract" / "vision": the explicit pick wins; if it cannot run, the
      reason says so instead of silently trying the other one - an explicit
      choice that quietly falls elsewhere is the settings lie this codebase
      keeps paying for.

    Never raises; the reason is "" when an engine resolved.
    """
    from vaf.core.config import Config

    choice = str(Config.get("ocr_engine", "auto") or "auto").strip().lower()

    def _tesseract_ok() -> Tuple[bool, str]:
        try:
            import pytesseract
        except ImportError:
            return False, "pytesseract is not installed (pip install 'vaf[pdf]')"
        try:
            pytesseract.get_tesseract_version()
            return True, ""
        except Exception:
            return False, ("Tesseract binary not found "
                           "(install tesseract-ocr and ensure it is on PATH)")

    def _vision_ok() -> Tuple[bool, str]:
        try:
            from vaf.core.vision_infer import select_vision_backend
            provider, _model = select_vision_backend()
            if provider:
                return True, ""
            return False, ("no vision model is configured "
                           "(set a vision-capable provider in Settings)")
        except Exception as e:
            return False, f"vision lane unavailable ({e.__class__.__name__})"

    if choice == "tesseract":
        ok, why = _tesseract_ok()
        return ("tesseract", "") if ok else (None, f"ocr_engine=tesseract, but {why}")
    if choice == "vision":
        ok, why = _vision_ok()
        return ("vision", "") if ok else (None, f"ocr_engine=vision, but {why}")
    # auto
    ok, t_why = _tesseract_ok()
    if ok:
        return "tesseract", ""
    ok, v_why = _vision_ok()
    if ok:
        return "vision", ""
    return None, f"{t_why}; and {v_why}"


def _page_images(
    file_path: Path,
    first_page: int,
    last_page: int,
    cancel: Optional[Callable[[], bool]],
):
    """Yield (pageno, jpeg_or_png_bytes, mime) for a page range - poppler-free.

    Fast path per page: the EMBEDDED image. A true scan is one full-page image
    per page, and PyPDF2 hands the original bytes over instantly (measured:
    0.00s for 6 pages, original quality, zero CPU). Pages without a usable
    embedded image (hybrid/vector layouts) are RENDERED via pypdfium2 - a
    permissively licensed pip wheel on every platform, which is what replaced
    the GPL poppler dependency. One page in memory at a time, never the batch.
    """
    import io

    import PyPDF2

    with open(file_path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        total = len(reader.pages)
        last_page = min(last_page, total)
        pdfium_doc = None
        try:
            for i in range(first_page - 1, last_page):
                if cancel is not None and cancel():
                    break
                pageno = i + 1
                # Embedded fast path: the largest image stream on the page.
                try:
                    imgs = list(reader.pages[i].images)
                except Exception:
                    imgs = []
                best = max(imgs, key=lambda im: len(im.data or b""), default=None)
                if best is not None and best.data and len(best.data) > 1024:
                    name = (getattr(best, "name", "") or "").lower()
                    mime = "image/png" if name.endswith(".png") else "image/jpeg"
                    yield pageno, best.data, mime
                    continue
                # Render fallback (also the path when PyPDF2 cannot decode the stream).
                if pdfium_doc is None:
                    import pypdfium2 as pdfium
                    pdfium_doc = pdfium.PdfDocument(str(file_path))
                page = pdfium_doc[i]
                try:
                    bitmap = page.render(scale=200 / 72)  # ~200 dpi
                    pil = bitmap.to_pil()
                    buf = io.BytesIO()
                    pil.save(buf, format="PNG")
                    yield pageno, buf.getvalue(), "image/png"
                finally:
                    page.close()
        finally:
            if pdfium_doc is not None:
                try:
                    pdfium_doc.close()
                except Exception:
                    pass


# The vision engine's transcription instruction: exact text out, no commentary -
# the output is stored as document content, so chat would be corruption.
_OCR_VISION_PROMPT = (
    "Transcribe ALL text in this scanned page exactly as written, preserving "
    "reading order and line breaks. Output ONLY the transcribed text - no "
    "commentary, no summary, no markdown fences. If the page contains no "
    "readable text, output nothing."
)


def _ocr_pages_tesseract(page_iter, cancel) -> str:
    """Tesseract over (pageno, bytes, mime) tuples. The OCR language is
    resolved ONCE on the first page (deu+eng -> eng -> default); missing
    traineddata raises TesseractError and falls through the candidates."""
    import io

    import pytesseract
    from PIL import Image

    lang_selected: Optional[str] = None
    parts: List[str] = []
    for pageno, raw, _mime in page_iter:
        if cancel is not None and cancel():
            break
        img = Image.open(io.BytesIO(raw))
        if lang_selected is None:
            for cand in ("deu+eng", "eng", ""):
                try:
                    kwargs = {"lang": cand} if cand else {}
                    text = pytesseract.image_to_string(img, **kwargs)
                    lang_selected = cand
                    break
                except pytesseract.TesseractError:
                    continue
            else:
                return (OCR_UNAVAILABLE_PREFIX
                        + "no usable Tesseract language data "
                          "(install tesseract-ocr language packs, e.g. eng)")
        else:
            kwargs = {"lang": lang_selected} if lang_selected else {}
            text = pytesseract.image_to_string(img, **kwargs)
        if text.strip():
            parts.append(f"--- Page {pageno} ---\n{text.strip()}")
    return "\n\n".join(parts)


def _ocr_pages_vision(page_iter, cancel, max_pages_budget: int) -> str:
    """Vision-model OCR over (pageno, bytes, mime) tuples: one model call per
    page, so the per-call budget is a COST guard and its truncation is named
    in the output (the batched learn job never hits it - its batches are
    smaller than the budget)."""
    from vaf.core.vision_infer import vision_infer

    parts: List[str] = []
    seen = 0
    truncated_at = None
    for pageno, raw, mime in page_iter:
        if cancel is not None and cancel():
            break
        if seen >= max_pages_budget:
            truncated_at = pageno
            break
        seen += 1
        text = vision_infer(
            [{"data": raw, "mime_type": mime, "name": f"page-{pageno}"}],
            _OCR_VISION_PROMPT,
            max_tokens=2048,
        )
        if text and text.strip():
            parts.append(f"--- Page {pageno} ---\n{text.strip()}")
    if truncated_at is not None:
        parts.append(
            f"[Vision OCR stopped after {max_pages_budget} page(s) - one model "
            f"call per page (ocr_vision_max_pages_per_call). Continue with "
            f"first_page={truncated_at}.]"
        )
    return "\n\n".join(parts)


def pdf_ocr_fallback(
    file_path: Path,
    max_pages: int,
    *,
    first_page: int = 1,
    cancel: Optional[Callable[[], bool]] = None,
) -> str:
    """Extract text from scanned (image-only) PDF pages via the resolved OCR
    engine (`resolve_ocr_engine`: tesseract, or the vision model).

    Returns the extracted text, "" when OCR ran fine and found nothing
    readable, or an `OCR unavailable: <reason>` sentinel (OCR_UNAVAILABLE_PREFIX)
    when it could not run - the old bare "" for both cases sent users hunting
    in empty documents while the actual problem was a missing binary.

    Page images come from `_page_images` (embedded fast path, pypdfium2 render
    fallback) - the pdf2image/poppler dependency is gone: poppler is GPL, has
    no official Windows build, and rendering is all it did.
    """
    first_page = max(1, int(first_page or 1))
    last_page = first_page + max(0, int(max_pages)) - 1
    if last_page < first_page:
        return ""

    engine, reason = resolve_ocr_engine()
    if engine is None:
        return OCR_UNAVAILABLE_PREFIX + reason

    try:
        page_iter = _page_images(file_path, first_page, last_page, cancel)
        if engine == "vision":
            from vaf.core.config import Config
            budget = int(Config.get("ocr_vision_max_pages_per_call", 10) or 10)
            return _ocr_pages_vision(page_iter, cancel, max(1, budget))
        return _ocr_pages_tesseract(page_iter, cancel)
    except Exception as e:
        # Tesseract binary vanished mid-run, an unreadable stream, ... Name the
        # class: cost was already paid, the user must not be told the document
        # was empty. (TesseractNotFoundError subclasses OSError and lands here
        # by design now - resolve_ocr_engine probes the binary up front, so
        # reaching this means it disappeared between probe and use.)
        return OCR_UNAVAILABLE_PREFIX + f"{e.__class__.__name__}: {e}"


def extract_pdf_markdown(
    file_path,
    max_pages: Optional[int] = None,
    ocr_fallback: bool = True,
    *,
    first_page: int = 1,
    cancel: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    Extract a PDF as Markdown (headings + tables).

    `max_pages=None` reads ALL pages (every in-repo caller passes an explicit
    budget; the library default is the whole document). `first_page` is 1-based
    and makes slice reads ("pages 100-120") possible; markers stay absolute.
    `cancel` is polled once per page; `None` wires it to
    `bounded_run.cancel_requested`, which reaches the extraction thread on every
    in-tool lane - a background job passes its own check (the thread-local
    default never fires on a bare thread or in a child process).

    Returns a dict; new consumers should read the honest fields, old keys stay:
      markdown        extracted text ("" possible)
      total_pages     pages in the DOCUMENT
      pages_read      pages actually consumed (cancel-aware)
      first_page      echo of the requested start (clamped >= 1)
      truncated       True when the caller did NOT see the whole document
      used_ocr/method as before; num_pages == total_pages (backward compat)
      ocr_unavailable_reason  "" unless OCR was needed but could not run

    Raises ImportError (with the `vaf[pdf]` remedy named) only when BOTH
    extraction engines are missing.
    """
    file_path = Path(file_path)
    first_page = max(1, int(first_page or 1))
    if cancel is None:
        try:
            from vaf.core.bounded_run import cancel_requested as cancel
        except Exception:
            cancel = None

    method = "pdfplumber"
    try:
        markdown, total_pages, pages_read = _extract_pdfplumber(
            file_path, max_pages, first_page=first_page, cancel=cancel
        )
    except Exception:
        method = "pypdf2"
        try:
            markdown, total_pages, pages_read = _extract_pypdf2(
                file_path, max_pages, first_page=first_page, cancel=cancel
            )
        except ImportError as exc:
            raise ImportError(
                "PDF support is not installed: pip install 'vaf[pdf]' (pdfplumber + PyPDF2)"
            ) from exc

    used_ocr = False
    ocr_unavailable_reason = ""
    content = _content_chars(markdown)
    if (
        ocr_fallback
        and content < _MIN_TEXT_CHARS
        and total_pages > 0
        and not (cancel is not None and cancel())
    ):
        budget = max(0, total_pages - (first_page - 1))
        if max_pages is not None:
            budget = min(budget, max_pages)
        if budget > 0:
            ocr_text = pdf_ocr_fallback(
                file_path, budget, first_page=first_page, cancel=cancel
            )
            if ocr_text.startswith(OCR_UNAVAILABLE_PREFIX):
                ocr_unavailable_reason = ocr_text[len(OCR_UNAVAILABLE_PREFIX):]
            elif _content_chars(ocr_text) > content:
                markdown, method, used_ocr = ocr_text, "ocr", True
                # Named approximation: OCR covered the requested slice. A
                # mid-OCR cancel may overcount trailing pages here.
                pages_read = budget

    truncated = not (first_page == 1 and pages_read >= total_pages)
    return {
        "markdown": markdown or "",
        "num_pages": total_pages,   # backward-compat alias
        "used_ocr": used_ocr,
        "method": method,
        "total_pages": total_pages,
        "pages_read": pages_read,
        "first_page": first_page,
        "truncated": truncated,
        "ocr_unavailable_reason": ocr_unavailable_reason,
    }


def format_pdf_read_result(
    res: Dict[str, Any],
    *,
    file_name: str,
    char_cap: int = _PDF_RESULT_CHAR_CAP,
) -> str:
    """Render an extract_pdf_markdown result for a read tool - honestly.

    The facts (page range covered, how to continue) come FIRST so they survive
    the chat lane's 2000-char tool-result cut; the char cap notice names the
    page it cut into instead of a bare "(truncated)". This is the ONE rendering
    of a PDF read: read_file and the librarian used to carry byte-identical
    hand-rolled 15k truncations whose messages guessed ("no embedded text?")
    where the extractor already knew.
    """
    md = (res.get("markdown") or "").strip()
    total = int(res.get("total_pages") or res.get("num_pages") or 0)
    read = int(res.get("pages_read") or 0)
    first = int(res.get("first_page") or 1)
    last = first + read - 1 if read > 0 else 0

    header = f"### PDF: {file_name}\n**Pages:** "
    header += f"{first}-{last} of {total}" if read > 0 else f"0 of {total}"
    if res.get("used_ocr"):
        header += " (OCR)"
    lines: List[str] = [header]

    if read > 0 and last < total:
        lines.append(
            f"[Pages {last + 1}-{total} not included - call read_file with "
            f"first_page={last + 1} (and optional last_page) to continue.]"
        )

    if not md:
        reason = (res.get("ocr_unavailable_reason") or "").strip()
        if read == 0 and total > 0 and first > total:
            lines.append(
                f"[Requested pages start at {first} but the document has only {total} pages.]"
            )
        elif reason:
            lines.append(f"[No text layer detected. OCR unavailable: {reason}]")
        else:
            lines.append("[No readable text found in these pages.]")
        return "\n".join(lines)

    capped = bool(char_cap) and len(md) > char_cap
    body = md[:char_cap] if capped else md
    lines.append(body)
    if capped:
        markers = _PAGE_MARKER_RE.findall(body)
        cut_page = int(markers[-1].split()[2]) if markers else first
        lines.append(
            f"[Output capped at {char_cap:,} characters inside page {cut_page} - "
            f"continue with first_page={cut_page}.]"
        )
    return "\n".join(lines)
