# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Mail transport layer: connect to mail servers using credentials from credential_store.

- IMAP/SMTP (TLS) for provider "imap".
- Gmail API (REST) for provider "gmail" (OAuth2).
- Microsoft Graph Mail API for provider "microsoft" (OAuth2).
- Agent/tools call fetch_mail / send_mail with account_id only; credentials are never exposed.
"""

import base64
import html
import imaplib
import logging
import quopri
import re
import smtplib
from email import message_from_bytes, message_from_string
from email.encoders import encode_base64
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from vaf.core.config import Config
from vaf.core.credential_store import get_email_credentials
from vaf.core.oauth_pkce import get_valid_access_token
from vaf.core.log_helper import append_domain_log_always

logger = logging.getLogger("vaf.core.email_transport")


def _decode_quoted_printable(raw: bytes) -> str:
    """Decode quoted-printable bytes to UTF-8 string. If not QP, decode as UTF-8. Uses lenient manual fallback if quopri fails."""
    if not raw:
        return ""
    # Only try QP if it looks like quoted-printable (=XX hex sequences or soft line breaks)
    if not re.search(rb"=[0-9A-Fa-f]{2}|=\r?\n", raw):
        return raw.decode("utf-8", errors="replace")
    try:
        decoded = quopri.decodestring(raw, header=False)
        return decoded.decode("utf-8", errors="replace")
    except Exception:
        pass
    # Lenient manual QP decode: =XX -> byte, =\n or =\r\n -> remove
    out: List[int] = []
    i = 0
    while i < len(raw):
        if raw[i : i + 1] == b"=":
            if i + 1 < len(raw) and raw[i + 1 : i + 2] in (b"\r", b"\n"):
                i += 1
                while i < len(raw) and raw[i : i + 1] in (b"\r", b"\n"):
                    i += 1
                continue
            if i + 2 <= len(raw):
                try:
                    hex_pair = raw[i + 1 : i + 3].decode("ascii")
                    if len(hex_pair) == 2 and all(c in "0123456789ABCDEFabcdef" for c in hex_pair):
                        out.append(int(hex_pair, 16))
                        i += 3
                        continue
                except Exception:
                    pass
            out.append(ord("="))
            i += 1
            continue
        out.append(raw[i])
        i += 1
    try:
        return bytes(out).decode("utf-8", errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace")


def _html_to_plain(html_str: str) -> str:
    """Strip HTML to plain text only. No script/style, no tags, no trackers. Decode entities."""
    if not html_str or not isinstance(html_str, str):
        return ""
    # If content still has quoted-printable sequences (e.g. =3D), decode first
    if re.search(r"=[0-9A-Fa-f]{2}", html_str):
        try:
            html_str = _decode_quoted_printable(html_str.encode("latin-1", errors="replace"))
        except Exception:
            pass
    s = re.sub(r"<script[^>]*>.*?</script>", "", html_str, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<style[^>]*>.*?</style>", "", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<noscript[^>]*>.*?</noscript>", "", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<img[^>]*>", " ", s, flags=re.IGNORECASE)  # trackers/pixels
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return html.unescape(s).strip()


def _extract_plain_from_raw_mime(body: str) -> Optional[str]:
    """
    If body is raw MIME (boundaries + Content-Type/Content-Transfer-Encoding headers), parse it
    and return the best part as plain text (text/plain preferred, else text/html stripped).
    Returns None if body doesn't look like raw MIME.
    """
    if not body or not isinstance(body, str) or len(body) < 50:
        return None
    body_lower = body.lower()
    if "content-transfer-encoding:" not in body_lower or "content-type:" not in body_lower:
        return None
    # Find MIME boundary: first line that looks like --something or -something (not closing --boundary--)
    boundary_line = None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") and len(stripped) > 2:
            boundary_line = stripped[2:].rstrip("-").strip()
            break
        if stripped.startswith("-") and len(stripped) > 1 and "content-type" not in stripped.lower():
            boundary_line = stripped.lstrip("-").rstrip("-").strip()
            break
    if not boundary_line:
        return None
    try:
        # Normalize: delimiter lines must be --boundary (some senders use single -)
        body = body.strip()
        if body.startswith("-") and not body.startswith("--"):
            body = "-" + body
        # Replace "-boundary" line starts with single - by "--boundary"
        body = body.replace("\n-" + boundary_line, "\n--" + boundary_line)
        body = body.replace("\r\n-" + boundary_line, "\r\n--" + boundary_line)
        # Build minimal MIME message so the parser can split parts
        safe_boundary = boundary_line.replace("\\", "\\\\").replace('"', '\\"')
        header = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/alternative; boundary=\"%s\"\r\n"
            "\r\n"
        ) % safe_boundary
        if not body.lstrip().startswith("--"):
            body = "--" + boundary_line + "\r\n" + body
        msg = message_from_string(header + body)
    except Exception:
        return None
    if not msg.is_multipart():
        return None
    plain: Optional[str] = None
    html_part: Optional[str] = None
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode("utf-8", errors="replace")
            if "text/plain" in ctype:
                plain = (plain or "") + text
            elif "text/html" in ctype:
                html_part = (html_part or "") + text
        except Exception:
            continue
    if plain and plain.strip():
        return re.sub(r"\s+", " ", plain).strip()
    if html_part and html_part.strip():
        return _html_to_plain(html_part)
    return None


def _ensure_plain_text(body: Optional[str]) -> Optional[str]:
    """
    Guarantee body is plain text: decode QP if present, strip HTML, return readable text only.
    If body is raw MIME (multipart with boundaries), parse and extract text/plain or text/html.
    Call this on any body before returning to the UI so fancy HTML mails never show as code.
    """
    if not body or not isinstance(body, str):
        return body
    s = body
    # 0) If body looks like raw MIME (boundaries + part headers), parse and use extracted part
    extracted = _extract_plain_from_raw_mime(s)
    if extracted:
        extracted = extracted.replace("\uFFFD", " ")
        return re.sub(r"\s+", " ", extracted).strip() or s.strip()
    # 1) Decode quoted-printable if still in string (e.g. =3D, =C2=A0)
    if re.search(r"=[0-9A-Fa-f]{2}", s):
        try:
            s = _decode_quoted_printable(s.encode("latin-1", errors="replace"))
        except Exception:
            pass
    # 2) If it looks like HTML, extract text only
    if "<" in s and ">" in s:
        s = _html_to_plain(s)
    # 3) Remove Unicode replacement character (U+FFFD, displays as) from decoding errors
    s = s.replace("\uFFFD", " ")
    # 4) Collapse whitespace and trim
    s = re.sub(r"\s+", " ", s).strip()
    return s if s else body.strip()


def _get_sender_rules(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Return sender→category rules from config. Each rule: {"pattern": "twitch.tv", "category": "social"}. First match wins."""
    ec = _get_email_config(username, user_scope_id=user_scope_id)
    rules = ec.get("sender_category_rules")
    if not isinstance(rules, list):
        return []
    out: List[Dict[str, str]] = []
    for r in rules:
        if isinstance(r, dict) and r.get("pattern") and r.get("category"):
            out.append({"pattern": str(r["pattern"]).strip(), "category": str(r["category"]).strip().lower().replace(" ", "_")[:64] or "primary"})
    return out


