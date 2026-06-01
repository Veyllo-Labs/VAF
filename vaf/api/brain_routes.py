"""
Agent Brain API — exposes the live working memory, plan, tasks, intent, and team state
so the Web UI can show what the agent is currently thinking about.
"""
from __future__ import annotations

import os
from typing import Optional
from fastapi import APIRouter

router = APIRouter(prefix="/api/agent", tags=["agent-brain"])


@router.get("/brain")
def get_brain(session_id: Optional[str] = None):
    """Return live working memory, intent, and team state for a session (defaults to the
    currently active session). Each chat has its own isolated store."""
    try:
        from vaf.core.main_persistence import MainPersistenceManager
        if not session_id:
            try:
                from vaf.core.subagent_ipc import get_current_session_id
                session_id = get_current_session_id()
            except Exception:
                session_id = None
        mpm = MainPersistenceManager(os.getcwd(), session_id=session_id)

        intent = mpm.get_user_intent() or ""
        memory = mpm.get_working_memory()
        team   = mpm.get_team_state()

        notes = [
            (e["text"] if isinstance(e, dict) else str(e))
            for e in memory.get("notes", [])
        ]
        plan = [
            (e["text"] if isinstance(e, dict) else str(e))
            for e in memory.get("plan", [])
        ]
        tasks = [
            {
                "text":   t.get("text", "") if isinstance(t, dict) else str(t),
                "status": t.get("status", "pending") if isinstance(t, dict) else "pending",
            }
            for t in memory.get("tasks", [])
        ]

        agents = []
        for k, v in team.active_agents.items():
            agents.append({
                "task_id":    v.task_id,
                "agent_type": v.agent_type,
                "status":     v.status,
                "task":       v.current_task or "",
                "question":   v.clarification_question or "",
                "result":     (v.result_summary or "")[:120],
            })

        return {
            "intent": intent,
            "notes":  notes,
            "plan":   plan,
            "tasks":  tasks,
            "agents": agents,
        }
    except Exception as e:
        return {"error": str(e), "intent": "", "notes": [], "plan": [], "tasks": [], "agents": []}
