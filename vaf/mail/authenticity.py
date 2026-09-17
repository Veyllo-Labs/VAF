# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Sender authentication for the mail client: a verdict from the provider's own
Authentication-Results header (RFC 8601) with DMARC-style alignment (RFC 7489 3.1).

VAF reads mailboxes over IMAP; it is not the MTA and never evaluates SPF, DKIM or
DMARC itself. It reads what the account's OWN provider wrote and trusts nothing else:

- Only the header written by the trusted authserv-id counts. Every other copy is
  untrusted and ignored (RFC 8601 sections 2.5, 4.1 and 7.1: a downstream reader
  must only use results from an authserv-id it knows, because anyone upstream can
  write the header). The TOPMOST matching header is taken, so a forged copy below a
  genuine one is never read. The residual assumption is the one RFC 8601 section 4.1
  places on the provider: an MTA removes or renames Authentication-Results headers
  carrying its own authserv-id that it did not write. A forged header with the
  trusted id and NO genuine one above it IS read; only the provider can prevent that.
- Microsoft 365 / outlook.com writes the header WITHOUT an authserv-id: it starts
  with "spf=", a stray tenant-domain token may follow the spf clause, DMARC carries
  "action=" and a "compauth=<result> reason=<nnn>" pair is usually present. With
  auth_profile "microsoft" the topmost id-less header is the trusted one.
- compauth=pass is NOT a verification result: Microsoft grants it on implicit
  signals (PTR alignment, sender history, DNS timeouts). It is recorded, never
  verifying. dmarc=bestguesspass is not a pass either.
- Alignment is decided without a public suffix list and fails safe: a domain is
  aligned with the From domain when it equals it or is an ANCESTOR of it. A
  signature by a CHILD of the From domain is deliberately not aligned, which is
  stricter than DMARC relaxed mode (RFC 7489 3.1.1 and 3.1.2 would accept either
  direction under the same organizational domain, and organizational domains need
  a suffix list to compute).
- ARC (RFC 8617) is recorded, never a pass by itself. Named boundary: VAF trusts
  the provider's dmarc verdict, which already accounts for ARC overrides where the
  provider applies them; VAF does not evaluate seals.
- DKIM signs the From header (display name included) and usually Reply-To, so both
  are integrity-protected by the signer, but no mechanism judges their MEANING. The
  reply_to_mismatch and own_domain_spoof flags exist for that reason, next to the
  state.

