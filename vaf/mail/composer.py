# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Mail Composer: the mail-shaped half of the Composer (pure functions, no IO).

NOT to be confused with its neighbour `compose.py`, which builds the RFC 822
message that goes on the wire. This module never produces a message and never
sends one: it turns a mail thread into the Composer's bounded context. The prompt,
the untrusted fence, the memory message, the follow-up turns and the cleaning of
the model's output are the shared Composer in `vaf/core/composer.py`, which the
messenger windows use as well; only what is true of MAIL lives here - quoted tails
and signatures to strip, the Sent folder deciding whose message it is, the
one-line quotes from other threads.

Everything here is IO-free and deterministic, so the interesting behaviour (budget
allocation, quote stripping, fence escaping) is unit-testable without a store, a
model or a network. The route does the IO (`vaf/core/composer_lane.py`).

The two invariants of the shared module hold here unchanged: mail bodies are
attacker-controlled and only ever sit inside the fence, and a message the phishing
filter flagged never contributes body text (the anchor being flagged refuses the
whole request in the route; any other flagged message collapses to a one-line
placeholder, so the thread shape survives without the payload).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from vaf.core.composer import (  # noqa: F401 - the shared Composer, re-exported for the mail callers and their tests
    ANCHOR_FLOOR_CHARS,
    EMAIL,
    MAX_CONTEXT_CHARS,
    MAX_KNOWLEDGE_CHARS,
    MAX_TURN_CHARS,
    MAX_TURNS,
    MIN_CONTEXT_CHARS,
    SNIPPET_CHARS,
    SNIPPET_RESERVE_RATIO,
    Entry,
    ThreadContext,
    _MEMORY_EMPTY,
    _MEMORY_GUIDANCE,
    _MEMORY_HEADING,
    _REWRITE_RULES,
    assemble,
    clamp_budget,
    clean_output,
    neutralize,
    when_label as _when,
)
from vaf.core.composer import build_prompt as _build_prompt

#: Older mail from OTHER threads, pulled in by keyword. Deliberately small: these
#: are messages the user did not open, so they are the least verified input in the
#: prompt and must never crowd out the thread being answered.
MAX_RELATED = 4
MAX_RELATED_CHARS = 500

_FENCE_OPEN = EMAIL.fence_open
_FENCE_CLOSE = EMAIL.fence_close
#: The mail profile's rules, whole, so a reader (and the ceiling test) can see them.
_SYSTEM_RULES = EMAIL.system_rules()

# Attribution line produced by compose.quote_reply, and the form other clients
# emit. Only meaningful when quoted lines follow it, so it is matched by the
# backward scan.
_ATTRIBUTION = re.compile(r"^(On .+ wrote:|.+ wrote:)\s*$")
# Hard block markers: everything after one of these is an embedded copy of another
# message, and it is NOT '>'-quoted, so a backward scan over quote markers cannot
# find it. Matched forwards instead.
_EMBEDDED_BLOCK = re.compile(
    r"^\s*-{2,}\s*(Original Message|Forwarded message)\s*-{2,}\s*$", re.IGNORECASE)
_SIGNATURE = re.compile(r"^-- $")


def strip_quoted_tail(text: str) -> str:
    """Drop the trailing quoted conversation and the signature.

    This is the single biggest budget win and it is lossless in context terms: the
    quoted block IS an earlier message of the thread, which the assembler includes
    as its own entry. Keeping both would spend the budget on the same words twice
    and bias the model toward the oldest message, which is usually the least
    relevant one.

    Conservative by construction: only a trailing run is removed, so a quote a
    human replied UNDER (interleaved, common in technical threads) survives.
    """
    lines = (text or "").replace("\r\n", "\n").split("\n")

    # 1. Embedded block markers cut forwards: the copied message under them is not
    #    quote-prefixed, so scanning backwards over '>' would never reach them.
    for idx, line in enumerate(lines):
        if _EMBEDDED_BLOCK.match(line):
            lines = lines[:idx]
            break

    # 2. Trailing quoted run cuts backwards, together with the attribution line
    #    that introduces it. Only a TRAILING run, so an interleaved reply survives.
    cut = len(lines)
    i = len(lines) - 1
    seen_quote = False
    while i >= 0:
        s = lines[i].rstrip()
        if not s:
            i -= 1
            continue
        if s.startswith(">"):
            seen_quote = True
            cut = i
            i -= 1
            continue
        if seen_quote and _ATTRIBUTION.match(s):
            cut = i
        break
    lines = lines[:cut]

    for idx, line in enumerate(lines):
        if _SIGNATURE.match(line):
            lines = lines[:idx]
            break
    return "\n".join(lines).strip()


