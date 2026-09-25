# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The verification round's harness (vaf/api/mail_routes.py, the mail window, the account
panel): the account rows carry the trusted provider id, the learn route reads the mailbox,
saves the id and re-assesses the stored mail, the patch route accepts the fields by hand and
backfills, the verdict route answers the full row, the thread and message rows carry `auth`,
the inbox's mail rows carry `verification`, and the web sources render the badge and the
learn control from the catalogues. Isolated: tmp data dir, in-memory account config,
pinned key.

MUTATION: drop `_account_row`'s auth fields and the first test goes red; drop the backfill
call from the learn route and the second goes red; drop `AuthBadge` from the reader and the
web guard goes red."""
import asyncio
import json
import os
import re
from pathlib import Path

import pytest

import vaf.api.mail_routes as mr
import vaf.mail.crypto as mail_crypto
from vaf.core.platform import Platform
from vaf.mail.parser import parse_message
from vaf.mail.store import MailStore

REPO = Path(__file__).resolve().parents[1]
SCOPE = "11111111-2222-3333-4444-555555555555"
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


def _gmail_raw(i: int) -> bytes:
    return (
        "Received: from mail.example.org by mx.google.com with ESMTPS id y2\n"
        "Authentication-Results: mx.google.com; dkim=pass header.i=@example.org; spf=pass smtp.mailfrom=alice@example.org; "
        "dmarc=pass header.from=example.org\n"
        "Authentication-Results: evil.example; dkim=pass header.i=@bank.example\n"
        f"From: Lena <lena@example.org>\nTo: {ACCOUNT}\nSubject: Vertrag {i}\nMessage-ID: <m{i}@example.org>\n"
        "\nzwei Punkte offen\n").encode("utf-8")


def _seed(n=3):
    s = MailStore(SCOPE)
    apk = s.upsert_account(ACCOUNT, "imap", ACCOUNT)
    fpk = s.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    pks = [s.ingest_message(apk, fpk, i + 1, parse_message(_gmail_raw(i)), raw=_gmail_raw(i)) for i in range(n)]
    s.close()
    return pks


def test_the_account_rows_carry_the_verification_state(world):
    rows = asyncio.run(mr.accounts(_user=USER))["accounts"]
    assert rows[0]["trusted_authserv_id"] == "" and rows[0]["auth_profile"] == "rfc8601"
    assert rows[0]["auth_ready"] is False and rows[0]["authserv_source"] == "" and rows[0]["aliases"] == []
    world["email_config_by_scope"][SCOPE]["accounts"][0].update({"provider": "microsoft"})
    assert asyncio.run(mr.accounts(_user=USER))["accounts"][0]["auth_ready"] is True, "a Microsoft account needs no id"


def test_learning_reads_the_mailbox_saves_the_id_and_reassesses_the_stored_mail(world):
    pks = _seed(3)
    before = asyncio.run(mr.list_threads(_user=USER))["threads"]
    assert {t["auth"]["state"] for t in before} == {"unknown"}, "no trusted id yet: nothing is verified"
    out = asyncio.run(mr.accounts_learn_auth(ACCOUNT, _user=USER))
    assert out["learned"] == {"authserv_id": "mx.google.com", "profile": "rfc8601", "count": 3, "total": 3,
                              "domains": 1}
    assert out["saved"] is True and out["backfilled"] == 3
    acc = world["email_config_by_scope"][SCOPE]["accounts"][0]
    assert acc["trusted_authserv_id"] == "mx.google.com" and acc["authserv_source"] == "mailbox" and acc["authserv_samples"] == 3
    after = asyncio.run(mr.list_threads(_user=USER))["threads"]
    assert {t["auth"]["state"] for t in after} == {"verified"} and after[0]["auth"]["aligned_by"] == "dmarc"
    detail = asyncio.run(mr.thread_detail(after[0]["thread_id"], _user=USER))["messages"]
    assert detail[0]["auth"]["state"] == "verified"
    verdict = asyncio.run(mr.message_verdict(pks[0], _user=USER))["verdict"]
    assert verdict["authserv_id"] == "mx.google.com" and verdict["headers"]["auth_results"]
    rows = asyncio.run(mr.accounts(_user=USER))["accounts"]
    assert rows[0]["auth_ready"] is True and rows[0]["authserv_learned_at"]


def test_too_few_samples_save_nothing(world):
    _seed(2)
    out = asyncio.run(mr.accounts_learn_auth(ACCOUNT, _user=USER))
    assert out["saved"] is False and out["learned"]["authserv_id"] == "" and out["learned"]["total"] == 2
    assert "trusted_authserv_id" not in world["email_config_by_scope"][SCOPE]["accounts"][0]
    with pytest.raises(mr.HTTPException):
        asyncio.run(mr.accounts_learn_auth("nobody@example.com", _user=USER))


def test_the_patch_route_takes_the_fields_by_hand_and_backfills(world):
    _seed(1)
    out = asyncio.run(mr.accounts_patch(ACCOUNT, body={"trusted_authserv_id": "MX.Google.com", "aliases": ["Info@Example.com", "junk"]}, _user=USER))
    assert out == {"ok": True, "backfilled": 1}
    acc = world["email_config_by_scope"][SCOPE]["accounts"][0]
    assert acc["trusted_authserv_id"] == "mx.google.com" and acc["authserv_source"] == "manual" and acc["aliases"] == ["info@example.com"]
    assert asyncio.run(mr.list_threads(_user=USER))["threads"][0]["auth"]["state"] == "verified"
    with pytest.raises(mr.HTTPException):
        asyncio.run(mr.accounts_patch(ACCOUNT, body={"auth_profile": "guess"}, _user=USER))
    assert asyncio.run(mr.accounts_patch(ACCOUNT, body={"label": "Work"}, _user=USER))["backfilled"] == 0, "a label change recomputes nothing"


def test_a_verdict_is_asked_for_a_message_that_has_none(world):
    with pytest.raises(mr.HTTPException):
        asyncio.run(mr.message_verdict(999, _user=USER))


def test_the_inbox_mail_rows_carry_the_verification(world):
    _seed(1)
    from vaf.core import inbox
    rows = inbox.list_conversations("alice", SCOPE)["rows"]
    mail = [r for r in rows if r["channel"] == "mail"]
    assert mail and mail[0]["verification"] == {"state": "unknown", "machine_kind": ""}


# ── the web sources ────────────────────────────────────────────────────────────────────

_PAGE = REPO / "web" / "app" / "mail" / "page.tsx"
_ACCOUNTS = REPO / "web" / "components" / "connections" / "MailAccounts.tsx"
_MESSAGES = REPO / "web" / "messages"


def test_the_mail_window_renders_the_badge_on_rows_and_in_the_reader():
    src = _PAGE.read_text(encoding="utf-8")
    assert "function AuthBadge(" in src
    assert src.count("<AuthBadge auth={row.auth} compact />") == 1, "the thread row"
    assert src.count("<AuthBadge auth={msg.auth} />") == 1, "the reader header"
    for state in ("'verified'", "'via'", "'unverified'"):
        assert f"auth.state === {state}" in src, state
    assert "own_domain_spoof" in src, "the red state"
    # MUTATION: naming dkim_domain for every method again (`auth.dkim_domain || auth.from_domain`) turns this red.
    assert "const domain = method === 'dkim' ? (auth.dkim_domain || auth.from_domain || '') : (auth.from_domain || '');" in src, \
        "the verified label names the aligned DKIM signer for DKIM and the From domain for DMARC and SPF"
    assert "t(`auth.machine.${kind}`)" in src


def test_the_account_panel_shows_the_trusted_id_and_learns_it():
    src = _ACCOUNTS.read_text(encoding="utf-8")
    assert "learn-auth" in src and "t('auth.learn')" in src
    for key in ("auth.accountLearned", "auth.accountMicrosoft", "auth.accountProvider", "auth.accountManual", "auth.accountNone",
                "auth.learnDone", "auth.learnDoneMicrosoft", "auth.learnTooFew", "auth.learnFailed"):
        assert f"t('{key}'" in src, key


def test_every_catalogue_carries_the_verification_strings():
    keys = {"verified", "via", "unverified", "spoof", "machine", "accountLearned", "accountMicrosoft", "accountProvider", "accountManual",
            "accountNone", "learn", "learnDone", "learnDoneMicrosoft", "learnTooFew", "learnFailed"}
    kinds = {"bounce", "mdn", "auto_reply", "list", "bulk", "calendar", "own_loop", "null_return_path"}
    for path in sorted(_MESSAGES.glob("*.json")):
        block = json.loads(path.read_text(encoding="utf-8"))["mailV2"]["auth"]
        assert set(block) == keys, path.name
        assert set(block["machine"]) == kinds, path.name
        assert re.search(r"\{method\}.*\{domain\}", block["verified"]), path.name
        assert "{count}" in block["learnTooFew"] and "{id}" in block["learnDone"], path.name
        assert "{provider}" in block["accountProvider"], path.name
