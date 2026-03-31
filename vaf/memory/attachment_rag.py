"""
Ephemeral Attachment RAG lane for Web UI sidebar documents.

This module keeps attachment-derived retrieval data separated from long-term memory
by using a dedicated metadata source and strict session+user scoping.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import and_, delete, or_, select

from vaf.core.config import Config
from vaf.core.log_helper import append_domain_log
from vaf.memory.crypto import get_crypto
from vaf.memory.database import get_db
from vaf.memory.models import Memory
from vaf.memory.rag import RagPipeline


ATTACHMENT_SOURCE = "attachment_ephemeral"


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


async def _cleanup_expired_async(user_scope_id: Optional[UUID] = None) -> int:
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
    if not (Config.get("attachment_rag_enabled", True)):
        return {"indexed": 0, "deleted_old": 0, "enabled": False}

    ttl_hours = int(Config.get("attachment_rag_ttl_hours", 24) or 24)
    max_chars = int(Config.get("attachment_rag_max_chars_per_doc", 24000) or 24000)
    now_iso = _now_iso()
    expires_at = (datetime.utcnow() + timedelta(hours=max(1, ttl_hours))).isoformat()

    async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
        # Keep lane clean and deterministic: one session scope index gets rebuilt on update.
        delete_filters = [
            Memory.meta["source"].astext == ATTACHMENT_SOURCE,
            Memory.meta["session_id"].astext == str(session_id),
            *_scope_filters(user_scope_id),
        ]
        deleted_old = await db.execute(delete(Memory).where(and_(*delete_filters)))

        pipeline = RagPipeline(db)
        indexed = 0

        for i, doc in enumerate(documents):
            name = str((doc or {}).get("name") or f"Attachment {i + 1}").strip()
            content = str((doc or {}).get("content") or "").strip()
            if not content:
                continue

            truncated = False
            if len(content) > max_chars:
                content = content[:max_chars].rstrip() + "\n\n... [attachment truncated]"
                truncated = True

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
            await pipeline.ingest(
                content=content,
                metadata=meta,
                auto_connect=False,
                user_scope_id=user_scope_id,
            )
            indexed += 1

        append_domain_log(
            "rag",
            f"ATTACH_INDEX session={session_id} scope={user_scope_id} indexed={indexed} deleted_old={int(getattr(deleted_old, 'rowcount', 0) or 0)}",
        )
        return {
            "indexed": indexed,
            "deleted_old": int(getattr(deleted_old, "rowcount", 0) or 0),
            "enabled": True,
        }


async def _clear_session_async(session_id: str, user_scope_id: Optional[UUID]) -> int:
    async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
        filters = [
            Memory.meta["source"].astext == ATTACHMENT_SOURCE,
            Memory.meta["session_id"].astext == str(session_id),
            *_scope_filters(user_scope_id),
        ]
        result = await db.execute(delete(Memory).where(and_(*filters)))
        cleared = int(getattr(result, "rowcount", 0) or 0)
        append_domain_log("rag", f"ATTACH_CLEAR session={session_id} scope={user_scope_id} cleared={cleared}")
        return cleared


async def _search_session_async(
    query: str,
    session_id: str,
    user_scope_id: Optional[UUID],
) -> List[Dict[str, Any]]:
    if not (Config.get("attachment_rag_enabled", True)):
        return []

    k = int(Config.get("attachment_rag_k", 4) or 4)
    k = max(1, min(12, k))
    threshold = float(Config.get("attachment_rag_threshold", 0.28) or 0.28)
    threshold = max(0.0, min(1.0, threshold))
    snippet_chars = int(Config.get("attachment_rag_snippet_chars", 900) or 900)
    snippet_chars = max(200, min(4000, snippet_chars))

    # Lazy cleanup on query path (cheap safety net)
    await _cleanup_expired_async(user_scope_id=user_scope_id)

    q = (query or "").strip()
    if not q:
        q = "Summarize the attached document content."

    async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
        pipeline = RagPipeline(db)
        sources = await pipeline.search(
            q,
            k=k,
            threshold=threshold,
            metadata_filter={"source": ATTACHMENT_SOURCE, "session_id": str(session_id)},
            user_scope_id=user_scope_id,
        )
        out: List[Dict[str, Any]] = []
        for src in sources:
            text = (src.text or "").strip()
            if len(text) > snippet_chars:
                text = text[:snippet_chars].rstrip() + "\n... [snippet truncated]"
            out.append(
                {
                    "text": text,
                    "score": src.score,
                    "attachment_name": (src.metadata or {}).get("attachment_name") or "Attachment",
                }
            )
        return out


async def _read_session_entries_async(
    session_id: str,
    user_scope_id: Optional[UUID],
    limit: int = 20,
) -> List[Dict[str, Any]]:
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


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return asyncio.run(coro)

    result_box = [None]
    error_box = [None]

    def _runner():
        try:
            result_box[0] = asyncio.run(coro)
        except Exception as e:
            error_box[0] = e

    import threading

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=30)
    if t.is_alive():
        return None
    if error_box[0] is not None:
        raise error_box[0]
    return result_box[0]


def index_session_attachments_sync(
    session_id: str,
    user_scope_id: Optional[str | UUID],
    documents: List[Dict[str, Any]],
) -> Dict[str, Any]:
    scope_uuid = _to_uuid(user_scope_id)
    return _run_async(_replace_session_async(session_id=str(session_id), user_scope_id=scope_uuid, documents=documents)) or {
        "indexed": 0,
        "deleted_old": 0,
        "enabled": bool(Config.get("attachment_rag_enabled", True)),
    }


def clear_session_attachments_sync(
    session_id: str,
    user_scope_id: Optional[str | UUID],
) -> int:
    scope_uuid = _to_uuid(user_scope_id)
    return int(_run_async(_clear_session_async(session_id=str(session_id), user_scope_id=scope_uuid)) or 0)


def search_session_attachments_sync(
    query: str,
    session_id: str,
    user_scope_id: Optional[str | UUID],
) -> List[Dict[str, Any]]:
    scope_uuid = _to_uuid(user_scope_id)
    return _run_async(_search_session_async(query=query, session_id=str(session_id), user_scope_id=scope_uuid)) or []


def read_session_attachments_sync(
    session_id: str,
    user_scope_id: Optional[str | UUID],
    limit: int = 20,
) -> List[Dict[str, Any]]:
    scope_uuid = _to_uuid(user_scope_id)
    return _run_async(_read_session_entries_async(session_id=str(session_id), user_scope_id=scope_uuid, limit=limit)) or []

