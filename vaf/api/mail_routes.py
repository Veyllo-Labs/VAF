# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Mail engine v2 REST API (/api/mail/*). Design: EMAIL_CLIENT.md.

Rules:
- Every endpoint resolves the caller via _get_current_user and builds a
  MailService for that scope only (fail-closed; the local admin's identity
  fallback resolves to the admin's REAL scope UUID, never to "no scope").
- Attachments are served with Content-Disposition: attachment and nosniff;
  only image/* (except SVG) keeps its real content type so cid: inline images
  render - everything else is application/octet-stream.
- Provider IO always runs via asyncio.to_thread - never on the event loop.
"""
import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Response

from vaf.api.config_routes import get_current_user_or_local_admin as _get_current_user
from vaf.core import composer_lane
from vaf.core.config import Config, get_local_admin_scope_id

logger = logging.getLogger("vaf.api.mail_routes")

router = APIRouter(prefix="/api/mail", tags=["mail-v2"])

# Strong references to in-flight undo-send delivery tasks so the event loop's
# weak task references cannot GC them mid-delivery (see /send fast path).
_INFLIGHT_SEND_TASKS: set = set()


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
    """Status for the UI: per-scope counts and the account list (cheap).

    The account list is the UNION of the engine store and the configured mail
    accounts: an account the engine does not sync yet (an OAuth account still
    awaiting the IMAP re-consent) has no store row, and listing only the store
    would silently drop it from the client - which reads as "my account is
    gone" rather than "this account needs re-consent". Config-only entries are
    marked synced=False so the UI can show that state instead.

    write_enabled still travels: server-side mailbox writes keep their own
    switch, which the client surfaces."""
    svc = _service(_user)
    synced = await asyncio.to_thread(svc.store.list_accounts)
    return {
        "write_enabled": bool(Config.get("mail_engine_write_enabled", False)),
        "composer_enabled": bool(Config.get("mail_composer_enabled", True)),
        "counts": await asyncio.to_thread(svc.counts),
        "accounts": await asyncio.to_thread(_union_config_accounts, synced, _user),
    }


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
    svc = _service(_user)

    def _run():
        items = svc.list_threads(account_id=account_id, folder=folder, limit=limit, offset=offset)
        return _with_inbox_state(items, _user)

    items = await asyncio.to_thread(_run)
    return {"threads": svc.annotate_visibility(items)}


def _with_inbox_state(threads: list, user: Dict[str, Any]) -> list:
    """`waits`, `waits_reason`, `done` and `answered_by_agent` on each thread row, from the
    rule and the done marks the inbox reads (`vaf.core.inbox.mail_thread_state`), so the mail
    window and the Posteingang never disagree about who waits."""
    from vaf.core.channel_message_store import chat_marks
    from vaf.core.inbox import mail_thread_state
    from vaf.mail.service import MailService
    marks = chat_marks(user.get("username") or "", _scope_of(user), channel="mail")
    drafts: Dict[int, Dict[str, Any]] = {}
    try:
        for d in MailService(_scope_of(user)).list_drafts():
            if d.get("thread_id") is not None:
                drafts.setdefault(int(d["thread_id"]), d)
    except Exception:
        drafts = {}
    for t in threads:
        draft = drafts.get(int(t.get("thread_id") or 0))
        state = mail_thread_state(t, marks.get(("mail", str(t.get("thread_id")))), draft=draft)
        for k in ("waits", "waits_reason", "done", "answered_by_agent"):
            t[k] = state[k]
        t["draft"] = ({"op_id": draft["op_id"], "to": draft.get("to") or "", "subject": draft.get("subject") or "",
                       "body": draft.get("body") or "", "created_at": draft.get("created_at") or ""}
                      if draft else None)
    return threads


@router.get("/threads/{thread_id}")
async def thread_detail(thread_id: int, _user: Dict[str, Any] = Depends(_get_current_user)):
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
    svc = _service(_user)
    items = await asyncio.to_thread(
        svc.list_messages, account_id=account_id, folder=folder, category=category,
        limit=limit, offset=offset, unread_only=unread_only)
    return {"messages": svc.annotate_visibility(items)}


@router.get("/messages/{message_pk}/body")
async def message_body(message_pk: int, allow_remote: bool = False,
                       _user: Dict[str, Any] = Depends(_get_current_user)):
    svc = _service(_user)
    body = await asyncio.to_thread(svc.get_body, message_pk, allow_remote)
    if body is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return body


@router.get("/messages/{message_pk}/parts/{part_ref}")
async def message_part(message_pk: int, part_ref: str,
                       _user: Dict[str, Any] = Depends(_get_current_user)):
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
    svc = _service(_user)
    items = await asyncio.to_thread(svc.search, q, account_id=account_id, limit=limit)
    return {"messages": svc.annotate_visibility(items)}


@router.get("/folders")
async def folders(account_id: str, _user: Dict[str, Any] = Depends(_get_current_user)):
    svc = _service(_user)
    return {"folders": await asyncio.to_thread(svc.folders, account_id)}


@router.post("/sync/{account_id}")
async def sync_account(account_id: str, folder: Optional[str] = None,
                       _user: Dict[str, Any] = Depends(_get_current_user)):
    """One on-demand engine sync for the caller's account (whole account by
    tier, or a single folder when given). Runs fully in a worker thread."""
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
            from vaf.mail.verification import auth_policy_for_account
            eng = ImapSyncEngine(svc.store, acc.get("account_id") or account_id,
                                 acc.get("provider") or "imap",
                                 acc.get("email") or account_id, client,
                                 auth_policy=auth_policy_for_account(acc))
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
    if "read" in body:
        # A read mark changes the inbox's unread count; a star changes no conversation list.
        from vaf.core.web_interface import notify_inbox_changed
        notify_inbox_changed(scope)
    return {"ok": True, "flags": flags}


@router.post("/messages/{message_pk}/archive")
async def archive_message(message_pk: int,
                          _user: Dict[str, Any] = Depends(_get_current_user)):
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
    """Gmail-style category relabel: {category: str}. Deliberately
    this ALSO learns a sender rule for the message's From address and backfills every
    stored mail from that sender. All of it is a LOCAL classification (nothing is
    written to the mail server), so it needs only the v2 flag, not
    mail_engine_write_enabled. Returns {ok, category, updated}."""
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
    scope = _scope_of(_user)
    username = _user.get("username")

    def _run():
        from vaf.mail.service import MailService
        return MailService(scope).apply_sender_rules_backfill(username=username)

    return {"ok": True, "updated": await asyncio.to_thread(_run)}


@router.get("/messages/{message_pk}/reply-prefill")
async def reply_prefill(message_pk: int, reply_all: bool = False, forward: bool = False,
                        _user: Dict[str, Any] = Depends(_get_current_user)):
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
        # send-only: the fast path exists to deliver THIS queued send; other write ops
        # (which may need a real IMAP session that this path might lack) are left for
        # the sweep, so their attempts are not burned against a session-less client.
        from vaf.mail.service import deliver_queued_sends
        try:
            await asyncio.to_thread(deliver_queued_sends, scope, acc, cred_username, account_id)
        except Exception as e:
            logger.warning("outbox fast-path delivery failed (sweep retries): %s", e)

    # Hold a strong reference: a bare create_task can be garbage-collected before
    # it runs, silently dropping the delivery (asyncio hazard). The sweep is the
    # restart-safe fallback, but the fast path must not vanish under GC.
    _task = asyncio.create_task(_deliver_later())
    _INFLIGHT_SEND_TASKS.add(_task)
    _task.add_done_callback(_INFLIGHT_SEND_TASKS.discard)
    return out


@router.delete("/send/{op_id}")
async def cancel_send(op_id: int, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Undo: withdraw a queued send while its undo window is open."""
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


@router.post("/ops/{op_id}/retry")
async def retry_op(op_id: int, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Re-arm a parked (failed) op so the outbox tries it again.

    Without this a parked send is a dead end: the banner reports it forever and
    nothing in the app can clear it, even after the cause was fixed (a
    re-connected account, a corrected credential)."""
    scope = _scope_of(_user)

    def _run():
        from vaf.mail.service import MailService
        store = MailService(scope).store
        return store.mark_op(op_id, "pending", expect_state="failed")

    if not await asyncio.to_thread(_run):
        raise HTTPException(status_code=404, detail="no parked op with that id")
    return {"ok": True}


@router.delete("/ops/{op_id}")
async def discard_op(op_id: int, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Drop a parked op the user does not want retried (the message stays in the
    store; only the queued delivery attempt is abandoned)."""
    scope = _scope_of(_user)

    def _run():
        from vaf.mail.service import MailService
        store = MailService(scope).store
        return store.mark_op(op_id, "cancelled", expect_state="failed")

    if not await asyncio.to_thread(_run):
        raise HTTPException(status_code=404, detail="no parked op with that id")
    return {"ok": True}


@router.get("/image-proxy")
async def image_proxy(url: str, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Remote-image proxy for explicit opt-in loading. SSRF-guarded, image-only,
    size-capped, no redirects followed.

    What it protects, precisely - the wording matters because this route is what a
    compliance review reads. The reader's BROWSER IDENTITY never reaches the
    sender: no cookies, no Referer, no real User-Agent, no Accept-Language, no DNT
    (the handler takes no Request object, so it cannot forward one by accident).
    What it does NOT do, and an earlier version of this docstring wrongly claimed
    it did: hide the reader's IP, or stop tracking. The backend runs on the
    reader's own machine, so the sender's host observes the same egress address a
    direct browser fetch would have used, and the tracking URL (including a
    per-recipient token) is forwarded verbatim - an open is still reported, with a
    read timestamp that is MORE accurate than an auto-loading client's, because
    the fetch happens when the user clicks.

    DNS-rebinding hardening: the host is resolved ONCE and the socket is pinned to
    that validated IP (assert_ip_safe rejects private/loopback/metadata), while the
    TLS cert is still checked against the original hostname - so a rebind between a
    validating lookup and the connect cannot reach an internal address. Only the
    standard web ports (80/443) are reachable, blocking port-scan style abuse."""
    from urllib.parse import urlparse
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="invalid url")
    hostname = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443):
        raise HTTPException(status_code=400, detail="only standard web ports are allowed")

    def _fetch():
        import socket

        import urllib3
        import requests as _rq
        from vaf.network.binding import (assert_ip_safe, resolve_pinned_target,
                                         system_proxy_for)

        headers = {"Host": hostname, "User-Agent": "VAF-Mail-ImageProxy", "Accept": "image/*"}
        timeout = urllib3.Timeout(connect=5, read=10)
        proxy_url = system_proxy_for(parsed.scheme, hostname)

        if proxy_url:
            # Managed network: the site proxy performs egress control AND name
            # resolution, so pinning an IP here is impossible (CONNECT carries the
            # hostname) and pointless - the proxy, not this process, decides what
            # is reachable. Defense in depth is kept where it still works: if the
            # host resolves locally to a non-public address we refuse before
            # handing it over, and only split-horizon names the resolver does not
            # know are passed through unchecked.
            try:
                for info in socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP):
                    assert_ip_safe(info[4][0], allow_private=False)
            except ValueError:
                return ("blocked", None)
            except OSError:
                pass                          # proxy-only DNS: let the proxy judge
            pool = urllib3.ProxyManager(proxy_url, maxsize=1, retries=False,
                                        timeout=timeout, ca_certs=_rq.certs.where())
            target = f"{parsed.scheme}://{hostname}:{port}{parsed.path or '/'}"
            if parsed.query:
                target = f"{target}?{parsed.query}"
        else:
            try:
                # mail image URLs are attacker-controlled: resolve ONCE, validate, pin.
                pinned_ip = resolve_pinned_target(hostname, port, allow_private=False)
            except ValueError:
                return ("blocked", None)      # resolved to a non-routable address
            except OSError:
                return ("error", None)        # host does not resolve

            target = parsed.path or "/"
            if parsed.query:
                target = f"{target}?{parsed.query}"
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
            r = pool.request("GET", target, headers=headers, redirect=False,
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
    from vaf.core.email_accounts import list_mail_accounts
    username, _cred, scope = _acct_identity(_user)
    rows = await asyncio.to_thread(lambda: list_mail_accounts(username, user_scope_id=scope))
    return {"accounts": [_account_row(a) for a in rows]}


def _account_row(a: Dict[str, Any]) -> Dict[str, Any]:
    """One account for the panel: the settings and the sender-verification state (the
    provider's Authentication-Results id the store trusts, how it was learned)."""
    from vaf.mail.verification import auth_policy_for_account
    policy = auth_policy_for_account(a)
    return {
        "account_id": a.get("account_id") or a.get("email"),
        "email": a.get("email") or a.get("account_id"),
        "provider": (a.get("provider") or "imap"),
        "label": (a.get("label") or "").strip(),
        "imap_ready": bool(a.get("imap_ready")),
        "auto_sync_enabled": bool(a.get("auto_sync_enabled")),
        "trusted_authserv_id": policy["trusted_authserv_id"],
        "auth_profile": policy["auth_profile"],
        "authserv_source": str(a.get("authserv_source") or ""),
        "authserv_learned_at": str(a.get("authserv_learned_at") or ""),
        "authserv_samples": int(a.get("authserv_samples") or 0),
        "aliases": [str(x) for x in (a.get("aliases") or [])],
        # The verification is set up when a trusted id is known, or the profile needs none.
        "auth_ready": bool(policy["trusted_authserv_id"]) or policy["auth_profile"] == "microsoft",
    }


def _login_failure(email: str, err: str, hint: Optional[str]) -> Dict[str, Any]:
    """The body both account endpoints return for a refused IMAP login.

    hint_detail carries the guidance in PARTS (provider, auth kind, whether IMAP
    has to be switched on, the provider's help page) so the UI renders it in the
    reader's language instead of the English `hint`. It rides along only when
    test_imap_login produced a hint, which is only when the server refused the
    login - a DNS failure must not be answered with app-password advice."""
    from vaf.core.email_accounts import auth_failure_hint
    body: Dict[str, Any] = {"ok": False, "error": err, "hint": hint}
    if hint:
        body["hint_detail"] = auth_failure_hint(email)
    return body


@router.post("/accounts/test")
async def accounts_test(body: Dict[str, Any] = Body(...), _user: Dict[str, Any] = Depends(_get_current_user)):
    """Try an IMAP login; nothing is saved."""
    from vaf.core.email_accounts import test_imap_login
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if not email or not password:
        raise HTTPException(status_code=422, detail="email and password are required")
    ok, err, hint = await asyncio.to_thread(
        lambda: test_imap_login(email, password, body.get("imap_host"), body.get("imap_port")))
    return {"ok": True, "error": "", "hint": None} if ok else _login_failure(email, err, hint)


@router.post("/accounts")
async def accounts_add(body: Dict[str, Any] = Body(...), _user: Dict[str, Any] = Depends(_get_current_user)):
    """Add an IMAP account: verify the login, store the password, add the config
    entry with host/port defaulted from the provider presets."""
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
        return _login_failure(email, err, hint)
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
    from vaf.core.email_accounts import get_account, patch_account
    from vaf.mail.verification import AUTH_PROFILES
    fields: Dict[str, Any] = {}
    if "label" in body:
        fields["label"] = (body.get("label") or "").strip()
    if "auto_sync_enabled" in body:
        fields["auto_sync_enabled"] = bool(body.get("auto_sync_enabled"))
    # Sender verification, set by hand: the provider's authserv-id, the header profile,
    # the account's other addresses. A verdict depends on them, so every stored verdict
    # of the account is recomputed under the new policy afterwards.
    if "trusted_authserv_id" in body:
        fields["trusted_authserv_id"] = str(body.get("trusted_authserv_id") or "").strip().lower()[:253]
        fields["authserv_source"] = "manual" if fields["trusted_authserv_id"] else ""
    if "auth_profile" in body:
        profile = str(body.get("auth_profile") or "").strip().lower()
        if profile not in AUTH_PROFILES:
            raise HTTPException(status_code=422, detail="auth_profile must be rfc8601, microsoft or none")
        fields["auth_profile"] = profile
    if "aliases" in body:
        raw_aliases = body.get("aliases") or []
        if not isinstance(raw_aliases, list):
            raise HTTPException(status_code=422, detail="aliases must be a list of addresses")
        fields["aliases"] = sorted({str(x).strip().lower() for x in raw_aliases if "@" in str(x)})[:32]
    if not fields:
        raise HTTPException(status_code=422, detail="nothing to patch (label / auto_sync_enabled / trusted_authserv_id / auth_profile / aliases)")
    username, _cred, scope = _acct_identity(_user)
    ok = await asyncio.to_thread(lambda: patch_account(account_id, fields, username, user_scope_id=scope))
    if not ok:
        raise HTTPException(status_code=404, detail="account not found")
    backfilled = 0
    if {"trusted_authserv_id", "auth_profile", "aliases"} & set(fields):
        backfilled = await asyncio.to_thread(
            lambda: _backfill_account(scope, get_account(account_id, username, user_scope_id=scope)))
    return {"ok": True, "backfilled": backfilled}


def _backfill_account(scope: str, account: Optional[Dict[str, Any]]) -> int:
    """Recompute the account's verdicts under its current policy (never raises: a
    backfill that fails leaves the old verdicts, and the next learn or patch retries)."""
    if not account:
        return 0
    try:
        from vaf.mail.service import MailService
        from vaf.mail.verification import auth_policy_for_account
        aid = account.get("account_id") or account.get("email") or ""
        return MailService(scope).backfill_verification(aid, auth_policy_for_account(account))
    except Exception as e:
        logger.warning("verification backfill failed for %s: %s", (str(account.get("account_id") or ""))[:3] + "***", e)
        return 0


@router.post("/accounts/{account_id}/learn-auth")
async def accounts_learn_auth(account_id: str, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Learn the provider's Authentication-Results id from the account's own inbox (the
    majority topmost authserv-id, or the Microsoft id-less form), save it on the account
    and recompute every stored verdict under it. Answers what was learned and how many
    rows were rewritten; with too few or disagreeing samples nothing is saved and the
    counts say why."""
    from vaf.core.email_accounts import get_account, patch_account
    username, _cred, scope = _acct_identity(_user)
    acc = get_account(account_id, username, user_scope_id=scope)
    if acc is None:
        raise HTTPException(status_code=404, detail="account not found")

    def _run():
        from vaf.mail.service import MailService
        svc = MailService(scope)
        learned = svc.learn_provider(acc.get("account_id") or account_id)
        saved = False
        backfilled = 0
        if learned.get("authserv_id") or learned.get("profile") == "microsoft":
            fields = {
                "trusted_authserv_id": learned.get("authserv_id") or "",
                "auth_profile": learned.get("profile") or "rfc8601",
                "authserv_source": "mailbox",
                "authserv_learned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "authserv_samples": int(learned.get("count") or 0),
            }
            saved = patch_account(account_id, fields, username, user_scope_id=scope)
            if saved:
                backfilled = _backfill_account(scope, get_account(account_id, username, user_scope_id=scope))
        return {"ok": True, "learned": learned, "saved": bool(saved), "backfilled": backfilled}

    return await asyncio.to_thread(_run)


@router.get("/drafts")
async def list_drafts(thread_id: Optional[int] = None, _user: Dict[str, Any] = Depends(_get_current_user)):
    """The agent's held answers awaiting the caller's approval (FRONT_OFFICE.md, "Mail"),
    newest first, narrowed to one thread when asked."""
    svc = _service(_user)
    rows = await asyncio.to_thread(lambda: svc.list_drafts(thread_id=thread_id))
    return {"drafts": rows}


@router.post("/drafts/{op_id}/send")
async def send_draft(op_id: int, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Approve a held answer: it becomes a queued send and leaves right away (the
    supervisor sweep is the restart-safe fallback), and the inbox stops waiting. The release
    itself is `release_held_draft`, the one function the chat card and `vaf outbox send` call
    too: this route used to carry its own copy of the two acts, and a send that did not leave
    was then parked out of the person's sight here while the card kept it. The account is
    checked first so a draft for an account this caller does not have stays held."""
    svc = _service(_user)
    op = await asyncio.to_thread(svc.store.get_op, int(op_id))
    if not op or op.get("kind") != "send" or op.get("state") != "held":
        raise HTTPException(status_code=404, detail="no held draft with that id")
    account_id = str((op.get("payload") or {}).get("account_id") or "")
    scope, _cred_username, acc = _account_ctx(_user, account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found")
    from vaf.mail.service import release_held_draft
    outcome = await asyncio.to_thread(release_held_draft, scope, str(_user.get("username") or ""),
                                      int(op_id), svc)
    if outcome.get("error") == "not waiting":
        raise HTTPException(status_code=409, detail="the draft is no longer held")
    try:
        from vaf.core.web_interface import notify_inbox_changed
        notify_inbox_changed(scope)
    except Exception:
        pass
    return {"ok": bool(outcome.get("ok")), "state": outcome.get("state") or "",
            "delivery": outcome.get("delivery") or "", "error": outcome.get("error") or ""}


@router.delete("/drafts/{op_id}")
async def discard_draft(op_id: int, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Discard a held answer; the mail it answered keeps waiting for the caller."""
    svc = _service(_user)
    if not await asyncio.to_thread(svc.discard_draft, int(op_id)):
        raise HTTPException(status_code=404, detail="no held draft with that id")
    try:
        from vaf.core.web_interface import notify_inbox_changed
        notify_inbox_changed(_scope_of(_user))
    except Exception:
        pass
    return {"ok": True}


@router.get("/messages/{message_pk}/verdict")
async def message_verdict(message_pk: int, _user: Dict[str, Any] = Depends(_get_current_user)):
    """The full stored verdict of one message: the machine kind, the provider's
    authentication results, the alignment, the identity flags and the reasons, plus the
    header snapshot it was computed from."""
    svc = _service(_user)
    row = await asyncio.to_thread(svc.message_verdict, message_pk)
    if row is None:
        raise HTTPException(status_code=404, detail="no verdict for that message")
    return {"verdict": row}


@router.delete("/accounts/{account_id}")
async def accounts_delete(account_id: str, _user: Dict[str, Any] = Depends(_get_current_user)):
    """Calendar-safe delete via the shared email_accounts orchestrator (a
    gmail/microsoft account keeps its shared OAuth token + entry for Calendar)."""
    from vaf.core.email_accounts import delete_mail_account
    username, cred_username, scope = _acct_identity(_user)
    res = await asyncio.to_thread(lambda: delete_mail_account(
        account_id, username=username, cred_username=cred_username, user_scope_id=scope))
    return {"ok": True, **res}


# ── Mail Composer: draft / rewrite into the compose box ────────────────────────
#
# The Composer itself is shared with the messenger windows: the prompt is
# vaf/core/composer.py, the ONE tool-less completion, the memory lookup and the
# settings are vaf/core/composer_lane.py. What is mail-shaped stays here: the
# thread from the mail store, the phishing flags, the mailbox search.

LocalModelUnavailable = composer_lane.LocalModelUnavailable


def _composer_settings() -> Dict[str, Any]:
    return composer_lane.settings()


def _composer_knowledge(user: Dict[str, Any], instruction: str, subject: str = "") -> str:
    """The user's own notes for this draft; with nothing typed, the subject of the
    mail being answered is the query (see composer_lane.knowledge for why)."""
    return composer_lane.knowledge(user.get("user_scope_id"), instruction, subject,
                                   caller="mail_composer")


def _composer_related(svc, instruction: str, thread_id: Optional[int]) -> str:
    """Older mail from OTHER threads that matches what the user asked for.

    Uses the FTS5 index the store already maintains (subject/from/to/body) - no
    vector lane, no second copy of anyone's mail. The same rule as the memory
    lookup applies and for the same reason: the query is the USER's instruction,
    never mail text, so a message cannot decide which of the user's older mail is
    pulled into the prompt.

    Everything found here is attacker-controlled correspondence the user did NOT
    open, so it is the least verified input in the prompt: hits run through the
    phishing scorer and flagged ones are dropped entirely (not placeholdered - an
    unopened keyword hit is not worth a line), the current thread is excluded, and
    the caller places the result inside the untrusted fence.
    """
    from vaf.mail import composer

    q = (instruction or "").strip()
    if len(q) < 3:
        return ""
    try:
        rows = svc.search(q, limit=12) or []
    except Exception as e:  # pragma: no cover - search must never break drafting
        logger.info("mail composer: mailbox search unavailable: %s", e)
        return ""
    rows = [r for r in rows if r.get("thread_id") != thread_id]
    rows = [r for r in svc.annotate_visibility(rows) if not r.get("suspicious_for_agent")]
    return composer.format_related(rows)


def _composer_stream(messages, max_tokens: int, temperature: float):
    """The Composer's one tool-less completion, booked on the mail usage lane."""
    return composer_lane.stream_completion(messages, max_tokens, temperature, lane="mail")


@router.post("/composer")
async def composer_draft(body: Dict[str, Any] = Body(...),
                         _user: Dict[str, Any] = Depends(_get_current_user)):
    """Draft a reply from a thread, or rewrite the user's own text.

    Streams plain text as SSE into the compose textarea. It NEVER sends: the
    response is text for the user to read, edit and send themselves.
    """
    from fastapi.responses import StreamingResponse

    from vaf.mail import composer

    cfg = _composer_settings()
    if not cfg["enabled"]:
        raise HTTPException(status_code=403, detail="mail composer is disabled")

    mode = (body.get("mode") or "draft").strip().lower()
    if mode not in ("draft", "rewrite"):
        raise HTTPException(status_code=422, detail="mode must be draft or rewrite")
    draft_text = str(body.get("draft") or "")
    if mode == "rewrite" and not draft_text.strip():
        raise HTTPException(status_code=422, detail="nothing to rewrite")

    svc = _service(_user)          # fail-closed scoping: 403 without a scope
    thread_id = body.get("thread_id")
    anchor_pk = body.get("anchor_pk")

    def _assemble():
        rows = svc.thread_messages(int(thread_id)) if thread_id is not None else []
        if thread_id is not None and not rows:
            return None                        # foreign or unknown id: 404, never leak
        rows = svc.annotate_visibility(rows)
        anchor = next((r for r in rows if r.get("id") == anchor_pk), rows[-1] if rows else None)
        if anchor is not None and anchor.get("suspicious_for_agent"):
            return "flagged"
        bodies: Dict[int, str] = {}
        if mode == "draft":
            # rewrite works on the user's own text, so it deliberately reads no
            # bodies at all: smallest context, smallest injection surface.
            for r in rows:
                if r.get("suspicious_for_agent"):
                    continue
                b = svc.get_body(int(r["id"]))
                if b and b.get("cached"):
                    bodies[int(r["id"])] = b.get("text") or ""
        # The user's own addresses let the assembler label which half of the
        # conversation they wrote, so the draft can match THEIR register rather
        # than the correspondent's. Folder wins over From (see is_own_message);
        # this list is only the fallback for unclassified folders.
        try:
            own = {a.get("email") for a in (svc.store.list_accounts() or []) if a.get("email")}
        except Exception:
            own = set()
        return composer.build_thread_context(
            rows, bodies, anchor_pk=int(anchor_pk) if anchor_pk is not None else -1,
            budget_chars=cfg["budget"], per_msg_chars=cfg["per_msg"],
            max_messages=cfg["max_messages"], own_addresses=own)

    ctx = await asyncio.to_thread(_assemble)
    if ctx is None:
        raise HTTPException(status_code=404, detail="thread not found")
    if ctx == "flagged":
        raise HTTPException(status_code=409, detail="this message is flagged as possible phishing")

    instruction = str(body.get("instruction") or "")
    knowledge = ""
    if cfg["memory"]:
        knowledge = await asyncio.to_thread(
            _composer_knowledge, _user, instruction, ctx.anchor_subject)
    related = ""
    if cfg["mailbox"] and mode == "draft":
        # rewrite works on the user's own text and deliberately reads no mail at all
        related = await asyncio.to_thread(
            _composer_related, svc, instruction,
            int(thread_id) if thread_id is not None else None)
    raw_turns = body.get("turns")
    turns = [t for t in raw_turns if isinstance(t, dict)] if isinstance(raw_turns, list) else []
    messages = composer.build_prompt(
        ctx, mode=mode, instruction=instruction,
        draft=draft_text, tone=str(body.get("tone") or ""),
        language=str(body.get("language") or ""), knowledge=knowledge,
        related=related, turns=turns)
    temperature = 0.2 if mode == "rewrite" else 0.3

    meta = {"included": ctx.included, "total": ctx.total, "truncated": ctx.truncated,
            "hidden_suspicious": ctx.hidden_suspicious, "dropped": ctx.dropped,
            "own_included": ctx.own_included}
    events = composer_lane.sse_events(messages, meta=meta, max_tokens=cfg["max_tokens"],
                                      temperature=temperature, stream=_composer_stream,
                                      log_name="mail composer")
    return StreamingResponse(events, media_type="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
