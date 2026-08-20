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


def _normalize_doc_tag(title: str) -> str:
    """Build document tag: lowercase, alphanumeric and hyphen only (e.g. 'doc-tora')."""
    s = (title or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return f"doc-{s}" if s else "doc-untitled"


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
    section_offset: int = 0,
    update_root: bool = True,
) -> dict:
    """Section-based, contextual ingestion of one document into long-term memory.

    Returns {"created": int, "sections": int, "doc_summary": str, "doc_tags": [str],
    "sections_total": int, "sections_dropped": int} - the last two exist so a
    caller can report a firing learn_max_sections cap as "X of Y" instead of a
    bare success count.

    Batch mode (the learn job feeds one PAGE RANGE per call): `section_offset`
    keeps `section_index` globally monotonic across batches - it is the resume
    key `delete_document_sections` cuts on, so per-batch indices restarting at
    0 would make crash orphans indistinguishable from batch 1. `update_root=False`
    skips the document_index upsert; the job writes ONE root at finalize,
    because a per-batch upsert would stamp the LAST batch's count and summary
    as the document's. Defaults preserve single-call behavior byte-identically.
    """
    from sqlalchemy import select, and_
    from vaf.memory.models import Memory
    from vaf.memory.rag import RagPipeline
    from vaf.memory.attachment_rag import _split_into_sections, is_toc_section
    from vaf.core.config import Config
    # Not BaseTool.log(): this is a module-level helper shared by two tools, so there is no
    # self to name - and the ingest lines belong in memory_*.log next to the rest of the RAG
    # trail, which a fixed "tools" domain could not give them.
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
    # 0 = store ALL sections (the deliberate default). The old code
    # hard-clamped even an explicit config raise to 80 and silently dropped the
    # tail - 40 of a 1000-page book's ~1000 sections were kept and the reply
    # still said success. A positive value is an opt-in spend cap; when it
    # bites, the caller reports "X of Y" (sections_total / sections_dropped in
    # the return dict).
    sections_total = len(sections)
    max_sections = int(Config.get("learn_max_sections", 0) or 0)
    if max_sections > 0:
        sections = sections[:max_sections]
    sections_dropped = sections_total - len(sections)

    doc_title = _clean_title(doc_title)

    # Pass 1: a plain-text contextual summary per section (robust for any model).
    items = []  # (section_index, section_title, section_text, context)
    contexts = []
    toc_titles = []
    for sec in sections:
        sec_text = (sec.get("text") or "").strip()
        if not sec_text:
            continue
        sec_title = (sec.get("title") or doc_title).strip()
        if is_toc_section(sec_title, sec_text):
            # Filler, not knowledge - skipping BEFORE the LLM call saves the
            # spend and the Memory row. Counted separately from the cap drop.
            toc_titles.append(sec_title)
            append_domain_log("memory", (
                f"[LEARN] skip ToC section '{sec_title[:60]}' ({len(sec_text)} chars)"))
            continue
        context = _contextualize_section_llm(sec_text, sec_title, doc_title, generate_fn)
        contexts.append(context)
        # Dense index (len(items), NOT sec["index"]): a skipped section must not
        # leave a hole - the ledger's section_count is the resume cut line and
        # the next batch continues at section_offset + created.
        items.append((section_offset + len(items), sec_title, sec_text, context))

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
    if created > 0 and update_root:
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

    return {"created": created, "sections": len(sections), "doc_summary": doc_summary,
            "doc_tags": doc_tags, "sections_total": sections_total,
            "sections_dropped": sections_dropped,
            "sections_skipped_toc": len(toc_titles), "toc_titles": toc_titles[:10]}


