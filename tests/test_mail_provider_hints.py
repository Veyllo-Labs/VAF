# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A refused IMAP login has to say what to do about it.

The gap these pin: IMAP_SMTP_DEFAULTS knew thirteen domains while the failure
hint knew two of them, so a GMX, WEB.DE, Yahoo, iCloud or T-Online user saw the
bare server string and no mention of the app-specific password that IMAP makes
mandatory once two-factor authentication is on. The presets and the guidance are
now one table with the hosts derived from it, and these tests fail if a provider
is ever added back with server settings but no guidance.
"""
import asyncio
import imaplib
import json
import re
from pathlib import Path

import pytest

import vaf.api.mail_routes as mr
import vaf.core.email_accounts as ea
import vaf.network.binding as binding

_REPO = Path(__file__).resolve().parents[1]
_USER = {"username": "admin", "user_scope_id": "s"}


@pytest.fixture
def no_host_check(monkeypatch):
    """The probe refuses private hosts before connecting; that guard is tested
    elsewhere and would otherwise resolve DNS in every case here."""
    monkeypatch.setattr(binding, "assert_safe_remote_host", lambda *a, **k: None)


# ── the table itself ──────────────────────────────────────────────────────────

def test_every_host_preset_carries_guidance():
    # The anti-drift guarantee: hosts are DERIVED, so "has a preset but no help
    # text" is unreachable rather than merely discouraged.
    assert set(ea.IMAP_SMTP_DEFAULTS) <= set(ea.MAIL_PROVIDERS)
    for domain in ea.IMAP_SMTP_DEFAULTS:
        hint = ea.auth_failure_hint(domain)
        assert hint["provider"], domain
        assert hint["auth"] != "unknown", domain
        assert hint["help_url"], domain


def test_every_provider_record_is_well_formed():
    for domain, provider in ea.MAIL_PROVIDERS.items():
        assert provider["name"], domain
        assert provider["auth"] in ea.AUTH_KINDS and provider["auth"] != "unknown", domain
        assert provider["help_url"].startswith("https://"), domain
        assert isinstance(provider["enable_imap"], bool), domain
        # A provider is either fully reachable or deliberately hostless (Proton
        # behind Bridge, Tuta without IMAP). A half-filled row would default one
        # side of the account to a host that is not the provider's.
        keys = {"imap_host", "smtp_host"} & set(provider)
        assert keys in ({"imap_host", "smtp_host"}, set()), domain


def test_hostless_providers_are_absent_from_the_host_presets():
    for domain in ("proton.me", "pm.me", "tuta.com", "tutanota.com"):
        assert domain in ea.MAIL_PROVIDERS
        assert domain not in ea.IMAP_SMTP_DEFAULTS


# ── the guidance ──────────────────────────────────────────────────────────────

def test_gmx_names_both_of_its_causes():
    # The reported failure: 2FA is on, so the mailbox password can never work,
    # and GMX additionally ships POP3/IMAP switched off.
    hint = ea.auth_failure_hint("someone@gmx.de")
    assert hint["auth"] == "app_password"
    assert hint["enable_imap"] is True
    assert "app-specific password" in hint["text"]
    assert "switched on" in hint["text"]
    assert "gmx.net" in hint["help_url"]


@pytest.mark.parametrize("domain,auth", [
    ("web.de", "app_password"), ("yahoo.com", "app_password"),
    ("icloud.com", "app_password"), ("t-online.de", "mail_password"),
    ("outlook.com", "oauth"), ("proton.me", "bridge"), ("tuta.com", "none"),
    ("posteo.de", "password"),
])
def test_known_providers_answer_with_their_own_auth_kind(domain, auth):
    assert ea.auth_failure_hint(f"user@{domain}")["auth"] == auth


def test_unknown_domain_still_gets_actionable_advice():
    # The fallback: never an empty hint, because "authentication failed" from
    # the server names nothing the reader can go and change.
    hint = ea.auth_failure_hint("user@self-hosted.example")
    assert hint["provider"] is None and hint["auth"] == "unknown"
    assert "password" in hint["text"] and "app-specific password" in hint["text"]


def test_hint_accepts_a_bare_domain_as_well_as_an_address():
    assert ea.auth_failure_hint("gmx.de") == ea.auth_failure_hint("a@GMX.DE")


# ── the probe ─────────────────────────────────────────────────────────────────

def test_unknown_domain_without_a_host_never_probes_gmail(monkeypatch):
    # The old fallback sent every unknown domain to imap.gmail.com, so the error
    # named Google for an address that had nothing to do with it.
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *a, **k: pytest.fail("connected anyway"))
    ok, err, hint = ea.test_imap_login("user@self-hosted.example", "pw")
    assert ok is False
    assert "Advanced" in err
    assert "app-specific password" in hint


def test_refused_login_carries_the_hint_and_a_broken_connection_does_not(monkeypatch, no_host_check):
    class _Refuses:
        def __init__(self, *a, **k): pass
        def login(self, *a): raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _Refuses)
    ok, err, hint = ea.test_imap_login("user@gmx.de", "pw")
    assert ok is False and "AUTHENTICATIONFAILED" in err
    assert hint and "app-specific password" in hint

    # A name-resolution or TLS failure is not an app-password problem; answering
    # it with app-password advice sends the reader to the wrong settings page.
    monkeypatch.setattr(imaplib, "IMAP4_SSL",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("Name or service not known")))
    ok, err, hint = ea.test_imap_login("user@gmx.de", "pw")
    assert ok is False and hint is None


def test_successful_login_uses_the_preset_host(monkeypatch, no_host_check):
    seen = {}

    class _Accepts:
        def __init__(self, host, port=None, **k): seen.update(host=host, port=port)
        def login(self, *a): return ("OK", [b""])
        def noop(self): return ("OK", [b""])
        def logout(self): return ("BYE", [b""])

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _Accepts)
    assert ea.test_imap_login("user@gmx.de", "pw") == (True, "", None)
    assert seen == {"host": "imap.gmx.net", "port": 993}


# ── the route the panel calls ─────────────────────────────────────────────────

def test_accounts_test_sends_the_parts_a_localized_ui_needs(monkeypatch):
    monkeypatch.setattr(ea, "test_imap_login",
                        lambda *a, **k: (False, "AUTHENTICATIONFAILED", "english prose"))
    out = asyncio.run(mr.accounts_test({"email": "user@gmx.de", "password": "pw"}, _USER))
    assert out["ok"] is False and out["hint"] == "english prose"
    assert out["hint_detail"]["auth"] == "app_password"
    assert out["hint_detail"]["enable_imap"] is True


def test_accounts_test_omits_the_parts_when_the_login_was_never_refused(monkeypatch):
    monkeypatch.setattr(ea, "test_imap_login", lambda *a, **k: (False, "timed out", None))
    out = asyncio.run(mr.accounts_test({"email": "user@gmx.de", "password": "pw"}, _USER))
    assert "hint_detail" not in out


# ── the UI half, guarded from here because it cannot guard itself ─────────────

def _mail_v2(locale):
    return json.loads((_REPO / "web" / "messages" / f"{locale}.json").read_bytes())["mailV2"]


# CRLF-tolerant on purpose: without .gitattributes coverage for .tsx, a Windows
# checkout (autocrlf) ends every line in \r\n, and a bare `$` anchor after the
# comma then matches nothing - findall came back EMPTY on the Windows CI leg
# while the same file parsed fine everywhere else.
_AUTH_MAP_RE = r"^\s{4}(\w+): '(authHint\w+)',\r?$"


def test_every_auth_kind_has_wording_in_every_locale():
    # Rule 2: the auth kinds are a registry with a copy in the message
    # catalogues. Adding a kind without wording renders a blank line.
    tsx = (_REPO / "web" / "components" / "connections" / "MailAccounts.tsx").read_bytes().decode("utf-8")
    mapped = dict(re.findall(_AUTH_MAP_RE, tsx, re.M))
    assert set(mapped) == set(ea.AUTH_KINDS), "MailAccounts.tsx AUTH_MESSAGE drifted from AUTH_KINDS"
    for locale in ("en", "de"):
        catalogue = _mail_v2(locale)
        for kind, key in mapped.items():
            assert key in catalogue, f"{locale}.json is missing {key} for auth kind {kind}"
        for key in ("authHintEnableImap", "authHintHelp"):
            assert key in catalogue, f"{locale}.json is missing {key}"


def test_the_auth_map_regex_survives_a_crlf_checkout():
    """The class, executable on Linux: the Windows shape is a literal sample."""
    crlf_sample = "    imap: 'authHintPassword',\r\n    oauth: 'authHintOauth',\r\n"
    assert dict(re.findall(_AUTH_MAP_RE, crlf_sample, re.M)) == {
        "imap": "authHintPassword", "oauth": "authHintOauth"}


def test_provider_placeholder_is_present_wherever_the_backend_supplies_one():
    for locale in ("en", "de"):
        catalogue = _mail_v2(locale)
        for key in ("authHintAppPassword", "authHintMailPassword", "authHintOauth",
                    "authHintBridge", "authHintNoImap", "authHintPassword",
                    "authHintEnableImap", "authHintHelp"):
            assert "{provider}" in catalogue[key], f"{locale}.json {key} drops the provider name"
        # The unknown-domain line has no provider to name, so it must not ask
        # for one: next-intl throws on a missing placeholder value.
        assert "{provider}" not in catalogue["authHintUnknown"]


def test_the_servers_reply_is_shown_without_imaplibs_bytes_wrapper(monkeypatch, no_host_check):
    # imaplib raises with the raw bytes, so the panel used to print
    # b'Authentication failed.' at the reader.
    class _Refuses:
        def __init__(self, *a, **k): pass
        def login(self, *a): raise imaplib.IMAP4.error(b"Authentication failed.")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _Refuses)
    _, err, _ = ea.test_imap_login("user@gmx.de", "pw")
    assert err == "Authentication failed."
