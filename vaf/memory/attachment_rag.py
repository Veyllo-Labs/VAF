# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Ephemeral Attachment RAG lane for Web UI sidebar documents.

This module keeps attachment-derived retrieval data separated from long-term memory
by using a dedicated metadata source and strict session+user scoping.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from queue import Empty, Queue
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import and_, delete, or_, select

from vaf.core.config import Config
from vaf.core.log_helper import append_domain_log
from vaf.memory.crypto import get_crypto
from vaf.memory.database import get_db
from vaf.memory.embeddings import cleanup_embedding_memory
from vaf.memory.models import Memory
from vaf.memory.embeddings import MAX_EMBED_INPUT_CHARS


ATTACHMENT_SOURCE = "attachment_ephemeral"
_attachment_rag_killed = False
_attachment_rag_kill_reason = ""
_fingerprint_lock = threading.Lock()
_session_fingerprint_cache: Dict[str, str] = {}
_safe_store_lock = threading.Lock()
# RAM rationale:
# Safe-mode keeps attachment data in a tiny session-scoped Python store and
# avoids the vector ingest/search stack (embedding model + pgvector + asyncpg
# churn). This is an intentional stabilization design to prevent the observed
# runaway RSS spikes under rapid index/search/clear loops.
_safe_session_store: Dict[str, Dict[str, Any]] = {}
_vector_runner_lock = threading.Lock()
_vector_runner_thread: Optional[threading.Thread] = None
_vector_runner_queue: "Queue[Dict[str, Any]]" = Queue(maxsize=32)
_vector_rate_lock = threading.Lock()
_vector_index_timestamps: List[float] = []
_vector_search_timestamps: List[float] = []
_vector_coalesce_lock = threading.Lock()
_vector_index_flights: Dict[str, Dict[str, Any]] = {}


def _to_uuid(value: Optional[str | UUID]) -> Optional[UUID]:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def _scope_filters(user_scope_id: Optional[UUID]) -> List[Any]:
    if user_scope_id is None:
        return [Memory.user_scope_id.is_(None)]
    return [Memory.user_scope_id == user_scope_id]


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _op_timeout_sec() -> int:
    timeout_sec = int(Config.get("attachment_rag_op_timeout_sec", 20) or 20)
    return max(5, min(120, timeout_sec))


def _safe_mode_enabled() -> bool:
    """
    Safe mode keeps the attachment lane on a lexical in-process store, bypassing the vector/embedding
    path. Default is now FALSE (vector mode on): the runaway-RSS root cause (a TextChunker infinite
    loop, see docs/memory/MEMORY_SYSTEM.md) was fixed and the vector + hierarchical paths verified stable.
    Set `attachment_rag_safe_mode=true` in config to force the lexical fallback.
    """
    return bool(Config.get("attachment_rag_safe_mode", False))


def _session_cache_key(session_id: str, user_scope_id: Optional[UUID]) -> str:
    return f"{str(user_scope_id) if user_scope_id else 'none'}::{str(session_id)}"


def _safe_store_cleanup_expired() -> int:
    # RAM rationale: hard TTL cleanup ensures ephemeral attachment payloads
    # do not accumulate across sessions or long-running processes.
    now = datetime.utcnow()
    removed = 0
    with _safe_store_lock:
        for key in list(_safe_session_store.keys()):
            entry = _safe_session_store.get(key) or {}
            expires_at = entry.get("expires_at")
            if isinstance(expires_at, datetime) and expires_at < now:
                _safe_session_store.pop(key, None)
                removed += 1
    return removed


def _docs_fingerprint(documents: List[Dict[str, Any]]) -> str:
    """
    Stable fingerprint for attachment payloads.
    Used to skip redundant delete+reingest cycles for unchanged sidebar docs.
    """
    h = hashlib.sha256()
    for doc in documents or []:
        name = str((doc or {}).get("name") or "").strip()
        content = str((doc or {}).get("content") or "").strip()
        h.update(name.encode("utf-8", errors="ignore"))
        h.update(b"\n")
        h.update(content.encode("utf-8", errors="ignore"))
        h.update(b"\n---\n")
    return h.hexdigest()


def _normalize_query(raw_query: str) -> str:
    """
    Mirror core RAG safety:
    - strip <think> blocks
    - cap embedding input length
    """
    q = (raw_query or "").strip()
    if not q:
        return ""

    q = re.sub(r"<think>.*?</think>", "", q, flags=re.DOTALL).strip()
    if "<think>" in q:
        q = q.split("<think>")[0].strip()
    if len(q) > MAX_EMBED_INPUT_CHARS:
        q = q[:MAX_EMBED_INPUT_CHARS].rstrip()
    return q


def _tokenize_lexical(text: str) -> List[str]:
    # Keep lexical mode deterministic and cheap.
    return [t for t in re.findall(r"[a-zA-Z0-9_]+", (text or "").lower()) if len(t) >= 2]


def _lexical_score(query: str, query_tokens: List[str], content: str) -> float:
    q = (query or "").strip().lower()
    c = (content or "").lower()
    if not c:
        return 0.0

    # Exact substring gets a strong boost.
    exact = 1.0 if q and q in c else 0.0

    # Token overlap handles normal keyword retrieval.
    c_tokens = set(_tokenize_lexical(c))
    q_set = set(query_tokens)
    overlap = (len(c_tokens & q_set) / len(q_set)) if q_set else 0.0

    # Weighted score stays in [0,1].
    score = min(1.0, (0.7 * overlap) + (0.3 * exact))
    return score


def _build_lexical_snippet(content: str, query_tokens: List[str], max_chars: int) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    lower = text.lower()
    hit_idx = -1
    for tok in query_tokens:
        i = lower.find(tok.lower())
        if i >= 0 and (hit_idx < 0 or i < hit_idx):
            hit_idx = i

    if hit_idx < 0:
        snippet = text[:max_chars].rstrip()
        return snippet + "\n... [snippet truncated]"

    # Center the snippet around the first lexical hit.
    lead = max_chars // 3
    start = max(0, hit_idx - lead)
    end = min(len(text), start + max_chars)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "... " + snippet
    if end < len(text):
        snippet = snippet + " ... [snippet truncated]"
    return snippet