class LearnDocumentTool(BaseTool):
    name = "learn_document"
    category    = "memory"
    identity_kwargs = ("user_scope_id", "user_role")
    file_access = "read"
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
            "resume": {"type": "boolean", "description": "Continue an interrupted learn of the same document where it stopped (default true). The stored progress is per document and user."},
            "force_relearn": {"type": "boolean", "description": "Discard the previously learned sections of this document and learn it again from scratch (default false). Required when the file changed since it was learned."},
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

        # ONE authority for "may this be read", and it is the same one every other file tool
        # uses. This tool used to carry its own, `_is_path_allowed`, which allowed anything
        # under the home directory, the cwd, the data dir and VAF's own directory - measured,
        # it said yes to ~/.ssh/id_rsa, ~/.env, ~/.vaf/config.json and ~/.vaf/secrets/, all of
        # which `is_safe_path` refuses. A second, weaker policy is worse than none: it reads
        # like a check.
        #
        # What made it sharp here rather than merely wrong: this tool does not display a file,
        # it INGESTS it. The content is chunked, summarised and written into long-term memory
        # under a scope, and is searchable afterwards. A credential pulled in this way outlives
        # the session, the chat and any cleanup - which is the exact reasoning that closed
        # ~/.vaf to the file tools in the first place.
        #
        # `is_safe_path` answers the static blocks AND the per-user jail, so asking it once
        # inside the jail covers both. Asked BEFORE existence is probed, or the error message
        # tells the caller what lives in directories they may not read.
        #
        # The jail is held for the DECISION ONLY, which is deliberately narrower than the same
        # guard in `document_viewer`, where it wraps the whole body. The difference is not
        # style, it is what sits downstream, and both halves were measured:
        #
        #   - the viewer's `_open_in_viewer` re-asks through `LibrarianTool._read_file`, which
        #     calls `is_safe_path` itself. Holding the jail across it is what makes the second
        #     asker get the same answer as the first.
        #   - here nothing re-asks: `path.read_text()` and `extract_pdf_markdown` consult no
        #     policy, and the ingestion runs via `_run_async_in_new_loop`, i.e. in a bare
        #     `threading.Thread`. Contextvars do not cross into a new thread, so a `with` around
        #     the whole body would NOT have covered the ingestion while looking exactly as if it
        #     did. A guard that appears to cover and provably does not is worse than a narrow
        #     one, so the decision is made here, up front, and the resolved path is what the
        #     rest of the function uses.
        from vaf.tools.filesystem import is_safe_path

        # Jail already installed via the file_access declaration; this is the decision only.
        safe, resolved = is_safe_path(str(path))
        if not safe:
            return f"[ERROR] {resolved}"
        path = Path(resolved)

        if not path.exists():
            return f"Error: File not found: {path}"
        if not path.is_file():
            return f"Error: Not a file: {path}"

        from uuid import UUID

        suffix = path.suffix.lower()
        if suffix not in (".pdf", ".txt", ".md"):
            return f"Error: Unsupported format. Use .pdf, .txt, or .md (got {suffix})."

        if document_title is None:
            document_title = _clean_title(path.stem or "document")
        doc_tag = _normalize_doc_tag(document_title)

        if user_scope_id is not None and isinstance(user_scope_id, str):
            try:
                user_scope_id = UUID(user_scope_id)
            except (ValueError, TypeError):
                user_scope_id = None

        import os

        # Function-local: learn_job imports THIS module's ingest helpers the
        # same way - both directions stay off the module level, so neither
        # import order can deadlock the pair.
        from vaf.tools.learn_job import LearnJobSpec, _learn_batches

        # An empty PDF gets its honest diagnosis BEFORE any job is spawned: a
        # scanned document without OCR should refuse loudly here, not spawn a
        # child that stores nothing (the generic "empty or unsupported" hid a
        # missing Tesseract behind a message about the document).
        if suffix == ".pdf":
            try:
                from vaf.core.pdf_extract import extract_pdf_markdown, _content_chars
                probe = extract_pdf_markdown(path, max_pages=3, ocr_fallback=True)
            except ImportError:
                return "Error: PDF support not installed. Run: pip install pdfplumber PyPDF2"
            except Exception as e:
                return f"Error reading PDF: {e}"
            if _content_chars(probe.get("markdown") or "") == 0:
                reason = (probe.get("ocr_unavailable_reason") or "").strip()
                if reason:
                    return (
                        f"Error: No text could be extracted from {path.name}. The PDF "
                        f"appears to be scanned (no text layer) and OCR is unavailable: "
                        f"{reason} Two ways out: install Tesseract (free, local - "
                        f"the installers set it up), or set a vision-capable "
                        f"provider so ocr_engine=auto reads pages with the vision "
                        f"model. Then retry."
                    )
                if int(probe.get("total_pages") or 0) <= 3:
                    # The probe covered the whole document and OCR ran empty.
                    return (
                        f"Error: No text could be extracted from {path.name}. "
                        f"OCR ran but found no readable text."
                    )
                # Only the first pages are blank (cover sheets): the job decides.

        spec = LearnJobSpec(
            path=str(path),
            document_title=document_title,
            doc_tag=doc_tag,
            resume=bool(kwargs.get("resume", True)),
            force_relearn=bool(kwargs.get("force_relearn", False)),
        )

        # ── Async default: the batched job in a child process ───────────────
        # A full learn is ~1 LLM call per page - it can never fit one bounded
        # tool call. The child has no tool budget, reports progress via the
        # heartbeat + learn_state, and its result arrives exactly once through
        # the runner drain.
        in_child = os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip() in ("1", "true", "yes")
        from vaf.core.config import Config
        if not in_child and Config.get("sub_agents_in_separate_terminals", False):
            from vaf.core.subagent_ipc import get_ipc, get_current_session_id
            from vaf.core.subagent_spawn import spawn_subagent

            session_id = get_current_session_id()
            # ONE learn job per session: the tool name is not the agent_type,
            # so the generic SUBAGENT_TOOLS duplicate guard does not cover this.
            try:
                if session_id and get_ipc().has_live_task("learn_agent", session_id):
                    return ("A document is already being learned for this session. "
                            "Wait for it to finish (the progress banner shows the batch), "
                            "or stop it first.")
            except Exception:
                pass

            _sub_env = {}
            if user_scope_id:
                _sub_env["VAF_USER_SCOPE_ID"] = str(user_scope_id)
            _role = kwargs.get("user_role") or os.environ.get("VAF_USER_ROLE")
            if _role:
                _sub_env["VAF_USER_ROLE"] = str(_role)

            spawned = spawn_subagent(
                "learn_agent",
                f"Learn document: {document_title}",
                include_task_arg=False,
                payload=spec.to_json(),
                extra_env=_sub_env,
                marker_note=(f"Learning \"{document_title}\" into long-term memory "
                             f"batch by batch. Progress shows in the banner; the "
                             f"result arrives as a message when done."),
            )
            if spawned:
                return spawned.marker
            # Spawn failed (task already cancelled) -> honest sync fallback below.

        # ── Sync fallback: ONE batch per call, inside the tool budget ───────
        # Deliberately NOT in SELF_SUPERVISED_TOOLS: an hour-long in-process
        # learn on the single worker is exactly the chat freeze the background
        # job exists to avoid. One batch fits the budget (on slow local models
        # the parse+10 LLM calls can still exceed it - the honest TIMEOUT then
        # names itself); the ledger keeps the progress, so repeated calls
        # genuinely finish a document.
        # Prefer dedicated extraction method if present, else compaction.
        if hasattr(agent, "_generate_for_document_extraction"):
            generate = agent._generate_for_document_extraction
        else:
            def generate(prompt: str) -> str:
                return agent._generate_for_compaction(prompt)

        from vaf.core.bounded_run import cancel_requested

        outcome = _learn_batches(
            spec,
            generate_fn=generate,
            user_scope_id=user_scope_id,
            session_id=kwargs.get("session_id"),
            cancel_cb=cancel_requested,
            max_batches=1,
        )
        if outcome.status in ("refused", "failed"):
            return outcome.message() if outcome.status == "failed" else f"Error: {outcome.error}"
        if outcome.status == "partial":
            toc_note = (f" Skipped {outcome.sections_skipped_toc} table-of-contents "
                        "section(s)." if outcome.sections_skipped_toc else "")
            return (
                f'Learned batch {outcome.batches_done} of {outcome.batches_total} of '
                f'"{document_title}" and saved the progress ({outcome.sections_stored} '
                f"section(s) so far).{toc_note} Background learning is off "
                f"(sub_agents_in_separate_terminals=false), so each call learns one "
                f"batch - call learn_document again to continue, or enable separate "
                f"terminals to finish in one run."
            )
        return outcome.message()