def apply_sender_rules_to_category(
    from_str: str,
    current_category: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> str:
    """
    Apply sender rules: if any rule's pattern is contained in from_str (case-insensitive), return that category.
    Used on sync (new mails) and on backfill (existing mails). Returns current_category if no rule matches.
    """
    rules = _get_sender_rules(username, user_scope_id=user_scope_id)
    from_lower = (from_str or "").lower()
    for r in rules:
        pattern = (r.get("pattern") or "").lower()
        if pattern and pattern in from_lower:
            return r.get("category") or current_category
    return current_category


def _get_email_config(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return email config for the given user with strict scope isolation."""
    from vaf.core.config import get_local_admin_scope_id, get_local_admin_username
    local_admin_scope = get_local_admin_scope_id()
    if user_scope_id:
        by_scope = Config.get("email_config_by_scope") or {}
        if isinstance(by_scope, dict):
            ec = by_scope.get(str(user_scope_id).strip())
            if isinstance(ec, dict) and ec.get("accounts") is not None:
                return ec

        if str(user_scope_id).strip() == str(local_admin_scope).strip():
            raw = Config.get("email_config")
            if isinstance(raw, dict):
                return raw
            return {"accounts": []}

    local_admin = get_local_admin_username().lower()
    if not username or username.strip().lower() == local_admin:
        raw = Config.get("email_config")
        if isinstance(raw, dict):
            return raw
        return {"accounts": []}
    by_user = Config.get("email_config_by_user") or {}
    ec = by_user.get(username.strip()) if isinstance(by_user, dict) else {}
    if isinstance(ec, dict) and ec.get("accounts") is not None:
        return ec
    return {"accounts": []}


def _email_config_candidates(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[tuple]:
    """Return only the current user's config candidate (no cross-scope fallback)."""
    ec = _get_email_config(username, user_scope_id=user_scope_id)
    if isinstance(ec, dict) and (ec.get("accounts") or []):
        return [(ec, user_scope_id)]
    return []


def get_account(
    account_id: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return account metadata for account_id (email or account_id). Optional username/user_scope_id for multi-user scope. Uses fallback config (legacy + single-scope) so account is found when scope mismatches."""
    want = (account_id or "").strip().lower()
    for ec, _ in _email_config_candidates(username, user_scope_id):
        for a in ec.get("accounts") or []:
            if (a.get("account_id") or a.get("email") or "").strip().lower() == want:
                return a
    return None


def _get_credentials_with_fallback(
    account_id: str,
    provider: str,
    username: Optional[str],
    user_scope_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Try credential lookup only in the current user scope."""
    return get_email_credentials(account_id, provider, username, user_scope_id=user_scope_id)


def get_credentials(
    account_id: str,
    provider: Optional[str] = None,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Load credentials from credential_store. For OAuth uses provider from account metadata.
    Returns dict with either password (IMAP) or access_token/refresh_token (OAuth).
    Optional username/user_scope_id for multi-user scope. Tries fallback scopes when primary lookup fails (avoids UUID/scope mismatch).
    """
    acc = get_account(account_id, username, user_scope_id=user_scope_id)
    if not acc:
        return None
    prov = provider or acc.get("provider") or "imap"
    if prov == "imap":
        creds = _get_credentials_with_fallback(account_id, "imap", username, user_scope_id)
        if creds and creds.get("type") == "imap":
            return creds
        return None
    creds = _get_credentials_with_fallback(account_id, prov, username, user_scope_id)
    if creds and creds.get("type") == "oauth":
        return creds
    return None


def _imap_connect(
    account_id: str,
    use_oauth: bool = False,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[imaplib.IMAP4_SSL]:
    """
    Connect to IMAP with TLS using account credentials. Returns connected IMAP4_SSL or None.
    For OAuth (Gmail XOAUTH2) we would use a different auth mechanism; for Phase 3 we only do password.
    """
    acc = get_account(account_id, username, user_scope_id=user_scope_id)
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
    creds = get_credentials(account_id, "imap", username, user_scope_id=user_scope_id)
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


def _smtp_connect(
    account_id: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[smtplib.SMTP]:
    """Connect to SMTP with STARTTLS using account credentials. Returns connected SMTP or None."""
    acc = get_account(account_id, username, user_scope_id=user_scope_id)
    if not acc:
        return None
    provider = acc.get("provider") or "imap"
    if provider != "imap":
        return None
    host = acc.get("smtp_host") or "smtp.gmail.com"
    port = int(acc.get("smtp_port") or 587)
    creds = get_credentials(account_id, "imap", username, user_scope_id=user_scope_id)
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


def verify_imap_connection(
    account_id: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> bool:
    """Verify IMAP login for an account. Returns True if NOOP succeeds. Optional username/user_scope_id for multi-user scope."""
    conn = _imap_connect(account_id, username=username, user_scope_id=user_scope_id)
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
    user_scope_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch mail via Gmail API. Returns list of dicts with subject, from, date, message_id."""
    token = get_valid_access_token(account_id, "gmail", username, user_scope_id=user_scope_id)
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
            label_ids = msg.get("labelIds") if isinstance(msg.get("labelIds"), list) else (msg.get("labels") or [])
            if not isinstance(label_ids, list):
                label_ids = []
            if label_ids and isinstance(label_ids[0], dict):
                label_ids = [lb.get("id") for lb in label_ids if lb.get("id")]
            if "SPAM" in label_ids:
                continue
            # Gmail auto-categories: Social and Promotions (advertisements) from provider
            if "CATEGORY_PROMOTIONS" in label_ids:
                category = "promotions"
            elif "CATEGORY_SOCIAL" in label_ids:
                category = "social"
            else:
                category = "primary"
            payload = msg.get("payload") or {}
            headers_list = payload.get("headers") or []
            headers = {h["name"].lower(): h["value"] for h in headers_list if h.get("name")}
            from_str = headers.get("from", "")
            category = apply_sender_rules_to_category(from_str, category, username, user_scope_id=user_scope_id)
            result.append({
                "subject": headers.get("subject", ""),
                "from": from_str,
                "date": headers.get("date", ""),
                "message_id": headers.get("message-id", ""),
                "body_snippet": (msg.get("snippet") or "")[:500],
                "category": category,
                "provider_message_id": mid,
            })
        return result
    except Exception as e:
        logger.warning("Gmail fetch failed: %s", e)
        return []


def _get_body_gmail(
    account_id: str, provider_message_id: str, username: Optional[str] = None, user_scope_id: Optional[str] = None
) -> Optional[str]:
    """Fetch full message body from Gmail API as plain text. Prefer text/plain; if only HTML, strip to plain."""
    token = get_valid_access_token(account_id, "gmail", username, user_scope_id=user_scope_id)
    if not token:
        return None
    try:
        r = requests.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{provider_message_id}",
            params={"format": "full"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        msg = r.json()
        payload = msg.get("payload") or {}
        plain: Optional[str] = None
        html_body: Optional[str] = None

        def collect_parts(part: Dict[str, Any]) -> None:
            nonlocal plain, html_body
            mime = (part.get("mimeType") or "").lower()
            body = part.get("body") or {}
            data = body.get("data")
            if data:
                try:
                    # Gmail base64 may contain newlines; strip for decode
                    b64 = (data if isinstance(data, str) else data.decode("utf-8", errors="replace")).replace("\n", "").replace("\r", "").strip()
                    padding = 4 - (len(b64) % 4)
                    if padding != 4:
                        b64 += "=" * padding
                    raw = base64.urlsafe_b64decode(b64)
                    text = _decode_quoted_printable(raw)
                    if "text/plain" in mime:
                        plain = (plain or "") + text
                    elif "text/html" in mime:
                        html_body = (html_body or "") + text
                except Exception:
                    pass
            for p in part.get("parts") or []:
                collect_parts(p)

        collect_parts(payload)
        if plain:
            return plain.strip()
        if html_body:
            return _html_to_plain(html_body)
        return msg.get("snippet") or ""
    except Exception as e:
        logger.warning("Gmail get body failed: %s", e)
        return None


def _build_mime_message(
    from_addr: str,
    to: str,
    subject: str,
    body: str,
    subtype: str = "plain",
    attachments: Optional[List[Dict[str, str]]] = None,
) -> Any:
    """Build MIME message, with optional attachments. Returns MIMEMultipart or MIMEText."""
    if attachments:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to
        msg.attach(MIMEText(body, subtype, "utf-8"))
        for att in attachments:
            path_str = att.get("path") or ""
            filename = att.get("filename") or Path(path_str).name
            path = Path(path_str)
            if not path.is_file():
                continue
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
        return msg
    msg = MIMEText(body, subtype, "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    return msg


def _send_mail_gmail(
    account_id: str,
    to: str,
    subject: str,
    body: str,
    subtype: str = "plain",
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
    attachments: Optional[List[Dict[str, str]]] = None,
) -> bool:
    """Send mail via Gmail API (users.messages.send with raw RFC 2822)."""
    acc = get_account(account_id, username, user_scope_id=user_scope_id)
    if not acc:
        logger.warning("_send_mail_gmail: account not found: %s", account_id)
        append_domain_log_always("backend", f"GMAIL_SEND_ERROR account_not_found account={account_id}")
        return False
    token = get_valid_access_token(account_id, "gmail", username, user_scope_id=user_scope_id)
    if not token:
        logger.warning("_send_mail_gmail: No valid token for account %s", account_id[:8] + "***")
        append_domain_log_always("backend", f"GMAIL_SEND_ERROR token_missing account={account_id}")
        return False
    from_addr = acc.get("email") or account_id
    msg = _build_mime_message(from_addr, to, subject, body, subtype, attachments)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    try:
        r = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            json={"raw": raw},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code not in (200, 201):
            logger.warning("Gmail API send failed for %s: %s %s", account_id, r.status_code, r.text[:300])
            append_domain_log_always("backend", f"GMAIL_SEND_ERROR account={account_id} status={r.status_code} response={r.text}")
            return False
        return True
    except Exception as e:
        logger.warning("Gmail API request failed for %s: %s", account_id, e)
        append_domain_log_always("backend", f"GMAIL_SEND_ERROR request_exception account={account_id} error={e}")
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
    user_scope_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch mail via Microsoft Graph (GET /me/mailFolders/.../messages)."""
    token = get_valid_access_token(account_id, "microsoft", username, user_scope_id=user_scope_id)
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
            category = apply_sender_rules_to_category(from_str, "primary", username, user_scope_id=user_scope_id)
            result.append({
                "subject": m.get("subject") or "",
                "from": from_str,
                "date": m.get("receivedDateTime") or "",
                "message_id": m.get("internetMessageId") or "",
                "body_snippet": (m.get("bodyPreview") or "")[:500],
                "category": category,
                "provider_message_id": m.get("id") or "",
            })
        return result
    except Exception as e:
        logger.warning("Graph fetch failed: %s", e)
        return []


def _get_body_microsoft(
    account_id: str, provider_message_id: str, username: Optional[str] = None, user_scope_id: Optional[str] = None
) -> Optional[str]:
    """Fetch full message body from Microsoft Graph as plain text. Strip HTML if needed."""
    token = get_valid_access_token(account_id, "microsoft", username, user_scope_id=user_scope_id)
    if not token:
        return None
    try:
        r = requests.get(
            f"https://graph.microsoft.com/v1.0/me/messages/{provider_message_id}",
            params={"$select": "body"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        body_obj = data.get("body") or {}
        content = body_obj.get("content") or ""
        ct = (body_obj.get("contentType") or "").lower()
        # Graph may return content in quoted-printable; decode before stripping HTML
        if isinstance(content, str) and re.search(r"=[0-9A-Fa-f]{2}", content):
            try:
                content = _decode_quoted_printable(content.encode("latin-1"))
            except Exception:
                pass
        if "html" in ct:
            return _html_to_plain(content)
        return content.strip()
    except Exception as e:
        logger.warning("Graph get body failed: %s", e)
        return None


def _send_mail_microsoft(
    account_id: str,
    to: str,
    subject: str,
    body: str,
    subtype: str = "plain",
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
    attachments: Optional[List[Dict[str, str]]] = None,
) -> bool:
    """Send mail via Microsoft Graph (POST /me/sendMail)."""
    acc = get_account(account_id, username, user_scope_id=user_scope_id)
    if not acc:
        return False
    token = get_valid_access_token(account_id, "microsoft", username, user_scope_id=user_scope_id)
    if not token:
        logger.warning("No valid Microsoft token for account %s", account_id[:8] + "***")
        return False
    message_obj: Dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": "html" if subtype == "html" else "text", "content": body},
        "toRecipients": [{"emailAddress": {"address": to}}],
    }
    if attachments:
        att_list: List[Dict[str, Any]] = []
        for att in attachments:
            path_str = att.get("path") or ""
            filename = att.get("filename") or Path(path_str).name
            path = Path(path_str)
            if not path.is_file():
                continue
            with open(path, "rb") as f:
                content_b64 = base64.b64encode(f.read()).decode("ascii")
            att_list.append({
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename,
                "contentType": "application/octet-stream",
                "contentBytes": content_b64,
            })
        if att_list:
            message_obj["attachments"] = att_list
    payload = {"message": message_obj}
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


def _get_body_imap(
    account_id: str,
    message_id: str,
    folder: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[str]:
    """Fetch full message body via IMAP. Search by Message-ID then fetch body as plain text."""
    conn = _imap_connect(account_id, username=username, user_scope_id=user_scope_id)
    if not conn or not message_id:
        return None
    try:
        conn.select(folder or "INBOX", readonly=True)
        search_id = message_id.strip()
        if not search_id.startswith("<"):
            search_id = "<" + search_id + ">"
        _, uids = conn.search(None, "HEADER", "Message-ID", search_id)
        ids = uids[0].split()
        if not ids:
            return None
        uid = ids[-1]
        _, data = conn.fetch(uid, "(BODY.PEEK[TEXT])")
        if not data or not data[0]:
            _, data = conn.fetch(uid, "(RFC822)")
            if not data or not data[0]:
                return None
            raw = data[0]
            if isinstance(raw, tuple) and len(raw) > 1:
                raw = raw[1]
            else:
                return None
            msg = message_from_bytes(raw)
            plain = None
            html_part = None
            for part in msg.walk():
                ctype = (part.get_content_type() or "").lower()
                try:
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        continue
                    text = payload.decode("utf-8", errors="replace")
                    if "text/plain" in ctype:
                        plain = (plain or "") + text
                    elif "text/html" in ctype:
                        html_part = (html_part or "") + text
                except Exception:
                    continue
            if plain:
                return plain.strip()
            if html_part:
                return _html_to_plain(html_part)
            return None
        part = data[0]
        if isinstance(part, tuple) and len(part) > 1:
            raw = part[1]
        else:
            return None
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace").strip()
        return str(raw).strip()
    except Exception as e:
        logger.warning("IMAP get body failed: %s", e)
        return None
    finally:
        try:
            conn.close()
            conn.logout()
        except Exception:
            pass


def get_message_body_plain(
    account_id: str,
    message_id: str,
    folder: str = "INBOX",
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
    provider_message_id: Optional[str] = None,
) -> Optional[str]:
    """
    Fetch full message body as plain text only (no HTML). Uses provider_message_id for Gmail/Graph when available.
    All return paths go through _ensure_plain_text so fancy HTML/QP never reaches the UI.
    Optional username/user_scope_id for multi-user scope.
    """
    acc = get_account(account_id, username, user_scope_id=user_scope_id)
    if not acc:
        append_domain_log_always("backend", f"GET_BODY_ERROR account_not_found account={account_id}")
        return None
    provider = (acc.get("provider") or "imap").lower()
    result: Optional[str] = None
    if provider == "gmail" and provider_message_id:
        result = _get_body_gmail(account_id, provider_message_id, username, user_scope_id=user_scope_id)
    elif provider == "microsoft" and provider_message_id:
        result = _get_body_microsoft(account_id, provider_message_id, username, user_scope_id=user_scope_id)
    elif provider == "imap":
        result = _get_body_imap(account_id, message_id, folder, username, user_scope_id=user_scope_id)
    elif provider == "microsoft" and message_id:
        token = get_valid_access_token(account_id, "microsoft", username)
        if token:
            try:
                r = requests.get(
                    "https://graph.microsoft.com/v1.0/me/messages",
                    params={"$filter": f"internetMessageId eq '{message_id.replace(chr(39), chr(39)+chr(39))}'", "$select": "id,body"},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=15,
                )
                if r.status_code == 200:
                    value = (r.json() or {}).get("value") or []
                    if value:
                        body_obj = (value[0] or {}).get("body") or {}
                        content = body_obj.get("content") or ""
                        if isinstance(content, str) and re.search(r"=[0-9A-Fa-f]{2}", content):
                            try:
                                content = _decode_quoted_printable(content.encode("latin-1"))
                            except Exception:
                                pass
                        if "html" in (body_obj.get("contentType") or "").lower():
                            result = _html_to_plain(content)
                        else:
                            result = content.strip()
            except Exception as e:
                logger.warning("Graph get body by Message-ID failed: %s", e)
    return _ensure_plain_text(result) if result is not None else None


def fetch_mail(
    account_id: str,
    folder: str = "INBOX",
    since: Optional[str] = None,
    max_messages: int = 50,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch messages from folder. Returns list of dicts with subject, from, date, body_snippet.
    Dispatches to IMAP (imap), Gmail API (gmail), or Microsoft Graph (microsoft).
    Optional username/user_scope_id for multi-user scope.
    """
    acc = get_account(account_id, username, user_scope_id=user_scope_id)
    if not acc:
        return []
    provider = (acc.get("provider") or "imap").lower()
    if provider == "gmail":
        return _fetch_mail_gmail(account_id, folder, since, max_messages, username, user_scope_id=user_scope_id)
    if provider == "microsoft":
        return _fetch_mail_microsoft(account_id, folder, since, max_messages, username, user_scope_id=user_scope_id)
    conn = _imap_connect(account_id, username=username, user_scope_id=user_scope_id)
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
                from_str = msg.get("From", "")
                category = apply_sender_rules_to_category(from_str, "primary", username, user_scope_id=user_scope_id)
                result.append({
                    "subject": msg.get("Subject", ""),
                    "from": from_str,
                    "date": msg.get("Date", ""),
                    "message_id": msg.get("Message-ID", ""),
                    "body_snippet": "",
                    "category": category,
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
    user_scope_id: Optional[str] = None,
    attachments: Optional[List[Dict[str, str]]] = None,
) -> bool:
    """Send one email. Returns True on success. Dispatches to Gmail API, Graph, or SMTP by provider.
    attachments: optional list of [{"path": "/full/path/to/file", "filename": "optional_name.pdf"}].
    Optional username/user_scope_id for multi-user scope."""
    acc = get_account(account_id, username, user_scope_id=user_scope_id)
    if not acc:
        logger.warning("send_mail: account not found: %s", account_id)
        append_domain_log_always("backend", f"SEND_MAIL_ERROR account_not_found account={account_id}")
        return False
    provider = (acc.get("provider") or "imap").lower()
    if provider == "gmail":
        return _send_mail_gmail(account_id, to, subject, body, subtype, username, user_scope_id=user_scope_id, attachments=attachments)
    if provider == "microsoft":
        return _send_mail_microsoft(account_id, to, subject, body, subtype, username, user_scope_id=user_scope_id, attachments=attachments)
    conn = _smtp_connect(account_id, username, user_scope_id=user_scope_id)
    if not conn:
        logger.warning("send_mail: _smtp_connect failed for %s", account_id)
        append_domain_log_always("backend", f"SEND_MAIL_ERROR smtp_connect_failed account={account_id}")
        return False
    try:
        from_addr = acc.get("email") or account_id
        msg = _build_mime_message(from_addr, to, subject, body, subtype, attachments)
        conn.sendmail(from_addr, [to], msg.as_string())
        return True
    except Exception as e:
        logger.warning("Send mail failed for %s: %s", account_id, e)
        return False
    finally:
        try:
            conn.quit()
        except Exception:
            pass
