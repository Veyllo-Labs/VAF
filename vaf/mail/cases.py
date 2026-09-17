# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Cases: which conversation an inbound mail belongs to, how far its sender is trusted,
and what the answering lane may do with it (FRONT_OFFICE.md, "Mail").

Three questions, answered separately, because a token never raises trust and trust never
attributes:

1. ATTRIBUTION (`attribute`): which case does this mail belong to. The signals, in the
   order ticket systems use them: an id VAF minted itself (the case anchor in the
   Message-ID, verified by its HMAC, or any Message-ID in the `sent_ids` ledger) found in
   In-Reply-To, References or Exchange's parent id (certain); a plus-addressed recipient
   `local+vaf-<case>@<own domain>` (certain); a subject tag `[VAF#...]` with a valid
   check (certain); the thread the store joined the mail to when a case rides on it
   (strong, never certain: header ids are not secrets). Every certain or strong signal
   is corroborated by the PARTICIPANT CHECK: the sender must already be on the case (a
   From, To or Cc of its messages, its correspondent, or an address the owner added), the
   rule osTicket and Freshdesk apply before a subject or header match counts. Outcomes:
   `case` (attributed), `conflict` (two cases named, never first-hit-wins), `foreign` (a
   token for a case the sender is not on: the token proved knowledge of the case, not
   identity), `orphan` (a verified anchor with no case behind it, a rebuilt store),
   `report` (a bounce or read receipt about a mail we sent), `new` (no signal). Nothing
   is ever guessed from a subject, a display name or a sender-plus-subject window.
2. TRUST (`trust_level`): T0 unverified (no aligned pass, an identity flag, machine
   mail), T1 authenticated on another domain ("via"), T2 verified stranger, T3 verified
   contact with "Can reach your assistant" on, T4 verified reply into a case the agent
   already wrote in (the WhatsApp reply-window rule, on mail).
3. DECISION (`decide`): with the mail channel's policy (`evaluate_ingress("email", ...)`
   for the same reason vocabulary the messenger bridges log), the per-address rate caps
   and the reply mode: `answer` (send at once), `draft` (a held answer the owner
   approves), `ignore` (with the reason). Below T2 nobody is answered automatically: an
   unverified human is the owner's to answer, and a flood of unverified mail must not
   become a flood of model turns.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any, Dict, Iterable, List, Optional, Tuple

from vaf.mail.parser import ParsedMessage

TRUST_LEVELS = ("T0", "T1", "T2", "T3", "T4")
OUTCOMES = ("case", "conflict", "foreign", "orphan", "report", "new")
ACTIONS = ("answer", "draft", "ignore")
# Identity flags that cap a sender at T0 whatever the provider said (the meaning DMARC
# does not judge: a Reply-To leaving the organisation, a claim on the owner's own
# domain that did not authenticate, no or several From addresses, no Message-ID).
CAPPING_FLAGS = frozenset({"own_domain_spoof", "dmarc_fail", "no_message_id", "multiple_from", "reply_to_mismatch"})


@dataclass(frozen=True)
class Attribution:
    outcome: str
    case_id: str = ""
    signal: str = ""        # own_id | sent_id | plus_address | subject_tag | thread | report | ""
    certainty: str = ""     # certain | strong | ""
    hints: Tuple[str, ...] = ()
    report_of: str = ""     # for a report: the Message-ID it reports on

    @property
    def attributed(self) -> bool:
        return self.outcome == "case"


@dataclass(frozen=True)
class Decision:
    action: str             # answer | draft | ignore
    reason: str
    ingress_reason: str = ""


def sender_address(from_addr: str) -> str:
    """The addr-spec of a From header, lowercased; "" when there is none."""
    _name, addr = parseaddr(str(from_addr or ""))
    addr = (addr or "").strip().lower()
    return addr if "@" in addr else ""


def sender_name(from_addr: str) -> str:
    name, addr = parseaddr(str(from_addr or ""))
    return (name or "").strip() or (addr or "").strip()


def candidate_ids(parsed: ParsedMessage) -> List[str]:
    """The ids a reply can point at, in the order they are trusted: the first bracketed
    In-Reply-To, References newest first, Exchange's parent id. The mail's own Message-ID
    is never a candidate."""
    own = str(getattr(parsed, "message_id", "") or "")
    out: List[str] = []
    first = str(getattr(parsed, "in_reply_to", "") or "")
    if first:
        out.append(first)
    for ref in reversed(list(getattr(parsed, "refs", None) or [])):
        if ref and ref not in out:
            out.append(ref)
    parent = str(getattr(parsed, "exchange_parent_id", "") or "")
    if parent and parent not in out:
        out.append(parent)
    return [c for c in out if c and c != own]


