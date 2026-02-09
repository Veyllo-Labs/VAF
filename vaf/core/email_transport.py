"""
Mail transport layer: connect to mail servers using credentials from credential_store.

- IMAP/SMTP (TLS) for provider "imap".
- Gmail API (REST) for provider "gmail" (OAuth2).
- Microsoft Graph Mail API for provider "microsoft" (OAuth2).
- Agent/tools call fetch_mail / send_mail with account_id only; credentials are never exposed.
"""

import base64
import imaplib
import logging
import smtplib
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

import requests

from vaf.core.config import Config
from vaf.core.credential_store import get_email_credentials
from vaf.core.oauth_pkce import get_valid_access_token

logger = logging.getLogger("vaf.core.email_transport")


def _get_email_config(username: Optional[str] = None) -> Dict[str, Any]:
    """Return email config for the given user. When username is None or local admin, use legacy email_config."""
    local_admin = (Config.get("local_admin_username") or "admin").strip().lower()
    if not username or username.strip().lower() == local_admin:
        raw = Config.get("email_config")
        if isinstance(raw, dict):
            return raw
        return {"accounts": []}
    by_user = Config.get("email_config_by_user") or {}
    ec = by_user.get(username.strip()) if isinstance(by_user, dict) else {}
    if isinstance(ec, dict):
        return ec
    return {"accounts": []}