Everything here is pure: no IO, no Config, and verdict() never raises (the parser's
boundary rule: one malformed message must never abort a folder sync).
"""
import re
from collections import Counter
from dataclasses import dataclass
from email.utils import getaddresses
from typing import Iterable, List, Optional, Tuple

from vaf.mail.parser import ParsedMessage

STATES = ("verified", "via", "unverified", "unknown")
PROFILES = ("rfc8601", "microsoft")
_MICROSOFT_LEADS = ("spf=", "dkim=", "dmarc=")
_RESULT_METHODS = ("spf", "dkim", "dmarc", "arc", "compauth")
_METHOD_RE = re.compile(r"^([a-z][a-z0-9_-]*)(?:/[0-9]+)?=(\S*)$")
_ARC_INSTANCE_RE = re.compile(r"^i=\d+$")


@dataclass(frozen=True)
class AuthResults:
    """One parsed Authentication-Results header (RFC 8601 section 2.2).

    Result tokens are lowercased ("pass", "fail", "softfail", "neutral", "none",
    "temperror", "permerror", "" when the method is absent). Domains are lowercased
    as given (IDNA is not normalised). dkim / dkim_domain are the FIRST dkim=pass
    clause, else the first dkim clause; dkim_pass_domains keeps every dkim=pass
    domain in header order because RFC 7489 3.1.1 lets any aligned signature pass.
    authserv_id is "" for the Microsoft id-less form, whose profile is "microsoft"."""
    authserv_id: str = ""
    spf: str = ""
    spf_domain: str = ""            # smtp.mailfrom domain (smtp.helo when mailfrom is absent)
    dkim: str = ""
    dkim_domain: str = ""           # header.d, else the domain of header.i
    dmarc: str = ""
    dmarc_domain: str = ""          # header.from
    arc: str = ""
    compauth: str = ""
    compauth_reason: str = ""
    profile: str = "rfc8601"
    raw: str = ""
    dkim_pass_domains: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthVerdict:
    """The verdict for one message. state is one of STATES; source says which header
    decided it ("provider" for an authserv-id match, "microsoft" for the id-less
    form, "none" when nothing was trusted). topmost_authserv_id is the id of the
    topmost header whatever its trust, for learn_authserv_id. flags are reported as
    found; the caller decides what each one caps. reasons are machine tokens."""
    state: str = "unknown"
    source: str = "none"
    authserv_id: str = ""
    topmost_authserv_id: str = ""
    from_domain: str = ""
    spf: str = ""
    spf_domain: str = ""
    dkim: str = ""
    dkim_domain: str = ""
    dmarc: str = ""
    arc: str = ""
    compauth: str = ""
    aligned_by: str = ""            # dmarc | dkim | spf | ""
    via_domain: str = ""            # for state via: the domain that did pass
    flags: Tuple[str, ...] = ()     # reply_to_mismatch, own_domain_spoof, no_message_id, dmarc_fail, multiple_from
    reasons: Tuple[str, ...] = ()


def _strip_comments(value: str) -> str:
    """Remove RFC 5322 comments (nested parentheses, section 3.2.2) outside quoted
    strings; a backslash escapes the next character in both."""
    out: List[str] = []
    depth = 0
    in_quote = False
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n and (in_quote or depth):
            if in_quote:
                out.append(ch)
                out.append(value[i + 1])
            i += 2
            continue
        if in_quote:
            out.append(ch)
            if ch == '"':
                in_quote = False
        elif depth:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        elif ch == "(":
            depth += 1
        else:
            out.append(ch)
            if ch == '"':
                in_quote = True
        i += 1
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _unquote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        token = token[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return token.strip("<>").strip()


def _domain_token(value: str) -> str:
    """A domain from a property value: an address gives the part after the @, a
    bare domain is taken as is, trailing dots and Microsoft's "none" drop."""
    v = _unquote(value).lower()
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    v = v.strip().rstrip(".")
    if v in ("none", "-"):
        return ""
    return v


def _is_id_less(first_segment: str) -> bool:
    tokens = first_segment.split()
    return bool(tokens) and bool(_METHOD_RE.match(tokens[0].lower()))


def _starts_like_microsoft(cleaned: str) -> bool:
    return cleaned.lower().startswith(_MICROSOFT_LEADS)


def parse_auth_results(value: str) -> AuthResults:
    """Parse one Authentication-Results value (RFC 8601 section 2.2), tolerantly.

    Comments are removed first; the authserv-id is the first token before the first
    ";" (a version number after it, "mx.google.com 1;", and an ARC instance in
    front of it, "i=1; mx.google.com;", are tolerated); methods are ";"-separated
    "method=result" clauses followed by "ptype.property=value" pairs; a segment
    without "=" (Microsoft's stray tenant-domain token, or "none") is skipped; an
    unknown method is ignored. Never raises."""
    raw = str(value or "")
    cleaned = _strip_comments(raw)
    segments = [s.strip() for s in cleaned.split(";")]
    authserv_id = ""
    profile = "rfc8601"
    if segments and _ARC_INSTANCE_RE.match(segments[0].lower()):
        segments = segments[1:]
    if segments and segments[0] and not _is_id_less(segments[0]):
        authserv_id = _unquote(segments[0].split()[0]).lower().rstrip(".")
        segments = segments[1:]
    elif segments and segments[0]:
        profile = "microsoft"

    results = {}
    first_dkim: Optional[Tuple[str, str]] = None
    first_dkim_pass: Optional[Tuple[str, str]] = None
    dkim_pass_domains: List[str] = []
    for seg in segments:
        if not seg:
            continue
        tokens = seg.split()
        m = _METHOD_RE.match(tokens[0].lower())
        if not m:
            continue
        method, result = m.group(1), _unquote(m.group(2)).lower()
        props = {}
        for tok in tokens[1:]:
            if "=" not in tok:
                continue
            key, _eq, val = tok.partition("=")
            props[key.strip().lower()] = val
        if method == "dkim":
            domain = _domain_token(props.get("header.d", "")) or _domain_token(props.get("header.i", ""))
            if first_dkim is None:
                first_dkim = (result, domain)
            if result == "pass":
                if first_dkim_pass is None:
                    first_dkim_pass = (result, domain)
                if domain and domain not in dkim_pass_domains:
                    dkim_pass_domains.append(domain)
            continue
        if method in results:
            continue  # the first clause of a method wins (dkim is the only repeated one in practice)
        if method == "spf":
            domain = _domain_token(props.get("smtp.mailfrom", "")) or _domain_token(props.get("smtp.helo", ""))
            results["spf"] = (result, domain)
        elif method == "dmarc":
            results["dmarc"] = (result, _domain_token(props.get("header.from", "")))
        elif method == "arc":
            results["arc"] = (result, "")
        elif method == "compauth":
            results["compauth"] = (result, _unquote(props.get("reason", "")))
    dkim = first_dkim_pass or first_dkim or ("", "")
    spf = results.get("spf", ("", ""))
    dmarc = results.get("dmarc", ("", ""))
    return AuthResults(
        authserv_id=authserv_id,
        spf=spf[0], spf_domain=spf[1],
        dkim=dkim[0], dkim_domain=dkim[1],
        dmarc=dmarc[0], dmarc_domain=dmarc[1],
        arc=results.get("arc", ("", ""))[0],
        compauth=results.get("compauth", ("", ""))[0],
        compauth_reason=results.get("compauth", ("", ""))[1],
        profile=profile,
        raw=raw,
        dkim_pass_domains=tuple(dkim_pass_domains),
    )


