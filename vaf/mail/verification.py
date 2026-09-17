# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The per-message verdict the mail store keeps next to every message (schema v2,
EMAIL_CLIENT.md "Verification and cases").

Two independent questions, answered once at ingest and persisted in `message_auth`:

1. Did a person write this? `classify.classify_machine` reads the headers and the MIME
   structure (a bounce, a read receipt, an auto-reply, a list, bulk mail, a calendar
   invitation, our own mail coming back) and the lexical no-reply rule of the inbox.
2. Is the From address who it claims to be? `authenticity.verdict` reads the
   Authentication-Results header the account's own provider wrote (RFC 8601), with
   DMARC-style alignment against the From domain (RFC 7489 section 3.1).

Both run over the parser's `ParsedMessage`, never over the raw bytes again, and the
identity headers they read are snapshotted into the row (`headers`), so a verdict can be
recomputed under a new policy (a freshly learned authserv-id) for a message whose raw
bytes were never cached or were evicted by retention. `policy_key` names the policy a
verdict was computed under; a row whose key differs is stale for the backfill.

The verdict is computed ONCE and replaced only by an explicit backfill: DKIM keys rotate
and a later recomputation from the same bytes can differ from the verdict at receipt
(RFC 6376 section 5.2), which is exactly why the provider's header is the record and not
a live re-check.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, Iterable, List, Optional

from vaf.mail.parser import ParsedMessage

# The identity fields the snapshot carries, in the parser's own names.
SNAPSHOT_FIELDS = (
    "reply_to", "sender", "return_path", "return_path_null", "delivered_to", "in_reply_to",
    "auto_submitted", "precedence", "x_auto_response_suppress", "auto_reply_headers",
    "list_id", "list_headers", "feedback_id", "report_type", "dsn_action",
    "original_message_id", "calendar_method", "thread_index", "exchange_parent_id",
    "auth_results", "arc_auth_results", "dkim_domains", "received",
)

AUTH_PROFILES = ("rfc8601", "microsoft", "none")


def identity_snapshot(parsed: ParsedMessage) -> Dict[str, Any]:
    """The identity headers of a parsed message as a JSON-ready dict."""
    out: Dict[str, Any] = {}
    for name in SNAPSHOT_FIELDS:
        v = getattr(parsed, name, None)
        if isinstance(v, (list, tuple)):
            out[name] = [str(x) for x in v][:32]
        elif isinstance(v, bool):
            out[name] = v
        else:
            out[name] = str(v or "")
    return out


def parsed_from_snapshot(snapshot: Dict[str, Any], *, from_addr: str = "", message_id: str = "",
                         subject: str = "") -> ParsedMessage:
    """A ParsedMessage with the envelope and the snapshotted identity headers, enough for
    both verdicts. Bodies and attachments are not part of it."""
    p = ParsedMessage(message_id=message_id or "", subject=subject or "", from_addr=from_addr or "")
    for name in SNAPSHOT_FIELDS:
        if name not in (snapshot or {}):
            continue
        v = snapshot[name]
        default = getattr(p, name)
        if isinstance(default, list):
            setattr(p, name, [str(x) for x in (v or [])])
        elif isinstance(default, bool):
            setattr(p, name, bool(v))
        else:
            setattr(p, name, str(v or ""))
    return p


