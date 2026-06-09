import asyncio
import os
import threading
from typing import Optional, List
from uuid import UUID

from vaf.tools.base import BaseTool


# session_id -> ts of the last "are you sure?" task-overwrite bounce (confirm-once guard, see run()).
_TASK_OVERWRITE_CONFIRM: dict = {}


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


def _current_session_id() -> Optional[str]:
    """Current session id (from the session context var), so persistence writes land in this
    chat's isolated store instead of the shared global one. Sub-agents inherit the parent
    session via VAF_SESSION_ID. None -> legacy global .vaf/main/."""
    try:
        from vaf.core.subagent_ipc import get_current_session_id
        return get_current_session_id()
    except Exception:
        return None

class UpdateIntentTool(BaseTool):
    """
    Update the User Intent (the session goal/task). 
    Use when the user states or changes their main goal for this session (e.g. "I want to build a website").
    Do NOT use for preferences like language or "remember that..." – use memory_save for long-term facts.
    """
    name = "update_intent"
    permission_level = "system"
    side_effect_class = "reversible"
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
            mpm = MainPersistenceManager(base_dir, session_id=_current_session_id())
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
    permission_level = "system"
    side_effect_class = "reversible"
    description = (
        "Update working memory (notes, plan, tasks) that persists across turns and appears in <working_memory>. "
        "plan = your high-level approach (short); tasks = the concrete steps that carry it out (tracked and kept on course). "
        "Set notes/plan to replace the list, add_notes/add_plan to append. "
        "Tasks: add_task to add a step (pending), mark_task_done(index) to mark done; for multi-step work put the steps in tasks, not plan. Done tasks auto-removed after 12h."
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
                "description": "Your high-level approach (a line or two), replaces existing. Keep it short; put concrete steps in tasks. Omit to keep current plan."
            },
            "add_notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Notes to append (does not replace existing)."
            },
            "add_plan": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Approach lines to append to the plan (does not replace existing)."
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
        user_scope_id = kwargs.get('user_scope_id')
        notes = kwargs.get('notes')
        plan = kwargs.get('plan')
        add_notes = kwargs.get('add_notes')
        add_plan = kwargs.get('add_plan')
        tasks = kwargs.get('tasks')
        # Be lenient: these are array params, but a model (esp. a small local one) often sends a bare
        # string. Wrap it so the call still succeeds instead of being mangled (a string iterated as chars).
        if isinstance(plan, str): plan = [plan.strip()] if plan.strip() else None
        if isinstance(notes, str): notes = [notes.strip()] if notes.strip() else None
        if isinstance(add_notes, str): add_notes = [add_notes.strip()] if add_notes.strip() else None
        if isinstance(add_plan, str): add_plan = [add_plan.strip()] if add_plan.strip() else None
        if isinstance(tasks, str): tasks = [tasks.strip()] if tasks.strip() else None
        add_task = kwargs.get('add_task')
        mark_task_done = kwargs.get('mark_task_done')
        
        try:
            mpm = MainPersistenceManager(base_dir, session_id=_current_session_id())
            pre_tasks = mpm.get_working_memory().get("tasks", [])

            # Overwrite guard: replacing the whole task list (tasks=[...]) while steps are still
            # pending can silently drop work in progress. Bounce the first such replace with the
            # pending steps listed; a re-call within the confirm window proceeds. Never a hard lock.
            if tasks is not None:
                try:
                    from vaf.core.config import Config as _CfgOv
                    if bool(_CfgOv.get("task_overwrite_guard_enabled", True)):
                        import time as _time
                        sid = _current_session_id() or "default"
                        pending = [
                            (i, t) for i, t in enumerate(pre_tasks)
                            if str((t.get("status") if isinstance(t, dict) else None) or "pending").lower() != "done"
                        ]
                        armed = _TASK_OVERWRITE_CONFIRM.get(sid)
                        window = float(_CfgOv.get("task_overwrite_confirm_window_seconds", 120))
                        if pending and not (armed and (_time.time() - armed) < window):
                            _TASK_OVERWRITE_CONFIRM[sid] = _time.time()
                            listed = "; ".join(
                                f"[{i}] \"{str(t.get('text', '') if isinstance(t, dict) else t)[:50]}\""
                                for i, t in pending[:5]
                            )
                            return (
                                f"⚠️ You're replacing the task list, but {len(pending)} step(s) are still pending: {listed}. "
                                f"If you finished them or are intentionally dropping them, call update_working_memory(tasks=[...]) again to confirm. "
                                f"Otherwise keep them — include them in the new list, or mark_task_done first."
                            )
                        _TASK_OVERWRITE_CONFIRM.pop(sid, None)
                except Exception:
                    pass

            mpm.update_working_memory(
                notes=notes, plan=plan, add_notes=add_notes, add_plan=add_plan,
                tasks=tasks, add_task=add_task, mark_task_done=mark_task_done,
            )
            # Thinking Workspace bridge: in thinking mode, mirror latest working_memory snapshot
            # into the per-user workspace for auditable run continuity.
            try:
                if os.environ.get("VAF_THINKING_MODE", "").strip() in ("1", "true", "yes"):
                    from vaf.core.thinking_workspace import mirror_working_memory_snapshot

                    snapshot = mpm.get_working_memory()
                    mirror_working_memory_snapshot(user_scope_id, snapshot)
            except Exception:
                pass
            # Out-of-order drift nudge: if a LATER task was marked done while an EARLIER one is
            # still pending, gently flag a possibly-skipped step (soft reminder, not a block).
            nudge = ""
            try:
                from vaf.core.config import Config as _CfgDrift
                if mark_task_done is not None and bool(_CfgDrift.get("plan_drift_nudge_enabled", True)):
                    _tasks = mpm.get_working_memory().get("tasks", [])
                    _mt = int(mark_task_done)
                    for _i in range(min(_mt, len(_tasks))):
                        _t = _tasks[_i]
                        _st = (_t.get("status") if isinstance(_t, dict) else None) or "pending"
                        if str(_st).lower() == "pending":
                            _txt = (_t.get("text", "") if isinstance(_t, dict) else str(_t)) or ""
                            nudge = (
                                f"\n\nNote: you marked task [{_mt}] done, but earlier task [{_i}] "
                                f"(\"{str(_txt)[:60]}\") is still pending — did you skip it? Complete it "
                                f"or update the plan. Ignore this if it is intentionally out of order."
                            )
                            break
            except Exception:
                nudge = ""
            return "✅ Working Memory updated." + nudge
        except Exception as e:
            return f"❌ Error updating working memory: {e}"

