import asyncio
import os
import threading
from typing import Optional, List
from uuid import UUID

from vaf.tools.base import BaseTool


def _run_async_in_new_loop(coro):
    """Run a coroutine in a new thread with its own event loop. Avoids 'attached to a different loop' when called from sync/other loop."""
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
from vaf.core.main_persistence import MainPersistenceManager

class UpdateIntentTool(BaseTool):
    """
    Update the User Intent (the session goal/task). 
    Use when the user states or changes their main goal for this session (e.g. "I want to build a website").
    Do NOT use for preferences like language or "remember that..." – use memory_save for long-term facts.
    """
    name = "update_intent"
    description = (
        "Update the primary User Intent (session goal/task). Use for the user's current objective, not for preferences "
        "(e.g. language) or 'remember that...' – use memory_save for those."
    )
    
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
    Use for multi-step tasks: save your plan, notes, and checkable tasks (pending/done; done removed after 12h).
    """
    name = "update_working_memory"
    description = (
        "Update working memory (notes, plan, tasks) that persists across turns and appears in <working_memory>. "
        "Use notes/plan to set the full list (replaces existing). Use add_notes/add_plan to append. "
        "Tasks: add_task to add a step (pending), mark_task_done(index) to mark done; done tasks are auto-removed after 12h. Pending = in progress or waiting on something."
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
            },
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "done"]},
                        "ts": {"type": "string", "description": "ISO timestamp (optional)."}
                    },
                    "required": ["text"]
                },
                "description": "Full list of tasks (replaces existing). Each: text, optional status (pending/done), optional ts."
            },
            "add_task": {
                "type": "string",
                "description": "Add one task (status pending)."
            },
            "mark_task_done": {
                "type": "integer",
                "description": "0-based index of the task to mark as done."
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
        tasks = kwargs.get('tasks')
        add_task = kwargs.get('add_task')
        mark_task_done = kwargs.get('mark_task_done')
        
        try:
            mpm = MainPersistenceManager(base_dir)
            mpm.update_working_memory(
                notes=notes, plan=plan, add_notes=add_notes, add_plan=add_plan,
                tasks=tasks, add_task=add_task, mark_task_done=mark_task_done,
            )
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


class MemorySearchTool(BaseTool):
    """
    Search long-term memory for facts about the user or system.
    Use when the user asks 'who am I?', 'what do you remember about me?', or similar.
    Do NOT use memory_save for lookup – memory_save only saves new facts.
    """
    name = "memory_search"
    description = (
        "🔍 SEARCH your long-term memory database (RAG) for stored facts, notes, preferences, or past conversations. "
        "USE THIS when user asks: 'who am I?', 'what do you know about me?', 'was hast du über mich gespeichert?', 'what have I told you?'. "
        "This is like searching a personal knowledge base - it retrieves previously saved information. "
        "Returns matching snippets from the vector database. If nothing found, tell user you have no stored info yet."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (e.g. 'user identity', 'who is this user', 'facts about user')."
            },
            "k": {
                "type": "integer",
                "description": "Max number of snippets to return (default 5).",
                "default": 5
            }
        },
        "required": ["query"]
    }

    def run(self, **kwargs) -> str:
        query = (kwargs.get("query") or "").strip()
        if not query:
            return "Error: query is required."
        # Reject model output (e.g. <think> blocks) — use Memory context block for this turn
        if query.startswith("<think>") or "</think>" in query:
            return (
                "Use the Memory context block for this turn. "
                "For memory_search pass only a short query (e.g. 'user name', 'user preferences')."
            )
        # Avoid huge RAG query (e.g. model passing full thinking) → RAM spike in embeddings
        from vaf.memory.embeddings import MAX_EMBED_INPUT_CHARS
        if len(query) > MAX_EMBED_INPUT_CHARS:
            query = query[:MAX_EMBED_INPUT_CHARS].rstrip()
        k = int(kwargs.get("k") or 5)
        k = max(1, min(k, 20))
        user_scope_id = kwargs.get("user_scope_id")
        if user_scope_id is not None and isinstance(user_scope_id, str):
            try:
                user_scope_id = UUID(user_scope_id)
            except (ValueError, TypeError):
                user_scope_id = None
        try:
            from vaf.memory.rag import run_memory_search_sync
            result = run_memory_search_sync(
                query=query, k=k, user_scope_id=user_scope_id, caller="tool"
            )
            if not result or not result.strip():
                return "No memories found for this query. You can tell the user you don't have stored information and offer to remember things from now on."
            return result
        except Exception as e:
            return f"Error searching memory: {e}"


class MemorySaveTool(BaseTool):
    """
    Save a NEW fact, note, or preference in long-term memory (RAG).
    Use ONLY when the user explicitly asks to remember or save something.
    Do NOT use for 'who am I?' or 'what do you remember?' – use memory_search or the Memory context block for that.
    """
    name = "memory_save"
    description = (
        "💾 SAVE new information to your long-term memory database (RAG). "
        "USE THIS when user explicitly asks: 'remember that...', 'merke dir...', 'save this...', 'speichere...'. "
        "This stores facts, preferences, notes permanently in the vector database for future retrieval. "
        "Do NOT use for lookups - use memory_search to retrieve stored information."
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
        # Allow None = global scope (e.g. Web UI without login); memories still stored and visible on /memory
        if user_scope_id is not None and isinstance(user_scope_id, str):
            try:
                user_scope_id = UUID(user_scope_id)
            except (ValueError, TypeError):
                return "Error: Invalid user_scope_id."
        title = (kwargs.get("title") or "").strip() or None
        tags = kwargs.get("tags")
        if tags is not None and not isinstance(tags, list):
            tags = None
        metadata = {"source": "memory_save", "type": "note"}
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
            return _run_async_in_new_loop(_ingest())
        except Exception as e:
            return f"Error storing memory: {e}"
