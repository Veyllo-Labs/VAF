# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Per-document learn ledger: what a batched learn job has durably finished.

The RAG ingest has NO dedup - every ingest mints a fresh UUID, so re-running a
batch duplicates every section it stored. This ledger is what makes a learn job
resumable AND idempotent:

- The child writes the ledger AFTER each batch's DB commit (write-after-commit:
  the opposite order would silently SKIP a rolled-back batch - data loss). The
  window that ordering leaves open - crash between commit and ledger write -
  is closed by the resume step, which first soft-deletes any section with
  `section_index >= section_count` (exactly the crash orphans; see
  RagPipeline.delete_document_sections) and then re-runs the batch.
- ONE file per job, ONE writer (the child), atomic tmp+rename per batch.
  Deliberately NOT rows in the shared guarded queue JSON: the 3-second
  heartbeat rewrites the task record continuously, and the queue guard itself
  documents that it degrades to an unlocked read-modify-write under
  contention - a second writer there is the lost-update failure mode.
- `content_sha256` pins the SOURCE: a resume against a file that changed since
  is refused (the stored sections describe a document that no longer exists on
  disk) unless the caller explicitly relearns from scratch.

Location: `<vaf_dir>/subagent_queue/learn_ledgers/<doc_tag>__<uid8|admin>.json`
- keyed by document AND user so the next run finds it, and two tenants
learning the same title never share a ledger. A completed job unlinks its
ledger (the document_index root is the durable record); stopped/capped/failed
ledgers stay for resume and are swept after LEDGER_RETENTION_DAYS.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

LEDGER_VERSION = 1
LEDGER_RETENTION_DAYS = 30


def file_sha256(path) -> str:
    """Streaming sha256 of a file (1 MiB chunks - the sources are big)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class LearnBatch:
    index: int            # 0-based batch number
    first_page: int       # 1-based absolute page the batch starts at
    pages: int            # pages consumed by this batch
    section_start: int    # global section_index of the batch's first section
    sections: int         # sections stored by this batch
    status: str = "done"  # only durably-committed batches are recorded


@dataclass
class LearnLedger:
    doc_tag: str
    source_path: str
    content_sha256: str
    user_scope_id: str            # "" for the unscoped local admin
    session_id: str
    total_pages: int
    batch_pages: int
    total_batches: int
    batches: List[LearnBatch] = field(default_factory=list)
    section_count: int = 0        # next global section_index (resume cut line)
    empty_page_ranges: List[List[int]] = field(default_factory=list)
    toc_skipped: int = 0          # ToC/list-of-X sections skipped (survives resume)
    status: str = "running"       # running | complete | stopped | capped | failed
    last_error: Optional[str] = None
    version: int = LEDGER_VERSION
    created_at: str = ""
    updated_at: str = ""

    # ── identity / location ─────────────────────────────────────────────────

    @staticmethod
    def _dir() -> Path:
        from vaf.core.platform import Platform
        d = Path(Platform.vaf_dir()) / "subagent_queue" / "learn_ledgers"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _key(doc_tag: str, user_scope_id: Optional[str]) -> str:
        # uid8 mirrors the session-workspace convention; "admin" for the
        # unscoped local admin. The tag is already a filesystem-safe slug.
        uid8 = (str(user_scope_id or "").replace("-", "")[:8]) or "admin"
        return f"{doc_tag}__{uid8}.json"

    @classmethod
    def path_for(cls, doc_tag: str, user_scope_id: Optional[str]) -> Path:
        return cls._dir() / cls._key(doc_tag, user_scope_id)

    # ── persistence (single writer: the job child) ──────────────────────────

    def save(self) -> None:
        """Atomic tmp+rename; timestamps stamped here so callers cannot forget."""
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        if not self.created_at:
            self.created_at = now
        self.updated_at = now
        target = self.path_for(self.doc_tag, self.user_scope_id)
        tmp = target.with_suffix(".json.tmp")
        data = asdict(self)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)

    @classmethod
    def load(cls, doc_tag: str, user_scope_id: Optional[str]) -> Optional["LearnLedger"]:
        """Load the ledger for (document, user), or None. Boundary coercion on
        the fields resume depends on (persisted files and model-shaped input
        both pass through here - Rule 4.7)."""
        p = cls.path_for(doc_tag, user_scope_id)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            return None
        try:
            batches = [LearnBatch(**{k: b.get(k) for k in
                                     ("index", "first_page", "pages", "section_start",
                                      "sections", "status")})
                       for b in (raw.get("batches") or [])]
            return cls(
                doc_tag=str(raw.get("doc_tag") or doc_tag),
                source_path=str(raw.get("source_path") or ""),
                content_sha256=str(raw.get("content_sha256") or ""),
                user_scope_id=str(raw.get("user_scope_id") or ""),
                session_id=str(raw.get("session_id") or ""),
                total_pages=int(raw.get("total_pages") or 0),
                batch_pages=int(raw.get("batch_pages") or 0),
                total_batches=int(raw.get("total_batches") or 0),
                batches=batches,
                section_count=int(raw.get("section_count") or 0),
                empty_page_ranges=[list(map(int, r)) for r in (raw.get("empty_page_ranges") or [])],
                toc_skipped=int(raw.get("toc_skipped") or 0),
                status=str(raw.get("status") or "running"),
                last_error=raw.get("last_error"),
                version=int(raw.get("version") or LEDGER_VERSION),
                created_at=str(raw.get("created_at") or ""),
                updated_at=str(raw.get("updated_at") or ""),
            )
        except Exception:
            return None

    def delete(self) -> None:
        try:
            self.path_for(self.doc_tag, self.user_scope_id).unlink(missing_ok=True)
        except Exception:
            pass

    # ── resume queries ──────────────────────────────────────────────────────

    def done_batch_indices(self) -> set:
        return {b.index for b in self.batches if b.status == "done"}

    def record_batch(self, batch: LearnBatch) -> None:
        """Record one durably-committed batch and advance the resume cut line.
        Call AFTER the DB commit, never before."""
        self.batches = [b for b in self.batches if b.index != batch.index] + [batch]
        self.batches.sort(key=lambda b: b.index)
        self.section_count = max(self.section_count, batch.section_start + batch.sections)
        self.save()


def sweep_stale_ledgers(max_age_days: int = LEDGER_RETENTION_DAYS) -> int:
    """Remove ledgers idle longer than the retention window. Completed jobs
    unlink their ledger themselves; this catches stopped/capped/failed jobs
    nobody resumed. Returns the count removed; never raises."""
    removed = 0
    try:
        cutoff = time.time() - max_age_days * 86400
        for p in LearnLedger._dir().glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except Exception:
                continue
    except Exception:
        pass
    return removed