def _rrf_fuse_ranked(
    vector_ranked: List[Dict[str, Any]],
    lexical_ranked: List[Dict[str, Any]],
    *,
    top_k: int,
    rrf_k: int = 60,
) -> List[Dict[str, Any]]:
    """
    Reciprocal Rank Fusion over two ranked result lists.

    Each item must contain:
    - key (stable doc id)
    - text
    - attachment_name
    """
    scores: Dict[str, float] = {}
    payload: Dict[str, Dict[str, Any]] = {}

    for rank, item in enumerate(vector_ranked, start=1):
        key = str(item.get("key") or "")
        if not key:
            continue
        scores[key] = scores.get(key, 0.0) + (1.0 / (rrf_k + rank))
        payload.setdefault(key, item)

    for rank, item in enumerate(lexical_ranked, start=1):
        key = str(item.get("key") or "")
        if not key:
            continue
        scores[key] = scores.get(key, 0.0) + (1.0 / (rrf_k + rank))
        payload.setdefault(key, item)

    merged = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[: max(1, int(top_k or 4))]
    out: List[Dict[str, Any]] = []
    for key, score in merged:
        item = payload.get(key) or {}
        out.append(
            {
                "doc_key": key,
                "text": str(item.get("text") or ""),
                "score": float(score),
                "attachment_name": str(item.get("attachment_name") or "Attachment"),
            }
        )
    return out


def _process_rss_bytes() -> Optional[int]:
    try:
        import psutil

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:
        return None


def _max_rss_bytes() -> int:
    max_gb = float(Config.get("attachment_rag_max_rss_gb", 4.0) or 4.0)
    max_gb = max(0.25, min(64.0, max_gb))
    return int(max_gb * 1024 * 1024 * 1024)


def _trip_memory_killer(origin: str) -> bool:
    global _attachment_rag_killed, _attachment_rag_kill_reason

    if _attachment_rag_killed:
        return True

    rss = _process_rss_bytes()
    if rss is None:
        return False
    limit = _max_rss_bytes()
    if rss <= limit:
        return False

    _attachment_rag_killed = True
    _attachment_rag_kill_reason = (
        f"Attachment RAG killed by memory guard: rss={rss} bytes exceeded limit={limit} bytes at {origin}"
    )
    append_domain_log("rag", _attachment_rag_kill_reason)
    return True


def _allow_vector_index_now() -> bool:
    """
    Backpressure for vector ingest path.

    Causality:
    - The observed RAM runaway is triggered by rapid repeated index churn.
    - ONNX embedding + pgvector ingest are memory-intensive under tight loops.
    - Limiting ingest rate keeps attachment vector lane functional for normal
      user interactions while preventing pathological stress spikes.
    """
    import time

    window_sec = float(Config.get("attachment_rag_vector_rate_window_sec", 10.0) or 10.0)
    max_ops = int(Config.get("attachment_rag_vector_max_index_ops_per_window", 1) or 1)
    window_sec = max(1.0, min(120.0, window_sec))
    max_ops = max(1, min(200, max_ops))

    now = time.monotonic()
    with _vector_rate_lock:
        cutoff = now - window_sec
        while _vector_index_timestamps and _vector_index_timestamps[0] < cutoff:
            _vector_index_timestamps.pop(0)
        if len(_vector_index_timestamps) >= max_ops:
            return False
        _vector_index_timestamps.append(now)
        return True


def _allow_vector_search_now() -> bool:
    """
    Backpressure for vector search path under extreme churn loops.
    """
    import time

    window_sec = float(Config.get("attachment_rag_vector_search_rate_window_sec", 10.0) or 10.0)
    max_ops = int(Config.get("attachment_rag_vector_max_search_ops_per_window", 4) or 4)
    window_sec = max(1.0, min(120.0, window_sec))
    max_ops = max(1, min(500, max_ops))

    now = time.monotonic()
    with _vector_rate_lock:
        cutoff = now - window_sec
        while _vector_search_timestamps and _vector_search_timestamps[0] < cutoff:
            _vector_search_timestamps.pop(0)
        if len(_vector_search_timestamps) >= max_ops:
            return False
        _vector_search_timestamps.append(now)
        return True


def _attachment_vector_unload_enabled() -> bool:
    """
    Aggressive mitigation for native ONNX retention in attachment vector lane.
    """
    return bool(Config.get("attachment_rag_vector_unload_model_after_op", False))


def _maybe_unload_attachment_vector_model(origin: str) -> None:
    if not _attachment_vector_unload_enabled():
        return
    try:
        cleanup_embedding_memory()
        append_domain_log("rag", f"ATTACH_VECTOR_MODEL_UNLOAD origin={origin}")
    except Exception:
        pass


def _vector_coalesce_enabled() -> bool:
    return bool(Config.get("attachment_rag_vector_coalesce_enabled", True))


# ── Hierarchical indexing helpers ─────────────────────────────────────────────

_HEADER_RE = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)
_PAGE_MARKER_RE = re.compile(
    r"(?:^|\n)(?:[-─═]{4,}|Page\s+\d+\b|CHAPTER\s+\d+\b|_{8,})",
    re.IGNORECASE,
)
_PARA_BREAK_RE = re.compile(r"\n{2,}")


def _split_into_sections(
    text: str,
    min_section_chars: int = 500,
    max_section_chars: int = 5000,
) -> List[Dict[str, Any]]:
    """
    Split document text into titled sections.
    Returns [{"title": str, "text": str, "index": int}, ...] or [] to signal flat fallback.
    Detection priority: markdown headers → page markers → paragraph breaks.
    """
    text = text or ""
    if not text.strip():
        return []

    def _boundaries_from_headers(t: str):
        matches = list(_HEADER_RE.finditer(t))
        if len(matches) < 2:
            return None, None
        return [m.start() for m in matches], [m.group(2).strip() for m in matches]

    def _boundaries_from_page_markers(t: str):
        matches = list(_PAGE_MARKER_RE.finditer(t))
        if len(matches) < 2:
            return None, None
        return [m.start() for m in matches], None

    def _boundaries_from_para_breaks(t: str):
        matches = list(_PARA_BREAK_RE.finditer(t))
        if len(matches) < 2:
            return None, None
        return [m.start() for m in matches], None

    boundaries, explicit_titles = _boundaries_from_headers(text)
    if boundaries is None:
        boundaries, explicit_titles = _boundaries_from_page_markers(text)
    if boundaries is None:
        boundaries, explicit_titles = _boundaries_from_para_breaks(text)
    if boundaries is None or len(boundaries) < 2:
        return []

    # Build raw sections from consecutive boundary pairs
    boundaries_with_end = sorted(set([0] + boundaries)) + [len(text)]
    raw_sections: List[Dict[str, Any]] = []
    for idx, (start, end) in enumerate(zip(boundaries_with_end, boundaries_with_end[1:])):
        body = text[start:end].strip()
        if not body:
            continue
        if explicit_titles and idx < len(explicit_titles):
            title = explicit_titles[idx]
        else:
            first_line = body.split("\n")[0].strip()
            title = first_line[:80] if first_line else f"Section {idx + 1}"
        raw_sections.append({"title": title, "text": body, "index": idx})

    # Merge sections shorter than min_section_chars into preceding section
    merged: List[Dict[str, Any]] = []
    for sec in raw_sections:
        if merged and len(sec["text"]) < min_section_chars:
            merged[-1]["text"] += "\n\n" + sec["text"]
        else:
            merged.append(dict(sec))

    # Split sections longer than max_section_chars at nearest sentence boundary
    final: List[Dict[str, Any]] = []
    for sec in merged:
        text_block = sec["text"]
        while len(text_block) > max_section_chars:
            split_at = max_section_chars
            # Search backward for sentence boundary
            for delim in (".", "!", "?"):
                pos = text_block.rfind(delim, max_section_chars // 2, split_at)
                if pos > 0:
                    split_at = pos + 1
                    break
            final.append({"title": sec["title"], "text": text_block[:split_at].strip(), "index": len(final)})
            text_block = text_block[split_at:].strip()
        if text_block:
            final.append({"title": sec["title"], "text": text_block, "index": len(final)})

    # Re-index and validate
    for i, s in enumerate(final):
        s["index"] = i
    return final if len(final) >= 2 else []


def _summarize_section_llm(
    section_text: str,
    section_title: str,
    *,
    timeout_sec: int = 8,
) -> str:
    """
    Generate a 1-2 sentence LLM summary for a section.
    Never raises — falls back to first 300 chars of section text.
    """
    fallback = (section_text or "").strip()[:300]
    try:
        from vaf.core.api_backend import APIBackendManager
        provider = str(Config.get("provider", "local") or "local")
        backend = APIBackendManager(provider)
        prompt = (
            f"Summarize the following document section in 1-2 sentences.\n"
            f"Section title: {section_title}\n\n"
            f"{section_text[:2000]}\n\n"
            f"Output only the summary, no preamble."
        )
        raw_chunks = list(
            backend.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=512,  # headroom for reasoning models (they emit <think>...</think> first)
                stream=False,
            )
        )
        result = "".join(c if isinstance(c, str) else str(c) for c in raw_chunks if c)
        # Strip reasoning-model <think>...</think> so it never becomes the section summary/title.
        result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL | re.IGNORECASE)
        if "<think>" in result.lower():
            result = re.split(r"(?i)<think>", result)[0]
        result = result.strip()
        return result if result else fallback
    except Exception:
        return fallback


