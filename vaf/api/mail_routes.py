# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Mail engine v2 REST API (/api/mail/*). Design: EMAIL_CLIENT.md.

Rules:
- Every endpoint resolves the caller via _get_current_user and builds a
  MailService for that scope only (fail-closed; the local admin's identity
  fallback resolves to the admin's REAL scope UUID, never to "no scope").
- Every endpoint is gated on mail_engine_v2_enabled via _require_v2 (404 while
  off) so the legacy /api/email lane keeps serving until the rollout flips. The
  ONE deliberate exception is GET /status: it answers even while the flag is off
  (v2_enabled=false) so the /mail page can render its flag-off screen instead of
  a bare 404.
- Attachments are served with Content-Disposition: attachment and nosniff;
  only image/* (except SVG) keeps its real content type so cid: inline images
  render - everything else is application/octet-stream.
- Provider IO always runs via asyncio.to_thread - never on the event loop.
"""
import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Response

from vaf.api.config_routes import get_current_user_or_local_admin as _get_current_user
from vaf.core.config import Config, get_local_admin_scope_id

logger = logging.getLogger("vaf.api.mail_routes")

router = APIRouter(prefix="/api/mail", tags=["mail-v2"])

# Strong references to in-flight undo-send delivery tasks so the event loop's
# weak task references cannot GC them mid-delivery (see /send fast path).
_INFLIGHT_SEND_TASKS: set = set()


def _require_v2() -> None:
    if not bool(Config.get("mail_engine_v2_enabled", False)):
        raise HTTPException(status_code=404, detail="mail engine v2 is not enabled")


def _scope_of(user: Dict[str, Any]) -> str:
    scope = (user.get("user_scope_id") or "").strip() or get_local_admin_scope_id()
    if not scope:
        raise HTTPException(status_code=403, detail="no user scope")
    return scope


def _service(user: Dict[str, Any]):
    from vaf.mail.service import MailService
    return MailService(_scope_of(user))


@router.get("/status")
async def status(_user: Dict[str, Any] = Depends(_get_current_user)) -> Dict[str, Any]:
    """Engine status for the UI: flag state + per-scope counts (cheap)."""
    enabled = bool(Config.get("mail_engine_v2_enabled", False))
    out: Dict[str, Any] = {"v2_enabled": enabled,
                           "write_enabled": bool(Config.get("mail_engine_write_enabled", False))}
    if enabled:
        svc = _service(_user)
        out["counts"] = await asyncio.to_thread(svc.counts)
        out["accounts"] = await asyncio.to_thread(svc.store.list_accounts)
    return out


@router.get("/threads")
async def list_threads(account_id: Optional[str] = None, folder: Optional[str] = None,
                       limit: int = 50, offset: int = 0,
                       _user: Dict[str, Any] = Depends(_get_current_user)):
    _require_v2()
    svc = _service(_user)
    items = await asyncio.to_thread(
        svc.list_threads, account_id=account_id, folder=folder, limit=limit, offset=offset)
    return {"threads": items}


@router.get("/threads/{thread_id}")
async def thread_detail(thread_id: int, _user: Dict[str, Any] = Depends(_get_current_user)):
    _require_v2()
    svc = _service(_user)
    msgs = await asyncio.to_thread(svc.thread_messages, thread_id)
    if not msgs:
        raise HTTPException(status_code=404, detail="Thread not found")
    return {"messages": msgs}


@router.get("/messages")
async def list_messages(account_id: Optional[str] = None, folder: Optional[str] = None,
                        category: Optional[str] = None, limit: int = 50, offset: int = 0,
                        unread_only: bool = False,
                        _user: Dict[str, Any] = Depends(_get_current_user)):
    _require_v2()
    svc = _service(_user)
    items = await asyncio.to_thread(
        svc.list_messages, account_id=account_id, folder=folder, category=category,
        limit=limit, offset=offset, unread_only=unread_only)
    return {"messages": items}


@router.get("/messages/{message_pk}/body")
async def message_body(message_pk: int, allow_remote: bool = False,
                       _user: Dict[str, Any] = Depends(_get_current_user)):
    _require_v2()
    svc = _service(_user)
    body = await asyncio.to_thread(svc.get_body, message_pk, allow_remote)
    if body is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return body


@router.get("/messages/{message_pk}/parts/{part_ref}")
async def message_part(message_pk: int, part_ref: str,
                       _user: Dict[str, Any] = Depends(_get_current_user)):
    _require_v2()
    svc = _service(_user)
    att = await asyncio.to_thread(svc.get_attachment, message_pk, part_ref)
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    filename, ctype, payload = att
    ctype_l = (ctype or "").lower()
    serve_type = ctype_l if (ctype_l.startswith("image/") and ctype_l != "image/svg+xml") \
        else "application/octet-stream"
    safe_name = "".join(c for c in (filename or "attachment") if c.isalnum() or c in "._- ")[:120]
    return Response(
        content=payload,
        media_type=serve_type,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name or "attachment"}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        })


@router.get("/search")
async def search(q: str, account_id: Optional[str] = None, limit: int = 50,
                 _user: Dict[str, Any] = Depends(_get_current_user)):
    _require_v2()
    svc = _service(_user)
    items = await asyncio.to_thread(svc.search, q, account_id=account_id, limit=limit)
    return {"messages": items}


@router.get("/folders")
async def folders(account_id: str, _user: Dict[str, Any] = Depends(_get_current_user)):
    _require_v2()
    svc = _service(_user)
    return {"folders": await asyncio.to_thread(svc.folders, account_id)}


@router.post("/sync/{account_id}")
async def sync_account(account_id: str, folder: Optional[str] = None,
                       _user: Dict[str, Any] = Depends(_get_current_user)):
    """One on-demand engine sync for the caller's account (whole account by
    tier, or a single folder when given). Runs fully in a worker thread."""
    _require_v2()
    scope = _scope_of(_user)
    username = _user.get("username")

    def _run() -> Dict[str, Any]:
        from vaf.core.email_accounts import get_email_config as _get_email_config
        from vaf.mail.imap_client import MailAuthError, _safe_logout, build_imap_client
        from vaf.mail.service import MailService
        from vaf.mail.sync import ImapSyncEngine
        ec = _get_email_config(username or "admin", user_scope_id=scope)
        acc = next((a for a in (ec.get("accounts") or [])
                    if (a.get("account_id") or a.get("email") or "").lower()
                    == (account_id or "").lower()), None)
        if acc is None:
            raise HTTPException(status_code=404, detail="Account not found")
        svc = MailService(scope)
        from vaf.tools.mail_utils import cred_username_from_kwargs
        cred_username = cred_username_from_kwargs({"username": username or ""})
        try:
            client = build_imap_client(acc, cred_username, scope)
        except MailAuthError as e:
            return {"ok": False, "error": f"auth: {e}"}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        try:
            eng = ImapSyncEngine(svc.store, acc.get("account_id") or account_id,
                                 acc.get("provider") or "imap",
                                 acc.get("email") or account_id, client)
            stats = (eng.sync_folder(folder) if folder else eng.sync_account())
            return {"ok": True, "stats": stats}
        finally:
            _safe_logout(client)

    return await asyncio.to_thread(_run)


# ── phase 2: write endpoints (local-first; server replay via op queue) ──────


def _account_ctx(user: Dict[str, Any], account_id: str):
    """(scope, cred_username, account_cfg) for the caller's own account only."""
    from vaf.core.email_accounts import get_email_config as _get_email_config
    from vaf.tools.mail_utils import cred_username_from_kwargs
    scope = _scope_of(user)
    username = user.get("username") or ""
    ec = _get_email_config(username or "admin", user_scope_id=scope)
    acc = next((a for a in (ec.get("accounts") or [])
                if (a.get("account_id") or a.get("email") or "").lower()
                == (account_id or "").lower()), None)
    return scope, cred_username_from_kwargs({"username": username}), acc


@router.patch("/messages/{message_pk}/flags")
async def patch_flags(message_pk: int, body: Dict[str, Any] = Body(...),
                      _user: Dict[str, Any] = Depends(_get_current_user)):
    """Local-first flag change: {read?: bool, starred?: bool}. The server
    write replays via the op queue when mail_engine_write_enabled is on."""
    _require_v2()
    scope = _scope_of(_user)

    def _run():
        from vaf.mail.service import MailService
        svc = MailService(scope)
        flags = None
        if "read" in body:
            flags = svc.mark_read(message_pk, bool(body["read"]))
        if "starred" in body:
            flags = svc.set_star(message_pk, bool(body["starred"]))
        return flags

    flags = await asyncio.to_thread(_run)
    if flags is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return {"ok": True, "flags": flags}


@router.post("/messages/{message_pk}/archive")
async def archive_message(message_pk: int,
                          _user: Dict[str, Any] = Depends(_get_current_user)):
    _require_v2()
    scope = _scope_of(_user)

    def _run():
        from vaf.mail.service import MailService
        return MailService(scope).archive(message_pk)

    out = await asyncio.to_thread(_run)
    if not out.get("ok"):
        raise HTTPException(status_code=409, detail=out.get("error") or "archive failed")
    return out


@router.post("/messages/{message_pk}/trash")
async def trash_message(message_pk: int,
                        _user: Dict[str, Any] = Depends(_get_current_user)):
    """Trash-only delete semantics: MOVE to the trash folder, never EXPUNGE."""
    _require_v2()
    scope = _scope_of(_user)

    def _run():
        from vaf.mail.service import MailService
        return MailService(scope).trash(message_pk)

    out = await asyncio.to_thread(_run)
    if not out.get("ok"):
        raise HTTPException(status_code=409, detail=out.get("error") or "trash failed")
    return out


@router.get("/messages/{message_pk}/reply-prefill")
async def reply_prefill(message_pk: int, reply_all: bool = False, forward: bool = False,
                        _user: Dict[str, Any] = Depends(_get_current_user)):
    _require_v2()
    scope = _scope_of(_user)

    def _run():
        from vaf.mail.service import MailService
        svc = MailService(scope)
        return svc.forward_prefill(message_pk) if forward else             svc.reply_prefill(message_pk, reply_all=reply_all)

    pre = await asyncio.to_thread(_run)
    if pre is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return pre


@router.post("/send")
async def send_message(body: Dict[str, Any] = Body(...),
                       _user: Dict[str, Any] = Depends(_get_current_user)):
    """Queue an outgoing mail with an undo window (client-delay model). The
    outbox op survives restarts; delivery runs through the v1 transport with
    its provider-correct auth and Bcc semantics."""
    _require_v2()
    account_id = (body.get("account_id") or "").strip()
    to = (body.get("to") or "").strip()
    if not account_id or not to:
        raise HTTPException(status_code=400, detail="account_id and to are required")
    scope, cred_username, acc = _account_ctx(_user, account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found")
    undo = max(0, min(int(body.get("undo_seconds") or 15), 60))

    def _run():
        from vaf.mail.service import MailService
        return MailService(scope).queue_send(
            account_id, to, (body.get("subject") or "").strip(),
            body.get("body") or "", cc=(body.get("cc") or "").strip(),
            bcc=(body.get("bcc") or "").strip(),
            in_reply_to=(body.get("in_reply_to") or "").strip(),
            references=(body.get("references") or "").strip(),
            undo_seconds=undo)

    out = await asyncio.to_thread(_run)

    async def _deliver_later():
        # fast path: deliver right after the undo window; the supervisor sweep
        # is the restart-safe fallback for anything this task misses
        await asyncio.sleep(undo + 2)
        def _process():
            from vaf.core.config import Config
            from vaf.mail.imap_client import MailAuthError, _safe_logout, build_imap_client
            from vaf.mail.service import MailService
            from vaf.mail.writeback import OpExecutor
            svc = MailService(scope)
            apk = svc.store.account_pk(account_id)
            if apk is None:
                return
            client = None
            try:
                try:
                    client = build_imap_client(acc, cred_username, scope)
                except (MailAuthError, ValueError):
                    client = None  # send still works; Sent-APPEND is skipped
                # send-only: the fast path exists to deliver THIS queued send;
                # other write ops (which may need a real IMAP session that this
                # path might lack) are left for the sweep, so their attempts are
                # not burned against a session-less client.
                OpExecutor(svc.store, apk, client or _NoImap(), acc, scope,
                           cred_username=cred_username).process(
                    write_enabled=bool(Config.get("mail_engine_write_enabled", False))
                    and client is not None,
                    allowed_kinds={"send"})
            finally:
                if client is not None:
                    _safe_logout(client)
        try:
            await asyncio.to_thread(_process)
        except Exception as e:
            logger.warning("outbox fast-path delivery failed (sweep retries): %s", e)

    # Hold a strong reference: a bare create_task can be garbage-collected before
    # it runs, silently dropping the delivery (asyncio hazard). The sweep is the
    # restart-safe fallback, but the fast path must not vanish under GC.
    _task = asyncio.create_task(_deliver_later())
    _INFLIGHT_SEND_TASKS.add(_task)
    _task.add_done_callback(_INFLIGHT_SEND_TASKS.discard)
    return out


class _NoImap:
    """Null client for send-only op processing when no IMAP session exists."""

    def has_capability(self, cap):
        return False

    def select_folder(self, *a, **k):
        raise RuntimeError("no imap session")

    def append(self, *a, **k):
        raise RuntimeError("no imap session")


@router.delete("/send/{op_id}")
async def cancel_send(op_id: int, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Undo: withdraw a queued send while its undo window is open."""
    _require_v2()
    scope = _scope_of(_user)

    def _run():
        from vaf.mail.service import MailService
        return MailService(scope).cancel_send(op_id)

    ok = await asyncio.to_thread(_run)
    if not ok:
        raise HTTPException(status_code=409, detail="Send already delivered or unknown")
    return {"ok": True}


@router.get("/ops")
async def list_ops(_user: Dict[str, Any] = Depends(_get_current_user)):
    """Pending/failed ops of the caller's store (outbox + write replay state)."""
    _require_v2()
    scope = _scope_of(_user)

    def _run():
        from vaf.mail.service import MailService
        svc = MailService(scope)
        rows = svc.store._conn().execute(
            "SELECT id, account_id, kind, state, attempts, created_at, updated_at "
            "FROM ops WHERE state IN ('pending', 'failed') ORDER BY id DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]

    return {"ops": await asyncio.to_thread(_run)}


@router.get("/image-proxy")
async def image_proxy(url: str, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Remote-image proxy for explicit opt-in loading (tracking protection:
    the reader's IP/cookies never reach the sender's server). SSRF-guarded,
    image-only, size-capped, no redirects followed off-host.

    DNS-rebinding hardening: the host is resolved ONCE and the socket is pinned to
    that validated IP (assert_ip_safe rejects private/loopback/metadata), while the
    TLS cert is still checked against the original hostname - so a rebind between a
    validating lookup and the connect cannot reach an internal address. Only the
    standard web ports (80/443) are reachable, blocking port-scan style abuse."""
    _require_v2()
    from urllib.parse import urlparse
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="invalid url")
    hostname = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443):
        raise HTTPException(status_code=400, detail="only standard web ports are allowed")

    def _fetch():
        import urllib3
        import requests as _rq
        from vaf.network.binding import resolve_pinned_target
        try:
            # mail image URLs are attacker-controlled: resolve ONCE, validate, pin.
            pinned_ip = resolve_pinned_target(hostname, port, allow_private=False)
        except ValueError:
            return ("blocked", None)          # resolved to a non-routable address
        except OSError:
            return ("error", None)            # host does not resolve

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        headers = {"Host": hostname, "User-Agent": "VAF-Mail-ImageProxy", "Accept": "image/*"}
        timeout = urllib3.Timeout(connect=5, read=10)
        if parsed.scheme == "https":
            pool = urllib3.HTTPSConnectionPool(
                pinned_ip, port=port, maxsize=1, retries=False, timeout=timeout,
                cert_reqs="CERT_REQUIRED", ca_certs=_rq.certs.where(),
                # connect to the pinned IP but verify the cert against the hostname
                server_hostname=hostname, assert_hostname=hostname)
        else:
            pool = urllib3.HTTPConnectionPool(
                pinned_ip, port=port, maxsize=1, retries=False, timeout=timeout)
        try:
            r = pool.request("GET", path, headers=headers, redirect=False,
                             preload_content=False, decode_content=False)
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if r.status != 200 or not ctype.startswith("image/") or ctype == "image/svg+xml":
                return ("error", None)
            data = r.read(5 * 1024 * 1024 + 1)
            if len(data) > 5 * 1024 * 1024:
                return ("error", None)
            return ("ok", ctype, data)
        except Exception:
            return ("error", None)
        finally:
            try:
                pool.close()
            except Exception:
                pass

    result = await asyncio.to_thread(_fetch)
    kind = result[0]
    if kind == "blocked":
        from vaf.core.security_events import log_security_event
        log_security_event("mail_image_proxy_blocked",
                           username=_user.get("username") or "",
                           detail=f"host refused: {hostname}")
        raise HTTPException(status_code=403, detail="host refused")
    if kind != "ok":
        raise HTTPException(status_code=502, detail="image not loadable")
    _, ctype, data = result
    return Response(content=data, media_type=ctype, headers={
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=86400",
        "Content-Security-Policy": "default-src 'none'",
    })