def get_account(account_id: str, username: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return account metadata for account_id (email or account_id). Optional username for multi-user scope."""
    ec = _get_email_config(username)
    for a in ec.get("accounts") or []:
        if (a.get("account_id") or a.get("email") or "").strip().lower() == (account_id or "").strip().lower():
            return a
    return None


def get_credentials(account_id: str, provider: Optional[str] = None, username: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Load credentials from credential_store. For OAuth uses provider from account metadata.
    Returns dict with either password (IMAP) or access_token/refresh_token (OAuth).
    Optional username for multi-user scope.
    """
    acc = get_account(account_id, username)
    if not acc:
        return None
    prov = provider or acc.get("provider") or "imap"
    if prov == "imap":
        creds = get_email_credentials(account_id, "imap", username)
        if creds and creds.get("type") == "imap":
            return creds
        return None
    creds = get_email_credentials(account_id, prov, username)
    if creds and creds.get("type") == "oauth":
        return creds
    return None


def _imap_connect(account_id: str, use_oauth: bool = False, username: Optional[str] = None) -> Optional[imaplib.IMAP4_SSL]:
    """
    Connect to IMAP with TLS using account credentials. Returns connected IMAP4_SSL or None.
    For OAuth (Gmail XOAUTH2) we would use a different auth mechanism; for Phase 3 we only do password.
    """
    acc = get_account(account_id, username)
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
    creds = get_credentials(account_id, "imap", username)
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


def _smtp_connect(account_id: str, username: Optional[str] = None) -> Optional[smtplib.SMTP]:
    """Connect to SMTP with STARTTLS using account credentials. Returns connected SMTP or None."""
    acc = get_account(account_id, username)
    if not acc:
        return None
    provider = acc.get("provider") or "imap"
    if provider != "imap":
        return None
    host = acc.get("smtp_host") or "smtp.gmail.com"
    port = int(acc.get("smtp_port") or 587)
    creds = get_credentials(account_id, "imap", username)
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


def verify_imap_connection(account_id: str, username: Optional[str] = None) -> bool:
    """Verify IMAP login for an account. Returns True if NOOP succeeds. Optional username for multi-user scope."""
    conn = _imap_connect(account_id, username=username)
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


def _gmail_label_from_folder(folder: str) -> str:
    """Map folder name to Gmail label id."""
    f = (folder or "INBOX").strip().upper()
    if f == "INBOX":
        return "INBOX"
    if f in ("SENT", "SENT ITEMS"):
        return "SENT"
    if f in ("TRASH", "DELETED"):
        return "TRASH"
    if f in ("DRAFT", "DRAFTS"):
        return "DRAFT"
    if f in ("SPAM", "JUNK"):
        return "SPAM"
    return "INBOX"


def _fetch_mail_gmail(
    account_id: str,
    folder: str = "INBOX",
    since: Optional[str] = None,
    max_messages: int = 50,
    username: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch mail via Gmail API. Returns list of dicts with subject, from, date, message_id."""
    token = get_valid_access_token(account_id, "gmail", username)
    if not token:
        logger.warning("No valid Gmail token for account %s", account_id[:8] + "***")
        return []
    label = _gmail_label_from_folder(folder)
    params: Dict[str, Any] = {"maxResults": min(max_messages, 100), "labelIds": label}
    if since:
        # Gmail search: after:YYYY/MM/DD
        q = since.replace("-", "/").strip()
        if q:
            params["q"] = f"after:{q}"
    try:
        r = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if r.status_code != 200:
            logger.warning("Gmail list failed: %s %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        messages = data.get("messages") or []
        result: List[Dict[str, Any]] = []
        for m in messages[:max_messages]:
            mid = m.get("id")
            if not mid:
                continue
            r2 = requests.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}",
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date", "Message-ID"]},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            if r2.status_code != 200:
                continue
            msg = r2.json()
            payload = msg.get("payload") or {}
            headers_list = payload.get("headers") or []
            headers = {h["name"].lower(): h["value"] for h in headers_list if h.get("name")}
            result.append({
                "subject": headers.get("subject", ""),
                "from": headers.get("from", ""),
                "date": headers.get("date", ""),
                "message_id": headers.get("message-id", ""),
                "body_snippet": (msg.get("snippet") or "")[:500],
            })
        return result
    except Exception as e:
        logger.warning("Gmail fetch failed: %s", e)
        return []


def _send_mail_gmail(
    account_id: str,
    to: str,
    subject: str,
    body: str,
    subtype: str = "plain",
    username: Optional[str] = None,
) -> bool:
    """Send mail via Gmail API (users.messages.send with raw RFC 2822)."""
    acc = get_account(account_id, username)
    if not acc:
        return False
    token = get_valid_access_token(account_id, "gmail", username)
    if not token:
        logger.warning("No valid Gmail token for account %s", account_id[:8] + "***")
        return False
    from_addr = acc.get("email") or account_id
    msg = MIMEText(body, subtype, "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    try:
        r = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            json={"raw": raw},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code not in (200, 201):
            logger.warning("Gmail send failed: %s %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        logger.warning("Gmail send failed: %s", e)
        return False


def _graph_folder_id(folder: str) -> str:
    """Map folder name to Microsoft Graph well-known folder id."""
    f = (folder or "INBOX").strip().upper()
    if f == "INBOX":
        return "inbox"
    if f in ("SENT", "SENT ITEMS"):
        return "sentitems"
    if f in ("DRAFT", "DRAFTS"):
        return "drafts"
    if f in ("TRASH", "DELETED"):
        return "deleteditems"
    if f in ("SPAM", "JUNK"):
        return "junkemail"
    return "inbox"


def _fetch_mail_microsoft(
    account_id: str,
    folder: str = "INBOX",
    since: Optional[str] = None,
    max_messages: int = 50,
    username: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch mail via Microsoft Graph (GET /me/mailFolders/.../messages)."""
    token = get_valid_access_token(account_id, "microsoft", username)
    if not token:
        logger.warning("No valid Microsoft token for account %s", account_id[:8] + "***")
        return []
    folder_id = _graph_folder_id(folder)
    url = f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder_id}/messages"
    params: Dict[str, Any] = {"$top": min(max_messages, 100), "$select": "subject,from,receivedDateTime,bodyPreview,internetMessageId"}
    try:
        r = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if r.status_code != 200:
            logger.warning("Graph list messages failed: %s %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        value = data.get("value") or []
        result: List[Dict[str, Any]] = []
        for m in value:
            from_obj = m.get("from") or {}
            from_email = (from_obj.get("emailAddress") or {}).get("address") or ""
            from_name = (from_obj.get("emailAddress") or {}).get("name") or ""
            from_str = from_name + (" <" + from_email + ">") if from_email else from_name or from_email
            result.append({
                "subject": m.get("subject") or "",
                "from": from_str,
                "date": m.get("receivedDateTime") or "",
                "message_id": m.get("internetMessageId") or "",
                "body_snippet": (m.get("bodyPreview") or "")[:500],
            })
        return result
    except Exception as e:
        logger.warning("Graph fetch failed: %s", e)
        return []


def _send_mail_microsoft(
    account_id: str,
    to: str,
    subject: str,
    body: str,
    subtype: str = "plain",
    username: Optional[str] = None,
) -> bool:
    """Send mail via Microsoft Graph (POST /me/sendMail)."""
    acc = get_account(account_id, username)
    if not acc:
        return False
    token = get_valid_access_token(account_id, "microsoft", username)
    if not token:
        logger.warning("No valid Microsoft token for account %s", account_id[:8] + "***")
        return False
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "html" if subtype == "html" else "text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
    }
    try:
        r = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code not in (200, 202, 204):
            logger.warning("Graph sendMail failed: %s %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        logger.warning("Graph send failed: %s", e)
        return False


def fetch_mail(
    account_id: str,
    folder: str = "INBOX",
    since: Optional[str] = None,
    max_messages: int = 50,
    username: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch messages from folder. Returns list of dicts with subject, from, date, body_snippet.
    Dispatches to IMAP (imap), Gmail API (gmail), or Microsoft Graph (microsoft).
    Optional username for multi-user scope.
    """
    acc = get_account(account_id, username)
    if not acc:
        return []
    provider = (acc.get("provider") or "imap").lower()
    if provider == "gmail":
        return _fetch_mail_gmail(account_id, folder, since, max_messages, username)
    if provider == "microsoft":
        return _fetch_mail_microsoft(account_id, folder, since, max_messages, username)
    conn = _imap_connect(account_id, username=username)
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
                    "body_snippet": "",
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
    username: Optional[str] = None,
) -> bool:
    """Send one email. Returns True on success. Dispatches to Gmail API, Graph, or SMTP by provider. Optional username for multi-user scope."""
    acc = get_account(account_id, username)
    if not acc:
        return False
    provider = (acc.get("provider") or "imap").lower()
    if provider == "gmail":
        return _send_mail_gmail(account_id, to, subject, body, subtype, username)
    if provider == "microsoft":
        return _send_mail_microsoft(account_id, to, subject, body, subtype, username)
    conn = _smtp_connect(account_id, username)
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
