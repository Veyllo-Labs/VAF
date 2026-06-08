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
from vaf.tools.learn_document import _normalize_doc_tag, _clean_title

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

        # Read the FULL attachment content from the session (the librarian-extracted Markdown), not the
        # per-section attachment-lane rows -- so each attachment is learned as one whole document and
        # our own section split applies. (In vector mode the lane stores section rows, not full docs.)
        entries = []
        try:
            from vaf.core.session import SessionManager
            _session = SessionManager().load(session_id)
            _docs = (getattr(_session, "runtime_state", None) or {}).get("sidebar_documents") or []
            for _d in _docs[:max_items]:
                _name = str((_d or {}).get("name") or "Attachment")
                _content = str((_d or {}).get("content") or "").strip()
                if _content:
                    entries.append({"attachment_name": _name, "content": _content})
        except Exception:
            entries = []
        if not entries:
            # Fallback to the attachment lane (full docs in safe mode; section rows in vector mode).
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

        def _emit_cursor(phase: str, **kw) -> None:
            try:
                from vaf.core.web_interface import get_web_interface
                get_web_interface()._push_session_update(session_id, {
                    "type": "cursor_animation",
                    "phase": phase,
                    **kw,
                })
            except Exception:
                pass

        # Animate the whole learning duration: the document analysis below is the slow part, so emit
        # one "start" up front (and "end" at the very end). The front-end walks all rendered PDF pages
        # on a 2s loop; total is a best-effort fallback (the front-end prefers the real page count).
        _anim_total = 1
        for _e in entries[:max_items]:
            _ep = _split_by_pages(str(_e.get("content") or ""))
            _anim_total = max(_anim_total, len(_ep) if _ep else 1)
        _emit_cursor("start", total=_anim_total)

        from vaf.memory.database import get_db
        from vaf.tools.learn_document import ingest_document_knowledge
        import asyncio

        async def _transfer() -> int:
            created = 0
            async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
                for idx, entry in enumerate(entries[:max_items], 1):
                    att_name = str(entry.get("attachment_name") or f"Attachment {idx}")
                    content = str(entry.get("content") or "").strip()
                    if not content:
                        continue
                    res = await ingest_document_knowledge(
                        db,
                        content_markdown=content,
                        doc_title=_clean_title(att_name),
                        doc_tag=_normalize_doc_tag(_clean_title(att_name)),
                        source="attachment_transfer",
                        mem_type="document",
                        generate_fn=generate_fn,
                        user_scope_id=user_scope_id,
                        extra_tags=tags,
                        attachment_name=att_name,
                        session_id=session_id,
                    )
                    created += int(res.get("created", 0))
            _emit_cursor("end")
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
            f"(split into sections; each stored as a self-contained, contextual memory)."
        )
