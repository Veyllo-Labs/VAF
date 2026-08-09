# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The batched document-learn job: whole documents, batch by batch, honestly.

One learn run over a 1000-page PDF is ~1000 sequential LLM calls - it can never
fit one bounded tool call, and the old shape (extract everything, cap silently
at 200 pages / 40 sections, one giant DB transaction, report success) learned
3.9% of the document and told nobody. This module is the replacement:

- **Batches of `learn_batch_pages` pages** (default 10). Each batch extracts its
  page range only (`extract_pdf_markdown(first_page=..., max_pages=...)` streams
  and closes pages, so peak memory is batch-sized), ingests with a globally
  monotonic `section_offset`, commits in ITS OWN transaction (`get_db` per
  batch - a crash loses at most one batch, not 780 rolled-back sections), and
  records itself in the LearnLedger AFTER the commit.
- **Resume is idempotent.** A new run over the same (doc_tag, user) loads the
  ledger, refuses on a source-checksum mismatch (unless `force_relearn`),
  soft-deletes any crash orphans past the ledger's cut line
  (`delete_document_sections`), and continues at the first batch not recorded
  as done.
- **Progress is data**: `set_run_progress(done, total)` rides the child's
  heartbeat to the IPC record (the TUI TasksLine renders it for free), and a
  `learn_state` frame goes to the Web UI via the subprocess-capable
  StatePublisher lane. Frame keys are ints and plain strings ONLY, and none of
  SubAgentStreamUpdate's typed field names - `progress` there is Optional[int],
  and a string in it is a silent ValidationError that kills the run's whole
  bridge stream.
- **Every remaining limit is NAMED.** A firing `learn_max_sections` cap ends
  the run with status `capped` and a message carrying the key and the resume
  hint; pages with no extractable text are listed, not silently absorbed.

Two lanes share `_learn_batches`:
- the child process (`run_learn_job`, dispatched as `learn_agent`), where the
  120s tool budget does not exist and cancellation is a flag file;
- the sync in-process fallback in learn_document (`max_batches=1`): one honest
  batch per call inside the tool budget, cancellation via
  `bounded_run.cancel_requested`. Repeated calls genuinely finish a document.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from vaf.core.learn_ledger import LearnBatch, LearnLedger, file_sha256

# Cancel flag files: one writer (the WS cancel handler), one reader (the job).
# Deliberately NOT a field on the IPC task record - the heartbeat rewrites that
# record every 3 seconds and its guard degrades to an unlocked read-modify-write
# under contention, so a second writer there is the lost-update failure mode.
_CANCEL_DIR = ("subagent_queue", "learn_cancel")