def matches_authserv(authserv_id: str, trusted: str) -> bool:
    """True when authserv_id is the trusted id exactly or a dot-suffix child of it:
    "mx4.messagingengine.com" matches "messagingengine.com" and "*.messagingengine.com"
    alike (the wildcard form is accepted for readability and means the same). Case
    does not matter, a trailing dot is ignored, and "" never matches on either side."""
    a = str(authserv_id or "").strip().lower().rstrip(".")
    t = str(trusted or "").strip().lower().rstrip(".")
    if t.startswith("*."):
        t = t[2:]
    if not a or not t:
        return False
    return a == t or a.endswith("." + t)


def trusted_results(parsed: ParsedMessage, *, trusted_authserv_id: str,
                    auth_profile: str = "rfc8601") -> Optional[AuthResults]:
    """The one Authentication-Results header VAF believes, or None.

    Walks the message's headers topmost first and returns the first whose authserv-id
    matches trusted_authserv_id (matches_authserv). With auth_profile "microsoft" the
    topmost id-less header starting with spf=, dkim= or dmarc= is trusted as well.
    Topmost-only is the forgery defence of RFC 8601 section 4.1: whatever the
    provider wrote sits above anything that arrived with the message."""
    profile = str(auth_profile or "rfc8601").strip().lower()
    trusted = str(trusted_authserv_id or "").strip()
    if not trusted and profile != "microsoft":
        return None
    for value in list(getattr(parsed, "auth_results", None) or []):
        ar = parse_auth_results(value)
        if trusted and matches_authserv(ar.authserv_id, trusted):
            return ar
        if profile == "microsoft" and ar.profile == "microsoft" and _starts_like_microsoft(_strip_comments(ar.raw)):
            return ar
    return None


def domain_of(addr_or_header: str) -> str:
    """The domain of the first addr-spec in an address header, lowercased, without a
    trailing dot; "" when the header names no address."""
    s = str(addr_or_header or "").strip()
    if not s:
        return ""
    try:
        pairs = getaddresses([s])
    except Exception:
        pairs = []
    for _name, addr in pairs:
        addr = (addr or "").strip()
        if "@" in addr:
            domain = addr.rsplit("@", 1)[1].strip().lower().rstrip(".")
            if domain:
                return domain
    m = re.search(r"@([A-Za-z0-9.\-\[\]:]+)", s)
    return m.group(1).lower().rstrip(".") if m else ""


def aligned(domain: str, from_domain: str) -> bool:
    """DMARC-style alignment without a public suffix list, failing safe: domain is
    aligned when it equals from_domain or is an ancestor of it. A child of the From
    domain is not aligned (stricter than RFC 7489 relaxed mode). Lowercased, IDNA
    as given, "" never aligns."""
    d = str(domain or "").strip().lower().rstrip(".")
    f = str(from_domain or "").strip().lower().rstrip(".")
    if not d or not f:
        return False
    return f == d or f.endswith("." + d)


