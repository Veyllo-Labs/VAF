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
    """
    Learn a document into long-term memory (RAG). Reads the file, splits by page or section,
    runs a short LLM extraction per part, and stores each extraction as one memory with
    type=document and a single document tag so the agent can recall the document later.
    """

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
            "path": {
                "type": "string",
                "description": "Full path to the document (PDF, .txt, or .md).",
            },
            "document_title": {
                "type": "string",
                "description": "Optional short title for the document (e.g. 'Tora'). Used as tag doc-<title>. If omitted, derived from filename.",
            },
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

        if not parts:
            return "Error: No text could be extracted from the document (empty or unsupported)."

        # Prefer dedicated method if present, else compaction with lower max_tokens
        if hasattr(agent, "_generate_for_document_extraction"):
            generate = agent._generate_for_document_extraction
        else:
            def generate(prompt: str) -> str:
                return agent._generate_for_compaction(prompt)

        stored = 0
        for page_num, text in parts:
            prompt = EXTRACTION_PROMPT_TEMPLATE.format(page_text=text)
            try:
                extraction = generate(prompt)
            except Exception as e:
                return f"Error during extraction (page {page_num}): {e}"
            extraction = (extraction or "").strip()
            if not extraction:
                continue

            async def _ingest_one() -> None:
                from vaf.memory.database import get_db
                from vaf.memory.rag import RagPipeline
                async with get_db() as db:
                    pipeline = RagPipeline(db)
                    await pipeline.ingest(
                        content=extraction,
                        metadata={
                            "type": "document",
                            "tags": [doc_tag],
                            "source": "learn_document",
                            "title": f"{document_title} – Page {page_num}",
                            "page": page_num,
                        },
                        user_scope_id=user_scope_id,
                        auto_connect=False,
                    )

            try:
                _run_async_in_new_loop(_ingest_one())
                stored += 1
            except Exception as e:
                return f"Error storing memory (page {page_num}): {e}"

        # Create (or update) one root "document index" memory so the whole document
        # appears as a single deletable unit in the Memory UI (type=document_index, amber node).
        # If an index with the same doc_tag already exists (re-ingest), update it in place
        # instead of creating a duplicate.
        if stored > 0:
            async def _ingest_index() -> None:
                from sqlalchemy import select, and_
                from vaf.memory.database import get_db
                from vaf.memory.rag import RagPipeline
                from vaf.memory.models import Memory
                async with get_db() as db:
                    # Check for existing document_index with same doc_tag
                    conditions = [
                        Memory.is_deleted == False,  # noqa: E712
                        Memory.meta["doc_tag"].as_string() == doc_tag,
                    ]
                    if user_scope_id is not None:
                        conditions.append(Memory.user_scope_id == user_scope_id)
                    result = await db.execute(select(Memory).where(and_(*conditions)))
                    existing = result.scalar_one_or_none()

                    if existing is not None:
                        # Update page_count in place — no duplicate node
                        meta = dict(existing.meta or {})
                        meta["page_count"] = stored
                        existing.meta = meta
                    else:
                        pipeline = RagPipeline(db)
                        await pipeline.ingest(
                            content=(
                                f"Document index: {document_title}. "
                                f"Contains {stored} pages of extracted knowledge."
                            ),
                            metadata={
                                "type": "document_index",
                                "tags": [doc_tag],
                                "source": "learn_document",
                                "title": document_title,
                                "doc_tag": doc_tag,
                                "page_count": stored,
                            },
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

        return f"Stored {stored} pages from «{document_title}» under tag {doc_tag}."
