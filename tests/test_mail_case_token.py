# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The case stamps of the mail engine (EMAIL_CLIENT.md): a stamp verifies for the scope
and account that minted it and for nothing else, a foreign header is refused without
touching the keyring, and the two opt-in stamps survive what clients do to subjects
and addresses. The keyring is the per-test scratch ring of conftest; the one pinned
vector uses a root the test names, so the arithmetic is fixed rather than the store.

Mutation proofs are stated on the tests that carry them: each names the line in
`vaf/mail/case_token.py` whose removal turns it red, and each was checked by removing
that line and reverting."""
import hashlib
import hmac
import re
from email.utils import make_msgid

import pytest

from vaf.mail import case_token as ct

SCOPE, OTHER_SCOPE = "scope-alice", "scope-bob"
ACCOUNT, OTHER_ACCOUNT = "acct-work", "acct-private"
DOMAIN = "mail.example.com"
B32 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def _flip(text: str, index: int) -> str:
    """The same string with ONE character replaced by a different alphabet character."""
    current = text[index]
    replacement = "A" if current != "A" else "B"
    return text[:index] + replacement + text[index + 1:]


# ── the key ─────────────────────────────────────────────────────────────────

def test_case_key_is_deterministic_32_bytes_and_differs_per_scope_and_account():
    key = ct.case_key(SCOPE, ACCOUNT)
    assert isinstance(key, bytes) and len(key) == 32
    assert ct.case_key(SCOPE, ACCOUNT) == key
    assert ct.case_key(OTHER_SCOPE, ACCOUNT) != key
    assert ct.case_key(SCOPE, OTHER_ACCOUNT) != key


def test_case_key_arithmetic_is_pinned(monkeypatch):
    """A test vector: HKDF-SHA256, no salt, info = DERIVE_INFO + "scope:account", over
    the UTF-8 root. The literal was computed with the stdlib expansion below, which is
    independent of the `cryptography` HKDF the module uses; a stranger's
    implementation reproduces it or their stamps never verify against ours."""
    root = "test-root-secret-that-is-at-least-32-chars-long"
    monkeypatch.setattr(ct, "_root_secret", lambda: root)

    def hkdf(ikm: bytes, info: bytes, length: int = 32) -> bytes:
        prk = hmac.new(b"\x00" * 32, ikm, hashlib.sha256).digest()
        out, block, counter = b"", b"", 1
        while len(out) < length:
            block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
            out += block
            counter += 1
        return out[:length]

    key = ct.case_key("scope-a", "acct-1")
    assert key.hex() == "4f5a35c757afa2a481dadf13f6c22ccfe2b1f98015f4fb95a1bc35daa4c34549"
    assert key == hkdf(root.encode("utf-8"), b"vaf-mail-case/v1/" + b"scope-a:acct-1")
    assert ct.DERIVE_INFO == b"vaf-mail-case/v1/"
    assert ct.ROOT_SECRET == "mail_case_root_key"


# ── the case id ─────────────────────────────────────────────────────────────

def test_mint_case_id_is_ten_base32_chars_and_fresh():
    first, second = ct.mint_case_id(), ct.mint_case_id()
    assert first != second
    for case_id in (first, second):
        assert len(case_id) == ct.CASE_ID_LEN == 10
        assert set(case_id) <= B32


def test_parse_and_format_accept_every_spelling_and_refuse_the_rest():
    assert ct.parse_case_id("ABCDEFGHIJ") == "ABCDEFGHIJ"
    assert ct.parse_case_id("ABCDE-FGHIJ") == "ABCDEFGHIJ"
    assert ct.parse_case_id("  abcde-fghij\n") == "ABCDEFGHIJ"
    assert ct.format_case_id("abcdefghij") == "ABCDE-FGHIJ"
    assert ct.format_case_id("ABCDE-FGHIJ") == "ABCDE-FGHIJ"
    for junk in ("", "ABCDEFGHI", "ABCDEFGHIJK", "ABCDE-FGHI1", "ABCD-EFGHIJ", "AB CDEFGHIJ", None, 42):
        assert ct.parse_case_id(junk) is None, junk
    assert ct.format_case_id("not a case") == "not a case"


# ── the Message-ID anchor ───────────────────────────────────────────────────

def test_mint_message_id_has_the_anchor_shape_and_a_fresh_nonce():
    case_id = ct.mint_case_id()
    first = ct.mint_message_id(SCOPE, ACCOUNT, case_id, DOMAIN)
    second = ct.mint_message_id(SCOPE, ACCOUNT, ct.format_case_id(case_id).lower(), DOMAIN)
    assert first != second, "the nonce makes every mail's id unique"
    for message_id in (first, second):
        assert message_id.startswith("<") and message_id.endswith(f"@{DOMAIN}>")
        local = message_id[1:-1].rsplit("@", 1)[0]
        match = ct.ANCHOR_RE.match(local)
        assert match is not None, local
        assert match.group(1) == case_id
        assert len(match.group(2)) == ct.NONCE_LEN and len(match.group(3)) == ct.TAG_LEN
    assert ct.ANCHOR_RE.match(first[1:-1]) is None, "the pattern is the local part only"
    with pytest.raises(ValueError):
        ct.mint_message_id(SCOPE, ACCOUNT, "not-a-case-id", DOMAIN)
    with pytest.raises(ValueError):
        ct.mint_message_id(SCOPE, ACCOUNT, case_id, "")


def test_a_minted_anchor_verifies_only_for_its_own_scope_and_account():
    """MUTATION PROOF: removing the `if not hmac.compare_digest(expected, tag): return
    None` lines in `verify_anchor` turns this red at the cross-scope assertion, because
    the id then verifies for everybody who can see the shape."""
    case_id = ct.mint_case_id()
    message_id = ct.mint_message_id(SCOPE, ACCOUNT, case_id, DOMAIN)
    assert ct.verify_anchor(SCOPE, ACCOUNT, message_id) == case_id
    assert ct.verify_anchor(OTHER_SCOPE, ACCOUNT, message_id) is None
    assert ct.verify_anchor(SCOPE, OTHER_ACCOUNT, message_id) is None
    assert ct.verify_anchor(OTHER_SCOPE, OTHER_ACCOUNT, message_id) is None


def test_tampering_one_character_of_tag_nonce_or_case_id_fails():
    """Every position of every part: a tag covering only the case id would let the
    nonce column pass, and a tag covering only the nonce would let the case id move."""
    case_id = ct.mint_case_id()
    message_id = ct.mint_message_id(SCOPE, ACCOUNT, case_id, DOMAIN)
    local, domain = message_id[1:-1].rsplit("@", 1)
    case_part, nonce, tag = local.split(".")
    assert ct.verify_anchor(SCOPE, ACCOUNT, message_id) == case_id
    for index in range(ct.CASE_ID_LEN):
        forged = f"<{_flip(case_part, index)}.{nonce}.{tag}@{domain}>"
        assert ct.verify_anchor(SCOPE, ACCOUNT, forged) is None, ("case id", index)
    for index in range(ct.NONCE_LEN):
        forged = f"<{case_part}.{_flip(nonce, index)}.{tag}@{domain}>"
        assert ct.verify_anchor(SCOPE, ACCOUNT, forged) is None, ("nonce", index)
    for index in range(ct.TAG_LEN):
        forged = f"<{case_part}.{nonce}.{_flip(tag, index)}@{domain}>"
        assert ct.verify_anchor(SCOPE, ACCOUNT, forged) is None, ("tag", index)


def test_verify_anchor_tolerates_whitespace_brackets_case_and_another_domain():
    case_id = ct.mint_case_id()
    message_id = ct.mint_message_id(SCOPE, ACCOUNT, case_id, DOMAIN)
    bare = message_id[1:-1]
    assert ct.verify_anchor(SCOPE, ACCOUNT, bare) == case_id
    assert ct.verify_anchor(SCOPE, ACCOUNT, f"  \t{message_id}\r\n") == case_id
    assert ct.verify_anchor(SCOPE, ACCOUNT, bare.lower()) == case_id
    # The tag is the proof, not the domain: a relay that rewrote the host part
    # (it happens with some gateways) still hands the case back.
    local = bare.rsplit("@", 1)[0]
    assert ct.verify_anchor(SCOPE, ACCOUNT, f"<{local}@other.example>") == case_id


def test_a_foreign_message_id_is_none_and_never_touches_the_keyring(monkeypatch):
    """MUTATION PROOF: moving the `case_key(...)` call in `verify_anchor` above the
    `ANCHOR_RE.match` check turns this red, because the patched key raises. Nearly
    every inbound Message-ID is foreign; deriving a key for each would be wasted work
    and would make every attribution depend on a keyring that a plain read of a
    stranger's header has no business opening."""

    def _no_key(*_args):
        raise AssertionError("a foreign id must not derive a key")

    monkeypatch.setattr(ct, "case_key", _no_key)
    for foreign in (
        "<abc@example.com>",
        make_msgid(),
        make_msgid(domain=DOMAIN),
        "",
        "   ",
        "no-at-sign",
        "<ABCDEFGHIJ.ABCDEFGHIJKL@example.com>",       # two parts, not three
        "<ABCDEFGHIJ.ABCDEFGHIJKL.ABCDEFGHIJK@x.y>",   # tag one char short
        "<ABCDEFGHIJ.ABCDEFGHIJKL.ABCDEFGHIJK1@x.y>",  # a digit outside the alphabet
        None,
        123,
    ):
        assert ct.verify_anchor(SCOPE, ACCOUNT, foreign) is None, foreign


