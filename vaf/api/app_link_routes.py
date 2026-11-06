# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Veyllo App routes: pairing for the caller's OWN account, the caller's devices, the
relay switch for admins, and the direct WebSocket ``/api/app-link``.

Identity comes from the authenticated caller (``request.state.user``; the tokenless desktop
is the machine owner). No pairing route takes an account id: a QR is only ever issued for the
account whose own second factor opened the window. Every answer that carries a QR, a
fingerprint or a pairing state is ``Cache-Control: no-store``.

The WebSocket takes no token and no cookie and grants nothing by the client's address, this
machine included: it only carries the bytes of the inner TLS into a link session, where the
device certificate decides. Admission (``AdmissionGuard``) has already refused a client
outside the admitted networks before this route runs.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_DIRECT_SESSIONS = 64
_direct_live = {"n": 0}
_NO_STORE = {"Cache-Control": "no-store"}


class CodeBody(BaseModel):
    code: str = ""


class PairingBody(BaseModel):
    mode: str = "relay"


class ConfirmBody(BaseModel):
    accept: bool = False


class RelaySwitch(BaseModel):
    enabled: bool


def _caller(request: Request) -> Dict[str, Any]:
    from vaf.api.user_routes import _current_user
    from vaf.core.config import get_local_admin_scope_id
    user = dict(_current_user(request))
    user["user_scope_id"] = str(user.get("user_scope_id") or get_local_admin_scope_id() or "")
    return user


def _is_admin(user: Dict[str, Any]) -> bool:
    from vaf.core.config import is_admin_identity
    return bool(is_admin_identity(user.get("role"), user.get("user_scope_id")))


def _no_store(content: Dict[str, Any], code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=code, content=content, headers=_NO_STORE)


def _device_view(row: Dict[str, Any], *, with_user: bool = False) -> Dict[str, Any]:
    from vaf.core.app_link import bridge
    out = {"id": row["device_id"], "model": row.get("model", ""), "platform": row.get("platform", ""),
           "app_version": row.get("app_version", ""), "created_at": row.get("created_at"),
           "last_seen_at": row.get("last_seen_at"), "last_transport": row.get("last_transport"),
           "connected": bridge.is_connected(row["device_id"]),
           "relay": bool(row.get("relay_member")),
           "fingerprint": _fingerprint_of(row)}
    if with_user:
        out["username"] = row.get("username", "")
    return out


def _fingerprint_of(row: Dict[str, Any]) -> str:
    head = str(row.get("spki_sha256") or "")[:16].upper()
    return " ".join(head[i:i + 4] for i in range(0, len(head), 4))


def _relay_status() -> Dict[str, Any]:
    from vaf.core.app_link import settings
    try:
        from vaf.core.app_link import relay
        return relay.status()
    except ImportError:
        cfg = settings()
        return {"enabled": bool(cfg.get("relay_enabled", True)), "state": "off" if not cfg.get(
            "relay_enabled", True) else "idle", "instance": bool(cfg.get("relay_instance_id")),
            "connected_since": None, "last_error": ""}


# ── status and pairing (the caller's own account) ────────────────────────────

@router.get("/api/app-link/status")
async def app_link_status(request: Request):
    from vaf.core.app_link import devices, pairing, settings
    from vaf.core.messaging_connections import channel_enabled_for_scope
    user = _caller(request)
    scope = user["user_scope_id"]
    direct = pairing.direct_addresses()
    return _no_store({
        "channel_enabled": bool(settings().get("enabled", True))
        and channel_enabled_for_scope("veyllo_app", scope),
        "relay": _relay_status(),
        "direct": {"available": bool(direct), "addresses": direct},
        "my_devices": [_device_view(r) for r in devices.for_scope(scope)],
    })


@router.post("/api/app-link/pairing-window")
async def open_pairing_window(body: CodeBody, request: Request):
    """Opens the caller's pairing window with the caller's own 2FA code."""
    from vaf.api.auth_routes import _caller_account, _client_ip, own_second_factor_refusal
    from vaf.auth.database import get_auth_db
    from vaf.core.app_link import pairing
    async with get_auth_db() as db:
        account = await _caller_account(db, request)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No account to pair a device for")
        refused = own_second_factor_refusal(account, body.code, ip=_client_ip(request),
                                            where="Veyllo App pairing", what="a pairing")
        if refused is not None:
            refused.headers.update(_NO_STORE)
            return refused
        scope = str(account.user_scope_id or "")
        username = str(account.username or "")
    until = pairing.open_window(scope, username)
    return _no_store({"open_until": int(until)})