class AddTaskAliasTool(BaseTool):
    """
    Compatibility alias — add_task is NOT a standalone tool.

    The correct call is:
        update_working_memory(add_task="Your task text here")

    This alias accepts the task text and delegates to update_working_memory
    so the agent's intent is not lost, but it also returns a warning so the
    LLM learns the correct pattern for future calls.
    """
    name = "add_task"
    permission_level = "system"
    side_effect_class = "reversible"
    description = (
        "[ALIAS — prefer update_working_memory] Add a single pending task to working memory. "
        "Correct usage: update_working_memory(add_task='<text>'). "
        "This alias exists only as a fallback."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The task text to add as pending."
            },
            "text": {
                "type": "string",
                "description": "Alternative argument name for the task text."
            }
        },
        "required": []
    }

    def run(self, **kwargs) -> str:
        task_text = (
            kwargs.get("task") or
            kwargs.get("text") or
            kwargs.get("add_task") or
            next((v for v in kwargs.values() if isinstance(v, str) and v.strip()), None)
        )
        if not task_text:
            return (
                "⚠️  add_task is not a standalone tool.\n"
                "Correct usage: update_working_memory(add_task='<task text>')\n"
                "No task text was provided, so nothing was saved."
            )
        try:
            base_dir = kwargs.get("base_dir", ".")
            mpm = MainPersistenceManager(base_dir, session_id=_current_session_id())
            mpm.update_working_memory(add_task=task_text.strip())
            return (
                f"✅ Task added: \"{task_text.strip()}\"\n\n"
                f"⚠️  Note: add_task is not a real tool — it is a parameter of update_working_memory.\n"
                f"Use the correct call next time:\n"
                f"  update_working_memory(add_task=\"{task_text.strip()}\")"
            )
        except Exception as e:
            return (
                f"❌ Failed to add task: {e}\n\n"
                f"⚠️  Remember: add_task is a parameter of update_working_memory, not a standalone tool.\n"
                f"Correct usage: update_working_memory(add_task=\"<task text>\")"
            )


class RequestClarificationTool(BaseTool):
    """
    [SUB-AGENT ONLY] Signal that you are blocked and need user input.
    Use this instead of failing or guessing.
    """
    name = "request_clarification"
    permission_level = "system"
    side_effect_class = "reversible"
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
            mpm = MainPersistenceManager(base_dir, session_id=_current_session_id())
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
    permission_level = "read"
    side_effect_class = "none"
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
    permission_level = "write"
    side_effect_class = "irreversible"
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
                "description": "One or more tags to categorize this memory (e.g. ['user', 'preference'] or ['project', 'decision']). Required — at least one tag must be provided."
            }
        },
        "required": ["content", "tags"]
    }

    def run(self, **kwargs) -> str:
        content = (kwargs.get("content") or "").strip()
        if not content:
            return "Error: content is required and cannot be empty."
        tags = kwargs.get("tags")
        if not tags or not isinstance(tags, list) or not any(str(t).strip() for t in tags):
            return (
                "Error: tags is required. Provide at least one tag to categorize this memory "
                "(e.g. tags=[\"user\"] or tags=[\"project\", \"decision\"]). "
                "Good tags make memories retrievable later."
            )
        user_scope_id = kwargs.get("user_scope_id")
        # Allow None = global scope (e.g. Web UI without login); memories still stored and visible on /memory
        if user_scope_id is not None and isinstance(user_scope_id, str):
            try:
                user_scope_id = UUID(user_scope_id)
            except (ValueError, TypeError):
                return "Error: Invalid user_scope_id."
        title = (kwargs.get("title") or "").strip() or None
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
