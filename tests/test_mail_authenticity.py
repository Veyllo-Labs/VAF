# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""vaf.mail.authenticity: the verdict comes from the provider's own Authentication-Results
header (RFC 8601) and nothing else, alignment is DMARC-style against the From domain
(RFC 7489 3.1) and fails safe, and the Microsoft id-less form is read only under its
profile. Fixtures are real-shaped headers from Gmail, Fastmail, GMX and Microsoft 365.

Mutation proofs (each verified by editing the module and reverting):
- test_microsoft_compauth_pass_with_bestguesspass_does_not_verify turns red when _decide
  accepts dmarc "bestguesspass" next to "pass", or treats compauth=pass as a pass.
- test_forged_header_below_the_genuine_one_is_ignored turns red when trusted_results
  walks the headers bottom-up (reversed) instead of topmost first.
- test_ancestor_domain_is_aligned_child_is_not turns red in its first half when the
  `f.endswith("." + d)` branch of aligned() is removed, and in its second half when a
  symmetric `d.endswith("." + f)` branch is added.
- test_unaligned_spf_pass_is_via_not_verified turns red when the
  `and aligned(trusted.spf_domain, from_domain)` guard on the spf branch of _decide is
  removed.
- test_dmarc_pass_without_header_from_needs_a_from_domain turns red when the
  `from_domain and` guard on the dmarc branch of _decide is dropped (first three
  checks), and when a header.from-less dmarc=pass is refused for every From (last
  check)."""
from types import SimpleNamespace

from vaf.mail.authenticity import (
    AuthResults,
    AuthVerdict,
    STATES,
    aligned,
    domain_of,
    learn_authserv_id,
    looks_microsoft,
    matches_authserv,
    parse_auth_results,
    trusted_results,
    verdict,
)
from vaf.mail.parser import ParsedMessage, parse_message

GMAIL = ("mx.google.com; dkim=pass header.i=@example.org header.s=20230601 header.b=abc; "
         "spf=pass (google.com: domain of alice@example.org designates 203.0.113.5 as permitted sender) "
         "smtp.mailfrom=alice@example.org; dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=example.org")
FASTMAIL = ("mx4.messagingengine.com; arc=none (no signatures found); "
            "dkim=pass (2048-bit rsa key sha256) header.d=example.org header.i=@example.org header.b=xyz; "
            "dmarc=pass policy.published-domain-policy=reject policy.applied-disposition=none "
            "(p=reject,d=none,d.eval=none) header.from=example.org; "
            "iprev=pass smtp.remote-ip=203.0.113.5 (mail.example.org); "
            "spf=pass smtp.mailfrom=alice@example.org smtp.helo=mail.example.org; "
            "x-ptr=pass smtp.helo=mail.example.org policy.ptr=mail.example.org")
GMX = "gmx.net; dkim=pass header.d=example.org; spf=pass smtp.mailfrom=example.org"
MICROSOFT = ("spf=pass (sender IP is 10.2.3.4) smtp.mailfrom=fabrikam.com; contoso.com; "
             "dkim=none (message not signed) header.d=none; contoso.com; "
             "dmarc=bestguesspass action=none header.from=fabrikam.com; compauth=pass reason=109")
MICROSOFT_PASS = ("spf=pass (sender IP is 203.0.113.5) smtp.mailfrom=example.org; "
                  "dkim=pass (signature was verified) header.d=example.org; "
                  "dmarc=pass action=none header.from=example.org; compauth=pass reason=100")
FORGED = "mx.google.com; dkim=pass header.d=bank.example; spf=pass smtp.mailfrom=bank.example; dmarc=pass header.from=bank.example"


def _msg(from_addr="Alice <alice@example.org>", auth_results=(), message_id="<a1@example.org>", **kw):
    p = ParsedMessage(from_addr=from_addr, message_id=message_id, auth_results=list(auth_results))
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def _mail(headers: str) -> bytes:
    return (headers.strip("\n") + "\n\nhello\n").encode("utf-8")


# ---- parsing --------------------------------------------------------------------

def test_gmail_header_parses_every_field():
    ar = parse_auth_results(GMAIL)
    assert ar.authserv_id == "mx.google.com" and ar.profile == "rfc8601"
    assert (ar.dkim, ar.dkim_domain) == ("pass", "example.org"), "header.i=@example.org gives the domain after the @"
    assert (ar.spf, ar.spf_domain) == ("pass", "example.org"), "smtp.mailfrom is an address; its domain is taken"
    assert (ar.dmarc, ar.dmarc_domain) == ("pass", "example.org")
    assert ar.arc == "" and ar.compauth == "" and ar.raw == GMAIL
    assert ar.dkim_pass_domains == ("example.org",)


def test_fastmail_header_parses_around_unknown_methods_and_comments():
    ar = parse_auth_results(FASTMAIL)
    assert ar.authserv_id == "mx4.messagingengine.com"
    assert (ar.dkim, ar.dkim_domain) == ("pass", "example.org"), "header.d wins over header.i"
    assert (ar.spf, ar.spf_domain, ar.dmarc, ar.arc) == ("pass", "example.org", "pass", "none")


def test_microsoft_header_parses_id_less_with_stray_tokens():
    ar = parse_auth_results(MICROSOFT)
    assert ar.authserv_id == "" and ar.profile == "microsoft"
    assert (ar.spf, ar.spf_domain) == ("pass", "fabrikam.com")
    assert (ar.dkim, ar.dkim_domain) == ("none", ""), "header.d=none is not a domain"
    assert (ar.dmarc, ar.dmarc_domain) == ("bestguesspass", "fabrikam.com")
    assert (ar.compauth, ar.compauth_reason) == ("pass", "109")


def test_parser_tolerates_versions_nested_comments_quotes_and_arc_instances():
    ar = parse_auth_results('mx.google.com 1; dkim=pass (a (nested) comment; with a semicolon) header.d="example.org"; '
                            'spf=pass smtp.mailfrom=<alice@example.org>')
    assert ar.authserv_id == "mx.google.com" and ar.dkim_domain == "example.org" and ar.spf_domain == "example.org"
    ar = parse_auth_results("i=1; mx.google.com; dkim=pass header.i=@example.org")
    assert ar.authserv_id == "mx.google.com" and ar.dkim == "pass", "an ARC-Authentication-Results instance tag is skipped"
    ar = parse_auth_results("Example.COM.; none")
    assert ar.authserv_id == "example.com" and ar.dkim == "" and ar.spf == "" and ar.profile == "rfc8601"
    ar = parse_auth_results("mx.example.net; dkim/1=pass header.d=example.org; spf=softfail smtp.mailfrom=Alice@Example.ORG")
    assert ar.dkim == "pass" and (ar.spf, ar.spf_domain) == ("softfail", "example.org")
    assert parse_auth_results("") == AuthResults()
    assert parse_auth_results(None) == AuthResults()


def test_a_folded_header_read_through_the_parser_is_the_same_result():
    raw = _mail("From: Alice <alice@example.org>\nSubject: x\nMessage-ID: <f@example.org>\n"
                "Authentication-Results: mx.example.net;\n\tdkim=pass header.d=example.org;\n\tspf=pass smtp.mailfrom=example.org")
    p = parse_message(raw)
    ar = parse_auth_results(p.auth_results[0])
    assert (ar.authserv_id, ar.dkim, ar.dkim_domain, ar.spf) == ("mx.example.net", "pass", "example.org", "pass")


def test_first_dkim_pass_wins_over_later_and_over_an_earlier_failure():
    ar = parse_auth_results("mx.example.net; dkim=fail header.d=lists.example; dkim=pass header.d=example.org; dkim=pass header.d=other.example")
    assert (ar.dkim, ar.dkim_domain) == ("pass", "example.org")
    assert ar.dkim_pass_domains == ("example.org", "other.example")
    ar = parse_auth_results("mx.example.net; dkim=fail header.d=lists.example; dkim=neutral header.d=x.example")
    assert (ar.dkim, ar.dkim_domain) == ("fail", "lists.example"), "without a pass the first dkim clause is reported"


# ---- matching, domains, alignment ----------------------------------------------

def test_matches_authserv_exact_suffix_wildcard_and_never_empty():
    assert matches_authserv("mx.google.com", "mx.google.com")
    assert matches_authserv("MX.Google.COM", "mx.google.com")
    assert matches_authserv("mx4.messagingengine.com", "messagingengine.com")
    assert matches_authserv("mx4.messagingengine.com", "*.messagingengine.com")
    assert matches_authserv("messagingengine.com", "*.messagingengine.com")
    assert not matches_authserv("notmessagingengine.com", "messagingengine.com"), "a suffix needs the dot"
    assert not matches_authserv("messagingengine.com.evil.example", "messagingengine.com")
    assert not matches_authserv("", "mx.google.com") and not matches_authserv("mx.google.com", "")
    assert not matches_authserv("", "")


def test_domain_of_takes_the_first_addr_spec():
    assert domain_of("Alice <Alice@Example.ORG>") == "example.org"
    assert domain_of("alice@example.org, bob@other.example") == "example.org"
    assert domain_of("alice@example.org.") == "example.org"
    assert domain_of("Nobody") == "" and domain_of("") == "" and domain_of(None) == ""


def test_ancestor_domain_is_aligned_child_is_not():
    assert aligned("example.org", "mail.example.org"), "the signer is an ancestor of the From domain"
    assert aligned("example.org", "example.org")
    assert not aligned("mail.example.org", "example.org"), "a child of the From domain is deliberately not aligned"
    assert not aligned("ample.org", "example.org"), "a suffix without the dot is a different domain"
    assert not aligned("", "example.org") and not aligned("example.org", "")
    assert aligned("Example.ORG", "mail.example.org.")


# ---- trusted header selection --------------------------------------------------

def test_gmail_dmarc_pass_is_verified_end_to_end():
    raw = _mail("From: Alice <alice@example.org>\nReply-To: alice@example.org\nSubject: Hi\nMessage-ID: <a2@example.org>\n"
                "Authentication-Results: " + GMAIL)
    v = verdict(parse_message(raw), trusted_authserv_id="mx.google.com")
    assert isinstance(v, AuthVerdict) and v.state in STATES
    assert (v.state, v.source, v.aligned_by, v.authserv_id) == ("verified", "provider", "dmarc", "mx.google.com")
    assert v.from_domain == "example.org" and v.topmost_authserv_id == "mx.google.com"
    assert v.flags == () and "dmarc=pass" in v.reasons
    assert (v.spf, v.spf_domain, v.dkim, v.dkim_domain, v.dmarc) == ("pass", "example.org", "pass", "example.org", "pass")


def test_fastmail_matches_a_suffix_or_wildcard_trusted_id():
    p = _msg(auth_results=[FASTMAIL])
    for trusted in ("messagingengine.com", "*.messagingengine.com", "mx4.messagingengine.com"):
        v = verdict(p, trusted_authserv_id=trusted)
        assert (v.state, v.aligned_by, v.authserv_id) == ("verified", "dmarc", "mx4.messagingengine.com"), trusted
    assert verdict(p, trusted_authserv_id="mx5.messagingengine.com").state == "unknown"


def test_gmx_without_dmarc_verifies_by_aligned_dkim():
    v = verdict(_msg(auth_results=[GMX]), trusted_authserv_id="gmx.net")
    assert (v.state, v.aligned_by, v.dkim_domain) == ("verified", "dkim", "example.org")
    assert "dkim=pass header.d=example.org aligned" in v.reasons


def test_aligned_spf_pass_verifies_when_dkim_is_missing():
    v = verdict(_msg(auth_results=["gmx.net; dkim=none; spf=pass smtp.mailfrom=bounces@example.org"]),
                trusted_authserv_id="gmx.net", auth_profile="rfc8601")
    assert v.state == "verified" and v.aligned_by == "spf" and v.spf_domain == "example.org"
    assert "spf=pass smtp.mailfrom=example.org aligned" in v.reasons
    v = verdict(_msg(auth_results=["gmx.net; dkim=none; spf=pass smtp.mailfrom=bounce.example.org"]),
                trusted_authserv_id="gmx.net")
    assert v.state == "via" and v.via_domain == "bounce.example.org", "a child of the From domain is not aligned for spf either"


def test_no_trusted_id_is_unknown_and_a_foreign_id_is_ignored():
    v = verdict(_msg(auth_results=[GMAIL]), trusted_authserv_id="")
    assert (v.state, v.source, v.reasons) == ("unknown", "none", ("no trusted authserv-id",))
    assert v.topmost_authserv_id == "mx.google.com", "the topmost id is still reported, for learning"
    v = verdict(_msg(auth_results=[GMAIL]), trusted_authserv_id="mx4.messagingengine.com")
    assert (v.state, v.reasons) == ("unknown", ("no matching authentication-results",))
    v = verdict(_msg(auth_results=[]), trusted_authserv_id="mx.google.com")
    assert (v.state, v.reasons) == ("unknown", ("no authentication-results",))
    assert trusted_results(_msg(auth_results=[GMAIL]), trusted_authserv_id="") is None


def test_forged_header_below_the_genuine_one_is_ignored():
    genuine = "mx.google.com; dkim=fail header.d=bank.example; spf=fail smtp.mailfrom=evil.example; dmarc=fail header.from=bank.example"
    p = _msg(from_addr="Bank <alerts@bank.example>", auth_results=[genuine, FORGED])
    ar = trusted_results(p, trusted_authserv_id="mx.google.com")
    assert ar is not None and ar.dmarc == "fail", "the topmost matching header is the provider's"
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert v.state == "unverified" and "dmarc_fail" in v.flags and v.aligned_by == ""


def test_forged_header_with_the_trusted_id_and_no_genuine_one_is_read():
    """The documented assumption (RFC 8601 section 4.1): the provider strips foreign
    headers carrying its own authserv-id. When it does not, and no genuine header sits
    above the forged one, the forged one is read. This test pins that boundary so a
    change in the reading is a decision, not an accident."""
    p = _msg(from_addr="Bank <alerts@bank.example>", auth_results=[FORGED])
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert v.state == "verified" and v.aligned_by == "dmarc"


def test_an_id_less_header_is_never_trusted_under_the_rfc8601_profile():
    v = verdict(_msg(from_addr="a@fabrikam.com", auth_results=[MICROSOFT_PASS]), trusted_authserv_id="mx.google.com")
    assert v.state == "unknown" and v.reasons == ("no matching authentication-results",)


# ---- Microsoft ---------------------------------------------------------------

def test_microsoft_compauth_pass_with_bestguesspass_does_not_verify():
    p = _msg(from_addr="Someone <someone@fabrikam.com>", auth_results=[MICROSOFT])
    v = verdict(p, trusted_authserv_id="", auth_profile="microsoft")
    assert v.source == "microsoft" and v.authserv_id == ""
    assert v.state == "verified" and v.aligned_by == "spf", "spf=pass smtp.mailfrom=fabrikam.com IS aligned with From fabrikam.com"
    assert v.compauth == "pass" and "compauth=pass reason=109" in v.reasons
    # The same header with an unaligned spf: compauth=pass and bestguesspass must carry nothing.
    p = _msg(from_addr="Someone <someone@fabrikam.com>",
             auth_results=[MICROSOFT.replace("smtp.mailfrom=fabrikam.com", "smtp.mailfrom=bulk.example")])
    v = verdict(p, trusted_authserv_id="", auth_profile="microsoft")
    assert v.state == "via" and v.via_domain == "bulk.example" and v.aligned_by == ""
    assert v.dmarc == "bestguesspass" and v.compauth == "pass"
    p = _msg(from_addr="Someone <someone@fabrikam.com>",
             auth_results=[MICROSOFT.replace("spf=pass", "spf=fail").replace("smtp.mailfrom=fabrikam.com", "smtp.mailfrom=bulk.example")])
    v = verdict(p, trusted_authserv_id="", auth_profile="microsoft")
    assert v.state == "unverified" and v.compauth == "pass"
    assert "dmarc=bestguesspass" in v.reasons and "spf=fail" in v.reasons


def test_microsoft_dmarc_pass_verifies():
    p = _msg(auth_results=[MICROSOFT_PASS])
    v = verdict(p, trusted_authserv_id="", auth_profile="microsoft")
    assert (v.state, v.source, v.aligned_by) == ("verified", "microsoft", "dmarc")
    assert "compauth=pass reason=100" in v.reasons


def test_microsoft_profile_takes_the_topmost_id_less_header_and_still_honours_an_id():
    p = _msg(auth_results=["evil.example; dmarc=pass header.from=example.org", MICROSOFT_PASS.replace("dmarc=pass", "dmarc=fail")])
    ar = trusted_results(p, trusted_authserv_id="", auth_profile="microsoft")
    assert ar is not None and ar.profile == "microsoft" and ar.dmarc == "fail", "a foreign id above is skipped, not trusted"
    p = _msg(auth_results=["compauth=pass reason=109", MICROSOFT_PASS])
    ar = trusted_results(p, trusted_authserv_id="", auth_profile="microsoft")
    assert ar is not None and ar.dmarc == "pass", "an id-less header not starting with spf/dkim/dmarc is not the Microsoft form"
    p = _msg(auth_results=[GMAIL])
    ar = trusted_results(p, trusted_authserv_id="mx.google.com", auth_profile="microsoft")
    assert ar is not None and ar.authserv_id == "mx.google.com"
    assert trusted_results(p, trusted_authserv_id="", auth_profile="microsoft") is None


# ---- alignment in the verdict ----------------------------------------------------

def test_dkim_pass_on_an_unrelated_domain_is_via_with_the_domain():
    p = _msg(from_addr="News <news@example.org>",
             auth_results=["mx.google.com; dkim=pass header.i=@mailer.example; spf=pass smtp.mailfrom=bounce.mailer.example; dmarc=fail header.from=example.org"])
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert (v.state, v.via_domain, v.aligned_by) == ("via", "mailer.example", "")
    assert "dkim=pass header.d=mailer.example not aligned" in v.reasons and "dmarc_fail" in v.flags


def test_unaligned_spf_pass_is_via_not_verified():
    p = _msg(from_addr="News <news@example.org>",
             auth_results=["mx.google.com; dkim=none; spf=pass smtp.mailfrom=bounce.mailer.example"])
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert (v.state, v.via_domain, v.aligned_by) == ("via", "bounce.mailer.example", "")


def test_verdict_alignment_accepts_an_ancestor_signer_and_rejects_a_child():
    p = _msg(from_addr="Alice <alice@mail.example.org>",
             auth_results=["mx.google.com; dkim=pass header.d=example.org; spf=none"])
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert (v.state, v.aligned_by, v.dkim_domain) == ("verified", "dkim", "example.org")
    p = _msg(from_addr="Alice <alice@example.org>",
             auth_results=["mx.google.com; dkim=pass header.d=mail.example.org; spf=none"])
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert (v.state, v.via_domain) == ("via", "mail.example.org")


def test_a_later_aligned_dkim_pass_wins_over_an_earlier_unaligned_one():
    p = _msg(from_addr="Alice <alice@example.org>",
             auth_results=["mx.google.com; dkim=pass header.d=lists.example; dkim=pass header.d=example.org; spf=pass smtp.mailfrom=lists.example"])
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert (v.state, v.aligned_by, v.dkim_domain) == ("verified", "dkim", "example.org")


def test_dmarc_pass_for_another_from_domain_does_not_verify_by_dmarc():
    p = _msg(from_addr="Alice <alice@example.org>",
             auth_results=["mx.google.com; dkim=pass header.d=other.example; dmarc=pass header.from=other.example"])
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert v.state == "via" and v.via_domain == "other.example"
    assert "dmarc=pass header.from=other.example not aligned" in v.reasons


def test_dmarc_pass_without_header_from_needs_a_from_domain():
    """A dmarc=pass clause without header.from is accepted for a From that names a
    domain (the provider evaluated the only From there is) and never for a From
    without one: verified says the From domain authenticated, and there is none.
    MUTATION: dropping the `from_domain and` guard on the dmarc branch of _decide
    turns the first three checks red; refusing a header.from-less dmarc=pass for
    every From turns the last one red."""
    p = _msg(from_addr="Nobody", auth_results=["mx.google.com; dmarc=pass"])
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert (v.state, v.aligned_by, v.from_domain) == ("unverified", "", "")
    assert "no from domain" in v.reasons and "dmarc=pass without header.from" in v.reasons
    p = _msg(from_addr="", auth_results=["mx.google.com; dkim=pass header.d=example.org; dmarc=pass"])
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert (v.state, v.via_domain, v.aligned_by) == ("via", "example.org", ""), "the dkim pass still names the domain it did pass for"
    p = _msg(from_addr="Nobody", auth_results=["spf=none; dkim=none; dmarc=pass action=none; compauth=pass reason=100"])
    v = verdict(p, trusted_authserv_id="", auth_profile="microsoft")
    assert (v.state, v.aligned_by) == ("unverified", "")
    v = verdict(_msg(auth_results=["mx.google.com; dmarc=pass"]), trusted_authserv_id="mx.google.com")
    assert (v.state, v.aligned_by) == ("verified", "dmarc"), "a From with a domain keeps the header.from-less pass"


def test_arc_is_recorded_but_never_a_pass():
    p = _msg(auth_results=["mx.google.com; arc=pass (i=1 spf=pass dkim=pass); dkim=fail header.d=example.org; spf=fail smtp.mailfrom=example.org; dmarc=fail header.from=example.org"],
             arc_auth_results=["i=1; mx.example.net; dkim=pass header.d=example.org"])
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert v.state == "unverified" and v.arc == "pass" and "arc=pass" in v.reasons and "dmarc_fail" in v.flags
    p = _msg(auth_results=["mx.google.com; dkim=fail header.d=example.org"], arc_auth_results=["i=1; mx.example.net; dkim=pass header.d=example.org"])
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert v.state == "unverified" and v.arc == "" and "arc-authentication-results present" in v.reasons


def test_a_pass_without_a_domain_is_unverified_not_via():
    v = verdict(_msg(auth_results=["mx.google.com; dkim=pass; spf=pass"]), trusted_authserv_id="mx.google.com")
    assert v.state == "unverified" and v.via_domain == ""
    assert "dkim=pass without header.d" in v.reasons and "spf=pass without smtp.mailfrom" in v.reasons
    v = verdict(_msg(auth_results=["mx.google.com; none"]), trusted_authserv_id="mx.google.com")
    assert v.state == "unverified" and v.reasons == ("no results",)


# ---- flags ---------------------------------------------------------------------

def test_reply_to_mismatch_flag():
    p = _msg(auth_results=[GMAIL], reply_to="Alice <alice@evil.example>")
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert v.state == "verified" and v.flags == ("reply_to_mismatch",), "verified says the mail is genuine, the flag says replies leave"
    p = _msg(auth_results=[GMAIL], reply_to="replies@mail.example.org")
    assert verdict(p, trusted_authserv_id="mx.google.com").flags == (), "a subdomain of the From domain is the same organization"
    p = _msg(from_addr="a@mail.example.org", auth_results=[GMAIL], reply_to="replies@example.org")
    assert verdict(p, trusted_authserv_id="mx.google.com").flags == ()
    p = _msg(auth_results=[GMAIL], reply_to="Alice")
    assert verdict(p, trusted_authserv_id="mx.google.com").flags == (), "a Reply-To without a domain cannot mismatch"


def test_own_domain_spoof_flag():
    p = _msg(from_addr="Boss <boss@example.org>", auth_results=["mx.google.com; dkim=none; spf=fail smtp.mailfrom=example.org; dmarc=fail header.from=example.org"])
    v = verdict(p, trusted_authserv_id="mx.google.com", own_domains=["Example.ORG"])
    assert v.state == "unverified" and v.flags == ("own_domain_spoof", "dmarc_fail")
    v = verdict(_msg(from_addr="it@corp.example.org", auth_results=[]), trusted_authserv_id="mx.google.com", own_domains=("example.org",))
    assert v.state == "unknown" and "own_domain_spoof" in v.flags, "a subdomain of an own domain claims the organization too"
    v = verdict(_msg(auth_results=[GMAIL]), trusted_authserv_id="mx.google.com", own_domains=["example.org"])
    assert "own_domain_spoof" not in v.flags, "a verified mail from the own domain is the owner's own mail"
    v = verdict(_msg(from_addr="x@other.example", auth_results=[]), trusted_authserv_id="mx.google.com", own_domains=["example.org"])
    assert "own_domain_spoof" not in v.flags


def test_multiple_from_and_no_message_id_flags():
    p = _msg(from_addr="Alice <alice@example.org>, Mallory <mallory@evil.example>", auth_results=[GMAIL], message_id="")
    v = verdict(p, trusted_authserv_id="mx.google.com")
    assert v.flags == ("no_message_id", "multiple_from")
    assert v.from_domain == "example.org", "the first addr-spec decides the From domain"
    p = _msg(auth_results=[GMAIL], message_id="   ")
    assert verdict(p, trusted_authserv_id="mx.google.com").flags == ("no_message_id",)


def test_verdict_never_raises():
    v = verdict(SimpleNamespace(), trusted_authserv_id="mx.google.com")
    assert v.state == "unknown" and v.flags == ("no_message_id",)
    v = verdict(_msg(auth_results=[None, 42, "mx.google.com; dkim=pass header.d=example.org"]), trusted_authserv_id="mx.google.com")
    assert v.state == "verified"
    v = verdict(None, trusted_authserv_id="mx.google.com")
    assert v.state == "unknown"
    v = verdict(_msg(auth_results=[GMAIL]), trusted_authserv_id=None, auth_profile=None, own_domains=None)
    assert v.state == "unknown"


# ---- learning ------------------------------------------------------------------

def test_learner_picks_a_clear_majority():
    ids = ["mx.google.com"] * 9 + ["evil.example"] + [""] * 5
    assert learn_authserv_id(ids) == ("mx.google.com", 9, 10)
    assert learn_authserv_id(["MX.Google.com", "mx.google.com", "mx.google.com."]) == ("mx.google.com", 3, 3)


def test_learner_refuses_below_threshold():
    assert learn_authserv_id(["mx.google.com", "mx.google.com"]) == ("", 2, 2), "two samples are below min_samples"
    assert learn_authserv_id(["mx.google.com"] * 8 + ["evil.example"] * 2) == ("", 8, 10), "80 percent is below min_share"
    assert learn_authserv_id(["mx.google.com"] * 8 + ["evil.example"] * 2, min_share=0.8) == ("mx.google.com", 8, 10)
    assert learn_authserv_id(["mx.google.com"] * 2, min_samples=2) == ("mx.google.com", 2, 2)
    assert learn_authserv_id([]) == ("", 0, 0)


def test_learner_sees_nothing_in_all_microsoft_samples_and_looks_microsoft_does():
    values = [MICROSOFT, MICROSOFT_PASS, MICROSOFT]
    ids = [parse_auth_results(v).authserv_id for v in values]
    assert ids == ["", "", ""] and learn_authserv_id(ids) == ("", 0, 0)
    assert looks_microsoft(values)
    assert not looks_microsoft(values[:2]), "fewer than three samples"
    assert not looks_microsoft(values + [GMAIL]), "one header with an authserv-id and it is not the Microsoft form"
    assert looks_microsoft(values + ["", None]), "empty samples are ignored"
    assert not looks_microsoft(["compauth=pass reason=109"] * 3), "only spf/dkim/dmarc leads count"