def _own(request: Request):
    user = _caller(request)
    return user["user_scope_id"], str(user.get("username") or "")


@router.post("/api/app-link/pairings")
async def issue_pairing(body: PairingBody, request: Request):
    from vaf.core.app_link import pairing
    scope, username = _own(request)
    if body.mode not in ("relay", "local"):
        return _no_store({"detail": "mode is relay or local", "code": "bad_mode"}, 400)
    try:
        issued = pairing.issue(scope, username, body.mode)
    except pairing.PairingRefused as e:
        return _no_store({"detail": e.code, "code": e.code}, 409)
    try:
        from vaf.core.app_link import relay
        relay.wake()
    except ImportError:
        pass
    return _no_store({"qr": issued.qr, "mode": issued.mode, "expires_at": int(issued.exp)})


@router.get("/api/app-link/pairings/current")
async def current_pairing(request: Request):
    from vaf.core.app_link import pairing
    scope, _username = _own(request)
    return _no_store(pairing.current(scope))


@router.post("/api/app-link/pairings/current/confirm")
async def confirm_pairing(body: ConfirmBody, request: Request):
    from vaf.core.app_link import pairing
    scope, _username = _own(request)
    state = await asyncio.to_thread(pairing.confirm, scope, bool(body.accept))
    return _no_store({"state": state})


@router.delete("/api/app-link/pairings/current")
async def withdraw_pairing(request: Request):
    from vaf.core.app_link import pairing
    scope, _username = _own(request)
    return _no_store({"withdrawn": pairing.withdraw(scope)})


# ── devices ──────────────────────────────────────────────────────────────────

@router.get("/api/app-link/devices")
async def list_devices(request: Request, all: int = 0):  # noqa: A002 - the query name
    from vaf.core.app_link import devices
    user = _caller(request)
    if all:
        if not _is_admin(user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only an admin can list every device.")
        return _no_store({"devices": [_device_view(r, with_user=True) for r in devices.all_devices()]})
    return _no_store({"devices": [_device_view(r) for r in devices.for_scope(user["user_scope_id"])]})


@router.delete("/api/app-link/devices/{device_id}")
async def remove_device(device_id: str, request: Request):
    """The device's own account or an admin removes it (also the way to cut off a lost phone)."""
    from vaf.core.app_link import devices
    user = _caller(request)
    row = devices.get(device_id)
    if row is None or (row.get("user_scope_id") != user["user_scope_id"] and not _is_admin(user)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such device")
    removed = await asyncio.to_thread(devices.remove_device, device_id,
                                      by=str(user.get("username") or ""), reason="removed")
    return {"removed": bool(removed)}


# ── the direct WebSocket ─────────────────────────────────────────────────────

@router.websocket("/api/app-link")
async def app_link_socket(websocket: WebSocket):
    """Binary frames carry the inner TLS of ONE link session, without the relay envelope.
    Text frames are ignored. Nothing here decides who the peer is."""
    from vaf.core.app_link import bridge
    from vaf.network.binding import connection_client_ip
    ip = connection_client_ip(websocket)
    if _direct_live["n"] >= MAX_DIRECT_SESSIONS:
        await websocket.close(code=1013)
        return
    loop = asyncio.get_running_loop()
    outgoing: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()

    def _send(data: bytes) -> None:
        loop.call_soon_threadsafe(outgoing.put_nowait, bytes(data))

    session = bridge.new_session("direct", _send, connection=f"direct:{id(websocket)}",
                                 peer=f"ip:{ip}", peer_label=ip)
    if session is None:
        await websocket.close(code=4008)
        return
    _direct_live["n"] += 1
    await websocket.accept()

    async def _writer() -> None:
        while True:
            try:
                data = await asyncio.wait_for(outgoing.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if session.closed:
                    break
                continue
            if data is None:
                break
            await websocket.send_bytes(data)
        try:
            await websocket.close()
        except Exception:
            pass

    writer = asyncio.create_task(_writer())
    try:
        while not session.closed:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data:
                await asyncio.to_thread(session.feed, data)
    except Exception:
        logger.debug("app link: direct socket ended", exc_info=True)
    finally:
        _direct_live["n"] = max(0, _direct_live["n"] - 1)
        session.close("disconnected", bye=False)
        outgoing.put_nowait(None)
        try:
            await asyncio.wait_for(writer, timeout=5)
        except Exception:
            writer.cancel()
