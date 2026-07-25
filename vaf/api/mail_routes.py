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
    """Engine status for the UI: flag state + per-scope counts (cheap).

    The account list is the UNION of the engine store and the configured mail
    accounts: an account the engine does not sync yet (an OAuth account still
    awaiting the IMAP re-consent) has no store row, and listing only the store
    would silently drop it from the client - which reads as "my account is
    gone" rather than "this account needs re-consent". Config-only entries are
    marked synced=False so the UI can show that state instead."""
    enabled = bool(Config.get("mail_engine_v2_enabled", False))
    out: Dict[str, Any] = {"v2_enabled": enabled,
                           "write_enabled": bool(Config.get("mail_engine_write_enabled", False))}
    if enabled:
        svc = _service(_user)
        out["counts"] = await asyncio.to_thread(svc.counts)
        synced = await asyncio.to_thread(svc.store.list_accounts)
        out["accounts"] = await asyncio.to_thread(_union_config_accounts, synced, _user)
    return out


def _union_config_accounts(synced: list, user: Dict[str, Any]) -> list:
    """Append configured mail accounts the engine store does not know yet."""
    rows = [{**a, "synced": True} for a in (synced or [])]
    known = {a.get("account_id") for a in rows}
    try:
        from vaf.core.email_accounts import list_mail_accounts
        username, _cred, scope = _acct_identity(user)
        for a in list_mail_accounts(username, user_scope_id=scope) or []:
            aid = a.get("account_id") or a.get("email")
            if aid and aid not in known:
                rows.append({"account_id": aid, "email": a.get("email") or aid,
                             "provider": a.get("provider") or "imap",
                             "imap_ready": bool(a.get("imap_ready")), "synced": False})
    except Exception as e:  # pragma: no cover - the store list stays usable alone
        logger.warning("config account union failed, showing synced accounts only: %s", e)
    return rows


@router.get("/threads")
async def list_threads(account_id: Optional[str] = None, folder: Optional[str] = None,
                       limit: int = 50, offset: int = 0,
                       _user: Dict[str, Any] = Depends(_get_current_user)):
    _require_v2()
    svc = _service(_user)
    items = await asyncio.to_thread(
        svc.list_threads, account_id=account_id, folder=folder, limit=limit, offset=offset)
    return {"threads": svc.annotate_visibility(items)}


@router.get("/threads/{thread_id}")
async def thread_detail(thread_id: int, _user: Dict[str, Any] = Depends(_get_current_user)):
    _require_v2()
    svc = _service(_user)
    msgs = await asyncio.to_thread(svc.thread_messages, thread_id)
    if not msgs:
        raise HTTPException(status_code=404, detail="Thread not found")
    return {"messages": svc.annotate_visibility(msgs)}


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
    return {"messages": svc.annotate_visibility(items)}


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
    return {"messages": svc.annotate_visibility(items)}


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
            # Carry the user's legacy labels/answered markers over here too: the
            # import used to run ONLY on the supervisor sweep, so a user with
            # auto-sync off never got them even though pressing Sync did surface
            # the mail itself.
            try:
                from vaf.mail.migrate import import_legacy_artifacts
                import_legacy_artifacts(svc.store, cred_username or "", scope,
                                        account_id=acc.get("account_id") or account_id)
            except Exception as e:
                logger.info("legacy artifact import skipped on manual sync: %s", e)
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


@router.patch("/messages/{message_pk}/category")
async def set_message_category(message_pk: int, body: Dict[str, Any] = Body(...),
                               _user: Dict[str, Any] = Depends(_get_current_user)):
    """Gmail-style category relabel: {category: str}. Per the owner decision (P5.4)
    this ALSO learns a sender rule for the message's From address and backfills every
    stored mail from that sender. All of it is a LOCAL classification (nothing is
    written to the mail server), so it needs only the v2 flag, not
    mail_engine_write_enabled. Returns {ok, category, updated}."""
    _require_v2()
    scope = _scope_of(_user)
    username = _user.get("username")

    def _run():
        from vaf.mail.service import MailService
        return MailService(scope).relabel_and_learn(
            message_pk, str(body.get("category") or ""), username=username)

    out = await asyncio.to_thread(_run)
    if out is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return {"ok": True, **out}