async def _hierarchical_ingest_doc(
    doc_name: str,
    content: str,
    session_id: str,
    user_scope_id: Optional[UUID],
    expires_at: str,
    now_iso: str,
    pipeline: Any,
    op_timeout: int,
) -> Dict[str, Any]:
    """
    Index a document using two-tier hierarchical structure:
    - Tier 1: one Memory row per section (embedding = LLM summary)
    - Tier 2: chunks of section text linked to the section Memory
    Returns {"sections": int, "chunks": int, "method": "hierarchical"|"flat_fallback"}.
    """
    max_sections = max(2, min(50, int(Config.get("attachment_rag_hierarchical_max_sections", 15) or 15)))
    sections = _split_into_sections(content, min_section_chars=500, max_section_chars=5000)

    if len(sections) < 2:
        return {"sections": 0, "chunks": 0, "method": "flat_fallback"}

    per_sec_timeout = max(3, min(8, op_timeout // max(1, len(sections))))
    total_sections = 0

    for sec in sections[:max_sections]:
        summary = _summarize_section_llm(sec["text"], sec["title"], timeout_sec=per_sec_timeout)
        section_meta = {
            # title = summary so that Memory.embedding encodes the section semantics
            "title": summary,
            "type": "attachment_section",
            "source": ATTACHMENT_SOURCE,
            "session_id": str(session_id),
            "attachment_name": doc_name,
            "section_index": sec["index"],
            "section_title": sec["title"],
            "section_summary": summary,
            "expires_at": expires_at,
            "indexed_at": now_iso,
        }
        await asyncio.wait_for(
            pipeline.ingest(
                content=sec["text"],
                metadata=section_meta,
                parent_id=None,
                auto_connect=False,
                user_scope_id=user_scope_id,
            ),
            timeout=per_sec_timeout + 5,
        )
        total_sections += 1

    return {"sections": total_sections, "chunks": total_sections, "method": "hierarchical"}


# ── End hierarchical helpers ───────────────────────────────────────────────────


async def _replace_session_vector_once_async(
    *,
    session_id: str,
    user_scope_id: Optional[UUID],
    documents: List[Dict[str, Any]],
    max_chars: int,
    now_iso: str,
    expires_at: str,
    op_timeout: int,
    cache_key: str,
    docs_fp: str,
) -> Dict[str, Any]:
    try:
        async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
            delete_filters = [
                Memory.meta["source"].astext == ATTACHMENT_SOURCE,
                Memory.meta["session_id"].astext == str(session_id),
                *_scope_filters(user_scope_id),
            ]
            deleted_old = await db.execute(delete(Memory).where(and_(*delete_filters)))
            indexed = 0

            hierarchical_enabled = bool(Config.get("attachment_rag_hierarchical_enabled", False))
            hierarchical_min_chars = max(500, int(Config.get("attachment_rag_hierarchical_min_chars", 4000) or 4000))

            for i, doc in enumerate(documents):
                name = str((doc or {}).get("name") or f"Attachment {i + 1}").strip()
                content = str((doc or {}).get("content") or "").strip()
                if not content:
                    continue

                truncated = False
                if len(content) > max_chars:
                    content = content[:max_chars].rstrip() + "\n\n... [attachment truncated]"
                    truncated = True

                from vaf.memory.rag import RagPipeline

                pipeline = RagPipeline(db)

                # Hierarchical path: build section-level index for large structured docs
                use_hierarchical = hierarchical_enabled and len(content) >= hierarchical_min_chars
                if use_hierarchical:
                    try:
                        h = await asyncio.wait_for(
                            _hierarchical_ingest_doc(
                                doc_name=name,
                                content=content,
                                session_id=session_id,
                                user_scope_id=user_scope_id,
                                expires_at=expires_at,
                                now_iso=now_iso,
                                pipeline=pipeline,
                                op_timeout=op_timeout,
                            ),
                            timeout=op_timeout,
                        )
                        if h["method"] == "hierarchical":
                            indexed += 1
                            append_domain_log(
                                "rag",
                                f"ATTACH_HIER_INDEX doc={name!r} sections={h['sections']} chunks={h['chunks']}",
                            )
                            continue  # skip flat ingest below
                    except Exception as hier_exc:
                        append_domain_log("rag", f"ATTACH_HIER_INDEX_FAIL doc={name!r} err={hier_exc} falling_back_to_flat")

                # Flat ingest (default path or hierarchical fallback)
                meta = {
                    "title": f"Attachment: {name}",
                    "type": "attachment_ephemeral",
                    "source": ATTACHMENT_SOURCE,
                    "session_id": str(session_id),
                    "attachment_name": name,
                    "expires_at": expires_at,
                    "indexed_at": now_iso,
                    "truncated": truncated,
                }
                await asyncio.wait_for(
                    pipeline.ingest(
                        content=content,
                        metadata=meta,
                        auto_connect=False,
                        user_scope_id=user_scope_id,
                    ),
                    timeout=op_timeout,
                )
                indexed += 1

            append_domain_log(
                "rag",
                (
                    f"ATTACH_INDEX session={session_id} scope={user_scope_id} indexed={indexed} "
                    f"deleted_old={int(getattr(deleted_old, 'rowcount', 0) or 0)} safe_mode=False"
                ),
            )
            with _fingerprint_lock:
                _session_fingerprint_cache[cache_key] = docs_fp
            return {
                "indexed": indexed,
                "deleted_old": int(getattr(deleted_old, "rowcount", 0) or 0),
                "enabled": True,
                "safe_mode": False,
            }
    finally:
        _maybe_unload_attachment_vector_model("index")


async def _replace_session_vector_coalesced_async(
    *,
    session_id: str,
    user_scope_id: Optional[UUID],
    documents: List[Dict[str, Any]],
    max_chars: int,
    now_iso: str,
    expires_at: str,
    op_timeout: int,
    cache_key: str,
    docs_fp: str,
) -> Dict[str, Any]:
    with _vector_coalesce_lock:
        state = _vector_index_flights.get(cache_key)
        if state and state.get("running"):
            state["pending_documents"] = documents
            state["pending_fp"] = docs_fp
            _vector_index_flights[cache_key] = state
            append_domain_log("rag", f"ATTACH_INDEX_COALESCED_QUEUED session={session_id} scope={user_scope_id}")
            return {"indexed": 0, "deleted_old": 0, "enabled": True, "safe_mode": False, "coalesced": True, "queued": True}

        _vector_index_flights[cache_key] = {
            "running": True,
            "pending_documents": None,
            "pending_fp": None,
        }

    runs = 0
    result: Dict[str, Any] = {"indexed": 0, "deleted_old": 0, "enabled": True, "safe_mode": False}
    current_docs = documents
    current_fp = docs_fp
    debounce_ms = int(Config.get("attachment_rag_vector_coalesce_debounce_ms", 300) or 300)
    debounce_ms = max(0, min(5000, debounce_ms))
    try:
        while True:
            # Keep existing skip-unchanged guard for each coalesced pass.
            with _fingerprint_lock:
                prev_fp = _session_fingerprint_cache.get(cache_key)
            if prev_fp and prev_fp == current_fp:
                result = {"indexed": 0, "deleted_old": 0, "enabled": True, "safe_mode": False, "skipped_unchanged": True}
            else:
                if not _allow_vector_index_now():
                    append_domain_log(
                        "rag",
                        f"ATTACH_INDEX_THROTTLED session={session_id} scope={user_scope_id} safe_mode=False",
                    )
                    result = {"indexed": 0, "deleted_old": 0, "enabled": True, "safe_mode": False, "throttled": True}
                else:
                    result = await _replace_session_vector_once_async(
                        session_id=session_id,
                        user_scope_id=user_scope_id,
                        documents=current_docs,
                        max_chars=max_chars,
                        now_iso=now_iso,
                        expires_at=expires_at,
                        op_timeout=op_timeout,
                        cache_key=cache_key,
                        docs_fp=current_fp,
                    )
            runs += 1

            with _vector_coalesce_lock:
                st = _vector_index_flights.get(cache_key) or {}
                pending_docs = st.get("pending_documents")
                pending_fp = st.get("pending_fp")
                st["pending_documents"] = None
                st["pending_fp"] = None
                _vector_index_flights[cache_key] = st

            if pending_docs is None:
                break
            if debounce_ms > 0:
                await asyncio.sleep(debounce_ms / 1000.0)
            current_docs = pending_docs
            current_fp = str(pending_fp or _docs_fingerprint(current_docs))
            now_iso = _now_iso()
            ttl_hours = int(Config.get("attachment_rag_ttl_hours", 24) or 24)
            expires_at = (datetime.utcnow() + timedelta(hours=max(1, ttl_hours))).isoformat()

        result["coalesced_runs"] = runs
        return result
    finally:
        with _vector_coalesce_lock:
            st = _vector_index_flights.get(cache_key)
            if st:
                st["running"] = False
                if st.get("pending_documents") is None:
                    _vector_index_flights.pop(cache_key, None)
                else:
                    _vector_index_flights[cache_key] = st


def _vector_runner_main() -> None:
    """
    Persistent single-thread async runner for vector-path sync calls.

    Rationale:
    - Keep one event loop alive instead of creating/closing loops per call.
    - Serialize attachment vector operations to reduce lifecycle churn.
    - Mirror the stable bounded-worker pattern used in mature memory flows.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        while True:
            try:
                job = _vector_runner_queue.get(timeout=0.5)
            except Empty:
                continue
            if not isinstance(job, dict):
                _vector_runner_queue.task_done()
                continue
            if job.get("type") == "stop":
                _vector_runner_queue.task_done()
                break

            done_evt: threading.Event = job["done_evt"]
            result_box: Dict[str, Any] = job["result_box"]
            timeout_sec = int(job["timeout_sec"])
            coro_factory = job["coro_factory"]

            try:
                coro = coro_factory()
                result = loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout_sec))
                result_box["result"] = result
            except Exception as e:
                result_box["error"] = e
            finally:
                done_evt.set()
                _vector_runner_queue.task_done()
    finally:
        try:
            loop.stop()
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _ensure_vector_runner() -> None:
    global _vector_runner_thread
    with _vector_runner_lock:
        if _vector_runner_thread is not None and _vector_runner_thread.is_alive():
            return
        _vector_runner_thread = threading.Thread(
            target=_vector_runner_main,
            daemon=True,
            name="attachment-rag-vector-runner",
        )
        _vector_runner_thread.start()


async def _cleanup_expired_async(user_scope_id: Optional[UUID] = None) -> int:
    if _attachment_rag_killed:
        return 0
    if _safe_mode_enabled():
        return _safe_store_cleanup_expired()
    now_iso = _now_iso()
    async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
        filters = [
            Memory.meta["source"].astext == ATTACHMENT_SOURCE,
            Memory.meta["expires_at"].astext.isnot(None),
            Memory.meta["expires_at"].astext < now_iso,
            *_scope_filters(user_scope_id),
        ]
        result = await db.execute(delete(Memory).where(and_(*filters)))
        return int(getattr(result, "rowcount", 0) or 0)


async def _replace_session_async(
    session_id: str,
    user_scope_id: Optional[UUID],
    documents: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if _attachment_rag_killed:
        return {"indexed": 0, "deleted_old": 0, "enabled": False, "killed": True}
    if _trip_memory_killer("replace_session.pre"):
        return {"indexed": 0, "deleted_old": 0, "enabled": False, "killed": True}
    if not (Config.get("attachment_rag_enabled", False)):
        return {"indexed": 0, "deleted_old": 0, "enabled": False}

    ttl_hours = int(Config.get("attachment_rag_ttl_hours", 24) or 24)
    max_chars = int(Config.get("attachment_rag_max_chars_per_doc", 24000) or 24000)
    now_iso = _now_iso()
    expires_at = (datetime.utcnow() + timedelta(hours=max(1, ttl_hours))).isoformat()
    op_timeout = _op_timeout_sec()

    # Skip redundant re-index when documents are unchanged for this session+scope.
    cache_key = _session_cache_key(session_id, user_scope_id)
    docs_fp = _docs_fingerprint(documents)
    with _fingerprint_lock:
        prev_fp = _session_fingerprint_cache.get(cache_key)
    if prev_fp and prev_fp == docs_fp:
        append_domain_log("rag", f"ATTACH_INDEX_SKIP_UNCHANGED session={session_id} scope={user_scope_id}")
        return {"indexed": 0, "deleted_old": 0, "enabled": True, "skipped_unchanged": True}

    safe_mode = _safe_mode_enabled()
    if safe_mode:
        # RAM rationale: Safe-mode bypasses DB/vector writes entirely.
        # Keeping this path in-memory removes the heavy per-turn resource churn
        # that previously triggered large memory growth.
        cache_key = _session_cache_key(session_id, user_scope_id)
        with _safe_store_lock:
            previous = _safe_session_store.get(cache_key) or {}
            deleted_old = len(previous.get("docs") or [])
            safe_docs: List[Dict[str, Any]] = []
            for i, doc in enumerate(documents):
                name = str((doc or {}).get("name") or f"Attachment {i + 1}").strip()
                content = str((doc or {}).get("content") or "").strip()
                if not content:
                    continue
                truncated = False
                if len(content) > max_chars:
                    content = content[:max_chars].rstrip() + "\n\n... [attachment truncated]"
                    truncated = True
                safe_docs.append(
                    {
                        "attachment_name": name,
                        "content": content,
                        "metadata": {
                            "title": f"Attachment: {name}",
                            "type": "attachment_ephemeral",
                            "source": ATTACHMENT_SOURCE,
                            "session_id": str(session_id),
                            "attachment_name": name,
                            "expires_at": expires_at,
                            "indexed_at": now_iso,
                            "truncated": truncated,
                            "lane_mode": "lexical_safe",
                        },
                    }
                )
            _safe_session_store[cache_key] = {
                "session_id": str(session_id),
                "user_scope_id": str(user_scope_id) if user_scope_id else None,
                "expires_at": datetime.utcnow() + timedelta(hours=max(1, ttl_hours)),
                "docs": safe_docs,
            }
        append_domain_log(
            "rag",
            (
                f"ATTACH_INDEX session={session_id} scope={user_scope_id} indexed={len(safe_docs)} "
                f"deleted_old={deleted_old} safe_mode={safe_mode}"
            ),
        )
        with _fingerprint_lock:
            _session_fingerprint_cache[cache_key] = docs_fp
        return {
            "indexed": len(safe_docs),
            "deleted_old": int(deleted_old),
            "enabled": True,
            "safe_mode": safe_mode,
        }

    if _vector_coalesce_enabled():
        return await _replace_session_vector_coalesced_async(
            session_id=session_id,
            user_scope_id=user_scope_id,
            documents=documents,
            max_chars=max_chars,
            now_iso=now_iso,
            expires_at=expires_at,
            op_timeout=op_timeout,
            cache_key=cache_key,
            docs_fp=docs_fp,
        )

    if not _allow_vector_index_now():
        append_domain_log(
            "rag",
            f"ATTACH_INDEX_THROTTLED session={session_id} scope={user_scope_id} safe_mode={safe_mode}",
        )
        return {"indexed": 0, "deleted_old": 0, "enabled": True, "safe_mode": safe_mode, "throttled": True}

    return await _replace_session_vector_once_async(
        session_id=session_id,
        user_scope_id=user_scope_id,
        documents=documents,
        max_chars=max_chars,
        now_iso=now_iso,
        expires_at=expires_at,
        op_timeout=op_timeout,
        cache_key=cache_key,
        docs_fp=docs_fp,
    )


async def _clear_session_async(session_id: str, user_scope_id: Optional[UUID]) -> int:
    if _safe_mode_enabled():
        cache_key = _session_cache_key(session_id, user_scope_id)
        with _safe_store_lock:
            previous = _safe_session_store.pop(cache_key, None) or {}
            cleared = len(previous.get("docs") or [])
        with _fingerprint_lock:
            _session_fingerprint_cache.pop(cache_key, None)
        append_domain_log("rag", f"ATTACH_CLEAR session={session_id} scope={user_scope_id} cleared={cleared}")
        return int(cleared)

    async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
        filters = [
            Memory.meta["source"].astext == ATTACHMENT_SOURCE,
            Memory.meta["session_id"].astext == str(session_id),
            *_scope_filters(user_scope_id),
        ]
        result = await db.execute(delete(Memory).where(and_(*filters)))
        cleared = int(getattr(result, "rowcount", 0) or 0)
        with _fingerprint_lock:
            _session_fingerprint_cache.pop(_session_cache_key(session_id, user_scope_id), None)
        append_domain_log("rag", f"ATTACH_CLEAR session={session_id} scope={user_scope_id} cleared={cleared}")
        return cleared


async def _search_session_async(
    query: str,
    session_id: str,
    user_scope_id: Optional[UUID],
) -> List[Dict[str, Any]]:
    if _attachment_rag_killed:
        return []
    if _trip_memory_killer("search_session.pre"):
        return []
    if not (Config.get("attachment_rag_enabled", False)):
        return []

    k = int(Config.get("attachment_rag_k", 4) or 4)
    k = max(1, min(12, k))
    vector_threshold = float(Config.get("attachment_rag_threshold", 0.28) or 0.28)
    vector_threshold = max(0.0, min(1.0, vector_threshold))
    lexical_min_score = float(Config.get("attachment_rag_lexical_min_score", 0.05) or 0.05)
    lexical_min_score = max(0.0, min(1.0, lexical_min_score))
    snippet_chars = int(Config.get("attachment_rag_snippet_chars", 900) or 900)
    snippet_chars = max(200, min(4000, snippet_chars))
    safe_mode = _safe_mode_enabled()

    # Lazy cleanup on query path (cheap safety net)
    await _cleanup_expired_async(user_scope_id=user_scope_id)

    q = _normalize_query(query or "")
    if not q:
        q = "Summarize the attached document content."
    query_tokens = _tokenize_lexical(q)
    if not query_tokens:
        query_tokens = _tokenize_lexical("attachment summarize")

    if safe_mode:
        # RAM rationale: lexical scan over bounded per-session docs (no embed/model
        # invocation, no pgvector query path) for deterministic low memory usage.
        _safe_store_cleanup_expired()
        cache_key = _session_cache_key(session_id, user_scope_id)
        with _safe_store_lock:
            docs = list((_safe_session_store.get(cache_key) or {}).get("docs") or [])
        scored: List[Dict[str, Any]] = []
        lexical_scored: List[float] = []
        for doc in docs:
            content = str((doc or {}).get("content") or "")
            score = _lexical_score(q, query_tokens, content)
            lexical_scored.append(float(score))
            if score < lexical_min_score:
                continue
            meta = (doc or {}).get("metadata") or {}
            snippet = _build_lexical_snippet(content, query_tokens, snippet_chars)
            scored.append(
                {
                    "text": snippet,
                    "score": score,
                    "attachment_name": str(meta.get("attachment_name") or meta.get("title") or "Attachment"),
                }
            )
        if bool(Config.get("debug_logs_enabled", True)):
            top_scored = sorted(lexical_scored, reverse=True)[:5]
            top_scored_str = ",".join(f"{s:.3f}" for s in top_scored) if top_scored else "none"
            append_domain_log(
                "rag",
                (
                    f"ATTACH_LEXICAL_DEBUG mode=safe session={session_id} scope={user_scope_id} "
                    f"scored_rows={len(lexical_scored)} kept_after_min_score={len(scored)} "
                    f"lexical_min_score={lexical_min_score:.3f} top_scores={top_scored_str}"
                ),
            )

        scored.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        return scored[:k]

    if not _allow_vector_search_now():
        append_domain_log(
            "rag",
            f"ATTACH_SEARCH_THROTTLED session={session_id} scope={user_scope_id} safe_mode={safe_mode}",
        )
        return []

    try:
        op_timeout = _op_timeout_sec()
        async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
            from vaf.memory.rag import RagPipeline
            from vaf.memory.models import Chunk as _Chunk

            hybrid_enabled = bool(Config.get("attachment_rag_hybrid_enabled", True))
            hybrid_rrf_k = int(Config.get("attachment_rag_hybrid_rrf_k", 60) or 60)
            hybrid_rrf_k = max(1, min(500, hybrid_rrf_k))
            vector_k = int(Config.get("attachment_rag_hybrid_vector_k", max(k * 4, 16)) or max(k * 4, 16))
            vector_k = max(k, min(80, vector_k))

            # ── Two-tier hierarchical retrieval ──────────────────────────────
            # Detect whether this session was indexed hierarchically
            _hier_probe = (
                select(Memory.id)
                .where(
                    and_(
                        Memory.meta["source"].astext == ATTACHMENT_SOURCE,
                        Memory.meta["session_id"].astext == str(session_id),
                        Memory.meta["type"].astext == "attachment_section",
                        *_scope_filters(user_scope_id),
                    )
                )
                .limit(1)
            )
            has_hierarchical = (await db.execute(_hier_probe)).scalar() is not None

            if has_hierarchical:
                try:
                    coarse_k = max(1, min(10, int(Config.get("attachment_rag_hierarchical_coarse_k", 3) or 3)))
                    from vaf.memory.embeddings import get_embedding_service as _get_emb
                    q_vec = await asyncio.wait_for(_get_emb().embed(q), timeout=op_timeout)
                    max_distance = 1.0 - vector_threshold

                    # Tier 1: cosine search over section Memory.embedding
                    _tier1_stmt = (
                        select(Memory, Memory.embedding.cosine_distance(q_vec).label("dist"))
                        .where(
                            and_(
                                Memory.meta["source"].astext == ATTACHMENT_SOURCE,
                                Memory.meta["session_id"].astext == str(session_id),
                                Memory.meta["type"].astext == "attachment_section",
                                Memory.embedding.isnot(None),
                                *_scope_filters(user_scope_id),
                            )
                        )
                        .order_by("dist")
                        .limit(coarse_k)
                    )
                    tier1_rows = (await db.execute(_tier1_stmt)).all()

                    if tier1_rows:
                        top_section_ids = [row[0].id for row in tier1_rows]
                        sec_meta_map = {str(row[0].id): row[0].meta or {} for row in tier1_rows}

                        # Tier 2: chunk search scoped to the top sections only
                        _tier2_stmt = (
                            select(_Chunk, _Chunk.embedding.cosine_distance(q_vec).label("dist"), Memory)
                            .join(Memory, _Chunk.memory_id == Memory.id)
                            .where(
                                and_(
                                    _Chunk.memory_id.in_(top_section_ids),
                                    _Chunk.embedding.cosine_distance(q_vec) < max_distance,
                                )
                            )
                            .order_by("dist")
                            .limit(vector_k)
                        )
                        tier2_rows = (await db.execute(_tier2_stmt)).all()

                        hier_results: List[Dict[str, Any]] = []
                        for row in tier2_rows:
                            chunk, _dist, mem = row[0], row[1], row[2]
                            sm = sec_meta_map.get(str(mem.id), mem.meta or {})
                            sec_title = str(sm.get("section_title") or "Section")
                            sec_summary = str(sm.get("section_summary") or "")
                            attachment_name = str(sm.get("attachment_name") or "Attachment")
                            prefix = f"[{sec_title}]\n{sec_summary}\n\n" if sec_summary else f"[{sec_title}]\n"
                            body = prefix + (chunk.text or "").strip()
                            if len(body) > snippet_chars:
                                body = body[:snippet_chars].rstrip() + "\n... [truncated]"
                            hier_results.append(
                                {
                                    "text": body,
                                    "score": float(1.0 - _dist),
                                    "attachment_name": attachment_name,
                                }
                            )

                        if hier_results:
                            append_domain_log(
                                "rag",
                                f"ATTACH_HIER_SEARCH session={session_id} scope={user_scope_id} "
                                f"tier1_sections={len(tier1_rows)} tier2_chunks={len(hier_results)} k={k}",
                            )
                            return hier_results[:k]
                        # tier2 empty → fall through to flat search
                except Exception as hier_search_exc:
                    append_domain_log("rag", f"ATTACH_HIER_SEARCH_FAIL session={session_id} err={hier_search_exc} falling_back_to_flat")
            # ── End two-tier hierarchical retrieval ───────────────────────────

            pipeline = RagPipeline(db)
            sources = await asyncio.wait_for(
                pipeline.search(
                    q,
                    k=vector_k,
                    threshold=vector_threshold,
                    metadata_filter={"source": ATTACHMENT_SOURCE, "session_id": str(session_id)},
                    user_scope_id=user_scope_id,
                ),
                timeout=op_timeout,
            )

            vector_ranked: List[Dict[str, Any]] = []
            for src in sources:
                text = (src.text or "").strip()
                if len(text) > snippet_chars:
                    text = text[:snippet_chars].rstrip() + "\n... [snippet truncated]"
                key = f"m:{str(getattr(src, 'memory_id', '') or '')}"
                if key == "m:":
                    key = f"v:{hash((text, (src.metadata or {}).get('attachment_name') or 'Attachment'))}"
                vector_ranked.append(
                    {
                        "key": key,
                        "text": text,
                        "score": src.score,
                        "attachment_name": (src.metadata or {}).get("attachment_name") or "Attachment",
                    }
                )

            if not hybrid_enabled:
                return vector_ranked[:k]

            lexical_k = int(Config.get("attachment_rag_hybrid_lexical_k", max(k * 4, 16)) or max(k * 4, 16))
            lexical_k = max(k, min(120, lexical_k))
            scan_limit = int(Config.get("attachment_rag_hybrid_lexical_scan_limit", 96) or 96)
            scan_limit = max(lexical_k, min(400, scan_limit))
            now_iso = _now_iso()
            filters = [
                Memory.meta["source"].astext == ATTACHMENT_SOURCE,
                Memory.meta["session_id"].astext == str(session_id),
                *_scope_filters(user_scope_id),
                or_(
                    Memory.meta["expires_at"].astext.is_(None),
                    Memory.meta["expires_at"].astext >= now_iso,
                ),
            ]
            stmt = (
                select(Memory)
                .where(and_(*filters))
                .order_by(Memory.updated_at.desc())
                .limit(scan_limit)
            )
            rows = (await db.execute(stmt)).scalars().all()
            lexical_debug_enabled = bool(Config.get("debug_logs_enabled", True))
            if lexical_debug_enabled:
                append_domain_log(
                    "rag",
                    (
                        f"ATTACH_HYBRID_LEXICAL_DEBUG stage=rows session={session_id} scope={user_scope_id} "
                        f"pre_score_rows={len(rows)} scan_limit={scan_limit} lexical_min_score={lexical_min_score:.3f}"
                    ),
                )
            crypto = get_crypto()
            lexical_ranked: List[Dict[str, Any]] = []
            lexical_scored: List[float] = []
            for m in rows:
                try:
                    content = crypto.decrypt(m.encrypted_content, m.nonce)
                except Exception:
                    continue
                lex_score = _lexical_score(q, query_tokens, content)
                lexical_scored.append(float(lex_score))
                if lex_score < lexical_min_score:
                    continue
                meta = m.meta or {}
                snippet = _build_lexical_snippet(content, query_tokens, snippet_chars)
                lexical_ranked.append(
                    {
                        "key": f"m:{str(getattr(m, 'id', '') or '')}",
                        "text": snippet,
                        "score": float(lex_score),
                        "attachment_name": str(meta.get("attachment_name") or meta.get("title") or "Attachment"),
                    }
                )
            if lexical_debug_enabled:
                top_scored = sorted(lexical_scored, reverse=True)[:5]
                top_scored_str = ",".join(f"{s:.3f}" for s in top_scored) if top_scored else "none"
                append_domain_log(
                    "rag",
                    (
                        f"ATTACH_HYBRID_LEXICAL_DEBUG stage=scored session={session_id} scope={user_scope_id} "
                        f"scored_rows={len(lexical_scored)} kept_after_min_score={len(lexical_ranked)} "
                        f"top_scores={top_scored_str}"
                    ),
                )

            lexical_ranked.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
            lexical_ranked = lexical_ranked[:lexical_k]
            vector_ranked = vector_ranked[:vector_k]
            fused = _rrf_fuse_ranked(
                vector_ranked,
                lexical_ranked,
                top_k=k,
                rrf_k=hybrid_rrf_k,
            )
            if fused:
                vector_keys = {str(it.get("key") or "") for it in vector_ranked if str(it.get("key") or "")}
                lexical_keys = {str(it.get("key") or "") for it in lexical_ranked if str(it.get("key") or "")}
                top_keys = [str(it.get("doc_key") or "") for it in fused if str(it.get("doc_key") or "")]
                both = 0
                vector_only = 0
                lexical_only = 0
                for key in top_keys:
                    in_v = key in vector_keys
                    in_l = key in lexical_keys
                    if in_v and in_l:
                        both += 1
                    elif in_v:
                        vector_only += 1
                    elif in_l:
                        lexical_only += 1
                append_domain_log(
                    "rag",
                    (
                        f"ATTACH_HYBRID_FUSION session={session_id} scope={user_scope_id} "
                        f"topk={len(top_keys)} both={both} vector_only={vector_only} lexical_only={lexical_only} "
                        f"vector_candidates={len(vector_keys)} lexical_candidates={len(lexical_keys)}"
                    ),
                )
                return [
                    {
                        "text": str(it.get("text") or ""),
                        "score": float(it.get("score") or 0.0),
                        "attachment_name": str(it.get("attachment_name") or "Attachment"),
                    }
                    for it in fused
                ]
            append_domain_log(
                "rag",
                (
                    f"ATTACH_HYBRID_FUSION session={session_id} scope={user_scope_id} "
                    f"topk=0 both=0 vector_only=0 lexical_only=0 "
                    f"vector_candidates={len(vector_ranked)} lexical_candidates={len(lexical_ranked)} fallback=vector_only"
                ),
            )
            return vector_ranked[:k]
    finally:
        _maybe_unload_attachment_vector_model("search")


async def _read_session_entries_async(
    session_id: str,
    user_scope_id: Optional[UUID],
    limit: int = 20,
) -> List[Dict[str, Any]]:
    if _attachment_rag_killed:
        return []
    if _safe_mode_enabled():
        _safe_store_cleanup_expired()
        cache_key = _session_cache_key(session_id, user_scope_id)
        with _safe_store_lock:
            docs = list((_safe_session_store.get(cache_key) or {}).get("docs") or [])[: max(1, min(200, int(limit or 20)))]
        out: List[Dict[str, Any]] = []
        for i, doc in enumerate(docs):
            out.append(
                {
                    "memory_id": f"safe:{cache_key}:{i}",
                    "attachment_name": str((doc or {}).get("attachment_name") or "Attachment"),
                    "content": str((doc or {}).get("content") or ""),
                    "metadata": (doc or {}).get("metadata") or {},
                }
            )
        return out
    now_iso = _now_iso()
    async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
        filters = [
            Memory.meta["source"].astext == ATTACHMENT_SOURCE,
            Memory.meta["session_id"].astext == str(session_id),
            *_scope_filters(user_scope_id),
            or_(
                Memory.meta["expires_at"].astext.is_(None),
                Memory.meta["expires_at"].astext >= now_iso,
            ),
        ]
        stmt = (
            select(Memory)
            .where(and_(*filters))
            .order_by(Memory.updated_at.desc())
            .limit(max(1, min(200, int(limit or 20))))
        )
        rows = (await db.execute(stmt)).scalars().all()

    crypto = get_crypto()
    out: List[Dict[str, Any]] = []
    for m in rows:
        try:
            content = crypto.decrypt(m.encrypted_content, m.nonce)
        except Exception:
            continue
        meta = m.meta or {}
        out.append(
            {
                "memory_id": str(m.id),
                "attachment_name": str(meta.get("attachment_name") or meta.get("title") or "Attachment"),
                "content": content,
                "metadata": meta,
            }
        )
    return out


async def index_session_attachments_async(
    session_id: str,
    user_scope_id: Optional[str | UUID],
    documents: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return await _replace_session_async(
        session_id=str(session_id),
        user_scope_id=_to_uuid(user_scope_id),
        documents=documents,
    )


async def clear_session_attachments_async(
    session_id: str,
    user_scope_id: Optional[str | UUID],
) -> int:
    return await _clear_session_async(
        session_id=str(session_id),
        user_scope_id=_to_uuid(user_scope_id),
    )


async def search_session_attachments_async(
    query: str,
    session_id: str,
    user_scope_id: Optional[str | UUID],
) -> List[Dict[str, Any]]:
    return await _search_session_async(
        query=query,
        session_id=str(session_id),
        user_scope_id=_to_uuid(user_scope_id),
    )


async def read_session_attachments_async(
    session_id: str,
    user_scope_id: Optional[str | UUID],
    limit: int = 20,
) -> List[Dict[str, Any]]:
    return await _read_session_entries_async(
        session_id=str(session_id),
        user_scope_id=_to_uuid(user_scope_id),
        limit=limit,
    )


def _run_async(coro_factory):
    """
    Run async attachment lane operation from sync callers.

    Memory-safety goal:
    - Avoid a long-lived dedicated event loop thread for attachment RAG.
      The incident showed repeated index/search/clear calls can accumulate resources
      when a dedicated loop/thread keeps asyncpg + asyncio internals alive forever.
    - Execute each op as a bounded unit:
      * if no loop is running: asyncio.run() creates/closes a fresh loop for this call.
      * if a loop is already running: execute in one short-lived helper thread.
    - Keep strict timeout + memory-guard checks around every call so runaway behavior
      is cut early instead of building up across turns.
    """
    if _attachment_rag_killed:
        raise RuntimeError(_attachment_rag_kill_reason or "Attachment RAG is killed by memory guard")
    if _trip_memory_killer("run_async.pre"):
        raise RuntimeError(_attachment_rag_kill_reason or "Attachment RAG is killed by memory guard")

    timeout_sec = int(Config.get("attachment_rag_sync_timeout_sec", 30) or 30)
    timeout_sec = max(5, min(120, timeout_sec))

    if not callable(coro_factory):
        raise TypeError("coro_factory must be callable and return a coroutine")

    # For safe-mode (in-memory lexical lane), per-call loop is acceptable and simple.
    # For vector mode, use persistent queue worker to reduce async lifecycle churn.
    if not _safe_mode_enabled():
        _ensure_vector_runner()
        done_evt = threading.Event()
        result_box: Dict[str, Any] = {}
        job = {
            "type": "run",
            "coro_factory": coro_factory,
            "timeout_sec": timeout_sec,
            "done_evt": done_evt,
            "result_box": result_box,
        }
        try:
            _vector_runner_queue.put_nowait(job)
        except Exception as e:
            raise RuntimeError(f"Attachment vector runner queue is full: {e}")

        waited = done_evt.wait(timeout=timeout_sec + 5)
        if not waited:
            raise TimeoutError(f"Attachment RAG operation timed out after {timeout_sec}s (queue worker)")
        if "error" in result_box:
            err = result_box["error"]
            if isinstance(err, asyncio.TimeoutError):
                raise TimeoutError(f"Attachment RAG operation timed out after {timeout_sec}s")
            raise err
        _trip_memory_killer("run_async.post")
        return result_box.get("result")

    async def _with_timeout():
        return await asyncio.wait_for(coro_factory(), timeout=timeout_sec)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        # Fresh loop per call. This keeps lifecycle deterministic (create -> run -> close)
        # and avoids a persistent attachment-specific event loop retaining memory.
        result = asyncio.run(_with_timeout())
        _trip_memory_killer("run_async.post")
        return result

    result_box = [None]
    error_box = [None]

    def _thread_runner():
        try:
            result_box[0] = asyncio.run(_with_timeout())
        except Exception as e:
            error_box[0] = e

    t = threading.Thread(target=_thread_runner, daemon=True, name="attachment-rag-call")
    t.start()
    t.join(timeout=timeout_sec + 2)
    if t.is_alive():
        raise TimeoutError(f"Attachment RAG operation timed out after {timeout_sec}s")
    if error_box[0] is not None:
        err = error_box[0]
        if isinstance(err, asyncio.TimeoutError):
            raise TimeoutError(f"Attachment RAG operation timed out after {timeout_sec}s")
        raise err

    _trip_memory_killer("run_async.post")
    return result_box[0]


def index_session_attachments_sync(
    session_id: str,
    user_scope_id: Optional[str | UUID],
    documents: List[Dict[str, Any]],
) -> Dict[str, Any]:
    scope_uuid = _to_uuid(user_scope_id)
    try:
        return _run_async(
            lambda: _replace_session_async(session_id=str(session_id), user_scope_id=scope_uuid, documents=documents)
        ) or {
            "indexed": 0,
            "deleted_old": 0,
            "enabled": bool(Config.get("attachment_rag_enabled", False)),
        }
    except Exception as e:
        append_domain_log("rag", f"ATTACH_INDEX_FAIL session={session_id} scope={scope_uuid} error={str(e)[:160]}")
        return {"indexed": 0, "deleted_old": 0, "enabled": False, "error": str(e)}


def clear_session_attachments_sync(
    session_id: str,
    user_scope_id: Optional[str | UUID],
) -> int:
    scope_uuid = _to_uuid(user_scope_id)
    try:
        return int(_run_async(lambda: _clear_session_async(session_id=str(session_id), user_scope_id=scope_uuid)) or 0)
    except Exception as e:
        append_domain_log("rag", f"ATTACH_CLEAR_FAIL session={session_id} scope={scope_uuid} error={str(e)[:160]}")
        return 0


def search_session_attachments_sync(
    query: str,
    session_id: str,
    user_scope_id: Optional[str | UUID],
) -> List[Dict[str, Any]]:
    scope_uuid = _to_uuid(user_scope_id)
    try:
        return _run_async(
            lambda: _search_session_async(query=query, session_id=str(session_id), user_scope_id=scope_uuid)
        ) or []
    except Exception as e:
        append_domain_log("rag", f"ATTACH_SEARCH_FAIL session={session_id} scope={scope_uuid} error={str(e)[:160]}")
        return []


def read_session_attachments_sync(
    session_id: str,
    user_scope_id: Optional[str | UUID],
    limit: int = 20,
) -> List[Dict[str, Any]]:
    scope_uuid = _to_uuid(user_scope_id)
    try:
        return _run_async(
            lambda: _read_session_entries_async(session_id=str(session_id), user_scope_id=scope_uuid, limit=limit)
        ) or []
    except Exception as e:
        append_domain_log("rag", f"ATTACH_READ_FAIL session={session_id} scope={scope_uuid} error={str(e)[:160]}")
        return []

