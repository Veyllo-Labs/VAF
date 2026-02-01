import asyncio
import os
from typing import Optional, List
from uuid import UUID

from vaf.tools.base import BaseTool
from vaf.core.main_persistence import MainPersistenceManager

class UpdateIntentTool(BaseTool):
    """
    Update the User Intent (The North Star). 
    Use this when the user clarifies or changes their main goal.
    This persists across the entire session and is always visible.
    """
    name = "update_intent"
    description = "Update the primary User Intent that guides the entire session."
    
    parameters = {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "The full, updated user intent description."
            }
        },
        "required": ["intent"]
    }
    
    def run(self, **kwargs) -> str:
        base_dir = kwargs.get('base_dir', '.')
        intent = kwargs.get('intent', '')
        
        if not intent:
            return "Error: Intent content is required."
        
        try:
            mpm = MainPersistenceManager(base_dir)
            mpm.update_user_intent(intent)
            return "✅ User Intent updated successfully."
        except Exception as e:
            return f"❌ Error updating intent: {e}"

class UpdateWorkingMemoryTool(BaseTool):
    """
    Update the Main Agent's Working Memory (Scratchpad).
    Persists across turns and appears in <working_memory> in your context.
    Use for multi-step tasks: save your plan and notes so they are visible on the next turn.
    """
    name = "update_working_memory"
    description = (
        "Update working memory (notes and plan) that persists across turns and appears in <working_memory>. "
        "Use notes/plan to set the full list (replaces existing). Use add_notes/add_plan to append without replacing."
    )
    
    parameters = {
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Full list of notes (replaces existing). Omit to keep current notes."
            },
            "plan": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Full list of plan steps (replaces existing). Omit to keep current plan."
            },
            "add_notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Notes to append (does not replace existing)."
            },
            "add_plan": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Plan steps to append (does not replace existing)."
            }
        },
        "required": []
    }
    
    def run(self, **kwargs) -> str:
        base_dir = kwargs.get('base_dir', '.')
        notes = kwargs.get('notes')
        plan = kwargs.get('plan')
        add_notes = kwargs.get('add_notes')
        add_plan = kwargs.get('add_plan')
        
        try:
            mpm = MainPersistenceManager(base_dir)
            mpm.update_working_memory(notes=notes, plan=plan, add_notes=add_notes, add_plan=add_plan)
            return "✅ Working Memory updated."
        except Exception as e:
            return f"❌ Error updating working memory: {e}"

class RequestClarificationTool(BaseTool):
    """
    [SUB-AGENT ONLY] Signal that you are blocked and need user input.
    Use this instead of failing or guessing.
    """
    name = "request_clarification"
    description = "Signal a blocker and ask the Main Agent/User for clarification."
    coder_only = True  # Only available to sub-agents
    
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The specific question for the user."
            },
            "context": {
                "type": "string",
                "description": "Why you need this info (context)."
            },
            "task_id": {
                "type": "string",
                "description": "Your current task ID."
            }
        },
        "required": ["question", "task_id"]
    }
    
    def run(self, **kwargs) -> str:
        base_dir = kwargs.get('base_dir', '.')
        question = kwargs.get('question', '')
        context = kwargs.get('context', '')
        task_id = kwargs.get('task_id', '')
        
        # Sub-agents know their type implicitly, but we need it for the manager
        # In a real run, this might be injected or inferred.
        # For now, we assume 'coder' as it's the main user.
        agent_type = "coding_agent" 
        
        if not question or not task_id:
            return "Error: Question and Task ID are required."
        
        try:
            mpm = MainPersistenceManager(base_dir)
            mpm.update_subagent_status(
                task_id=task_id,
                agent_type=agent_type,
                status="needs_clarification",
                question=f"{question} (Context: {context})"
            )
            return "✅ Clarification request sent to Main Agent. Waiting for update..."
        except Exception as e:
            return f"❌ Error requesting clarification: {e}"


class MemoryStoreTool(BaseTool):
    """
    Store a fact, note, or preference in long-term memory (RAG).
    Use when the user says to remember something, or when storing preferences or decisions.
    Stored content is searchable and injected into context on future turns.
    """
    name = "memory_store"
    description = (
        "Store information in long-term memory. Use when the user asks to remember something, "
        "or for preferences, decisions, or important facts. Stored content is retrieved automatically in future turns."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The information to store (fact, preference, note, or decision)."
            },
            "title": {
                "type": "string",
                "description": "Optional short title for this memory."
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for filtering."
            }
        },
        "required": ["content"]
    }

    def run(self, **kwargs) -> str:
        content = (kwargs.get("content") or "").strip()
        if not content:
            return "Error: content is required and cannot be empty."
        user_scope_id = kwargs.get("user_scope_id")
        if user_scope_id is None:
            return "Error: Login required to store memory (user scope not set)."
        if isinstance(user_scope_id, str):
            try:
                user_scope_id = UUID(user_scope_id)
            except (ValueError, TypeError):
                return "Error: Invalid user_scope_id."
        title = (kwargs.get("title") or "").strip() or None
        tags = kwargs.get("tags")
        if tags is not None and not isinstance(tags, list):
            tags = None
        metadata = {"source": "memory_store", "type": "note"}
        if title:
            metadata["title"] = title
        if tags:
            metadata["tags"] = tags

        async def _ingest() -> str:
            from vaf.memory.database import get_db
            from vaf.memory.rag import RagPipeline
            async with get_db() as db:
                pipeline = RagPipeline(db)
                await pipeline.ingest(
                    content=content,
                    metadata=metadata,
                    user_scope_id=user_scope_id,
                    auto_connect=False,
                )
            return "Memory stored."

        try:
            return asyncio.run(_ingest())
        except Exception as e:
            return f"Error storing memory: {e}"