# ── the subject tag ─────────────────────────────────────────────────────────

def test_subject_tag_round_trips_through_reply_prefixes_and_case_folding():
    case_id = ct.mint_case_id()
    tag = ct.subject_tag(SCOPE, ACCOUNT, case_id)
    assert re.fullmatch(r"\[VAF#[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{4}\]", tag), tag
    assert tag[5:16] == ct.format_case_id(case_id)

    replied = f"Re: AW: {tag} Original subject"
    found = ct.find_subject_tags(replied)
    assert found == [(case_id, tag[-5:-1])]
    assert ct.verify_subject_tag(SCOPE, ACCOUNT, *found[0])

    mangled = f"re: {tag.lower().replace('-', '')} original subject"
    found = ct.find_subject_tags(mangled)
    assert found == [(case_id, tag[-5:-1])]
    assert ct.verify_subject_tag(SCOPE, ACCOUNT, *found[0])

    folded = f"Fwd: [VAF# {ct.format_case_id(case_id)}-{tag[-5:-1]}] subject"
    assert ct.find_subject_tags(folded) == [(case_id, tag[-5:-1])]


def test_a_wrong_check_is_found_but_refused():
    """MUTATION PROOF: replacing the `hmac.compare_digest(...)` return in
    `verify_subject_tag` with `return True` turns this red at the refusal, and so does
    removing the `_CHECK_RE.match` guard for the malformed checks below."""
    case_id = ct.mint_case_id()
    tag = ct.subject_tag(SCOPE, ACCOUNT, case_id)
    check = tag[-5:-1]
    wrong = _flip(check, 2)
    forged = f"Re: [VAF#{ct.format_case_id(case_id)}-{wrong}] Original subject"
    assert ct.find_subject_tags(forged) == [(case_id, wrong)]
    assert ct.verify_subject_tag(SCOPE, ACCOUNT, case_id, wrong) is False
    assert ct.verify_subject_tag(SCOPE, ACCOUNT, case_id, check) is True
    assert ct.verify_subject_tag(SCOPE, ACCOUNT, case_id, check.lower()) is True
    # Another scope or account computes another check for the same id.
    assert ct.verify_subject_tag(OTHER_SCOPE, ACCOUNT, case_id, check) is False
    assert ct.verify_subject_tag(SCOPE, OTHER_ACCOUNT, case_id, check) is False
    # Malformed input is a refusal, never an exception.
    for bad_check in ("", "ABC", "ABCDE", "AB1D", None):
        assert ct.verify_subject_tag(SCOPE, ACCOUNT, case_id, bad_check) is False, bad_check
    assert ct.verify_subject_tag(SCOPE, ACCOUNT, "not-an-id", check) is False
    with pytest.raises(ValueError):
        ct.subject_tag(SCOPE, ACCOUNT, "not-an-id")


