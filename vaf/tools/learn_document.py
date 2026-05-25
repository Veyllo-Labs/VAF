"""
learn_document: Learn a document into long-term memory (RAG).
Splits by page/section, runs LLM extraction per part (Variant B), stores each as one memory
with type=document and a single document tag (e.g. doc-tora).
"""
import os
import re
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse
from urllib.request import url2pathname

from vaf.tools.base import BaseTool


def _path_from_string(path_str: str) -> Path:
    """Accept a path or file:// URL and return a resolved Path (cross-platform)."""
    s = (path_str or "").strip()
    if not s:
        return Path(".")
    if s.lower().startswith("file://"):
        s = url2pathname(urlparse(s).path)
    return Path(s).resolve()


def _is_path_allowed(file_path: Path) -> bool:
    """True if path is under an allowed root (home, cwd, VAF data/vaf dirs)."""
    from vaf.core.platform import Platform
    try:
        real = file_path.resolve()
        roots = [
            Path.home(),
            Path(os.getcwd()),
            Platform.data_dir(),
            Platform.vaf_dir(),
        ]
        for root in roots:
            if not root.exists():
                continue
            try:
                root_resolved = root.resolve()
                if real == root_resolved or str(real).startswith(str(root_resolved) + os.sep):
                    return True
            except (OSError, ValueError):
                continue
    except (OSError, ValueError):
        pass
    return False