def attribute(store: Any, account_pk: int, *, user_scope_id: str, account_id: str,
              parsed: ParsedMessage, thread_id: Optional[int] = None, machine_kind: str = "",
              own_domains: Iterable[str] = (), extra_participants: Iterable[str] = ()) -> Attribution:
    """Which case this mail belongs to (see the module docstring). `store` is the
    MailStore of the scope; `extra_participants` are the sender's other addresses the
    contact book knows (a person answering from a second address of theirs)."""
    from vaf.mail import case_token

    hints: List[str] = []
    sender = sender_address(parsed.from_addr)
    # A report about a mail we sent is attributed to that send, never to a case as a reply.
    if machine_kind in ("bounce", "mdn"):
        original = str(getattr(parsed, "original_message_id", "") or "")
        row = store.sent_id(account_pk, original) if original else None
        if row:
            return Attribution("report", str(row.get("case_id") or ""), "report", "certain", (), str(row.get("message_id") or ""))
        return Attribution("new", "", "", "", ("report of an unknown mail",) if original else ())

    found: Dict[str, Tuple[str, str]] = {}

    def _seen(case_id: str, signal: str, certainty: str) -> None:
        cid = str(case_id or "").upper()
        if cid and cid not in found:
            found[cid] = (signal, certainty)

    for candidate in candidate_ids(parsed):
        verified = case_token.verify_anchor(user_scope_id, account_id, candidate)
        if verified:
            _seen(verified, "own_id", "certain")
            continue
        if case_token.ANCHOR_RE.search(candidate.strip().strip("<>").split("@", 1)[0] or ""):
            # Shaped like one of our anchors but not ours: another scope, another
            # account, or tampered. Reported, never trusted.
            hints.append("anchor_unverified")
        row = store.sent_id(account_pk, candidate)
        if row and row.get("case_id"):
            _seen(str(row["case_id"]), "sent_id", "certain")
    for domain in own_domains:
        for case_id in case_token.find_plus_addresses(
                [getattr(parsed, "to_addrs", ""), getattr(parsed, "cc_addrs", ""), getattr(parsed, "delivered_to", "")], domain):
            _seen(case_id, "plus_address", "certain")
    for case_id, check in case_token.find_subject_tags(getattr(parsed, "subject", "") or ""):
        if case_token.verify_subject_tag(user_scope_id, account_id, case_id, check):
            _seen(case_id, "subject_tag", "certain")
        else:
            hints.append("subject_tag_unverified")
    if thread_id is not None:
        riding = store.case_for_thread(account_pk, int(thread_id))
        if riding:
            _seen(str(riding["case_id"]), "thread", "strong")

    if not found:
        return Attribution("new", "", "", "", tuple(hints))
    if len(found) > 1:
        return Attribution("conflict", "", "", "", tuple(hints) + tuple(sorted(found)))
    case_id, (signal, certainty) = next(iter(found.items()))
    case = store.case_by_id(account_pk, case_id)
    if not case:
        return Attribution("orphan", case_id, signal, certainty, tuple(hints))
    participants = set(store.case_participants(account_pk, case_id))
    participants.update(a.strip().lower() for a in extra_participants if a)
    if not sender or sender not in participants:
        return Attribution("foreign", case_id, signal, certainty, tuple(hints))
    return Attribution("case", case_id, signal, certainty, tuple(hints))


def trust_level(*, auth: Optional[Dict[str, Any]], machine_kind: str = "",
                contact: Optional[Dict[str, Any]] = None, attribution: Optional[Attribution] = None,
                agent_wrote_in_case: bool = False) -> str:
    """The rung a sender stands on for this mail (see the module docstring). `auth` is the
    stored verdict row or its summary (`auth_state` or `state`, `flags`)."""
    a = auth or {}
    state = str(a.get("auth_state") or a.get("state") or "unknown")
    flags = set(a.get("flags") or [])
    if machine_kind or state in ("unknown", "unverified") or (flags & CAPPING_FLAGS):
        return "T0"
    if state == "via":
        return "T1"
    if state != "verified":
        return "T0"
    if attribution is not None and attribution.attributed and agent_wrote_in_case:
        return "T4"
    if contact and contact.get("allow_as_assistant_user"):
        return "T3"
    return "T2"


def decide(*, trust: str, attribution: Attribution, raw_policy: Any, reply_mode: str = "draft",
           machine_kind: str = "", opted_out: bool = False,
           replies_last_hour: int = 0, replies_last_day: int = 0,
           max_per_hour: int = 3, max_per_day: int = 10) -> Decision:
    """What the answering lane does with one mail. Pure over its inputs."""
    from vaf.core.channel_ingress_policy import evaluate_ingress

    if machine_kind:
        return Decision("ignore", f"machine:{machine_kind}")
    if attribution.outcome == "report":
        return Decision("ignore", "report")
    if trust in ("T0", "T1"):
        return Decision("ignore", "unverified" if trust == "T0" else "via")
    if attribution.outcome in ("conflict", "foreign", "orphan"):
        return Decision("ignore", attribution.outcome)
    allowed, ingress_reason = evaluate_ingress(
        "email", raw_policy, explicit_match=False, contact_match=(trust == "T3"),
        conversation_match=(trust == "T4"), sender_opted_out=opted_out)
    if not allowed:
        return Decision("ignore", ingress_reason, ingress_reason)
    if opted_out and ingress_reason != "open_conversation":
        return Decision("ignore", "opted_out", ingress_reason)
    # A cap of 0 is honoured as written: no automatic answer, every mail waits for the owner.
    if replies_last_hour >= max(0, int(max_per_hour)) or replies_last_day >= max(0, int(max_per_day)):
        return Decision("ignore", "capped", ingress_reason)
    action = "answer" if str(reply_mode or "draft").lower() == "send" else "draft"
    return Decision(action, "ok", ingress_reason)


def lock_expired(closed_at: Optional[str], *, lock_days: int, now: Optional[datetime] = None) -> bool:
    """Whether a closed case is past its reopen window: a reply inside it reopens the
    case, a later one opens a new case linked to the old (Help Scout's lock)."""
    if not closed_at:
        return False
    try:
        closed = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
        if closed.tzinfo is None:
            closed = closed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    moment = now or datetime.now(timezone.utc)
    return moment - closed > timedelta(days=max(0, int(lock_days)))