def is_own_message(row: Dict[str, Any], own_addresses: Optional[set] = None) -> bool:
    """Whether the user wrote this message themselves.

    The FOLDER decides, not the From header. A header is trivially forged, and a
    message wrongly labelled as the user's own would be read as an example of how
    THEY write - handing an attacker a way to steer the voice of every future draft,
    and a claim of authority inside the fence. A message sitting in this mailbox's
    Sent folder genuinely left this mailbox.

    The address match is a fallback for the case the folder cannot answer (a
    provider without SPECIAL-USE, an account whose folders are not classified yet),
    and it is refused outright for anything sitting in Inbox or Junk - which is
    exactly where a forged From lands.
    """
    special = (row.get("folder_special_use") or "").strip()
    if special == "\\Sent":
        return True
    if special in ("\\Inbox", "\\Junk", "\\Trash"):
        return False
    if not own_addresses:
        return False
    from email.utils import parseaddr
    _name, addr = parseaddr(row.get("from_addr") or row.get("from") or "")
    return addr.strip().lower() in {a.strip().lower() for a in own_addresses if a}


def _sender(row: Dict[str, Any]) -> str:
    return (row.get("from_addr") or row.get("from") or "unknown sender").strip()


def _body_of(row: Dict[str, Any], bodies: Dict[int, str]) -> str:
    """Cached body text if we have it, else the stored snippet. Never fetches:
    a compose assist must not trigger a network round trip per message."""
    pk = row.get("id")
    text = bodies.get(pk) if pk is not None else None
    if not (text or "").strip():
        text = row.get("snippet") or ""
    return strip_quoted_tail(text)


def build_thread_context(rows: List[Dict[str, Any]], bodies: Dict[int, str], *,
                         anchor_pk: int, budget_chars: int, per_msg_chars: int,
                         max_messages: int,
                         own_addresses: Optional[set] = None) -> ThreadContext:
    """Assemble thread text newest-first under a character budget.

    Decides what is true of each mail - who wrote it, whether the phishing filter
    hid it, what its text is once the quoted tail is gone - and hands the
    allocation to the shared `assemble`. `rows` arrives chronological
    (store.thread_messages orders by date); `bodies` maps message pk to
    already-fetched plain text.
    """
    if not rows:
        return ThreadContext()
    anchor = next((r for r in rows if r.get("id") == anchor_pk), rows[-1])
    anchor_index = next(i for i, r in enumerate(rows) if r.get("id") == anchor.get("id"))
    entries: List[Entry] = []
    for i, row in enumerate(rows):
        own = is_own_message(row, own_addresses)
        # "YOUR USER" rather than a name, so the label cannot be confused with a
        # display name inside the fence.
        who = EMAIL.own_label if own else _sender(row)
        when = _when(row.get("date_ts") or row.get("internaldate_ts"))
        hidden = None
        if row.get("suspicious_for_agent") and i != anchor_index:
            hidden = "[hidden: this message is flagged as possible phishing]"
        entries.append(Entry(who=who, when=when, body=_body_of(row, bodies), own=own,
                             hidden=hidden, snippet=(row.get("snippet") or "")))
    return assemble(entries, anchor_index=anchor_index, budget_chars=budget_chars,
                    per_msg_chars=per_msg_chars, max_messages=max_messages,
                    subject=(anchor.get("subject") or ""))


def format_related(rows: List[Dict[str, Any]]) -> str:
    """Short quotes from older mail in OTHER threads, for the untrusted fence.

    Bounded hard and kept to snippets rather than bodies: these messages were found
    by keyword, not chosen by the user, so they are the least verified thing in the
    prompt. Anything the phishing filter flagged must already have been dropped by
    the caller - this formats, it does not re-check.
    """
    out = []
    for row in (rows or [])[:MAX_RELATED]:
        text = strip_quoted_tail(row.get("snippet") or "")[:MAX_RELATED_CHARS]
        if not text.strip():
            continue
        out.append(f"--- earlier mail | from: {_sender(row)} | "
                   f"date: {_when(row.get('date_ts') or row.get('internaldate_ts'))} | "
                   f"subject: {row.get('subject') or ''} ---\n{text}")
    return "\n\n".join(out)


def build_prompt(ctx: ThreadContext, *, mode: str, instruction: str = "",
                 draft: str = "", tone: str = "", language: str = "",
                 knowledge: str = "", related: str = "",
                 turns: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, str]]:
    """The shared prompt, bound to the mail profile (see `vaf.core.composer.build_prompt`)."""
    return _build_prompt(ctx, mode=mode, instruction=instruction, draft=draft, tone=tone,
                         language=language, knowledge=knowledge, related=related,
                         turns=turns, profile=EMAIL)