def auth_policy_for_account(account: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The verification policy of one configured mail account (the config entry of
    `email_accounts.get_email_config`): the trusted authserv-id, the header profile and
    the account's own addresses. Missing fields fail safe: no trusted id means every
    sender stays unknown, never verified."""
    acc = account or {}
    own: List[str] = []
    for key in ("email", "account_id"):
        v = str(acc.get(key) or "").strip().lower()
        if "@" in v and v not in own:
            own.append(v)
    for alias in acc.get("aliases") or []:
        a = str(alias or "").strip().lower()
        if "@" in a and a not in own:
            own.append(a)
    profile = str(acc.get("auth_profile") or "").strip().lower()
    if profile not in AUTH_PROFILES:
        profile = "microsoft" if str(acc.get("provider") or "").lower() == "microsoft" else "rfc8601"
    return {
        "trusted_authserv_id": str(acc.get("trusted_authserv_id") or "").strip().lower(),
        "auth_profile": profile,
        "own_addresses": own,
        "own_domains": sorted({a.rsplit("@", 1)[-1] for a in own if "@" in a}),
    }


def policy_key(policy: Optional[Dict[str, Any]]) -> str:
    """A short stable name of a policy, stored on every verdict computed under it."""
    p = policy or {}
    canon = json.dumps({
        "trusted_authserv_id": str(p.get("trusted_authserv_id") or ""),
        "auth_profile": str(p.get("auth_profile") or ""),
        "own_addresses": sorted(str(x) for x in (p.get("own_addresses") or [])),
    }, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def assess(parsed: ParsedMessage, *, policy: Optional[Dict[str, Any]] = None,
           is_own_message_id: Optional[Callable[[str], bool]] = None,
           category: str = "") -> Dict[str, Any]:
    """Both verdicts of one message as the `message_auth` row (without the pk). Never
    raises: a failure inside one half leaves that half at its unknown value and is noted
    in `reasons`, because a verdict row must exist for every message."""
    from vaf.mail import authenticity, classify

    pol = policy or {}
    row: Dict[str, Any] = {
        "machine_kind": "", "machine_reason": "", "auth_state": "unknown", "auth_source": "none",
        "authserv_id": "", "topmost_authserv_id": "", "from_domain": "", "spf": "", "spf_domain": "",
        "dkim": "", "dkim_domain": "", "dmarc": "", "arc": "", "compauth": "", "aligned_by": "",
        "via_domain": "", "flags": [], "reasons": [], "headers": identity_snapshot(parsed),
        "policy_key": policy_key(pol),
    }
    try:
        mv = classify.classify_machine(
            parsed, own_addresses=pol.get("own_addresses") or (),
            is_own_message_id=is_own_message_id, category=category or "")
        row["machine_kind"] = mv.kind
        row["machine_reason"] = mv.reason
    except Exception as e:  # pragma: no cover - a verdict row must exist whatever happened
        row["reasons"].append(f"classify_failed:{type(e).__name__}")
    try:
        av = authenticity.verdict(
            parsed, trusted_authserv_id=str(pol.get("trusted_authserv_id") or ""),
            auth_profile=str(pol.get("auth_profile") or "rfc8601"),
            own_domains=pol.get("own_domains") or ())
        row.update({
            "auth_state": av.state, "auth_source": av.source, "authserv_id": av.authserv_id,
            "topmost_authserv_id": av.topmost_authserv_id, "from_domain": av.from_domain,
            "spf": av.spf, "spf_domain": av.spf_domain, "dkim": av.dkim, "dkim_domain": av.dkim_domain,
            "dmarc": av.dmarc, "arc": av.arc, "compauth": av.compauth, "aligned_by": av.aligned_by,
            "via_domain": av.via_domain, "flags": list(av.flags),
        })
        row["reasons"] = list(av.reasons) + row["reasons"]
    except Exception as e:  # pragma: no cover
        row["reasons"].append(f"authenticity_failed:{type(e).__name__}")
    return row


def summary(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The part of a verdict a list row or an agent row carries: enough for a badge and
    a decision, small enough to ride on every message. `state` is unknown for a message
    that was never assessed."""
    r = row or {}
    return {
        "state": str(r.get("auth_state") or "unknown"),
        "source": str(r.get("auth_source") or "none"),
        "aligned_by": str(r.get("aligned_by") or ""),
        "via_domain": str(r.get("via_domain") or ""),
        "dkim_domain": str(r.get("dkim_domain") or ""),
        "from_domain": str(r.get("from_domain") or ""),
        "dmarc": str(r.get("dmarc") or ""),
        "flags": list(r.get("flags") or []),
        "machine_kind": str(r.get("machine_kind") or ""),
        "machine_reason": str(r.get("machine_reason") or ""),
    }


def learn_provider(topmost_ids: Iterable[str], topmost_headers: Iterable[str]) -> Dict[str, Any]:
    """What the mailbox says about the provider's Authentication-Results: the majority
    authserv-id (`authenticity.learn_authserv_id`) or the Microsoft id-less profile.
    Returns {authserv_id, profile, count, total}; an empty authserv_id with profile
    rfc8601 means nothing could be learned yet (too few samples, or no agreement)."""
    from vaf.mail import authenticity

    ids = [str(x or "") for x in topmost_ids]
    heads = [str(x or "") for x in topmost_headers]
    learned, count, total = authenticity.learn_authserv_id(ids)
    if learned:
        return {"authserv_id": learned, "profile": "rfc8601", "count": count, "total": total}
    if authenticity.looks_microsoft(heads):
        return {"authserv_id": "", "profile": "microsoft", "count": len(heads), "total": len(heads)}
    return {"authserv_id": "", "profile": "rfc8601", "count": count, "total": total}
