# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Native v2 mail delivery core (mail v2-only port, P2).

One place owns outbound submission for every account. Dispatch by
(provider, imap_ready):
  - provider 'imap'                    -> SMTP with password AUTH
  - gmail/microsoft AND imap_ready     -> SMTP with SASL XOAUTH2
  - gmail/microsoft AND NOT imap_ready -> PERMANENT refusal naming the fix. The
    legacy REST/Graph delegate this used to fall back on was deleted with the rest
    of the legacy stack (P7.3); such an account simply has no delivery lane until
    it is reconnected.

send() is SYNCHRONOUS and must be called from a worker thread (agent tool run or
the OpExecutor drain via asyncio.to_thread), never from inside a running event
loop. We use the standard-library smtplib (already the proven SMTP path in this
codebase) rather than an async SMTP client: every caller is synchronous, smtplib
gives precise control over the SMTP conversation for honest hand-off
classification, and it avoids event-loop bridging. aiosmtplib was declared as a
dependency for a while and never imported; it has been dropped.

Hand-off classification (never double-send): handed_off flips True the instant the
DATA command is issued. A failure BEFORE hand-off is transient (connect/4xx, safe
to retry) or permanent (5xx auth/recipient reject). A failure AT OR AFTER hand-off
is 'ambiguous' - the server may already have accepted the mail - so the outbox
parks it and never re-sends.

TLS is always verified (ssl.create_default_context, never disableable) and the
SMTP host passes the shared SSRF guard, exactly like the IMAP client.

Internationalised addresses: the envelope carries `addressing.envelope_address` of every
address (the A-label of a domain outside ASCII). An address with a local part outside ASCII
needs the server's SMTPUTF8 (RFC 6531): the extension is read after the last EHLO, and a
server without it is refused PERMANENTLY before any credential is spent and before MAIL FROM.
Every result after the EHLO carries what the server said (`SendResult.smtputf8`), which the
outbox records per account so the next such mail is refused at queue time. The login (AUTH
PLAIN/LOGIN, XOAUTH2) uses the account's envelope form too, because smtplib writes AUTH in
ASCII. NAMED BOUNDARY: an account whose OWN address has a local part outside ASCII cannot log
in through smtplib at all (the AUTH string is ASCII-encoded); it fails as a permanent error.
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
from vaf.mail.addressing import (UndeliverableAddress, envelope_address, needs_smtputf8,
                                  normalize_recipients, refusal_text)
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
    byte-identical to the stored copy - the wire bytes are never rebuilt."""
    account: Dict[str, Any]
    raw_bytes: bytes
    to: Any = ""
    cc: Any = ""
    bcc: Any = ""
    username: Optional[str] = None
    user_scope_id: Optional[str] = None
    # Envelope/header metadata the callers already carry. Kept on the dataclass
    # (all four construction sites populate it) even though the native path sends
    # raw_bytes verbatim; useful for logging and for any future non-SMTP lane.
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
    error: Optional[str] = None
    # Whether the server offered SMTPUTF8 after its last EHLO; None when no EHLO answered.
    smtputf8: Optional[bool] = None
    # The `UndeliverableAddress` code when an address was refused, so a log can name the
    # refusal without the address the error text carries.
    refusal: Optional[str] = None


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
    if provider in ("gmail", "microsoft") and not bool(msg.account.get("imap_ready")):
        # No delivery lane: the REST/Graph delegate this used to fall back on was
        # removed with the legacy stack. Refuse PERMANENTLY and say what fixes it -
        # falling through to _smtp_send would open a real SMTP connection and try
        # XOAUTH2 with a token that has no mail scope, turning a clear "reconnect
        # this account" into an opaque auth error.
        logger.warning(
            "mail.sender: NO_DELIVERY_LANE account=%s provider=%s is not connected "
            "for the mail engine", _account_id(msg)[:3] + "***", provider)
        return SendResult(
            False, "permanent",
            error="account cannot send: it has not been connected for the mail engine yet - "
                  "reconnect it in the mail account panel")
    return _smtp_send(msg, provider)


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
        # Only an op written by hand reaches this with an address the funnel would refuse.
        mail_from = envelope_address(from_addr)
        rcpts = [envelope_address(a) for a in envelope]
    except UndeliverableAddress as e:
        return SendResult(False, "permanent", refusal=e.code,
                          error=refusal_text(e.code, e.address, "Nothing was sent."))
    try:
        wire = _wire_bytes(msg.raw_bytes)
    except Exception as e:
        return SendResult(False, "permanent", error=f"could not parse message bytes: {e}")
    international = [a for a in [from_addr] + envelope if needs_smtputf8(a)]
    seen: Dict[str, Optional[bool]] = {"smtputf8": None}
    res = _smtp_conversation(msg, provider, host, port, mail_from, rcpts,
                             international, wire, seen)
    res.smtputf8 = seen["smtputf8"]
    return res


def _smtp_conversation(msg: OutgoingMessage, provider: str, host: str, port: int,
                       mail_from: str, rcpts: List[str],
                       international: List[str], wire: bytes,
                       seen: Dict[str, Optional[bool]]) -> SendResult:
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
        seen["smtputf8"] = bool(conn.has_extn("smtputf8"))
        if international and not seen["smtputf8"]:
            return SendResult(False, "permanent", refusal="smtputf8",
                              error=refusal_text("smtputf8", international[0], "Nothing was sent."))

        if provider == "imap":
            creds = get_email_credentials(_account_id(msg), "imap", msg.username, user_scope_id=msg.user_scope_id)
            if not creds or not creds.get("password"):
                return SendResult(False, "permanent", error="no SMTP password stored for account")
            # The envelope form: smtplib writes AUTH in ASCII, so an account on a domain
            # outside ASCII logs in with its A-label, the same DNS name. A plain address is
            # unchanged.
            conn.login(mail_from, creds["password"])
        else:  # gmail/microsoft + imap_ready -> XOAUTH2 (mirror imap_client token lanes)
            token_provider = "microsoft_imap" if provider == "microsoft" else provider
            token = get_valid_access_token(_account_id(msg), token_provider, msg.username, user_scope_id=msg.user_scope_id)
            if not token:
                return SendResult(False, "permanent", error="no valid OAuth token (IMAP re-consent required?)")
            ir = _xoauth2_string(mail_from, token)
            conn.auth("XOAUTH2", lambda challenge=None: ir if challenge is None else "", initial_response_ok=True)

        if international:
            # The pair the standard library's own send_message passes for such a mail; with
            # SMTPUTF8 smtplib writes the commands in UTF-8 instead of ASCII.
            conn.mail(mail_from, ["SMTPUTF8", "BODY=8BITMIME"])
        else:
            conn.mail(mail_from)
        for rcpt in rcpts:
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