def _address_count(header: str) -> int:
    try:
        return sum(1 for _n, a in getaddresses([str(header or "")]) if a and "@" in a)
    except Exception:
        return 0


def _decide(trusted: AuthResults, from_domain: str) -> Tuple[str, str, str, str, List[str]]:
    """(state, aligned_by, via_domain, dkim_domain, reasons) from one trusted header.
    Order: dmarc=pass, an aligned dkim=pass, an aligned spf=pass, an unaligned pass
    of either ("via"), else unverified. dmarc=pass is only accepted when its
    header.from is the From domain VAF sees (or an ancestor of it): a provider
    evaluates DMARC on the RFC5322.From it saw, and a mismatch means the header does
    not describe this From."""
    reasons: List[str] = []
    if not from_domain:
        reasons.append("no from domain")
    if trusted.dmarc == "pass":
        if not trusted.dmarc_domain or aligned(trusted.dmarc_domain, from_domain):
            reasons.append("dmarc=pass")
            return "verified", "dmarc", "", trusted.dkim_domain, reasons
        reasons.append(f"dmarc=pass header.from={trusted.dmarc_domain} not aligned")
    elif trusted.dmarc:
        reasons.append(f"dmarc={trusted.dmarc}")
    for d in trusted.dkim_pass_domains:
        if aligned(d, from_domain):
            reasons.append(f"dkim=pass header.d={d} aligned")
            return "verified", "dkim", "", d, reasons
    if trusted.spf == "pass" and aligned(trusted.spf_domain, from_domain):
        reasons.append(f"spf=pass smtp.mailfrom={trusted.spf_domain} aligned")
        return "verified", "spf", "", trusted.dkim_domain, reasons
    if trusted.dkim_pass_domains:
        d = trusted.dkim_pass_domains[0]
        reasons.append(f"dkim=pass header.d={d} not aligned")
        return "via", "", d, d, reasons
    if trusted.spf == "pass" and trusted.spf_domain:
        reasons.append(f"spf=pass smtp.mailfrom={trusted.spf_domain} not aligned")
        return "via", "", trusted.spf_domain, trusted.dkim_domain, reasons
    if trusted.dkim == "pass":
        reasons.append("dkim=pass without header.d")
    elif trusted.dkim:
        reasons.append(f"dkim={trusted.dkim}")
    if trusted.spf == "pass":
        reasons.append("spf=pass without smtp.mailfrom")
    elif trusted.spf:
        reasons.append(f"spf={trusted.spf}")
    if not (trusted.dmarc or trusted.dkim or trusted.spf):
        reasons.append("no results")
    return "unverified", "", "", trusted.dkim_domain, reasons


def verdict(parsed: ParsedMessage, *, trusted_authserv_id: str, auth_profile: str = "rfc8601",
            own_domains: Iterable[str] = ()) -> AuthVerdict:
    """The authentication verdict for one parsed message. Never raises: an internal
    error yields state "unknown" with an "error:<type>" reason, the way the parser
    records a defect instead of aborting a sync.

    States: "verified" (dmarc=pass, or an aligned dkim=pass, or an aligned spf=pass in
    the trusted header), "via" (a dkim=pass or spf=pass by an unrelated domain, named
    in via_domain), "unverified" (a trusted header without a pass), "unknown" (no
    trusted header). Flags: reply_to_mismatch (Reply-To names a domain outside the
    From domain's tree, in either direction), own_domain_spoof (the From domain is one
    of own_domains or below one, and the state is not verified), no_message_id,
    dmarc_fail, multiple_from (two or more addr-specs in From, which RFC 7489 6.6.1
    treats as suspect)."""
    try:
        return _verdict(parsed, trusted_authserv_id=trusted_authserv_id, auth_profile=auth_profile,
                        own_domains=own_domains)
    except Exception as e:  # the boundary rule: a verdict failure is a defect, never an abort
        return AuthVerdict(state="unknown", source="none", reasons=(f"error:{type(e).__name__}",))


