# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The caller's sandbox environments (vaf/core/environments.py) for Settings, Connections.

GET lists the caller's own environments and the background processes in them; an admin
may ask for everybody's (`?all=1`). POST .../stop and DELETE act on one; an admin may act
on another person's with `?all=1`. Nothing here runs code in an environment - that is the
agent's tools and `vaf env exec`, and never on someone else's.
"""
import asyncio
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from vaf.api.contact_routes import get_current_vaf_user

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])


def _caller(request: Request):
    """The caller and whether they are an admin, by VAF's one definition of admin (the
    database role, or the configured owner scope - the tokenless desktop has no role)."""
    from vaf.core.config import is_admin_identity
    user = get_current_vaf_user(request)
    role = (getattr(request.state, "user", None) or {}).get("role")
    try:
        admin = bool(is_admin_identity(role, user["user_scope_id"]))
    except Exception:
        admin = False
    return user, admin


def _row(env, *, with_owner: bool) -> Dict[str, Any]:
    row = {"id": env.id, "name": env.name, "kind": env.kind, "network": env.network,
           "state": env.state or "unknown", "created": env.created, "expires": env.expires,
           "project_path": env.project_path, "degraded": env.degraded,
           "memory_mb": env.memory_mb}
    if with_owner:
        row["owner"] = env.owner
    return row


def _overview(scope, everyone: bool) -> Dict[str, Any]:
    from vaf.core.environments import EnvironmentRefused, get_environment_manager
    from vaf.core.service_stack import is_docker_daemon_running
    if not is_docker_daemon_running():
        return {"available": False, "reason": "docker_unavailable", "environments": [],
                "processes": []}
    mgr = get_environment_manager()
    try:
        envs = mgr.list(scope, everyone=everyone)
        procs: List[Dict[str, Any]] = mgr.processes(scope)
    except EnvironmentRefused as e:
        return {"available": False, "reason": str(e), "environments": [], "processes": []}
    except Exception as e:
        # docker not answering or timing out: the same answer _act gives, as data the
        # section can show rather than an internal error.
        return {"available": False, "reason": f"sandbox unavailable: {e}"[:300],
                "environments": [], "processes": []}
    return {"available": True, "environments": [_row(e, with_owner=everyone) for e in envs],
            "processes": procs}


@router.get("")
async def sandbox_overview(request: Request, all: int = 0) -> Dict[str, Any]:
    """The caller's environments and processes; with all=1 (admin) everybody's environments."""
    user, is_admin = _caller(request)
    if all and not is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    result = await asyncio.to_thread(_overview, user["user_scope_id"], bool(all))
    result["is_admin"] = is_admin
    return result


async def _act(request: Request, env_id: str, action: str, all: int) -> Dict[str, Any]:
    from vaf.core.environments import EnvironmentRefused, get_environment_manager
    user, is_admin = _caller(request)
    if all and not is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    mgr = get_environment_manager()
    fn = mgr.stop if action == "stop" else mgr.delete
    try:
        env = await asyncio.to_thread(fn, user["user_scope_id"], env_id, admin=bool(all))
    except EnvironmentRefused as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        # docker missing, not answering or timing out: the sandbox is unavailable, which
        # is not the same as "no such environment".
        raise HTTPException(status_code=503, detail=f"sandbox unavailable: {e}"[:300])
    return {"ok": True, "id": env.id, "action": action}


@router.post("/{env_id}/stop")
async def stop_environment(env_id: str, request: Request, all: int = 0) -> Dict[str, Any]:
    """Stop one environment; its files stay."""
    return await _act(request, env_id, "stop", all)


@router.delete("/{env_id}")
async def delete_environment(env_id: str, request: Request, all: int = 0) -> Dict[str, Any]:
    """Delete one environment: container, files and network."""
    return await _act(request, env_id, "delete", all)
