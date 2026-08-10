# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Agent Brain API - the live working memory, plan, tasks, intent and team state of
ONE chat, so the Web UI can show what the agent is thinking about.

Two properties this endpoint got wrong and now enforces, both worth stating
because they look like details and are not:

**It answers for the session the caller names, and for nothing else.** The
working memory is stored per session (`.vaf/main/sessions/<id>/`) with a legacy
shared directory as the fallback for lanes that have no session - scheduled
automations declare exactly that. Resolving the session from process state
inside an HTTP request always found "no session", so the panel served that shared
directory: a chat displayed a scheduled automation's goal beside an unrelated
lane's plan. A request-scoped view must be built from the request, never from
whatever the process last did.

**It checks who is asking.** The route was mounted with no auth dependency at
all, so the agent's goal, plan, tasks and sub-agent state were readable by
anyone who could reach the port - which on a local-network install is the
network. Naming a session id without an ownership check would have turned that
into a way to read another user's working memory on purpose, so the two halves
had to land together. The ownership rule is `SessionManager.may_access`, the
same one the WebSocket commands use.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from vaf.api.config_routes import get_current_user_or_local_admin

router = APIRouter(prefix="/api/agent", tags=["agent-brain"])

_EMPTY: Dict[str, Any] = {"intent": "", "notes": [], "plan": [], "tasks": [], "agents": []}


@router.get("/brain")
def get_brain(
    session_id: Optional[str] = None,
    user: Dict[str, Any] = Depends(get_current_user_or_local_admin),
):
    """Working memory, intent and team state of ONE chat.

    `session_id` is required in practice: without it there is no chat to answer
    for, and the legacy shared store is never served here - it holds the
    leftovers of every session-less lane and belongs to no chat on screen.
    """
    if not session_id:
        return dict(_EMPTY)

    from vaf.core.config import get_local_admin_scope_id
    from vaf.core.session import get_manager, session_access_allowed

    scope = user.get("user_scope_id")
    is_admin = (str(user.get("role") or "user").lower() == "admin") or (
        scope is not None and str(scope) == str(get_local_admin_scope_id())
    )
    allowed, _loaded = session_access_allowed(
        get_manager(), session_id, user_scope_id=scope, is_admin=is_admin
    )
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    try:
        from vaf.core.main_persistence import MainPersistenceManager
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
        return {"error": str(e), **_EMPTY}