def _normalize_doc_tag(title: str) -> str:
    """Build document tag: lowercase, alphanumeric and hyphen only (e.g. 'doc-tora')."""
    s = (title or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return f"doc-{s}" if s else "doc-untitled"


# Max chars per page/section to send to LLM (avoid token overflow)
_EXTRACTION_INPUT_MAX_CHARS = 4000


def _split_pdf(path: Path, max_pages: int) -> List[Tuple[int, str]]:
    """Yield (page_num_1based, text) for each PDF page. Skips empty pages."""
    import PyPDF2
    from vaf.core.config import Config
    out = []
    config = Config.load()
    with open(path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        total = len(reader.pages)
        limit = min(total, max_pages)
        for i in range(limit):
            text = (reader.pages[i].extract_text() or "").strip()
            if text:
                if len(text) > _EXTRACTION_INPUT_MAX_CHARS:
                    text = text[:_EXTRACTION_INPUT_MAX_CHARS] + "\n... [truncated]"
                out.append((i + 1, text))
    return out


def _split_txt_md(path: Path, max_sections: int) -> List[Tuple[int, str]]:
    """Split TXT/MD by form feed, or by ##/# headers, or by fixed size. Returns (section_index, text)."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    parts = []

    if "\f" in raw:
        for i, block in enumerate(raw.split("\f")):
            if i >= max_sections:
                break
            text = block.strip()
            if text:
                if len(text) > _EXTRACTION_INPUT_MAX_CHARS:
                    text = text[:_EXTRACTION_INPUT_MAX_CHARS] + "\n... [truncated]"
                parts.append((i + 1, text))
    else:
        # Split by markdown headers: ## or #
        header_re = re.compile(r"^(?:#{1,6})\s+.+$", re.MULTILINE)
        matches = list(header_re.finditer(raw))
        if len(matches) >= 2:
            for i in range(len(matches)):
                if i >= max_sections:
                    break
                start = matches[i].start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
                text = raw[start:end].strip()
                if text:
                    if len(text) > _EXTRACTION_INPUT_MAX_CHARS:
                        text = text[:_EXTRACTION_INPUT_MAX_CHARS] + "\n... [truncated]"
                    parts.append((i + 1, text))
        else:
            # Fixed-size chunks
            chunk_size = 3500
            for i in range(0, len(raw), chunk_size):
                if len(parts) >= max_sections:
                    break
                text = raw[i : i + chunk_size].strip()
                if text:
                    if len(text) > _EXTRACTION_INPUT_MAX_CHARS:
                        text = text[:_EXTRACTION_INPUT_MAX_CHARS] + "\n... [truncated]"
                    parts.append((len(parts) + 1, text))
    return parts


def _merge_thin_pages(pages: List[Tuple[int, str]], min_chars: int = 80) -> List[Tuple[int, str]]:
    """Merge consecutive pages with fewer than min_chars of stripped text into the next page."""
    if not pages:
        return pages
    result: List[Tuple[int, str]] = []
    pending_num: int | None = None
    pending_text: str = ""
    for page_num, text in pages:
        stripped = text.strip()
        if pending_text:
            combined = pending_text + "\n\n" + stripped if stripped else pending_text
            if len(stripped) < min_chars:
                pending_text = combined
            else:
                result.append((pending_num, combined))
                pending_num = None
                pending_text = ""
        else:
            if len(stripped) < min_chars:
                pending_num = page_num
                pending_text = stripped
            else:
                result.append((page_num, text))
    if pending_text:
        if result:
            prev_num, prev_text = result[-1]
            result[-1] = (prev_num, prev_text + "\n\n" + pending_text)
        else:
            result.append((pending_num, pending_text))
    return result


_ANALYSIS_PROMPT_TEMPLATE = (
    "You are a document analysis assistant. Given the pages of a document below, "
    "produce a JSON object with these keys:\n"
    '  "doc_summary": a 2-sentence overview of the entire document,\n'
    '  "doc_tags": a list of 5 to 8 lowercase semantic tags describing the document topics,\n'
    '  "pages": a list of objects, one per page, each with:\n'
    '      "page": the page number (integer),\n'
    '      "title": a descriptive 5-10 word title for that page,\n'
    '      "content": 1-3 sentences capturing the key facts of that page.\n'
    "Output ONLY the JSON object. Do not include any explanation or markdown fences.\n\n"
    "=== Document: {doc_title} ===\n\n"
    "{pages_block}"
)


def _analyze_document_llm(
    pages: List[Tuple[int, str]],
    doc_title: str,
    generate_fn,
    preview_chars: int = 400,
) -> dict:
    """Make one LLM call to analyze all pages. Returns parsed dict or {} on any failure."""
    if not pages or generate_fn is None:
        return {}
    pages_block_parts = []
    for page_num, text in pages:
        preview = text.strip()[:preview_chars]
        pages_block_parts.append(f"--- Page {page_num} ---\n{preview}")
    pages_block = "\n\n".join(pages_block_parts)
    prompt = _ANALYSIS_PROMPT_TEMPLATE.format(doc_title=doc_title, pages_block=pages_block)
    try:
        raw = generate_fn(prompt)
    except Exception:
        return {}
    raw = (raw or "").strip()
    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()
    try:
        import json
        return json.loads(raw)
    except Exception:
        return {}


def _run_async_in_new_loop(coro):
    """Run a coroutine in a new thread with its own event loop."""
    import asyncio
    import threading
    result = [None]
    exception = [None]

    def _thread_run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result[0] = loop.run_until_complete(coro)
        except Exception as e:
            exception[0] = e
        finally:
            loop.close()

    t = threading.Thread(target=_thread_run)
    t.start()
    t.join()
    if exception[0]:
        raise exception[0]
    return result[0]


EXTRACTION_PROMPT_TEMPLATE = """Extract the key facts and knowledge from the following page. Output only the extracted knowledge in one concise paragraph or short bullet list. No preamble or meta-commentary.

--- Page ---
{page_text}
"""


class LearnDocumentTool(BaseTool):
    name = "learn_document"
    permission_level = "write"
    side_effect_class = "irreversible"
    description = (
        "Learn a document into long-term memory. Use when the user wants you to "
        "'learn', 'remember', or 'ingest' a document (PDF, TXT, MD) so you can answer "
        "questions about it later. Pass the full file path; optionally give a document_title "
        "(e.g. 'Tora') for the tag. Each page/section is summarized by the model and stored."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Full path to the document (PDF, .txt, or .md)."},
            "document_title": {"type": "string", "description": "Optional short title for the document (e.g. 'Tora'). Used as tag doc-<title>. If omitted, derived from filename."},
        },
        "required": ["path"],
    }

    def run(self, **kwargs) -> str:
        path_str = (kwargs.get("path") or "").strip()
        if not path_str:
            return "Error: path is required."
        document_title = (kwargs.get("document_title") or "").strip() or None
        user_scope_id = kwargs.get("user_scope_id")
        agent = kwargs.get("_agent")

        if agent is None:
            return "Error: learn_document requires the agent (internal error: _agent not set)."

        path = _path_from_string(path_str)
        if not path.exists():
            return f"Error: File not found: {path}"
        if not path.is_file():
            return f"Error: Not a file: {path}"
        if not _is_path_allowed(path):
            return "Error: Path is outside allowed directories (home, cwd, or VAF data)."

        from vaf.core.config import Config
        from uuid import UUID

        config = Config.load()
        max_pages = int(config.get("learn_document_max_pages", 200) or 200)
        suffix = path.suffix.lower()

        if document_title is None:
            document_title = path.stem or "document"
        doc_tag = _normalize_doc_tag(document_title)

        if user_scope_id is not None and isinstance(user_scope_id, str):
            try:
                user_scope_id = UUID(user_scope_id)
            except (ValueError, TypeError):
                user_scope_id = None

        parts: List[Tuple[int, str]] = []
        if suffix == ".pdf":
            try:
                parts = _split_pdf(path, max_pages)
            except ImportError:
                return "Error: PDF support not installed. Run: pip install PyPDF2"
            except Exception as e:
                return f"Error reading PDF: {e}"
        elif suffix in (".txt", ".md"):
            try:
                parts = _split_txt_md(path, max_pages)
            except Exception as e:
                return f"Error reading file: {e}"
        else:
            return f"Error: Unsupported format. Use .pdf, .txt, or .md (got {suffix})."

        parts = _merge_thin_pages(parts)

        if not parts:
            return "Error: No text could be extracted from the document (empty or unsupported)."

        # Prefer dedicated method if present, else compaction with lower max_tokens
        if hasattr(agent, "_generate_for_document_extraction"):
            generate = agent._generate_for_document_extraction
        else:
            def generate(prompt: str) -> str:
                return agent._generate_for_compaction(prompt)

        analysis = _analyze_document_llm(parts, document_title, generate)
        doc_summary = (analysis.get("doc_summary") or "").strip()
        llm_tags = [str(t).strip().lower() for t in (analysis.get("doc_tags") or []) if str(t).strip()]
        page_analysis = {
            int(p.get("page", 0)): p
            for p in (analysis.get("pages") or [])
            if isinstance(p, dict) and p.get("page")
        }

        stored = 0
        for page_num, text in parts:
            pa = page_analysis.get(page_num, {})
            page_title = (pa.get("title") or "").strip() or f"{document_title} \u2013 Page {page_num}"
            ctx = f"Document context: {doc_summary}\n\n" if doc_summary else ""
            prompt = ctx + EXTRACTION_PROMPT_TEMPLATE.format(page_text=text)
            try:
                extraction = generate(prompt)
            except Exception as e:
                return f"Error during extraction (page {page_num}): {e}"
            extraction = (extraction or "").strip()
            if not extraction:
                continue

            _page_title = page_title
            _page_num = page_num

            async def _ingest_one(
                _extraction=extraction,
                _page_title=_page_title,
                _page_num=_page_num,
            ) -> None:
                from vaf.memory.database import get_db
                from vaf.memory.rag import RagPipeline
                async with get_db() as db:
                    pipeline = RagPipeline(db)
                    await pipeline.ingest(
                        content=_extraction,
                        metadata={
                            "type": "document",
                            "tags": list(dict.fromkeys([doc_tag] + llm_tags)),
                            "source": "learn_document",
                            "title": _page_title,
                            "page": _page_num,
                            "doc_tag": doc_tag,
                        },
                        user_scope_id=user_scope_id,
                        auto_connect=False,
                    )

            try:
                _run_async_in_new_loop(_ingest_one())
                stored += 1
            except Exception as e:
                return f"Error storing memory (page {page_num}): {e}"

        if stored > 0:
            async def _ingest_index() -> None:
                from sqlalchemy import select, and_
                from vaf.memory.database import get_db
                from vaf.memory.rag import RagPipeline
                from vaf.memory.models import Memory
                async with get_db() as db:
                    conditions = [
                        Memory.is_deleted == False,
                        Memory.meta["type"].as_string() == "document_index",
                        Memory.meta["doc_tag"].as_string() == doc_tag,
                    ]
                    if user_scope_id is not None:
                        conditions.append(Memory.user_scope_id == user_scope_id)
                    result = await db.execute(select(Memory).where(and_(*conditions)))
                    existing = result.scalar_one_or_none()

                    if existing is not None:
                        meta = dict(existing.meta or {})
                        meta["page_count"] = stored
                        if doc_summary:
                            meta["doc_summary"] = doc_summary
                        existing.meta = meta
                    else:
                        index_content = f"Document index: {document_title}."
                        if doc_summary:
                            index_content += f" {doc_summary}"
                        index_content += f" Contains {stored} pages of extracted knowledge."
                        index_tags = list(dict.fromkeys([doc_tag] + llm_tags))
                        index_meta: dict = {
                            "type": "document_index",
                            "tags": index_tags,
                            "source": "learn_document",
                            "title": document_title,
                            "doc_tag": doc_tag,
                            "page_count": stored,
                        }
                        if doc_summary:
                            index_meta["doc_summary"] = doc_summary
                        pipeline = RagPipeline(db)
                        await pipeline.ingest(
                            content=index_content,
                            metadata=index_meta,
                            user_scope_id=user_scope_id,
                            auto_connect=False,
                        )

            try:
                _run_async_in_new_loop(_ingest_index())
            except Exception as e:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    f"learn_document: failed to create document_index for {doc_tag}: {e}"
                )

        return f"Stored {stored} pages from \u00ab{document_title}\u00bb under tag {doc_tag}."