def test_find_subject_tags_keeps_order_and_ignores_lookalikes():
    assert ct.find_subject_tags("Re: [VAF#ABCDE-FGHIJ-KLMN] and [VAF#KLMNO-PQRST-UVWX] merged") == [
        ("ABCDEFGHIJ", "KLMN"), ("KLMNOPQRST", "UVWX"),
    ]
    for lookalike in (
        "XVAF#ABCDE-FGHIJ-KLMN glued to a word",
        "VAF#ABCDE-FGHIJ-KLMNO one character too long",
        "VAF#ABCDE-FGHIJ-KLM one character too short",
        "VAF#ABCDE-FGHIJ-KL1N a digit outside the alphabet",
        "VAF ABCDE-FGHIJ-KLMN without the hook",
        "",
    ):
        assert ct.find_subject_tags(lookalike) == [], lookalike
    assert ct.find_subject_tags(None) == []
    assert ct.SUBJECT_TAG_RE.flags & re.IGNORECASE


def test_strip_reply_prefixes_takes_the_clients_prefixes_and_nothing_else():
    assert ct.strip_reply_prefixes("Re: Re: AW: WG: Fwd: hello") == "hello"
    assert ct.strip_reply_prefixes("RE[2]: hello") == "hello"
    assert ct.strip_reply_prefixes("Rescue plan") == "Rescue plan"
    assert ct.strip_reply_prefixes("re: rescue plan") == "rescue plan"
    assert ct.strip_reply_prefixes("[Fwd: hello]") == "hello"
    assert ct.strip_reply_prefixes("Fw: SV: VS: TR: hello") == "hello"
    assert ct.strip_reply_prefixes("RE : hello") == "hello"
    assert ct.strip_reply_prefixes("Re: AW: [VAF#ABCDE-FGHIJ-KLMN] Original") == "[VAF#ABCDE-FGHIJ-KLMN] Original"
    assert ct.strip_reply_prefixes("Fwd: [VAF#ABCDE-FGHIJ-KLMN]") == "[VAF#ABCDE-FGHIJ-KLMN]"
    assert ct.strip_reply_prefixes("  hello  ") == "hello"
    assert ct.strip_reply_prefixes("") == ""
    assert ct.strip_reply_prefixes(None) == ""


