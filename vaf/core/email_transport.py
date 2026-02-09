"""
Mail transport layer: connect to mail servers using credentials from credential_store.

- IMAP/SMTP (TLS) for provider "imap" and for OAuth providers that support IMAP (e.g. Gmail).
- Agent/tools call fetch_mail / send_mail with account_id only; credentials are never exposed.
"""

import imaplib
import logging
import smtplib
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from vaf.core.config import Config
from vaf.core.credential_store import get_email_credentials

logger = logging.getLogger("vaf.core.email_transport")


def _get_email_config() -> Dict[str, Any]:
    raw = Config.get("email_config")
    if isinstance(raw, dict):
        return raw
    return {"accounts": []}


def get_account(account_id: str) -> Optional[Dict[str, Any]]:
    """Return account metadata for account_id (email or account_id)."""
    ec = _get_email_config()
    for a in ec.get("accounts") or []:
        if (a.get("account_id") or a.get("email") or "").strip().lower() == (account_id or "").strip().lower():
            return a
    return None


def get_credentials(account_id: str, provider: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Load credentials from credential_store. For OAuth uses provider from account metadata.
    Returns dict with either password (IMAP) or access_token/refresh_token (OAuth).
    """
    acc = get_account(account_id)
    if not acc:
        return None
    prov = provider or acc.get("provider") or "imap"
    if prov == "imap":
        creds = get_email_credentials(account_id, "imap")
        if creds and creds.get("type") == "imap":
            return creds
        return None
    creds = get_email_credentials(account_id, prov)
    if creds and creds.get("type") == "oauth":
        return creds
    return None


def _imap_connect(account_id: str, use_oauth: bool = False) -> Optional[imaplib.IMAP4_SSL]:
    """
    Connect to IMAP with TLS using account credentials. Returns connected IMAP4_SSL or None.
    For OAuth (Gmail XOAUTH2) we would use a different auth mechanism; for Phase 3 we only do password.
    """
    acc = get_account(account_id)
    if not acc:
        logger.warning("Account not found: %s", account_id[:8] + "***")
        return None
    provider = acc.get("provider") or "imap"
    if provider != "imap":
        # OAuth IMAP (e.g. Gmail) would use XOAUTH2 here; for now we only support IMAP password
        logger.debug("OAuth IMAP not yet implemented for %s", provider)
        return None
    host = acc.get("imap_host") or "imap.gmail.com"
    port = int(acc.get("imap_port") or 993)
    creds = get_credentials(account_id, "imap")
    if not creds or "password" not in creds:
        logger.warning("No IMAP credentials for account %s", account_id[:8] + "***")
        return None
    try:
        conn = imaplib.IMAP4_SSL(host, port=port)
        conn.login(acc.get("email") or account_id, creds["password"])
        return conn
    except Exception as e:
        logger.warning("IMAP login failed for %s: %s", account_id[:8] + "***", e)
        return None


def _smtp_connect(account_id: str) -> Optional[smtplib.SMTP]:
    """Connect to SMTP with STARTTLS using account credentials. Returns connected SMTP or None."""
    acc = get_account(account_id)
    if not acc:
        return None
    provider = acc.get("provider") or "imap"
    if provider != "imap":
        return None
    host = acc.get("smtp_host") or "smtp.gmail.com"
    port = int(acc.get("smtp_port") or 587)
    creds = get_credentials(account_id, "imap")
    if not creds or "password" not in creds:
        return None
    try:
        conn = smtplib.SMTP(host, port=port)
        conn.starttls()
        conn.login(acc.get("email") or account_id, creds["password"])
        return conn
    except Exception as e:
        logger.warning("SMTP login failed for %s: %s", account_id[:8] + "***", e)
        return None


def verify_imap_connection(account_id: str) -> bool:
    """Verify IMAP login for an account. Returns True if NOOP succeeds."""
    conn = _imap_connect(account_id)
    if not conn:
        return False
    try:
        conn.noop()
        return True
    except Exception:
        return False
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def fetch_mail(
    account_id: str,
    folder: str = "INBOX",
    since: Optional[str] = None,
    max_messages: int = 50,
) -> List[Dict[str, Any]]:
    """
    Fetch messages from folder. Returns list of dicts with subject, from, date, body_snippet.
    Uses IMAP only (provider=imap). OAuth providers would need Gmail API / Graph later.
    """
    conn = _imap_connect(account_id)
    if not conn:
        return []
    result: List[Dict[str, Any]] = []
    try:
        conn.select(folder, readonly=True)
        _, msg_ids = conn.search(None, "ALL")
        ids = msg_ids[0].split()
        if since:
            # IMAP date format: DD-Mon-YYYY
            _, ids = conn.search(None, f'(SINCE "{since}")')
            ids = ids[0].split()
        for uid in reversed(ids[-max_messages:]):
            try:
                _, data = conn.fetch(uid, "(BODY.PEEK[HEADER])")
                if not data or not data[0]:
                    continue
                from email import message_from_bytes
                payload = data[0]
                if isinstance(payload, tuple) and len(payload) > 1:
                    payload = payload[1]
                else:
                    continue
                msg = message_from_bytes(payload)
                result.append({
                    "subject": msg.get("Subject", ""),
                    "from": msg.get("From", ""),
                    "date": msg.get("Date", ""),
                    "message_id": msg.get("Message-ID", ""),
                })
            except Exception:
                continue
        return result
    except Exception as e:
        logger.warning("Fetch mail failed: %s", e)
        return []
    finally:
        try:
            conn.close()
            conn.logout()
        except Exception:
            pass


def send_mail(
    account_id: str,
    to: str,
    subject: str,
    body: str,
    subtype: str = "plain",
) -> bool:
    """Send one email. Returns True on success. Uses SMTP (provider=imap only for now)."""
    acc = get_account(account_id)
    if not acc:
        return False
    conn = _smtp_connect(account_id)
    if not conn:
        return False
    try:
        msg = MIMEText(body, subtype, "utf-8")
        msg["Subject"] = subject
        msg["From"] = acc.get("email") or account_id
        msg["To"] = to
        conn.sendmail(acc.get("email") or account_id, [to], msg.as_string())
        return True
    except Exception as e:
        logger.warning("Send mail failed: %s", e)
        return False
    finally:
        try:
            conn.quit()
        except Exception:
            pass