@router.post("/messages/apply-sender-rules")
async def apply_sender_rules(_user: Dict[str, Any] = Depends(_get_current_user)):
    """Re-apply the sender->category rules to every stored message (backfill).
    Local classification only; gated by the v2 flag. Returns {ok, updated}."""
    _require_v2()
    scope = _scope_of(_user)
    username = _user.get("username")

    def _run():
        from vaf.mail.service import MailService
        return MailService(scope).apply_sender_rules_backfill(username=username)

    return {"ok": True, "updated": await asyncio.to_thread(_run)}


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
            "SELECT id, account_id, kind, state, attempts, created_at, updated_at, "
            "json_extract(payload, '$.last_error') AS last_error, "
            "json_extract(payload, '$.subject') AS subject "
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


# ── account management (P4.3): the /mail account panel (P5) builds on these; OAuth
#    sign-in stays on the shared /api/email hub (P4.4). All build on the
#    email_accounts SSOT + credential_store + the P4.2 calendar-safe delete. ──

def _acct_identity(user: Dict[str, Any]):
    from vaf.tools.mail_utils import cred_username_from_kwargs
    return (user.get("username") or "admin",
            cred_username_from_kwargs({"username": user.get("username")}),
            user.get("user_scope_id"))


@router.get("/accounts")
async def accounts(_user: Dict[str, Any] = Depends(_get_current_user)):
    """Connected mail accounts (calendar-only leftovers hidden via mail_enabled)."""
    _require_v2()
    from vaf.core.email_accounts import list_mail_accounts
    username, _cred, scope = _acct_identity(_user)
    rows = await asyncio.to_thread(lambda: list_mail_accounts(username, user_scope_id=scope))
    return {"accounts": [{
        "account_id": a.get("account_id") or a.get("email"),
        "email": a.get("email") or a.get("account_id"),
        "provider": (a.get("provider") or "imap"),
        "label": (a.get("label") or "").strip(),
        "imap_ready": bool(a.get("imap_ready")),
        "auto_sync_enabled": bool(a.get("auto_sync_enabled")),
    } for a in rows]}


@router.post("/accounts/test")
async def accounts_test(body: Dict[str, Any] = Body(...), _user: Dict[str, Any] = Depends(_get_current_user)):
    """Try an IMAP login; nothing is saved."""
    _require_v2()
    from vaf.core.email_accounts import test_imap_login
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if not email or not password:
        raise HTTPException(status_code=422, detail="email and password are required")
    ok, err, hint = await asyncio.to_thread(
        lambda: test_imap_login(email, password, body.get("imap_host"), body.get("imap_port")))
    return {"ok": ok, "error": err, "hint": hint}


@router.post("/accounts")
async def accounts_add(body: Dict[str, Any] = Body(...), _user: Dict[str, Any] = Depends(_get_current_user)):
    """Add an IMAP account: verify the login, store the password, add the config
    entry with host/port defaulted from the provider presets."""
    _require_v2()
    from vaf.core.credential_store import set_email_imap_password
    from vaf.core.email_accounts import (
        IMAP_SMTP_DEFAULTS, add_account, oauth_provider_for, test_imap_login,
    )
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        raise HTTPException(status_code=422, detail="email and password are required")
    username, cred_username, scope = _acct_identity(_user)
    # Adding a password account for an address that is already connected via OAuth
    # would REPLACE that entry, and the calendar resolves its accounts by exactly
    # that provider - so it would lose the account without saying so. Refuse and
    # point at the sign-in, which grants everything the engine needs anyway.
    connected = await asyncio.to_thread(
        lambda: oauth_provider_for(email, username, user_scope_id=scope))
    if connected:
        return {"ok": False,
                "error": f"This address is already connected via {connected}.",
                "hint": "Use Reconnect on that account instead - signing in grants the "
                        "mail access the engine needs and keeps your calendar connected."}
    d = IMAP_SMTP_DEFAULTS.get(email.split("@")[-1] if "@" in email else "", {})
    imap_host = (body.get("imap_host") or "").strip() or d.get("imap_host")
    imap_port = int(body.get("imap_port") or d.get("imap_port") or 993)
    smtp_host = (body.get("smtp_host") or "").strip() or d.get("smtp_host")
    smtp_port = int(body.get("smtp_port") or d.get("smtp_port") or 587)
    ok, err, hint = await asyncio.to_thread(lambda: test_imap_login(email, password, imap_host, imap_port))
    if not ok:
        return {"ok": False, "error": err, "hint": hint}
    await asyncio.to_thread(lambda: set_email_imap_password(email, password, cred_username, user_scope_id=scope))
    await asyncio.to_thread(lambda: add_account({
        "account_id": email, "email": email, "provider": "imap", "enabled": True,
        "label": (body.get("label") or "").strip(), "imap_host": imap_host, "imap_port": imap_port,
        "smtp_host": smtp_host, "smtp_port": smtp_port, "auto_sync_enabled": True, "mail_enabled": True,
    }, username, user_scope_id=scope))
    return {"ok": True, "account_id": email}