def cancel_flag_path(task_id: str) -> Path:
    from vaf.core.platform import Platform
    d = Path(Platform.vaf_dir()).joinpath(*_CANCEL_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d / str(task_id)


@dataclass
class LearnJobSpec:
    """The machine-readable job spec (travels as the IPC payload sidecar -
    argv cannot carry paths with spaces plus flags reliably). Identity
    (scope/role/session) crosses in the child ENV, the coder pattern."""
    path: str
    document_title: Optional[str] = None
    doc_tag: str = ""
    resume: bool = True
    force_relearn: bool = False

    def to_json(self) -> str:
        return json.dumps({
            "path": self.path, "document_title": self.document_title,
            "doc_tag": self.doc_tag, "resume": bool(self.resume),
            "force_relearn": bool(self.force_relearn),
        })

    @classmethod
    def from_json(cls, raw: str) -> "LearnJobSpec":
        d = json.loads(raw)
        return cls(path=str(d.get("path") or ""),
                   document_title=d.get("document_title"),
                   doc_tag=str(d.get("doc_tag") or ""),
                   resume=bool(d.get("resume", True)),
                   force_relearn=bool(d.get("force_relearn", False)))


@dataclass
class LearnOutcome:
    """What one _learn_batches run achieved - the honest numbers the completion
    message is built from."""
    status: str                  # complete | partial | stopped | capped | failed | refused
    doc_tag: str = ""
    doc_title: str = ""
    total_pages: int = 0
    pages_learned: int = 0
    sections_stored: int = 0     # ledger.section_count after the run
    batches_done: int = 0
    batches_total: int = 0
    empty_page_ranges: List[List[int]] = field(default_factory=list)
    sections_skipped_toc: int = 0
    error: str = ""

    def message(self) -> str:
        """The completion text. Explicit numbers: the runner drain feeds this
        through a model turn, so the facts must survive a relay."""
        t = self.doc_title or self.doc_tag
        if self.status == "refused":
            return self.error
        if self.status == "failed":
            base = (f'Learning "{t}" failed at batch {self.batches_done + 1} of '
                    f"{self.batches_total}: {self.error} ")
            if self.sections_stored:
                base += (f"{self.sections_stored} section(s) covering ~{self.pages_learned} "
                         f"page(s) are already stored and will be kept - ")
            return base + f'resume with learn_document(path="{self._path_hint}").'
        parts = [f'Learned "{t}" into long-term memory:']
        if self.status == "complete":
            parts.append(f"{self.pages_learned} of {self.total_pages} page(s),")
        else:
            parts.append(f"{self.pages_learned} of {self.total_pages} page(s) so far,")
        parts.append(f"{self.sections_stored} section(s), stored under tag {self.doc_tag}.")
        if self.empty_page_ranges:
            ranges = ", ".join(f"{a}-{b}" if a != b else str(a)
                               for a, b in self.empty_page_ranges)
            parts.append(f"Pages {ranges} had no extractable text.")
        if self.sections_skipped_toc:
            parts.append(f"Skipped {self.sections_skipped_toc} "
                         "table-of-contents section(s).")
        if self.status == "capped":
            parts.append(
                "Stopped at the configured limit learn_max_sections - raise the key "
                f'or re-run learn_document(path="{self._path_hint}") after raising it '
                "to continue where it stopped.")
        elif self.status == "stopped":
            parts.append(
                f"Stopped by you at batch {self.batches_done} of {self.batches_total} - "
                f'resume with learn_document(path="{self._path_hint}").')
        return " ".join(parts)

    _path_hint: str = ""


def _probe_document(path: Path):
    """(total_pages, is_pdf). A PDF probe reads ZERO pages (max_pages=0 slices
    empty) but still reports the true total."""
    if path.suffix.lower() == ".pdf":
        from vaf.core.pdf_extract import extract_pdf_markdown
        res = extract_pdf_markdown(path, max_pages=0, ocr_fallback=False,
                                   cancel=lambda: False)
        return int(res.get("total_pages") or 0), True
    return 1, False


def _extract_batch(path: Path, first_page: int, pages: int, is_pdf: bool,
                   cancel: Callable[[], bool]):
    """(markdown, ocr_unavailable_reason) for one batch's page range."""
    if is_pdf:
        from vaf.core.pdf_extract import extract_pdf_markdown
        res = extract_pdf_markdown(path, max_pages=pages, ocr_fallback=True,
                                   first_page=first_page, cancel=cancel)
        return res.get("markdown") or "", res.get("ocr_unavailable_reason") or ""
    return path.read_text(encoding="utf-8", errors="replace"), ""


async def _collect_section_titles(db, doc_tag: str, user_scope_id) -> List[str]:
    """The stored section context summaries (meta.title) for doc_tag - the
    finalize summary is built from THESE so it covers batches of earlier runs
    too (their in-memory contexts are gone after a resume)."""
    from sqlalchemy import select, and_
    from vaf.memory.models import Memory
    conditions = [
        Memory.is_deleted == False,  # noqa: E712
        Memory.meta["doc_tag"].as_string() == doc_tag,
        Memory.meta["type"].as_string() != "document_index",
    ]
    if user_scope_id is not None:
        conditions.append(Memory.user_scope_id == user_scope_id)
    rows = (await db.execute(select(Memory).where(and_(*conditions)))).scalars().all()
    titles = []
    for m in rows:
        t = (m.meta or {}).get("title")
        if t:
            titles.append(str(t))
    return titles


def _learn_batches(
    spec: LearnJobSpec,
    *,
    generate_fn: Callable[[str], str],
    user_scope_id,
    session_id: Optional[str],
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    max_batches: Optional[int] = None,
) -> LearnOutcome:
    """Run (or continue) one document's batched learn. Shared by the child and
    the sync one-batch fallback; never raises - failures become the outcome.

    ONE event loop for the WHOLE job (this wrapper's single asyncio.run): the
    async DB engine is created once and cached, and its connections are bound
    to the loop that created them - a per-batch asyncio.run gave every batch a
    fresh loop, so batch 1 committed and batch 2 died on the cached engine
    ("got Future attached to a different loop", first live run). Per-batch
    COMMITS are unaffected: they come from the per-batch get_db context, not
    from the loop.
    """
    return asyncio.run(_learn_batches_async(
        spec,
        generate_fn=generate_fn,
        user_scope_id=user_scope_id,
        session_id=session_id,
        progress_cb=progress_cb,
        cancel_cb=cancel_cb,
        max_batches=max_batches,
    ))


async def _learn_batches_async(
    spec: LearnJobSpec,
    *,
    generate_fn: Callable[[str], str],
    user_scope_id,
    session_id: Optional[str],
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    max_batches: Optional[int] = None,
) -> LearnOutcome:
    from vaf.core.config import Config
    from vaf.tools.learn_document import (
        _clean_title,
        _normalize_doc_tag,
        ingest_document_knowledge,
    )

    path = Path(spec.path)
    cancel = cancel_cb or (lambda: False)
    progress = progress_cb or (lambda done, total: None)
    doc_title = _clean_title(spec.document_title or path.stem or "document")
    doc_tag = spec.doc_tag or _normalize_doc_tag(doc_title)

    outcome = LearnOutcome(status="failed", doc_tag=doc_tag, doc_title=doc_title)
    outcome._path_hint = str(path)
    try:
        sha = file_sha256(path)
        total_pages, is_pdf = _probe_document(path)
        if total_pages <= 0:
            outcome.error = "the document has no pages"
            return outcome
        batch_pages = max(2, min(100, int(Config.get("learn_batch_pages", 10) or 10)))
        if not is_pdf:
            batch_pages = 1  # txt/md: one batch, whole file
        total_batches = max(1, (total_pages + batch_pages - 1) // batch_pages)
        max_sections = int(Config.get("learn_max_sections", 0) or 0)

        # ── Ledger: fresh, resume, or refuse ────────────────────────────────
        ledger = LearnLedger.load(doc_tag, str(user_scope_id or "")) if spec.resume else None
        if ledger and ledger.content_sha256 != sha:
            if not spec.force_relearn:
                outcome.status = "refused"
                outcome.error = (
                    f'The file at {path} changed since it was last learned '
                    f"(checksum mismatch). Pass force_relearn=true to relearn it "
                    f"from scratch, or keep the stored knowledge as it is.")
                return outcome
            ledger = None
        if spec.force_relearn:
            # Relearn from scratch: soft-delete the old sections + root, fresh ledger.
            from vaf.memory.database import get_db
            from vaf.memory.rag import RagPipeline
            async with get_db(user_scope_id=user_scope_id) as db:
                await RagPipeline(db).delete_by_tag(doc_tag, soft=True,
                                                    user_scope_id=user_scope_id)
            if ledger:
                ledger.delete()
            ledger = None
        if ledger is None:
            ledger = LearnLedger(
                doc_tag=doc_tag, source_path=str(path), content_sha256=sha,
                user_scope_id=str(user_scope_id or ""), session_id=str(session_id or ""),
                total_pages=total_pages, batch_pages=batch_pages,
                total_batches=total_batches)
            ledger.save()
        else:
            # Close the write-after-commit window: sections past the cut line are
            # crash orphans of a batch whose ledger write never happened.
            from vaf.memory.database import get_db
            from vaf.memory.rag import RagPipeline
            async with get_db(user_scope_id=user_scope_id) as db:
                await RagPipeline(db).delete_document_sections(
                    doc_tag, from_section_index=ledger.section_count,
                    user_scope_id=user_scope_id)
            # A resumed job is RUNNING again: the previous run's terminal
            # status must not survive it (live run: the learn-status endpoint
            # reported "failed" for half an hour of healthy batching).
            ledger.status = "running"
            ledger.last_error = None
            ledger.save()

        outcome.total_pages = ledger.total_pages
        done_set = ledger.done_batch_indices()

        # ── The batch loop ──────────────────────────────────────────────────
        ran = 0
        for b in range(ledger.total_batches):
            if b in done_set:
                continue
            if cancel():
                ledger.status = "stopped"
                ledger.save()
                outcome.status = "stopped"
                break
            if max_sections and ledger.section_count >= max_sections:
                ledger.status = "capped"
                ledger.save()
                outcome.status = "capped"
                break
            if max_batches is not None and ran >= max_batches:
                outcome.status = "partial"
                break
            first = b * ledger.batch_pages + 1
            pages = min(ledger.batch_pages, ledger.total_pages - first + 1)
            progress(len(done_set), ledger.total_batches)
            md, _ocr_reason = _extract_batch(path, first, pages, is_pdf, cancel)

            async def _ingest_one(markdown: str):
                from vaf.memory.database import get_db
                async with get_db(user_scope_id=user_scope_id) as db:
                    return await ingest_document_knowledge(
                        db,
                        content_markdown=markdown,
                        doc_title=doc_title,
                        doc_tag=doc_tag,
                        source="learn_document",
                        mem_type="document",
                        generate_fn=generate_fn,
                        user_scope_id=user_scope_id,
                        session_id=session_id,
                        section_offset=ledger.section_count,
                        update_root=False,
                    )
                # commit happens at the context exit above - THIS batch is durable now

            created = 0
            if (md or "").strip():
                res = (await _ingest_one(md)) or {}
                created = int(res.get("created") or 0)
                ledger.toc_skipped += int(res.get("sections_skipped_toc") or 0)
            if created == 0:
                ledger.empty_page_ranges.append([first, first + pages - 1])
            ledger.record_batch(LearnBatch(
                index=b, first_page=first, pages=pages,
                section_start=ledger.section_count, sections=created))
            done_set.add(b)
            ran += 1
            progress(len(done_set), ledger.total_batches)
        else:
            outcome.status = "complete"

        # ── Finalize / report ──────────────────────────────────────────────
        outcome.batches_done = len(done_set)
        outcome.batches_total = ledger.total_batches
        outcome.sections_stored = ledger.section_count
        outcome.pages_learned = sum(b.pages for b in ledger.batches if b.status == "done")
        outcome.empty_page_ranges = [list(r) for r in ledger.empty_page_ranges]
        outcome.sections_skipped_toc = ledger.toc_skipped

        if outcome.status == "complete":
            await _finalize_root(
                doc_tag=doc_tag, doc_title=doc_title, ledger=ledger,
                generate_fn=generate_fn, user_scope_id=user_scope_id)
            ledger.status = "complete"
            ledger.save()
            ledger.delete()  # the document_index root is the durable record
            # The graph cache never learns about ingests (only HTTP routes
            # invalidate it) - without this, the finished document appears in
            # the graph up to 5 minutes late and "Refresh" seems dead.
            try:
                from vaf.memory.cache import get_cache
                await get_cache().invalidate_graph()
            except Exception:
                pass  # cache is an accelerator; TTL expires a stale entry
        return outcome
    except Exception as e:  # never raises - the outcome carries the failure
        outcome.status = "failed"
        outcome.error = f"{e.__class__.__name__}: {e}"
        try:
            ledger.status = "failed"
            ledger.last_error = outcome.error
            ledger.save()
        except Exception:
            pass
        return outcome


async def _finalize_root(*, doc_tag: str, doc_title: str, ledger: LearnLedger,
                         generate_fn, user_scope_id) -> None:
    """ONE document_index root upsert for the whole job. Built from the STORED
    section titles so it is correct after a resume, and stamped with the
    coverage facts the UI's learn-status reads."""
    from sqlalchemy import select, and_
    from vaf.core.user_time import user_now
    from vaf.memory.database import get_db
    from vaf.memory.models import Memory
    from vaf.memory.rag import RagPipeline
    from vaf.tools.learn_document import _summarize_doc_from_contexts

    async with get_db(user_scope_id=user_scope_id) as db:
        titles = await _collect_section_titles(db, doc_tag, user_scope_id)
        doc_summary, doc_tags = _summarize_doc_from_contexts(titles, doc_title, generate_fn)
        learned_pages = sum(b.pages for b in ledger.batches if b.status == "done")
        stamp = {
            "page_count": ledger.section_count,
            "learned_pages": learned_pages,
            "total_pages": ledger.total_pages,
            "learn_status": "complete" if learned_pages >= ledger.total_pages else "partial",
            "source_path": ledger.source_path,
            "content_sha256": ledger.content_sha256,
            "learned_at": user_now().isoformat(timespec="seconds"),
        }
        conditions = [
            Memory.is_deleted == False,  # noqa: E712
            Memory.meta["type"].as_string() == "document_index",
            Memory.meta["doc_tag"].as_string() == doc_tag,
        ]
        if user_scope_id is not None:
            conditions.append(Memory.user_scope_id == user_scope_id)
        existing = (await db.execute(select(Memory).where(and_(*conditions)))).scalar_one_or_none()
        if existing is not None:
            meta = dict(existing.meta or {})
            meta.update(stamp)
            if doc_summary:
                meta["doc_summary"] = doc_summary
            existing.meta = meta
        else:
            content = f"Document index: {doc_title}."
            if doc_summary:
                content += f" {doc_summary}"
            content += f" Contains {ledger.section_count} section(s) of knowledge from a document."
            meta = {"type": "document_index", "source": "learn_document",
                    "title": doc_title, "doc_tag": doc_tag,
                    "tags": list(dict.fromkeys([doc_tag, "knowledge"] + doc_tags)),
                    **stamp}
            if doc_summary:
                meta["doc_summary"] = doc_summary
            await RagPipeline(db).ingest(content=content, metadata=meta,
                                         user_scope_id=user_scope_id, auto_connect=False)


def _child_generate_fn() -> Callable[[str], str]:
    """One-shot LLM calls in the child: the framework completion primitive, no
    Agent construction. VAF_PROVIDER is passed EXPLICITLY - complete() resolves
    Config's provider, not the child-env override."""
    from vaf.core.completion import complete
    from vaf.core.config import Config

    provider = (os.environ.get("VAF_PROVIDER") or "").strip() or None
    max_tokens = int(Config.get("memory_document_extraction_max_tokens", 1200) or 1200)
    max_tokens = max(400, min(max_tokens, 4000))

    def _gen(prompt: str) -> str:
        return complete(prompt, provider=provider, max_tokens=max_tokens,
                        temperature=0.2, caller="learn_agent") or ""
    return _gen


def run_learn_job(spec_json: str) -> str:
    """Child entry (dispatched as `learn_agent`). Never raises: the returned
    string is the completion message the drain delivers exactly once."""
    try:
        spec = LearnJobSpec.from_json(spec_json)
    except Exception:
        # A bare path is an acceptable degenerate spec (manual CLI runs).
        spec = LearnJobSpec(path=str(spec_json or "").strip())
    if not spec.path:
        return "Error: learn job spec carries no path."

    from vaf.core.progress import StatePublisher, resolve_ui_session_id, set_run_progress

    task_id = (os.environ.get("VAF_TASK_ID") or "").strip()
    user_scope_id = _scope_from_env()
    session_id = resolve_ui_session_id() or None
    flag = cancel_flag_path(task_id) if task_id else None
    publisher = StatePublisher("learn_state", min_interval=0.5, dedupe=True)
    # The DISPLAY title, never the persisted basename: the on-disk name carries
    # a timestamp+uuid uniqueness prefix that means nothing to the user (live
    # feedback: the banner read "1786...c6b8..._Study.pdf").
    doc_name = (spec.document_title or "").strip() or Path(spec.path).name

    def _cancel() -> bool:
        return bool(flag is not None and flag.exists())

    def _progress(done: int, total: int) -> None:
        set_run_progress(done, total)  # -> heartbeat -> IPC record -> TUI TasksLine
        # Ints and plain strings ONLY, and none of SubAgentStreamUpdate's typed
        # field names (its `progress` is Optional[int]; a string there is a
        # silent ValidationError that kills the run's whole bridge stream).
        publisher.publish(
            {"docName": doc_name, "batch": int(done), "batchesTotal": int(total),
             "phase": "learning"},
            session_id=session_id or "")

    try:
        outcome = _learn_batches(
            spec,
            generate_fn=_child_generate_fn(),
            user_scope_id=user_scope_id,
            session_id=session_id,
            progress_cb=_progress,
            cancel_cb=_cancel,
        )
        publisher.publish(
            {"docName": doc_name, "docTag": outcome.doc_tag,
             "batch": int(outcome.batches_done), "batchesTotal": int(outcome.batches_total),
             "phase": "done" if outcome.status == "complete" else outcome.status},
            session_id=session_id or "", force=True)
        return outcome.message()
    finally:
        if flag is not None:
            try:
                flag.unlink(missing_ok=True)
            except Exception:
                pass


def _scope_from_env():
    """The child's caller scope as UUID, or None for the unscoped local admin.
    Identity crosses the fork as DATA (VAF_USER_SCOPE_ID), the coder pattern."""
    raw = (os.environ.get("VAF_USER_SCOPE_ID") or "").strip()
    if not raw:
        return None
    try:
        from uuid import UUID
        return UUID(raw)
    except Exception:
        return None
