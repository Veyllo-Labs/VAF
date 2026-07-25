# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Legacy-shape bridge: serves the v1 row/​body contracts from the v2 store.

The seven agent tools, the legacy /api/email message routes and MailDashboard
all consume email_sync_store's row shape and get_message_body_plain's text
contract. When mail_engine_v2_enabled is on, email_sync_store delegates here
so every consumer switches to the engine store with ONE flag and IDENTICAL
output shapes (EMAIL_CLIENT.md: tools keep names/signatures/shapes). The
legacy scope semantics are preserved exactly: user_scope_id=None means the
local admin and resolves to the admin's real scope UUID."""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("vaf.mail.tool_bridge")


def _scope(user_scope_id: Optional[str]) -> str:
    if user_scope_id and str(user_scope_id).strip():
        return str(user_scope_id).strip()
    from vaf.core.config import get_local_admin_scope_id
    return get_local_admin_scope_id()


def _legacy_row(m: Dict[str, Any]) -> Dict[str, Any]:
    ts = m.get("date_ts") or m.get("internaldate_ts")
    iso = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else None
    return {
        "account_id": m.get("acct") or "",
        "folder": m.get("folder_name") or "INBOX",
        "message_id": m.get("message_id") or f"pk-{m.get('id')}",
        "category": m.get("category") or "primary",
        "provider_message_id": m.get("gm_msgid") or "",
        "subject": m.get("subject") or "",
        "from": m.get("from_addr") or "",
        "date": iso or "",
        "message_date_iso": iso,
        "body_snippet": m.get("snippet") or "",
        "synced_at": m.get("created_at") or "",
        "answered_at": (m.get("answered_at") or "").strip() if m.get("answered_at") else "",
    }


def list_messages(account_id: Optional[str], folder: Optional[str], limit: int, offset: int,
                  username: str, user_scope_id: Optional[str],
                  category: Optional[str] = None) -> List[Dict[str, Any]]:
    from vaf.mail.store import MailStore
    store = MailStore(_scope(user_scope_id))
    cat = None if (category or "").strip() in ("", "all") else category
    rows = store.list_messages(account_id=account_id or None, folder=folder or None,
                               category=cat, limit=limit, offset=offset)
    return [_legacy_row(m) for m in rows]


def search_messages(query: str, account_id: Optional[str], limit: int,
                    username: str, user_scope_id: Optional[str]) -> List[Dict[str, Any]]:
    from vaf.mail.store import MailStore
    store = MailStore(_scope(user_scope_id))
    rows = store.search(query, account_id=account_id or None, limit=limit)
    return [_legacy_row(m) for m in rows]


def get_body_text(account_id: str, message_id: str, username: Optional[str],
                  user_scope_id: Optional[str]) -> Optional[str]:
    """Cached plain-text body by Message-ID (offline path for read_mail and the
    legacy body route). None when not cached - caller falls back to live fetch."""
    from vaf.mail.parser import parse_message
    from vaf.mail.store import MailStore
    store = MailStore(_scope(user_scope_id))
    mid = (message_id or "").strip()
    variants = {mid, mid.strip("<>"), f"<{mid.strip('<>')}>"}
    q = ",".join("?" for _ in variants)
    # A Message-ID can exist in several folders (e.g. an INBOX copy AND a Sent /
    # All-Mail copy of a self-addressed mail). Prefer a copy whose body is cached
    # and return the first candidate that actually yields a body - otherwise the
    # bare id-DESC pick lands on an uncached duplicate and read_mail wrongly reports
    # "message not found or empty".
    rows = store._conn().execute(
        f"SELECT m.id FROM messages m JOIN accounts a ON a.id=m.account_id "
        f"WHERE m.message_id IN ({q}) AND (?='' OR a.account_id=?) "
        f"ORDER BY (m.body_state='cached') DESC, m.id DESC",
        (*variants, account_id or "", account_id or "")).fetchall()
    for row in rows:
        raw = store.get_raw(int(row["id"]))
        if raw is None:
            continue
        body = parse_message(raw).body_text
        if body:
            return body
    return None


