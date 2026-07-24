# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Native v2 mail delivery core (mail v2-only port, P2).

One place owns outbound submission for every account. Dispatch by
(provider, imap_ready):
  - provider 'imap'                    -> SMTP with password AUTH
  - gmail/microsoft AND imap_ready     -> SMTP with SASL XOAUTH2
  - gmail/microsoft AND NOT imap_ready -> documented delegate to the legacy
    vaf.core.email_transport REST/Graph path (the shrinking strangler tail; every
    fall onto it emits a distinct, countable log line so the P6 go/no-go can
    assert it is never hit; deleted in P7).

send() is SYNCHRONOUS and must be called from a worker thread (agent tool run or
the OpExecutor drain via asyncio.to_thread), never from inside a running event
loop. We use the standard-library smtplib (already the proven SMTP path in this
codebase) rather than aiosmtplib: every caller is synchronous, smtplib gives
precise control over the SMTP conversation for honest hand-off classification,
and it avoids event-loop bridging.

Hand-off classification (never double-send): handed_off flips True the instant the
DATA command is issued. A failure BEFORE hand-off is transient (connect/4xx, safe
to retry) or permanent (5xx auth/recipient reject). A failure AT OR AFTER hand-off
is 'ambiguous' - the server may already have accepted the mail - so the outbox
parks it and never re-sends.

TLS is always verified (ssl.create_default_context, never disableable) and the
SMTP host passes the shared SSRF guard, exactly like the IMAP client.
"""
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email import message_from_bytes
from typing import Any, Dict, List, Optional

from vaf.core.config import Config
from vaf.core.credential_store import get_email_credentials
from vaf.core.oauth_pkce import get_valid_access_token
from vaf.mail.addressing import normalize_recipients
from vaf.network.binding import assert_safe_remote_host

logger = logging.getLogger("vaf.mail.sender")

SMTP_TIMEOUT_SEC = 60

# SMTP submission defaults per provider (host, STARTTLS port). OAuth accounts
# usually store only imap_host, so the native sender needs these fallbacks.
_PROVIDER_SMTP_DEFAULTS: Dict[str, tuple] = {
    "gmail": ("smtp.gmail.com", 587),
    "microsoft": ("smtp.office365.com", 587),
}


@dataclass
class OutgoingMessage:
    """One message to deliver. `raw_bytes` is the exact RFC822 Sent copy (WITH a
    Bcc header) used by the native SMTP path so the delivered Message-ID is
    byte-identical to the stored copy. The structured fields feed ONLY the legacy
    delegate (non-imap_ready OAuth accounts), which rebuilds the MIME itself."""
    account: Dict[str, Any]
    raw_bytes: bytes
    to: Any = ""
    cc: Any = ""
    bcc: Any = ""
    username: Optional[str] = None
    user_scope_id: Optional[str] = None
    # delegate-only structured fields (email_transport.send_mail rebuilds the MIME)
    subject: str = ""
    body: str = ""
    subtype: str = "plain"
    message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: Optional[str] = None
    attachments: Optional[List[Dict[str, str]]] = None


@dataclass
class SendResult:
    """Outcome of one delivery attempt. classification drives the outbox: 'ok' ->
    done; 'transient' -> re-pend (retry, still attempt-capped); 'permanent' ->
    park; 'ambiguous' -> park and NEVER re-send."""
    ok: bool
    classification: str  # 'ok' | 'transient' | 'permanent' | 'ambiguous'
    handed_off: bool = False
    used_delegate: bool = False
    error: Optional[str] = None


def _from_addr(msg: OutgoingMessage) -> str:
    return (msg.account.get("email") or msg.account.get("account_id") or "").strip()


def _account_id(msg: OutgoingMessage) -> str:
    return (msg.account.get("account_id") or msg.account.get("email") or "").strip()


def _envelope(msg: OutgoingMessage) -> List[str]:
    return (normalize_recipients(msg.to)
            + normalize_recipients(msg.cc)
            + normalize_recipients(msg.bcc))


def _wire_bytes(raw: bytes) -> bytes:
    """The delivered message never carries a Bcc header (Bcc recipients ride the
    SMTP envelope only). Strip it once here."""
    m = message_from_bytes(raw)
    del m["Bcc"]
    return m.as_bytes()


def _smtp_target(acc: Dict[str, Any]) -> tuple:
    provider = (acc.get("provider") or "imap").lower()
    dh, dp = _PROVIDER_SMTP_DEFAULTS.get(provider, ("", 587))
    host = (acc.get("smtp_host") or "").strip() or dh
    port = int(acc.get("smtp_port") or dp)
    return host, port


def _xoauth2_string(user: str, token: str) -> str:
    """RAW SASL XOAUTH2 initial-response string. smtplib.SMTP.auth() base64-encodes
    the authobject's return value ITSELF, so returning base64 here would double-encode
    it and the server rejects it (Gmail: '501 5.5.2 Cannot Decode response')."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01"