# ── the plus address ────────────────────────────────────────────────────────

def test_plus_address_has_the_shape_and_finds_only_our_domain_with_the_prefix():
    case_id = ct.mint_case_id()
    address = ct.plus_address("support", DOMAIN, case_id)
    assert address == f"support+vaf-{case_id.lower()}@{DOMAIN}"
    assert ct.PLUS_ADDRESS_RE.match(address).group("case").upper() == case_id

    other = ct.mint_case_id()
    headers = [
        f"Support <{address}>, bob@example.org",
        f"support+vaf-{other}@{DOMAIN.upper()}",             # any case, our domain
        f"support+vaf-{case_id}@other.example",              # their domain, ignored
        f"support+{case_id.lower()}@{DOMAIN}",               # no vaf- prefix, ignored
        f"support+ticket-{case_id.lower()}@{DOMAIN}",        # another product's prefix
        f"support+vaf-{case_id[:9]}@{DOMAIN}",               # one character short
        f"bad<<addr, alice+work+vaf-{case_id.lower()}@{DOMAIN}",  # a subaddress already
        address,                                             # the same case twice
    ]
    assert ct.find_plus_addresses(headers, DOMAIN) == [case_id, other]
    assert ct.find_plus_addresses(headers, DOMAIN.upper()) == [case_id, other]
    assert ct.find_plus_addresses(headers, "other.example") == [case_id]
    assert ct.find_plus_addresses(headers, "") == []
    assert ct.find_plus_addresses([], DOMAIN) == []
    assert ct.find_plus_addresses([None, 3, ""], DOMAIN) == []


def test_plus_address_refuses_what_is_not_a_mailbox():
    for local, domain, case_id in (
        ("", DOMAIN, "ABCDEFGHIJ"),
        ("support", "", "ABCDEFGHIJ"),
        ("sup@port", DOMAIN, "ABCDEFGHIJ"),
        ("support", DOMAIN, "not-an-id"),
    ):
        with pytest.raises(ValueError):
            ct.plus_address(local, domain, case_id)