def _v2_account_ids(store) -> set:
    return {a.get("account_id") for a in store.list_accounts()}


def v2_syncs_account(account_id: Optional[str], user_scope_id: Optional[str]) -> bool:
    """True when the v2 engine actually has this account in its store.

    An account row is created only by the sync engine, which the supervisor runs
    solely for provider=='imap' or imap_ready accounts. Callers use this to tell
    "synced, just empty" apart from "the engine never covered this account", so
    the second case can still fall back to the legacy lane instead of reporting
    an empty mailbox."""
    if not account_id:
        return False
    try:
        from vaf.mail.store import MailStore
        return str(account_id) in _v2_account_ids(MailStore(_scope(user_scope_id)))
    except Exception as e:  # pragma: no cover - availability fallback
        logger.warning("v2 account-set check failed, assuming not synced: %s", e)
        return False


def _merge(v2_rows, legacy_rows, v2_accounts, limit: int, offset: int):
    """v2 rows win for accounts the engine syncs; accounts unknown to v2 (e.g.
    Gmail-API/Graph until the phase-3 re-consent) keep their legacy rows, so
    enabling the flag never blanks a mailbox (review finding)."""
    merged = list(v2_rows) + [r for r in legacy_rows
                              if (r.get("account_id") or "") not in v2_accounts]
    merged.sort(key=lambda r: (r.get("message_date_iso") or r.get("date") or
                               r.get("synced_at") or ""), reverse=True)
    return merged[offset:offset + limit]


def list_messages_merged(account_id, folder, limit, offset, username, user_scope_id,
                         category=None):
    from vaf.core.email_sync_store import list_messages as legacy_list
    from vaf.mail.store import MailStore
    store = MailStore(_scope(user_scope_id))
    v2_accounts = _v2_account_ids(store)
    span = max(1, int(limit)) + max(0, int(offset))
    if account_id and account_id in v2_accounts:
        legacy_rows = []
    else:
        legacy_rows = legacy_list(account_id=account_id, folder=folder, limit=span,
                                  offset=0, username=username, user_scope_id=user_scope_id,
                                  category=category, _skip_v2=True)
    v2_rows = list_messages(account_id, folder, span, 0, username, user_scope_id,
                            category=category)
    return _merge(v2_rows, legacy_rows, v2_accounts, int(limit), int(offset))


def search_messages_merged(query, folder, limit, username, user_scope_id):
    from vaf.core.email_sync_store import search_messages as legacy_search
    from vaf.mail.store import MailStore
    store = MailStore(_scope(user_scope_id))
    v2_accounts = _v2_account_ids(store)
    legacy_rows = legacy_search(query, folder=folder or "INBOX", limit=limit,
                                username=username, user_scope_id=user_scope_id,
                                _skip_v2=True)
    v2_rows = [_legacy_row(m) for m in store.search(query, account_id=None,
                                                    limit=limit, folder=folder)]
    return _merge(v2_rows, legacy_rows, v2_accounts, int(limit), 0)


def update_message_field(user_scope_id, account_id, message_id, field, value) -> bool:
    """Mirror a category/answered_at write into the v2 store (split-brain fix).
    Field name is allow-listed; matching is bracket-tolerant by Message-ID."""
    if field not in ("category", "answered_at"):
        return False
    from vaf.mail.store import MailStore
    store = MailStore(_scope(user_scope_id))
    mid = (message_id or "").strip()
    variants = {mid, mid.strip("<>"), f"<{mid.strip('<>')}>"}
    q = ",".join("?" for _ in variants)
    conn = store._conn()
    cur = conn.execute(
        f"UPDATE messages SET {field}=? WHERE message_id IN ({q}) "
        f"AND account_id IN (SELECT id FROM accounts WHERE (?='' OR account_id=?))",
        (value, *variants, account_id or "", account_id or ""))
    conn.commit()
    return cur.rowcount > 0
