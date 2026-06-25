# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
learn_document: Learn a document into long-term memory (RAG).
Extracts clean Markdown, splits it into sections, and for each section makes one LLM call that
produces a contextual summary (used as the memory title / embedding key) prepended to the section
text. Stores one memory per section (type=document) plus a single document_index root, all under one
document tag (e.g. doc-tora). Shared with learn_attached_knowledge via ingest_document_knowledge().
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


# ── Section-based contextual ingestion (shared by learn_document + learn_attached_knowledge) ──
# Best-practice RAG: clean markdown -> structure-aware sections -> one focused LLM call per FULL
# section that produces a self-explanatory "context" (which becomes the Memory title, i.e. the
# embedding key in RagPipeline.ingest) -> store context + section text. doc_summary lives only in the
# document_index root, never glued onto every unit.

_MAX_DOC_CHARS = 16000

_SECTION_CONTEXT_PROMPT = (
    'Summarize this section of the document "{doc_title}" for a knowledge base. '
    "Write 2-4 plain-text sentences: first what this section is about, then its key facts, so it is "
    "understandable on its own. No preamble, no markdown, no JSON -- just the sentences.\n\n"
    "Section heading: {section_title}\n\n{section_text}"
)


def _strip_json_fences(raw: str) -> str:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _strip_think(text: str) -> str:
    """Remove reasoning-model <think>...</think> blocks. If an unclosed <think> remains (output was
    truncated mid-reasoning), drop everything from it on -- so reasoning never leaks into stored text."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE)
    if "<think>" in t.lower():
        t = re.split(r"(?i)<think>", t)[0]
    return t.strip()


def _strip_librarian_wrapper(md: str) -> str:
    """Remove the Librarian's '### PDF: <name>\\n**Pages:** N' header (and the temp filename it carries)
    from the very start, so the tool wrapper is not learned as document content."""
    return re.sub(
        r"\A\s*###\s+\w[\w .-]*:[^\n]*\n(?:\*\*Pages:\*\*[^\n]*\n)?\s*",
        "", md or "", count=1,
    )


def _clean_title(name) -> str:
    """Strip file extensions and '-compressed' noise from a filename so it reads as a title."""
    t = (name or "").strip()
    for _ in range(4):
        before = t
        t = re.sub(r"(?i)[ _-]*compressed$", "", t).strip()
        t = re.sub(r"(?i)\.(pdf|docx?|pptx?|txt|md|csv|xlsx?|odt|odp|ods|rtf)$", "", t).strip()
        if t == before:
            break
    return t or (name or "").strip()


def _contextualize_section_llm(section_text, section_title, doc_title, generate_fn, max_chars: int = 6000) -> str:
    """One PLAIN-TEXT LLM call over the FULL section -> a 2-4 sentence self-contained summary.
    No JSON required (robust for any model). Never raises; falls back to a clean section label
    (never the raw section text)."""
    fallback = (
        f"{section_title} — from {doc_title}."
        if section_title and section_title != doc_title else f"Section of {doc_title}."
    )
    if not (section_text or "").strip() or generate_fn is None:
        return fallback
    prompt = _SECTION_CONTEXT_PROMPT.format(
        doc_title=doc_title, section_title=section_title, section_text=(section_text or "")[:max_chars]
    )
    try:
        out = _strip_json_fences(_strip_think(generate_fn(prompt) or ""))
        out = re.sub(r"^\s*(context|summary)\s*[:\-]\s*", "", out, flags=re.I)
        out = " ".join(out.split())  # collapse newlines/whitespace
        return out[:600] if len(out) >= 15 else fallback
    except Exception:
        return fallback


def _summarize_doc_from_contexts(contexts, doc_title, generate_fn) -> tuple:
    """Doc-level (doc_summary, doc_tags) from the per-section contexts (one call). Never raises."""
    joined = "\n\n".join(contexts)[:8000]
    fallback = ((contexts[0].strip()[:300] if contexts else ""), [])
    if not joined.strip() or generate_fn is None:
        return fallback
    prompt = (
        "Given these section summaries of a document, output a JSON object with "
        '"doc_summary" (2-sentence overview) and "doc_tags" (5-8 lowercase tags). Output ONLY JSON.\n\n'
        f"=== Document: {doc_title} ===\n\n{joined}"
    )
    try:
        import json
        d = json.loads(_strip_json_fences(_strip_think(generate_fn(prompt) or "")))
        summary = str(d.get("doc_summary") or "").strip() or fallback[0]
        tags = [str(t).strip().lower() for t in (d.get("doc_tags") or []) if str(t).strip()]
        return (summary, tags)
    except Exception:
        return fallback


async def ingest_document_knowledge(
    db,
    *,
    content_markdown: str,
    doc_title: str,
    doc_tag: str,
    source: str,
    mem_type: str,
    generate_fn,
    user_scope_id,
    extra_tags=None,
    attachment_name=None,
    session_id=None,
) -> dict:
    """Section-based, contextual ingestion of one document into long-term memory.

    Returns {"created": int, "sections": int, "doc_summary": str, "doc_tags": [str]}.
    """
    from sqlalchemy import select, and_
    from vaf.memory.models import Memory
    from vaf.memory.rag import RagPipeline
    from vaf.memory.attachment_rag import _split_into_sections
    from vaf.core.config import Config
    try:
        from vaf.core.log_helper import append_domain_log
    except Exception:  # pragma: no cover
        def append_domain_log(*_a, **_k):
            return None

    extra_tags = [str(t).strip().lower() for t in (extra_tags or []) if str(t).strip()]
    origin = "attachment" if attachment_name else "document"
    pipeline = RagPipeline(db)

    content_markdown = _strip_librarian_wrapper(content_markdown or "")
    sections = _split_into_sections(content_markdown, 500, 5000)
    if len(sections) < 2:
        sections = [{"title": doc_title, "text": (content_markdown or "")[:_MAX_DOC_CHARS], "index": 0}]
    max_sections = max(2, min(80, int(Config.get("learn_max_sections", 40) or 40)))
    sections = sections[:max_sections]

    doc_title = _clean_title(doc_title)

    # Pass 1: a plain-text contextual summary per section (robust for any model).
    items = []  # (section_index, section_title, section_text, context)
    contexts = []
    for sec in sections:
        sec_text = (sec.get("text") or "").strip()
        if not sec_text:
            continue
        sec_title = (sec.get("title") or doc_title).strip()
        context = _contextualize_section_llm(sec_text, sec_title, doc_title, generate_fn)
        contexts.append(context)
        items.append((sec.get("index", len(items)), sec_title, sec_text, context))

    # Doc-level summary + tags from the clean section contexts (applied to every section + the root).
    doc_summary, doc_tags = _summarize_doc_from_contexts(contexts, doc_title, generate_fn)

    # Pass 2: ingest each section -- the context summary is the title (embedding key) + body prefix.
    created = 0
    for sec_index, sec_title, sec_text, context in items:
        all_tags = list(dict.fromkeys([doc_tag, "knowledge", f"from-{origin}"] + doc_tags + extra_tags))
        meta = {
            "title": context,  # drives Memory.embedding (rag.py) -> contextual retrieval key
            "type": mem_type,
            "source": source,
            "knowledge_origin": origin,
            "doc_tag": doc_tag,
            "section_title": sec_title,
            "section_index": sec_index,
            "tags": all_tags,
        }
        if attachment_name:
            meta["attachment_name"] = attachment_name
        if session_id:
            meta["attachment_session_id"] = session_id
        body = f"{context}\n\n{sec_text}"
        await pipeline.ingest(content=body, metadata=meta, auto_connect=True, user_scope_id=user_scope_id)
        created += 1
        append_domain_log("memory", (
            f"[LEARN] store section {sec_index} '{doc_title}': "
            f"title={context[:70]!r} tags={all_tags} chars={len(body)}"
        ))

    # document_index root -- created/updated exactly once (doc_summary lives here, not per section).
    if created > 0:
        conditions = [
            Memory.is_deleted == False,  # noqa: E712
            Memory.meta["type"].as_string() == "document_index",
            Memory.meta["doc_tag"].as_string() == doc_tag,
        ]
        if user_scope_id is not None:
            conditions.append(Memory.user_scope_id == user_scope_id)
        existing_root = (await db.execute(select(Memory).where(and_(*conditions)))).scalar_one_or_none()
        if existing_root is not None:
            meta_upd = dict(existing_root.meta or {})
            meta_upd["page_count"] = created
            if doc_summary:
                meta_upd["doc_summary"] = doc_summary
            existing_root.meta = meta_upd
        else:
            index_content = f"Document index: {doc_title}."
            if doc_summary:
                index_content += f" {doc_summary}"
            index_content += f" Contains {created} section(s) of knowledge from a {origin}."
            index_tags = list(dict.fromkeys([doc_tag, f"from-{origin}"] + doc_tags + extra_tags))
            index_meta = {
                "type": "document_index",
                "source": source,
                "title": doc_title,
                "doc_tag": doc_tag,
                "page_count": created,
                "tags": index_tags,
            }
            if doc_summary:
                index_meta["doc_summary"] = doc_summary
            await pipeline.ingest(content=index_content, metadata=index_meta,
                                  user_scope_id=user_scope_id, auto_connect=False)
        append_domain_log("memory", (
            f"[LEARN] doc-index '{doc_title}' doc_tag={doc_tag} sections={created} "
            f"summary={doc_summary[:80]!r}"
        ))

    return {"created": created, "sections": len(sections), "doc_summary": doc_summary, "doc_tags": doc_tags}


class LearnDocumentTool(BaseTool):
    name = "learn_document"
    permission_level = "write"
    side_effect_class = "irreversible"
    description = (
        "Learn a document into long-term memory. Use when the user wants you to "
        "'learn', 'remember', or 'ingest' a document (PDF, TXT, MD) so you can answer "
        "questions about it later. Pass the full file path; optionally give a document_title "
        "(e.g. 'Tora') for the tag. The document is split into sections; each section is summarized "
        "for context and stored as one memory under the document tag."
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
            document_title = _clean_title(path.stem or "document")
        doc_tag = _normalize_doc_tag(document_title)

        if user_scope_id is not None and isinstance(user_scope_id, str):
            try:
                user_scope_id = UUID(user_scope_id)
            except (ValueError, TypeError):
                user_scope_id = None

        # Extract clean markdown (headings/tables); the shared section-based ingestion then handles
        # sectioning + per-section contextual summaries + storage (one consistent pipeline).
        if suffix == ".pdf":
            try:
                from vaf.core.pdf_extract import extract_pdf_markdown
                content_markdown = (extract_pdf_markdown(path, max_pages=max_pages) or {}).get("markdown", "")
            except ImportError:
                return "Error: PDF support not installed. Run: pip install pdfplumber PyPDF2"
            except Exception as e:
                return f"Error reading PDF: {e}"
        elif suffix in (".txt", ".md"):
            try:
                content_markdown = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return f"Error reading file: {e}"
        else:
            return f"Error: Unsupported format. Use .pdf, .txt, or .md (got {suffix})."

        if not (content_markdown or "").strip():
            return "Error: No text could be extracted from the document (empty or unsupported)."

        # Prefer dedicated extraction method if present, else compaction.
        if hasattr(agent, "_generate_for_document_extraction"):
            generate = agent._generate_for_document_extraction
        else:
            def generate(prompt: str) -> str:
                return agent._generate_for_compaction(prompt)

        result: dict = {}

        async def _do_ingest() -> None:
            from vaf.memory.database import get_db
            async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
                result.update(await ingest_document_knowledge(
                    db,
                    content_markdown=content_markdown,
                    doc_title=document_title,
                    doc_tag=doc_tag,
                    source="learn_document",
                    mem_type="document",
                    generate_fn=generate,
                    user_scope_id=user_scope_id,
                ))

        try:
            _run_async_in_new_loop(_do_ingest())
        except Exception as e:
            return f"Error: Failed to learn document: {e}"

        created = int(result.get("created", 0))
        if created <= 0:
            return "No knowledge memories were created (document may be empty or unreadable)."
        return f'Stored {created} knowledge section(s) from "{document_title}" under tag {doc_tag}.'
