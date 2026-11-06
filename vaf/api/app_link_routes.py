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


def _register_agent_names() -> None:
    """The app shows the account's agent by its persona name: the harness's lookup, handed
    to the framework's link (which imports nothing from the auth layer)."""
    from vaf.auth.user_workspace import agent_display_name
    from vaf.core.app_link import set_agent_name_resolver
    set_agent_name_resolver(agent_display_name)


_register_agent_names()

MAX_DIRECT_SESSIONS = 64
_direct_live = {"n": 0}
_NO_STORE = {"Cache-Control": "no-store"}


# A sentence with the next step for each refusal; `code` stays the catalogue's key.
_REFUSALS = {
    "window_closed": "The pairing window is closed. Confirm with your 2FA code to open it again.",
    "relay_off": "The Veyllo relay is switched off on this VAF. Pair over your network or VPN "
                 "instead, or ask an admin to switch the relay on.",
    "no_direct_path": "There is no direct address for the phone: switch Local Network and its "
                      "TLS on in Settings, or pair through the Veyllo relay.",
}


class CodeBody(BaseModel):
    code: str = ""


class PairingBody(BaseModel):
    mode: str = "relay"


class ConfirmBody(BaseModel):
    accept: bool = False


class RelaySwitch(BaseModel):
    enabled: bool


def _caller(request: Request) -> Dict[str, Any]:
    """The caller as authenticated. Only the tokenless desktop, which has no identity on the
    request at all, is the machine owner; an authenticated caller without a scope is
    refused, never filled in with the owner's scope."""
    from vaf.api.user_routes import _current_user
    user = dict(_current_user(request))
    if not str(user.get("user_scope_id") or "").strip():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="This sign-in carries no account, so it cannot use the "
                                   "Veyllo App. Sign in again.")
    user["user_scope_id"] = str(user["user_scope_id"]).strip()
    return user


def _is_admin(request: Request) -> bool:
    from vaf.api.user_routes import caller_is_admin
    return bool(caller_is_admin(request))


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


# What a signed-in account may know about the relay: the spec's status fields. The counts
# across accounts (members, devices on the relay) are the admin route's.
_STATUS_FIELDS = ("enabled", "state", "instance", "connected_since", "last_error")


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
        "relay": {k: v for k, v in _relay_status().items() if k in _STATUS_FIELDS},
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
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="There is no active account to pair a device for. Sign "
                                       "in with your account.")
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
        return _no_store({"detail": "Choose how the phone connects: \"relay\" (through the "
                                    "Veyllo relay) or \"local\" (directly, on your network or "
                                    "VPN).", "code": "bad_mode"}, 400)
    try:
        issued = pairing.issue(scope, username, body.mode)
    except pairing.PairingRefused as e:
        return _no_store({"detail": _REFUSALS.get(e.code, e.code), "code": e.code}, 409)
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
        if not _is_admin(request):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Only an admin can list every account's devices.")
        return _no_store({"devices": [_device_view(r, with_user=True) for r in devices.all_devices()]})
    return _no_store({"devices": [_device_view(r) for r in devices.for_scope(user["user_scope_id"])]})


@router.delete("/api/app-link/devices/{device_id}")
async def remove_device(device_id: str, request: Request):
    """The device's own account or an admin removes it (also the way to cut off a lost phone)."""
    from vaf.core.app_link import devices
    user = _caller(request)
    row = devices.get(device_id)
    if row is None or (row.get("user_scope_id") != user["user_scope_id"] and not _is_admin(request)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such device of this account. List them with "
                                   "GET /api/app-link/devices or `vaf devices list`.")
    removed = await asyncio.to_thread(devices.remove_device, device_id,
                                      by=str(user.get("username") or ""), reason="removed")
    return {"removed": bool(removed)}


# ── the relay (admins) ───────────────────────────────────────────────────────

def _admin(request: Request) -> Dict[str, Any]:
    user = _caller(request)
    if not _is_admin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only an admin can change the Veyllo relay.")
    return user


@router.get("/api/app-link/relay")
async def relay_state(request: Request):
    _admin(request)
    return _no_store(_relay_status())


@router.put("/api/app-link/relay")
async def switch_relay(body: RelaySwitch, request: Request):
    """On or off. Off closes the relay connection and stops relay QR codes, claims and push
    notices; the instance, its secret and the devices stay, so on again needs no pairing."""
    from vaf.core.app_link import relay
    user = _admin(request)
    await asyncio.to_thread(relay.set_relay_enabled, body.enabled,
                            username=str(user.get("username") or ""))
    return _no_store(_relay_status())


@router.post("/api/app-link/relay/rotate")
async def rotate_relay(request: Request):
    from vaf.core.app_link import relay
    user = _admin(request)
    connector = relay.connector()
    if not await connector.rotate(username=str(user.get("username") or "")):
        return _no_store({"detail": relay.admin_failure(connector.last_error),
                          "code": "rotate_failed"}, 502)
    return _no_store(_relay_status())


@router.post("/api/app-link/relay/dissolve")
async def dissolve_relay(body: CodeBody, request: Request):
    """Removes the installation's relay instance with every member; the devices stay for the
    direct path. An admin, with their own 2FA code."""
    from vaf.api.auth_routes import _caller_account, _client_ip, own_second_factor_refusal
    from vaf.auth.database import get_auth_db
    from vaf.core.app_link import relay
    user = _admin(request)
    async with get_auth_db() as db:
        account = await _caller_account(db, request)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="There is no active admin account behind this sign-in. "
                                       "Sign in with your admin account.")
        refused = own_second_factor_refusal(account, body.code, ip=_client_ip(request),
                                            where="Veyllo relay dissolve", what="dissolving the relay")
        if refused is not None:
            refused.headers.update(_NO_STORE)
            return refused
    connector = relay.connector()
    if not await connector.dissolve(username=str(user.get("username") or "")):
        return _no_store({"detail": relay.admin_failure(connector.last_error),
                          "code": "dissolve_failed"}, 502)
    return _no_store(_relay_status())


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