@router.post("/accounts/{account_id}/verify")
async def accounts_verify(account_id: str, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Re-check the connection for a saved account (IMAP password / OAuth token)."""
    _require_v2()
    from vaf.core.email_accounts import get_account, test_imap_login
    username, cred_username, scope = _acct_identity(_user)
    acc = await asyncio.to_thread(lambda: get_account(account_id, username, user_scope_id=scope))
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")
    provider = (acc.get("provider") or "imap").lower()
    if provider in ("gmail", "microsoft"):
        from vaf.core.oauth_pkce import get_valid_access_token
        lane = "microsoft_imap" if provider == "microsoft" else provider
        tok = await asyncio.to_thread(lambda: get_valid_access_token(account_id, lane, cred_username, user_scope_id=scope))
        return {"ok": bool(tok), "error": "" if tok else "no valid token (re-consent may be required)"}
    from vaf.core.credential_store import get_email_credentials
    creds = await asyncio.to_thread(lambda: get_email_credentials(account_id, "imap", cred_username, user_scope_id=scope))
    if not creds or not creds.get("password"):
        return {"ok": False, "error": "no stored password"}
    ok, err, _hint = await asyncio.to_thread(
        lambda: test_imap_login(acc.get("email") or account_id, creds["password"], acc.get("imap_host"), acc.get("imap_port")))
    return {"ok": ok, "error": err}


@router.patch("/accounts/{account_id}")
async def accounts_patch(account_id: str, body: Dict[str, Any] = Body(...), _user: Dict[str, Any] = Depends(_get_current_user)):
    """Edit a per-account label or auto-sync toggle."""
    _require_v2()
    from vaf.core.email_accounts import patch_account
    fields: Dict[str, Any] = {}
    if "label" in body:
        fields["label"] = (body.get("label") or "").strip()
    if "auto_sync_enabled" in body:
        fields["auto_sync_enabled"] = bool(body.get("auto_sync_enabled"))
    if not fields:
        raise HTTPException(status_code=422, detail="nothing to patch (label / auto_sync_enabled)")
    username, _cred, scope = _acct_identity(_user)
    ok = await asyncio.to_thread(lambda: patch_account(account_id, fields, username, user_scope_id=scope))
    if not ok:
        raise HTTPException(status_code=404, detail="account not found")
    return {"ok": True}


@router.delete("/accounts/{account_id}")
async def accounts_delete(account_id: str, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Calendar-safe delete via the shared email_accounts orchestrator (a
    gmail/microsoft account keeps its shared OAuth token + entry for Calendar)."""
    _require_v2()
    from vaf.core.email_accounts import delete_mail_account
    username, cred_username, scope = _acct_identity(_user)
    res = await asyncio.to_thread(lambda: delete_mail_account(
        account_id, username=username, cred_username=cred_username, user_scope_id=scope))
    return {"ok": True, **res}
