# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Sender verification is on by default, and it stops flagging the owner's own world.

MEASURED BEFORE THE FIX, on a real Gmail mailbox: every verdict stayed "unknown", because
the provider's Authentication-Results id was learned only by a button nobody had found.
And the own domain was the address's domain, so for alice@gmail.com every mail from any
other Gmail user carried `own_domain_spoof`: phishing score 5 against a threshold of 3,
hidden from the agent. The owner's own sent mail carried the flag too (no provider header
on a Sent copy), which put a false "somebody writes as you" into the security log.

WHAT HOLDS NOW, each pinned below:
- a provider's shared domain (gmail.com, web.de) is nobody's own domain; own addresses stay;
- the owner's own copy without any provider header is not a forgery, a forgery that
  arrived (with the provider's failing header) still is;
- Gmail is checked without learning: its id is a fact in the provider table;
- every other account learns its provider's id after a sync, with more evidence than the
  button asks for, and verdicts from an older policy are recomputed;
- the free-mail heuristics read the provider table instead of a hand-written copy.
Isolated: tmp data dir, in-memory account config, pinned key.
"""
import ast
import asyncio
import json
import os
from pathlib import Path

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.core.platform import Platform
from vaf.mail.parser import parse_message
from vaf.mail.verification import assess, auth_policy_for_account, summary

REPO = Path(__file__).resolve().parents[1]
SCOPE = "ab12cd34-0000-4000-8000-00000000000a"
USER = {"username": "alice", "user_scope_id": SCOPE, "role": "user"}
ACCOUNT = "alice@example.com"


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    state = {"email_config_by_scope": {SCOPE: {"accounts": [
        {"account_id": ACCOUNT, "email": ACCOUNT, "provider": "imap", "enabled": True, "auto_sync_enabled": True},
    ]}}}
    import vaf.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    monkeypatch.setattr(cfg_mod.Config, "load", classmethod(lambda cls: json.loads(json.dumps(state))))
    monkeypatch.setattr(cfg_mod.Config, "save", classmethod(lambda cls, cfg: state.update(cfg)))
    yield state
    mail_crypto._cached_key = old


def _account(world):
    return world["email_config_by_scope"][SCOPE]["accounts"][0]


def _raw(i, *, sender_domain="sender0.example", authserv="mx.provider.example", dmarc="pass",
         from_addr=None, to=ACCOUNT):
    sender = from_addr or f"person{i}@{sender_domain}"
    head = (f"Authentication-Results: {authserv}; dmarc={dmarc} header.from={sender.rsplit('@', 1)[1]}\n"
            if authserv else "")
    return (head + f"From: Person <{sender}>\nTo: {to}\nSubject: Mail {i}\nMessage-ID: <m{i}@{sender.rsplit('@', 1)[1]}>\n"
            "\nHallo\n").encode("utf-8")


def _seed(raws, *, account=ACCOUNT, policy=None):
    from vaf.mail.store import MailStore
    s = MailStore(SCOPE)
    apk = s.upsert_account(account, "imap", account)
    fpk = s.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    for i, raw in enumerate(raws):
        s.ingest_message(apk, fpk, i + 1, parse_message(raw), raw=raw, auth_policy=policy)
    s.close()


def _states(account=ACCOUNT):
    from vaf.mail.store import MailStore
    s = MailStore(SCOPE)
    try:
        apk = s.account_pk(account)
        rows = s._conn().execute("SELECT ma.auth_state FROM message_auth ma JOIN messages m ON m.id=ma.message_pk "
                                 "WHERE m.account_id=?", (apk,)).fetchall()
        return sorted({r[0] for r in rows})
    finally:
        s.close()


def _verdict(raw, account):
    return summary(assess(parse_message(raw), policy=auth_policy_for_account(account)))


# ── a provider's domain is nobody's own domain ─────────────────────────────────────

def test_a_providers_shared_domain_is_nobodys_own_domain():
    """The measured bug. MUTATION: drop `- SHARED_MAIL_DOMAINS` in auth_policy_for_account - red."""
    from vaf.tools.mail_utils import filter_phishing_messages_for_agent

    gmail = {"email": "alice@gmail.com", "provider": "imap"}
    assert auth_policy_for_account(gmail)["own_domains"] == []
    assert auth_policy_for_account({"email": "bob@firma.example"})["own_domains"] == ["firma.example"], \
        "an organisation's own domain stays"
    v = _verdict(_raw(1, from_addr="bob@gmail.com", authserv=""), gmail)
    assert "own_domain_spoof" not in v["flags"]
    safe, hidden = filter_phishing_messages_for_agent(
        [{"from": "Bob <bob@gmail.com>", "subject": "Treffen", "body_snippet": "morgen?", "auth": v}])
    assert len(safe) == 1 and hidden == 0, "a Gmail friend's mail reaches the agent"


def test_a_forgery_of_the_owners_own_address_is_still_flagged():
    """With the domain gone, the address carries the claim. MUTATION: drop the address
    half of `claims_own` - red."""
    gmail = {"email": "alice@gmail.com", "provider": "gmail"}
    forged = _raw(1, from_addr="alice@gmail.com", authserv="mx.google.com", dmarc="fail")
    v = _verdict(forged, gmail)
    assert v["state"] == "unverified" and "own_domain_spoof" in v["flags"]


def test_the_owners_own_copy_is_not_a_forgery():
    """A Sent copy has no provider header: nothing was received, so nothing failed. It is
    the owner's own mail (classify says own_loop). MUTATION: drop the
    `not (state == "unknown" and is_own_address)` clause - red. A colleague's address from
    the own domain without a header keeps the fail-safe flag."""
    org = {"email": "bob@firma.example", "provider": "imap", "trusted_authserv_id": "mx.provider.example"}
    own_copy = _verdict(_raw(1, from_addr="bob@firma.example", authserv=""), org)
    assert own_copy["state"] == "unknown" and own_copy["machine_kind"] == "own_loop"
    assert own_copy["flags"] == []
    colleague = _verdict(_raw(2, from_addr="it@firma.example", authserv=""), org)
    assert "own_domain_spoof" in colleague["flags"]
    # The owner's own address WITH a header the trusted id does not match arrived from
    # somewhere: that is what a relayed forgery looks like, so it keeps the flag. MUTATION:
    # drop `and not headers` from the own-copy exemption - red.
    relayed = _verdict(_raw(3, from_addr="bob@firma.example", authserv="mx.elsewhere.example"), org)
    assert relayed["state"] == "unknown" and "own_domain_spoof" in relayed["flags"]


# ── Gmail needs no learning ────────────────────────────────────────────────────────

def test_gmail_is_checked_without_learning():
    """MUTATION: drop the provider fallback in auth_policy_for_account - red."""
    for acc in ({"email": "alice@gmail.com", "provider": "gmail"},
                {"email": "alice@googlemail.com", "provider": "imap"},
                {"email": "alice@firma.example", "provider": "imap", "imap_host": "imap.gmail.com"}):
        assert auth_policy_for_account(acc)["trusted_authserv_id"] == "mx.google.com", acc
    v = _verdict(_raw(1, from_addr="bob@example.org", authserv="mx.google.com"), {"email": "a@gmail.com", "provider": "gmail"})
    assert v["state"] == "verified"
    manual = {"email": "alice@gmail.com", "provider": "gmail", "trusted_authserv_id": "mx.other.example"}
    assert auth_policy_for_account(manual)["trusted_authserv_id"] == "mx.other.example", "an id on the account wins"
    none = {"email": "alice@gmail.com", "provider": "gmail", "auth_profile": "none"}
    assert _verdict(_raw(1, authserv="mx.google.com"), none)["state"] == "unknown", "profile none trusts nothing"
    assert auth_policy_for_account({"email": "a@firma.example"})["trusted_authserv_id"] == ""


def test_the_account_row_says_the_id_comes_from_the_provider(world):
    import vaf.api.mail_routes as mr

    _account(world).update({"provider": "gmail", "email": "alice@gmail.com", "account_id": "alice@gmail.com"})
    row = asyncio.run(mr.accounts(_user=USER))["accounts"][0]
    assert row["auth_ready"] is True and row["trusted_authserv_id"] == "mx.google.com"
    assert row["authserv_source"] == "provider" and row["authserv_provider"] == "Gmail"


# ── everyone else learns after a sync, with more evidence ──────────────────────────

def test_learning_after_a_sync_needs_more_evidence_than_the_button(world):
    """Three forged headers in a nearly empty mailbox are enough for the button, whose
    result the person sees - never for the learn nobody watches. MUTATION: let the
    automatic learn use the button's thresholds - red."""
    from vaf.mail.service import MailService

    _seed([_raw(i, authserv="evil.example") for i in range(3)])
    svc = MailService(SCOPE)
    out = svc.learn_sender_check(_account(world), "alice", automatic=True)
    assert out["saved"] is False and "trusted_authserv_id" not in _account(world)
    assert svc.learn_provider(ACCOUNT)["authserv_id"] == "evil.example", "the button's bar is lower on purpose"


def test_many_mails_from_one_sender_domain_are_not_enough(world):
    from vaf.mail.service import MailService

    _seed([_raw(i, sender_domain="one.example") for i in range(25)])
    learned = MailService(SCOPE).learn_sender_check(_account(world), "alice", automatic=True)["learned"]
    assert learned["authserv_id"] == "" and learned["count"] == 25 and learned["domains"] == 1


def test_a_sync_sets_the_check_up_once_the_evidence_is_there(world):
    """settle_verification is what both sync lanes call. MUTATION: skip the learn in
    settle_verification - red."""
    from vaf.mail.service import MailService

    _seed([_raw(i, sender_domain=f"sender{i % 5}.example") for i in range(20)])
    assert _states() == ["unknown"]
    MailService(SCOPE).settle_verification(_account(world), "alice")
    acc = _account(world)
    assert acc["trusted_authserv_id"] == "mx.provider.example" and acc["authserv_source"] == "mailbox"
    assert acc["authserv_samples"] == 20
    assert _states() == ["verified"], "the stored mail was re-assessed under the learned id"


def test_a_sync_recomputes_verdicts_from_an_older_policy(world):
    """A Gmail account whose mail was assessed before VAF knew Gmail's id. MUTATION: drop
    the reassess in settle_verification - red."""
    from vaf.mail.service import MailService

    gmail = "alice@gmail.com"
    _account(world).update({"provider": "gmail", "email": gmail, "account_id": gmail})
    old_policy = {"trusted_authserv_id": "", "auth_profile": "rfc8601",
                  "own_addresses": [gmail], "own_domains": ["gmail.com"]}
    _seed([_raw(i, authserv="mx.google.com", to=gmail) for i in range(2)], account=gmail, policy=old_policy)
    assert _states(gmail) == ["unknown"]
    MailService(SCOPE).settle_verification(_account(world), "alice")
    assert _states(gmail) == ["verified"]
    assert "trusted_authserv_id" not in _account(world), "a known provider id is not written onto the account"


def test_settling_never_raises():
    from vaf.mail.service import MailService

    svc = MailService.__new__(MailService)
    svc.user_scope_id = SCOPE
    svc.store = None  # every store call fails
    svc.settle_verification({"account_id": "x@firma.example"}, None)


def test_both_sync_lanes_settle_the_verification():
    """The wiring as code: the sweep and the Sync button both run the pass right after
    the sync itself."""
    def _calls(path, func):
        tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func)
        calls = [(n.lineno, n.func.attr) for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        return [attr for _line, attr in sorted(calls)]

    sweep = _calls("vaf/mail/supervisor.py", "_sync_one")
    assert sweep.index("settle_verification") > sweep.index("sync_account")
    button = _calls("vaf/api/mail_routes.py", "sync_account")
    assert button.index("settle_verification") > button.index("sync_account")


# ── one table for the free-mail heuristics ─────────────────────────────────────────

def test_the_free_mail_heuristics_read_the_provider_table():
    """The hand-written copy knew twelve domains and missed web.de and T-Online."""
    import vaf.tools.mail_utils as mu
    import vaf.tools.send_mail as sm
    from vaf.core.email_accounts import MAIL_PROVIDERS, SHARED_MAIL_DOMAINS

    assert SHARED_MAIL_DOMAINS == frozenset(MAIL_PROVIDERS)
    assert not hasattr(mu, "_FREE_MAIL_DOMAINS") and not hasattr(sm, "_FREE_MAIL_DOMAINS")
    assert "possible_exec_impersonation_to_free_mail_domain" in sm._high_risk_send_reasons(
        "chef@web.de", "Ueberweisung", "Bitte an die Buchhaltung weiterleiten", [])
    score, reasons = mu._phishing_score({"from": "CEO Office <ceo.office@t-online.de>", "subject": "Hallo",
                                         "body_snippet": "kurz", "auth": {}})
    assert "exec_impersonation_free_mail" in reasons


def test_the_security_log_hears_of_a_forged_own_address_and_not_of_own_mail(world, monkeypatch):
    """The sync's spoof report reads the same claim: an own address counts, a provider's
    domain does not. MUTATION: drop the own-address half of `_report_spoof` - red."""
    import vaf.core.security_events as se
    from vaf.mail.store import MailStore
    from vaf.mail.sync import ImapSyncEngine

    events = []
    monkeypatch.setattr(se, "log_security_event", lambda kind, **kw: events.append((kind, kw.get("detail"))))
    gmail = "alice@gmail.com"
    policy = auth_policy_for_account({"email": gmail, "provider": "gmail"})
    store = MailStore(SCOPE)
    apk = store.upsert_account(gmail, "gmail", gmail)
    fpk = store.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    eng = ImapSyncEngine.__new__(ImapSyncEngine)
    eng.store, eng.auth_policy = store, policy
    try:
        for uid, raw in enumerate((_raw(1, from_addr=gmail, authserv="mx.google.com", dmarc="fail", to=gmail),
                                   _raw(2, from_addr=gmail, authserv="", to=gmail),
                                   _raw(3, from_addr="bob@gmail.com", authserv="", to=gmail)), start=1):
            parsed = parse_message(raw)
            pk = store.ingest_message(apk, fpk, uid, parsed, raw=raw, auth_policy=policy)
            eng._report_spoof(pk, parsed)
    finally:
        store.close()
    assert [kind for kind, _d in events] == ["mail_spoofed_own_domain"], events
