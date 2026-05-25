"""
learn_attached_knowledge: persist knowledge from ephemeral attachment lane
into long-term memory (type=knowledge).
"""

from __future__ import annotations

import re
from typing import Any, List, Tuple
from uuid import UUID

from vaf.tools.base import BaseTool
from vaf.memory.attachment_rag import read_session_attachments_sync
from vaf.tools.learn_document import _normalize_doc_tag, _merge_thin_pages, _analyze_document_llm

_PAGE_MARKER_RE = re.compile(r"^---\s*Page\s+(\d+)\s*---\s*$", re.MULTILINE)
_MAX_PAGE_CHARS = 4000  # per-page content cap sent to the vector store


def _split_by_pages(content: str) -> List[Tuple[int, str]]:
    matches = list(_PAGE_MARKER_RE.finditer(content))
    if len(matches) < 2:
        return []
    pages: List[Tuple[int, str]] = []
    for i, m in enumerate(matches):
        page_num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        text = content[start:end].strip()
        if text:
            if len(text) > _MAX_PAGE_CHARS:
                text = text[:_MAX_PAGE_CHARS].rstrip() + "\n... [truncated]"
            pages.append((page_num, text))
    return pages


class LearnAttachedKnowledgeTool(BaseTool):
    name = "learn_attached_knowledge"
    permission_level = "write"
    side_effect_class = "irreversible"
    description = (
        "Persist knowledge from currently attached Web UI documents into long-term memory. "
        "This requires explicit confirmation via confirm_learn=true. "
        "Use when the user says to remember/learn knowledge from current attachments for future chats."
    )
    parameters = {
        "type": "object",
        "properties": {
            "confirm_learn": {"type": "boolean", "description": "Must be true to confirm long-term learning from attached documents."},
            "title": {"type": "string", "description": "Optional title prefix for created knowledge memories."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional additional tags for created memories."},
            "max_items": {"type": "integer", "description": "Maximum number of attachment-derived memories to create (default 3, max 10)."},
        },
        "required": ["confirm_learn"],
    }

    def run(self, **kwargs) -> str:
        confirm_learn = bool(kwargs.get("confirm_learn", False))
        if not confirm_learn:
            return (
                "Blocked: learning attachment knowledge into long-term memory requires explicit confirmation. "
                "Ask the user for confirmation, then call learn_attached_knowledge(confirm_learn=true)."
            )

        session_id = (kwargs.get("session_id") or "").strip()
        if not session_id:
            try:
                from vaf.core.subagent_ipc import get_current_session_id
                session_id = (get_current_session_id() or "").strip()
            except Exception:
                session_id = ""

        if not session_id:
            return "Error: No active session found. This tool needs a current Web UI session with attached documents."

        user_scope_id = kwargs.get("user_scope_id")
        if isinstance(user_scope_id, str):
            try:
                user_scope_id = UUID(user_scope_id)
            except (ValueError, TypeError):
                user_scope_id = None

        max_items = int(kwargs.get("max_items", 3) or 3)
        max_items = max(1, min(10, max_items))
        title_prefix = (kwargs.get("title") or "Attachment knowledge").strip()
        tags: List[str] = [str(t).strip().lower() for t in (kwargs.get("tags") or []) if str(t).strip()]
        tags = list(dict.fromkeys(tags))[:10]

        entries = read_session_attachments_sync(session_id=session_id, user_scope_id=user_scope_id, limit=max_items)
        if not entries:
            return "No attached-document knowledge found for the current session."

        agent = kwargs.get("_agent")
        generate_fn = None
        if agent is not None:
            if hasattr(agent, "_generate_for_document_extraction"):
                generate_fn = agent._generate_for_document_extraction
            elif hasattr(agent, "_generate_for_compaction"):
                def generate_fn(prompt: str) -> str:
                    return agent._generate_for_compaction(prompt)

        entry_analyses: dict = {}
        if generate_fn:
            for entry in entries[:max_items]:
                att_name = str(entry.get("attachment_name") or "Attachment")
                content = str(entry.get("content") or "").strip()
                if not content:
                    continue
                pages = _split_by_pages(content)
                if not pages:
                    pages = [(1, content[:_MAX_PAGE_CHARS])]
                pages = _merge_thin_pages(pages)
                analysis = _analyze_document_llm(pages, att_name, generate_fn)
                entry_analyses[att_name] = {
                    "doc_summary": (analysis.get("doc_summary") or "").strip(),
                    "llm_tags": [str(t).strip().lower() for t in (analysis.get("doc_tags") or []) if str(t).strip()],
                    "page_analysis": {
                        int(p.get("page", 0)): p
                        for p in (analysis.get("pages") or [])
                        if isinstance(p, dict) and p.get("page")
                    },
                }

        from vaf.memory.database import get_db
        from vaf.memory.rag import RagPipeline
        import asyncio

        async def _transfer() -> int:
            from sqlalchemy import select, and_
            from vaf.memory.models import Memory
            async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
                pipeline = RagPipeline(db)
                created = 0
                for idx, entry in enumerate(entries[:max_items], 1):
                    att_name = str(entry.get("attachment_name") or f"Attachment {idx}")
                    content = str(entry.get("content") or "").strip()
                    if not content:
                        continue

                    doc_tag = _normalize_doc_tag(att_name)
                    doc_title = att_name
                    pages_stored = 0

                    ea = entry_analyses.get(att_name, {})
                    doc_summary = ea.get("doc_summary", "")
                    llm_tags = ea.get("llm_tags", [])
                    page_analysis = ea.get("page_analysis", {})
                    all_tags = list(dict.fromkeys([doc_tag, "knowledge", "from-attachment"] + llm_tags + tags))

                    pages = _split_by_pages(content)
                    if pages:
                        for page_num, page_text in pages:
                            if not page_text:
                                continue
                            pa = page_analysis.get(page_num, {})
                            page_title = (pa.get("title") or "").strip() or f"{doc_title} \u2013 Page {page_num}"
                            llm_content = (pa.get("content") or "").strip()
                            stored_parts = []
                            if doc_summary:
                                stored_parts.append(f"[{doc_summary}]")
                            if llm_content:
                                stored_parts.append(llm_content)
                            stored_parts.append(page_text)
                            stored_content = "\n\n".join(stored_parts)
                            meta = {
                                "title": page_title,
                                "type": "document",
                                "source": "attachment_transfer",
                                "knowledge_origin": "attachment",
                                "attachment_name": att_name,
                                "attachment_session_id": session_id,
                                "doc_tag": doc_tag,
                                "tags": all_tags,
                            }
                            await pipeline.ingest(
                                content=stored_content,
                                metadata=meta,
                                auto_connect=True,
                                user_scope_id=user_scope_id,
                            )
                            created += 1
                            pages_stored += 1
                    else:
                        pa = page_analysis.get(1, {})
                        page_title = (pa.get("title") or "").strip() or doc_title
                        llm_content = (pa.get("content") or "").strip()
                        stored_parts = []
                        if doc_summary:
                            stored_parts.append(f"[{doc_summary}]")
                        if llm_content:
                            stored_parts.append(llm_content)
                        stored_parts.append(content[:16000].rstrip())
                        stored_content = "\n\n".join(stored_parts)
                        meta = {
                            "title": page_title,
                            "type": "knowledge",
                            "source": "attachment_transfer",
                            "knowledge_origin": "attachment",
                            "attachment_name": att_name,
                            "attachment_session_id": session_id,
                            "doc_tag": doc_tag,
                            "tags": all_tags,
                        }
                        await pipeline.ingest(
                            content=stored_content,
                            metadata=meta,
                            auto_connect=True,
                            user_scope_id=user_scope_id,
                        )
                        created += 1
                        pages_stored += 1

                    if pages_stored > 0:
                        conditions = [
                            Memory.is_deleted == False,
                            Memory.meta["type"].as_string() == "document_index",
                            Memory.meta["doc_tag"].as_string() == doc_tag,
                        ]
                        if user_scope_id is not None:
                            conditions.append(Memory.user_scope_id == user_scope_id)
                        result = await db.execute(select(Memory).where(and_(*conditions)))
                        existing_root = result.scalar_one_or_none()

                        if existing_root is not None:
                            meta_upd = dict(existing_root.meta or {})
                            meta_upd["page_count"] = pages_stored
                            if doc_summary:
                                meta_upd["doc_summary"] = doc_summary
                            existing_root.meta = meta_upd
                        else:
                            index_content = f"Document index: {doc_title}."
                            if doc_summary:
                                index_content += f" {doc_summary}"
                            index_content += f" Contains {pages_stored} page(s) of knowledge from an attached document."
                            index_tags = list(dict.fromkeys([doc_tag, "from-attachment"] + llm_tags + tags))
                            index_meta: dict = {
                                "type": "document_index",
                                "source": "attachment_transfer",
                                "title": doc_title,
                                "doc_tag": doc_tag,
                                "page_count": pages_stored,
                                "tags": index_tags,
                            }
                            if doc_summary:
                                index_meta["doc_summary"] = doc_summary
                            await pipeline.ingest(
                                content=index_content,
                                metadata=index_meta,
                                user_scope_id=user_scope_id,
                                auto_connect=False,
                            )

                return created

        try:
            created_count = asyncio.run(_transfer())
        except RuntimeError:
            import threading
            out = {"count": 0, "error": None}
            def _runner():
                try:
                    out["count"] = asyncio.run(_transfer())
                except Exception as e:
                    out["error"] = e
            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            t.join(timeout=30)
            if t.is_alive():
                return "Error: Timed out while transferring attachment knowledge."
            if out["error"] is not None:
                return f"Error: Failed to transfer attachment knowledge: {out['error']}"
            created_count = int(out["count"])
        except Exception as e:
            return f"Error: Failed to transfer attachment knowledge: {e}"

        if created_count <= 0:
            return "No knowledge memories were created (attachments may be empty or unavailable)."
        return (
            f"Learned {created_count} knowledge memor"
            f"{'y' if created_count == 1 else 'ies'} from attachment(s) into long-term memory "
            f"(each PDF page stored individually for precise retrieval)."
        )