def _verdict(parsed: ParsedMessage, *, trusted_authserv_id: str, auth_profile: str,
             own_domains: Iterable[str]) -> AuthVerdict:
    from_header = str(getattr(parsed, "from_addr", "") or "")
    from_domain = domain_of(from_header)
    headers = list(getattr(parsed, "auth_results", None) or [])
    topmost_id = parse_auth_results(headers[0]).authserv_id if headers else ""
    trusted = trusted_results(parsed, trusted_authserv_id=trusted_authserv_id, auth_profile=auth_profile)
    profile = str(auth_profile or "rfc8601").strip().lower()

    if trusted is None:
        if not str(trusted_authserv_id or "").strip() and profile != "microsoft":
            reasons: List[str] = ["no trusted authserv-id"]
        elif not headers:
            reasons = ["no authentication-results"]
        else:
            reasons = ["no matching authentication-results"]
        state, source, aligned_by, via_domain = "unknown", "none", "", ""
        spf = spf_domain = dkim = dkim_domain = dmarc = arc = compauth = ""
    else:
        source = "microsoft" if trusted.profile == "microsoft" else "provider"
        state, aligned_by, via_domain, dkim_domain, reasons = _decide(trusted, from_domain)
        spf, spf_domain, dkim = trusted.spf, trusted.spf_domain, trusted.dkim
        dmarc, arc, compauth = trusted.dmarc, trusted.arc, trusted.compauth
        if arc:
            reasons.append(f"arc={arc}")
        elif getattr(parsed, "arc_auth_results", None):
            reasons.append("arc-authentication-results present")
        if compauth:
            reasons.append(f"compauth={compauth}" + (f" reason={trusted.compauth_reason}" if trusted.compauth_reason else ""))

    flags: List[str] = []
    reply_domain = domain_of(str(getattr(parsed, "reply_to", "") or ""))
    if reply_domain and from_domain and not (aligned(reply_domain, from_domain) or aligned(from_domain, reply_domain)):
        flags.append("reply_to_mismatch")
    own = [str(d or "").strip().lower().rstrip(".") for d in (own_domains or ())]
    if from_domain and state != "verified" and any(aligned(d, from_domain) for d in own if d):
        flags.append("own_domain_spoof")
    if not str(getattr(parsed, "message_id", "") or "").strip():
        flags.append("no_message_id")
    if trusted is not None and trusted.dmarc == "fail":
        flags.append("dmarc_fail")
    if _address_count(from_header) >= 2:
        flags.append("multiple_from")

    return AuthVerdict(
        state=state, source=source,
        authserv_id=trusted.authserv_id if trusted is not None else "",
        topmost_authserv_id=topmost_id, from_domain=from_domain,
        spf=spf, spf_domain=spf_domain, dkim=dkim, dkim_domain=dkim_domain,
        dmarc=dmarc, arc=arc, compauth=compauth,
        aligned_by=aligned_by, via_domain=via_domain,
        flags=tuple(flags), reasons=tuple(reasons),
    )


def learn_authserv_id(topmost_ids: Iterable[str], *, min_samples: int = 3,
                      min_share: float = 0.9) -> Tuple[str, int, int]:
    """(id, count, total): the most common non-empty topmost authserv-id when it has at
    least min_samples samples and at least min_share of all non-empty samples, else
    ("", count, total). The Microsoft id-less form contributes "" and is therefore
    invisible here; looks_microsoft() is the caller's test for that profile."""
    counts: Counter = Counter()
    for raw in topmost_ids or ():
        s = str(raw or "").strip().lower().rstrip(".")
        if s:
            counts[s] += 1
    total = sum(counts.values())
    if not counts:
        return "", 0, 0
    top, count = counts.most_common(1)[0]
    if count >= max(1, int(min_samples)) and count >= float(min_share) * total:
        return top, count, total
    return "", count, total


def looks_microsoft(values: Iterable[str], *, min_samples: int = 3) -> bool:
    """True when at least min_samples of the raw topmost Authentication-Results values
    are the Microsoft id-less form (starting with spf=, dkim= or dmarc=) and none of
    the non-empty values carries an authserv-id."""
    hits = 0
    for raw in values or ():
        s = str(raw or "").strip()
        if not s:
            continue
        ar = parse_auth_results(s)
        if ar.authserv_id:
            return False
        if ar.profile == "microsoft" and _starts_like_microsoft(_strip_comments(s)):
            hits += 1
    return hits >= max(1, int(min_samples))