def send(msg: OutgoingMessage) -> SendResult:
    """Deliver one message. Synchronous; call from a worker thread only."""
    provider = (msg.account.get("provider") or "imap").lower()
    imap_ready = bool(msg.account.get("imap_ready"))
    if provider in ("gmail", "microsoft") and not imap_ready:
        return _delegate(msg)
    return _smtp_send(msg, provider)


def _delegate(msg: OutgoingMessage) -> SendResult:
    """Legacy REST/Graph fall-back for an OAuth account that has not completed the
    IMAP re-consent yet. Emits a distinct, countable signal so the P6 go/no-go can
    assert the tail is never exercised. Deleted in P7."""
    logger.warning(
        "mail.sender: SMTP_XOAUTH2_UNAVAILABLE delegating to legacy transport "
        "(provider=%s account=%s) - account not imap_ready",
        msg.account.get("provider"), _account_id(msg)[:3] + "***")
    try:
        from vaf.core import email_transport
        ok = email_transport.send_mail(
            _account_id(msg), msg.to, msg.subject, msg.body,
            subtype=msg.subtype, username=msg.username, user_scope_id=msg.user_scope_id,
            attachments=msg.attachments, cc=msg.cc, bcc=msg.bcc,
            in_reply_to=msg.in_reply_to, references=msg.references,
            message_id=msg.message_id)
        return SendResult(bool(ok), "ok" if ok else "transient",
                          used_delegate=True, error=None if ok else "delegate send returned False")
    except Exception as e:  # transport raises MailConnectError on connect/auth
        return SendResult(False, "transient", used_delegate=True, error=str(e))


def _smtp_send(msg: OutgoingMessage, provider: str) -> SendResult:
    acc = msg.account
    host, port = _smtp_target(acc)
    if not host:
        return SendResult(False, "permanent", error="no SMTP host configured for account")
    from_addr = _from_addr(msg)
    envelope = _envelope(msg)
    if not envelope:
        return SendResult(False, "permanent", error="no valid recipients")
    try:
        wire = _wire_bytes(msg.raw_bytes)
    except Exception as e:
        return SendResult(False, "permanent", error=f"could not parse message bytes: {e}")

    handed_off = False
    conn: Optional[smtplib.SMTP] = None
    try:
        assert_safe_remote_host(host, allow_private=bool(Config.get("email_allow_private_hosts", False)))
        ctx = ssl.create_default_context()
        if port == 465:
            conn = smtplib.SMTP_SSL(host, port=port, context=ctx, timeout=SMTP_TIMEOUT_SEC)
            conn.ehlo()
        else:
            conn = smtplib.SMTP(host, port=port, timeout=SMTP_TIMEOUT_SEC)
            conn.ehlo()
            conn.starttls(context=ctx)
            conn.ehlo()

        if provider == "imap":
            creds = get_email_credentials(_account_id(msg), "imap", msg.username, user_scope_id=msg.user_scope_id)
            if not creds or not creds.get("password"):
                return SendResult(False, "permanent", error="no SMTP password stored for account")
            conn.login(from_addr, creds["password"])
        else:  # gmail/microsoft + imap_ready -> XOAUTH2 (mirror imap_client token lanes)
            token_provider = "microsoft_imap" if provider == "microsoft" else provider
            token = get_valid_access_token(_account_id(msg), token_provider, msg.username, user_scope_id=msg.user_scope_id)
            if not token:
                return SendResult(False, "permanent", error="no valid OAuth token (IMAP re-consent required?)")
            ir = _xoauth2_string(from_addr, token)
            conn.auth("XOAUTH2", lambda challenge=None: ir if challenge is None else "", initial_response_ok=True)

        conn.mail(from_addr)
        for rcpt in envelope:
            conn.rcpt(rcpt)
        handed_off = True
        conn.data(wire)
        return SendResult(True, "ok", handed_off=True)

    except smtplib.SMTPAuthenticationError as e:
        return SendResult(False, "permanent", handed_off=handed_off, error=f"SMTP auth failed: {e}")
    except smtplib.SMTPRecipientsRefused as e:
        return SendResult(False, "permanent", handed_off=handed_off, error=str(e))
    except smtplib.SMTPResponseException as e:
        cls = "ambiguous" if handed_off else ("transient" if 400 <= int(e.smtp_code) < 500 else "permanent")
        return SendResult(False, cls, handed_off=handed_off, error=str(e))
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError, OSError) as e:
        return SendResult(False, "ambiguous" if handed_off else "transient", handed_off=handed_off, error=str(e))
    except ValueError as e:  # SSRF guard (assert_safe_remote_host)
        return SendResult(False, "permanent", handed_off=handed_off, error=str(e))
    except Exception as e:
        return SendResult(False, "ambiguous" if handed_off else "permanent", handed_off=handed_off, error=str(e))
    finally:
        if conn is not None:
            try:
                conn.quit()
            except Exception:
                pass
