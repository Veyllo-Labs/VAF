# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Case tokens: the stamps that tie a reply to the case the agent sent it from.

Every mail the agent sends belongs to a case. The correspondent's client is free to
mangle what it replies with: In-Reply-To is dropped by some webmail, References are
truncated, subjects are re-prefixed and re-encoded, and Reply-To is honoured or not.
So the outgoing mail carries up to three stamps, each self-verifying, and a reply is
attributed to the case by whichever survived:

1. The Message-ID, always: ``<CASEID.NONCE.TAG@domain>`` where TAG is an HMAC over
   the case id and the nonce. A reply that keeps In-Reply-To or References hands
   the case back with certainty, and a forged id fails the tag.
2. The subject tag, opt-in: ``[VAF#XXXXX-XXXXX-CCCC]`` with CCCC an HMAC over the
   case id. Survives every reply prefix a client adds; the check keeps a guessed or
   mistyped id from landing in someone else's case.
3. The plus address, opt-in: ``local+vaf-caseid@domain`` as Reply-To. Survives a
   client that keeps nothing but the address it answers to. It carries no check of
   its own because delivery to our own mailbox is the proof of possession here.

The key every tag is computed with is derived per user scope and account from one
account-level secret and never stored, the same construction as the A2A room keys
(`vaf.core.a2a.signing.room_seed`): losing the secret loses the ability to verify
old stamps, and nothing has to be kept in sync per case.

Everything here is pure apart from the secret lookup. Malformed input gets a None,
False or empty answer wherever the signature has one; the minting functions raise
ValueError on a case id that is not one, because there is no stamp to hand back.
Module level is stdlib only; `cryptography` is imported inside `case_key`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from email.utils import getaddresses
from typing import Iterable, List, Optional, Tuple

# The keyring entry every case key is derived from. One secret per machine account.
ROOT_SECRET = "mail_case_root_key"

# The info prefix of the derivation, versioned so a later scheme derives different
# keys from the same root instead of colliding with this one.
DERIVE_INFO = b"vaf-mail-case/v1/"

# RFC 4648 base32 alphabet, unpadded and uppercase everywhere a token is minted.
# Chosen over hex or base64 because it survives case folding, has no characters a
# mail client treats as punctuation, and reads aloud without ambiguity.
_B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

CASE_ID_LEN = 10   # 50 bits from `secrets`
NONCE_LEN = 12     # 60 bits from `secrets`, one per minted Message-ID
TAG_LEN = 12       # 60 bits of HMAC-SHA256(key, f"{case_id}.{nonce}")
CHECK_LEN = 4      # 20 bits of HMAC-SHA256(key, case_id) in the subject tag

_B32 = "[A-Z2-7]"

# The local part of a minted Message-ID, and nothing else: verify_anchor splits the
# address at its last "@" first, so the domain never reaches this pattern.
ANCHOR_RE = re.compile(
    rf"^({_B32}{{{CASE_ID_LEN}}})\.({_B32}{{{NONCE_LEN}}})\.({_B32}{{{TAG_LEN}}})$",
    re.IGNORECASE,
)

# The subject tag as a client may hand it back: any case, the dashes optional, up to
# two spaces after the hook where a folded subject line was re-joined. The leading
# assertion is "not preceded by a word character", the fixed-width spelling of
# "preceded by a non-word character or at the start" that Python's `re` accepts.
SUBJECT_TAG_RE = re.compile(
    rf"(?<!\w)VAF#\s{{0,2}}({_B32}{{5}})-?({_B32}{{5}})-?({_B32}{{{CHECK_LEN}}})\b",
    re.IGNORECASE,
)

# A whole address: the plus tag sits after ANY local part, so a mailbox that already
# carries a subaddress ("alice+work") still matches, and the domain is captured for
# the caller to compare against its own.
PLUS_ADDRESS_RE = re.compile(
    rf"^(?P<local>[^@\s]+?)\+vaf-(?P<case>{_B32}{{{CASE_ID_LEN}}})@(?P<domain>[^@\s]+)$",
    re.IGNORECASE,
)

_CASE_ID_RE = re.compile(rf"^({_B32}{{5}})-?({_B32}{{5}})$")
_CHECK_RE = re.compile(rf"^{_B32}{{{CHECK_LEN}}}$")

# Reply and forward prefixes as mail clients write them, by language of the client
# rather than of the mail. A token counts only with its colon, optionally with a
# counter ("RE[2]:", "AW(3):") and optionally wrapped in a bracket the client put
# around the whole subject ("[Fwd: ...]"), so "Rescue plan" is a subject and not a
# reply to "scue plan".
_PREFIX_TOKENS = (
    "re", "aw", "fwd", "fw", "wg", "sv", "vs", "tr", "antw", "antwort",
    "rif", "odp", "vb", "rv", "res", "enc", "doorst",
)
_PREFIX_RE = re.compile(
    r"^\s*(?P<open>[\[(])?\s*(?:"
    + "|".join(sorted(map(re.escape, _PREFIX_TOKENS), key=len, reverse=True))
    + r")(?:\s*[\[(]\d+[\])])?\s*:\s*",
    re.IGNORECASE,
)
_CLOSING = {"[": "]", "(": ")"}


# ── key ─────────────────────────────────────────────────────────────────────

def _root_secret() -> str:
    """The account-level secret every case key is derived from. A function so a
    test can pin the arithmetic against a known root without touching a keyring."""
    from vaf.core.data_keyring import get_data_secret

    return get_data_secret(ROOT_SECRET)


def case_key(user_scope_id: str, account_id: str) -> bytes:
    """The 32 key bytes of this account IN THIS SCOPE, derived and never stored.

    Two users of one install get different keys for the same account id, and one
    user gets different keys per account, so a stamp minted anywhere else fails to
    verify here even though the root secret is shared by the whole install.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    info = DERIVE_INFO + f"{user_scope_id}:{account_id}".encode("utf-8")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        _root_secret().encode("utf-8")
    )


def _b32_tag(key: bytes, data: str, length: int) -> str:
    digest = hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()
    return base64.b32encode(digest).decode("ascii")[:length]


def _random_b32(length: int) -> str:
    return "".join(secrets.choice(_B32_ALPHABET) for _ in range(length))


# ── case id ─────────────────────────────────────────────────────────────────

def mint_case_id() -> str:
    """A fresh canonical case id: CASE_ID_LEN uppercase base32 characters."""
    return _random_b32(CASE_ID_LEN)


def parse_case_id(text: str) -> Optional[str]:
    """The canonical id behind any spelling a human or client hands back, or None.

    Accepts the display form with its dash and the bare form without it, in any
    case, with surrounding whitespace. Anything else is None, never an exception.
    """
    if not isinstance(text, str):
        return None
    match = _CASE_ID_RE.match(text.strip().upper())
    if match is None:
        return None
    return match.group(1) + match.group(2)


def format_case_id(case_id: str) -> str:
    """The display form, ``XXXXX-XXXXX``. A string that is not a case id comes back
    untouched, so a display path never raises over a value it only shows."""
    canonical = parse_case_id(case_id)
    if canonical is None:
        return case_id
    return f"{canonical[:5]}-{canonical[5:]}"


def _require_case_id(case_id: str) -> str:
    canonical = parse_case_id(case_id)
    if canonical is None:
        raise ValueError(f"not a case id: {case_id!r}")
    return canonical


# ── Message-ID anchor ───────────────────────────────────────────────────────

def mint_message_id(user_scope_id: str, account_id: str, case_id: str, domain: str) -> str:
    """A Message-ID that proves which case it belongs to: ``<CASEID.NONCE.TAG@domain>``.

    The nonce makes every id unique per mail (a case sends many); the tag covers
    the case id AND the nonce, so neither can be moved to another id. The domain
    is the sending account's; a Message-ID without one is not one, so an empty
    domain is a ValueError rather than a malformed header on the wire.
    """
    canonical = _require_case_id(case_id)
    host = (domain or "").strip().strip("<>").strip()
    if not host or "@" in host or any(c.isspace() for c in host):
        raise ValueError(f"not a Message-ID domain: {domain!r}")
    nonce = _random_b32(NONCE_LEN)
    tag = _b32_tag(case_key(user_scope_id, account_id), f"{canonical}.{nonce}", TAG_LEN)
    return f"<{canonical}.{nonce}.{tag}@{host}>"


def verify_anchor(user_scope_id: str, account_id: str, message_id: str) -> Optional[str]:
    """The case id a Message-ID carries, or None when it is not ours.

    Tolerant of surrounding whitespace, missing angle brackets and case folding.
    The shape is checked BEFORE the key is derived: a foreign id, which is what
    nearly every inbound Message-ID is, never touches the keyring at all. The tag is
    compared in constant time; a wrong scope or account derives a different key and
    therefore a different tag, which is the whole cross-user refusal.
    """
    if not isinstance(message_id, str):
        return None
    local, sep, _domain = message_id.strip().strip("<>").strip().rpartition("@")
    if not sep:
        return None
    match = ANCHOR_RE.match(local)
    if match is None:
        return None
    case_id, nonce, tag = (part.upper() for part in match.groups())
    expected = _b32_tag(case_key(user_scope_id, account_id), f"{case_id}.{nonce}", TAG_LEN)
    if not hmac.compare_digest(expected, tag):
        return None
    return case_id


# ── subject tag ─────────────────────────────────────────────────────────────

def _check(user_scope_id: str, account_id: str, canonical: str) -> str:
    return _b32_tag(case_key(user_scope_id, account_id), canonical, CHECK_LEN)


def subject_tag(user_scope_id: str, account_id: str, case_id: str) -> str:
    """The tag to put in an outgoing subject: ``[VAF#XXXXX-XXXXX-CCCC]``."""
    canonical = _require_case_id(case_id)
    return f"[VAF#{format_case_id(canonical)}-{_check(user_scope_id, account_id, canonical)}]"


def strip_reply_prefixes(subject: str) -> str:
    """The subject with every leading reply or forward prefix removed.

    ``Re: Re: AW: WG: Fwd: hello`` and ``RE[2]: hello`` both become ``hello``;
    ``[Fwd: hello]`` loses the bracket the forwarding client wrapped it in as well;
    ``Rescue plan`` stays what it is. Only prefixes are touched, never the tag: a
    ``[VAF#...]`` bracket is not a prefix and survives.
    """
    if not isinstance(subject, str):
        return ""
    text = subject.strip()
    while True:
        match = _PREFIX_RE.match(text)
        if match is None:
            return text
        rest = text[match.end():]
        opened = match.group("open")
        if opened and rest.endswith(_CLOSING[opened]):
            rest = rest[:-1]
        text = rest.strip()


def find_subject_tags(subject: str) -> List[Tuple[str, str]]:
    """Every ``(case_id, check)`` a subject carries, canonical and in order.

    Found is not verified: the check is handed back for `verify_subject_tag` so the
    caller can tell "no tag" from "a tag that is not ours" and react to each.
    """
    if not isinstance(subject, str):
        return []
    found: List[Tuple[str, str]] = []
    for match in SUBJECT_TAG_RE.finditer(strip_reply_prefixes(subject)):
        head, tail, check = match.groups()
        found.append(((head + tail).upper(), check.upper()))
    return found


def verify_subject_tag(user_scope_id: str, account_id: str, case_id: str, check: str) -> bool:
    """Whether ``check`` is the check this scope and account computes for ``case_id``."""
    canonical = parse_case_id(case_id)
    if canonical is None or not isinstance(check, str):
        return False
    given = check.strip().upper()
    if _CHECK_RE.match(given) is None:
        return False
    return hmac.compare_digest(_check(user_scope_id, account_id, canonical), given)


# ── plus address ────────────────────────────────────────────────────────────

def plus_address(local_part: str, domain: str, case_id: str) -> str:
    """The Reply-To that names the case in the address: ``local+vaf-caseid@domain``.

    The case id is lowercased because that is how addresses are usually shown and
    compared; the reader is case-insensitive either way.
    """
    canonical = _require_case_id(case_id)
    local = (local_part or "").strip()
    host = (domain or "").strip().lstrip("@").strip()
    if not local or "@" in local or not host or "@" in host:
        raise ValueError(f"not a mailbox: {local_part!r} at {domain!r}")
    return f"{local}+vaf-{canonical.lower()}@{host}"


def _mailboxes(entry: str) -> List[str]:
    """The addresses in one header value, leniently: a bad mailbox in the list must
    not hide a good one beside it, which the strict parser of newer Pythons does by
    returning nothing at all for the whole value."""
    try:
        pairs = getaddresses([entry], strict=False)
    except TypeError:  # a Python without the keyword parses leniently anyway
        pairs = getaddresses([entry])
    return [addr for _name, addr in pairs if addr]


def find_plus_addresses(addresses: Iterable[str], domain: str) -> List[str]:
    """The case ids the plus form addresses at OUR domain, canonical, in order, once each.

    ``addresses`` are header values as found (a To line, a Cc line, a Delivered-To
    line, each possibly listing several mailboxes). Another domain's ``+vaf-`` tag
    is not ours and is ignored; so is anything without the ``vaf-`` prefix.
    """
    ours = (domain or "").strip().lstrip("@").strip().lower()
    if not ours:
        return []
    found: List[str] = []
    for entry in addresses or ():
        if not isinstance(entry, str):
            continue
        for mailbox in _mailboxes(entry):
            match = PLUS_ADDRESS_RE.match(mailbox.strip())
            if match is None or match.group("domain").lower() != ours:
                continue
            case_id = match.group("case").upper()
            if case_id not in found:
                found.append(case_id)
    return found
