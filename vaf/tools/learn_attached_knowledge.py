"""
learn_attached_knowledge: persist knowledge from ephemeral attachment lane
into long-term memory (type=knowledge).
"""

from __future__ import annotations

from typing import Any, List
from uuid import UUID

from vaf.tools.base import BaseTool
from vaf.memory.attachment_rag import read_session_attachments_sync


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
            "confirm_learn": {
                "type": "boolean",
                "description": "Must be true to confirm long-term learning from attached documents.",
            },
            "title": {
                "type": "string",
                "description": "Optional title prefix for created knowledge memories.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional additional tags for created memories.",
            },
            "max_items": {
                "type": "integer",
                "description": "Maximum number of attachment-derived memories to create (default 3, max 10).",
            },
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

        from vaf.memory.database import get_db
        from vaf.memory.rag import RagPipeline
        import asyncio

        async def _transfer() -> int:
            async with get_db(user_scope_id=str(user_scope_id) if user_scope_id else None) as db:
                pipeline = RagPipeline(db)
                created = 0
                for idx, entry in enumerate(entries[:max_items], 1):
                    att_name = str(entry.get("attachment_name") or f"Attachment {idx}")
                    content = str(entry.get("content") or "").strip()
                    if not content:
                        continue
                    # Keep transfer payload concise and useful for future retrieval.
                    content = content[:16000].rstrip()
                    meta = {
                        "title": f"{title_prefix}: {att_name}",
                        "type": "knowledge",
                        "source": "attachment_transfer",
                        "knowledge_origin": "attachment",
                        "attachment_name": att_name,
                        "attachment_session_id": session_id,
                        "tags": ["knowledge", "from-attachment", *tags],
                    }
                    await pipeline.ingest(
                        content=content,
                        metadata=meta,
                        auto_connect=True,
                        user_scope_id=user_scope_id,
                    )
                    created += 1
                return created

        try:
            created_count = asyncio.run(_transfer())
        except RuntimeError:
            # Already inside event loop: offload to a dedicated thread.
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
            f"Learned {created_count} attachment-derived knowledge entr"
            f"{'y' if created_count == 1 else 'ies'} into long-term memory (type=knowledge)."
        )

