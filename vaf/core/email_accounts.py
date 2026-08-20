# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Route-independent SSOT for per-user email account CONFIG (metadata only;
credentials live in credential_store).

Why this module exists (mail v2-only port, Phase 1): the account-config reader
used to live in vaf/api/email_routes.py and a drifting near-duplicate in
vaf/core/email_transport.py. Both the v2 mail engine (mail_routes/supervisor),
the calendar client and the label_mail tool imported them, which put a FastAPI
route module on everyone's import path and blocked the eventual legacy teardown.
This module is the single home; the old private names (`_get_email_config` /
`_save_email_config`) are re-exported from their historical locations so existing
importers keep working, and a guard test pins them to the same objects.

Scope isolation (unchanged behavior, carried over verbatim from the email_routes
copy): a user_scope_id resolves email_config_by_scope FIRST and never falls back
across scopes; the local admin uses the legacy email_config blob; a non-admin
username without a scope resolves email_config_by_user.
"""
import re
from typing import Any, Dict, List, Optional

from vaf.core.config import (
    Config,
    get_local_admin_scope_id,
    get_local_admin_username,
)


def get_email_config(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the email config for the given user. When username is None or the
    local admin, use the legacy email_config. If user_scope_id is set,
    email_config_by_scope is tried first; otherwise a username-based lookup."""
    local_admin_scope = get_local_admin_scope_id()
    if user_scope_id:
        by_scope = Config.get("email_config_by_scope") or {}
        if isinstance(by_scope, dict):
            ec = by_scope.get(str(user_scope_id).strip())
            if isinstance(ec, dict) and ec.get("accounts") is not None:
                return ec
        if str(user_scope_id).strip() == str(local_admin_scope).strip():
            raw = Config.get("email_config")
            if isinstance(raw, dict):
                return raw
            return {"accounts": []}
    local_admin = get_local_admin_username().lower()
    if not username or username.strip().lower() == local_admin:
        raw = Config.get("email_config")
        if isinstance(raw, dict):
            return raw
        return {"accounts": []}
    by_user = Config.get("email_config_by_user") or {}
    ec = by_user.get(username.strip(), {}) if isinstance(by_user, dict) else {}
    return ec if isinstance(ec, dict) else {"accounts": []}


def save_email_config(
    ec: Dict[str, Any],
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> None:
    """Persist the email config for the given user. When username is None or the
    local admin, write the legacy email_config. If user_scope_id is set, write to
    email_config_by_scope; otherwise a username-based write."""
    config = Config.load()
    local_admin_scope = get_local_admin_scope_id()
    if user_scope_id and str(user_scope_id).strip() != str(local_admin_scope).strip():
        by_scope = config.get("email_config_by_scope") or {}
        if not isinstance(by_scope, dict):
            by_scope = {}
        by_scope[str(user_scope_id).strip()] = ec
        config["email_config_by_scope"] = by_scope
        Config.save(config)
        return
    local_admin = get_local_admin_username().lower()
    if not username or username.strip().lower() == local_admin:
        config["email_config"] = ec
    else:
        by_user = config.get("email_config_by_user") or {}
        if not isinstance(by_user, dict):
            by_user = {}
        by_user[username.strip()] = ec
        config["email_config_by_user"] = by_user
    Config.save(config)


# ── account lookup + sender-category rules (relocated from email_transport, P3.1) ──
# These read only the account-config blob this module already owns, so the agent
# tools and the sync path can resolve accounts/rules without importing the heavy
# email_transport module. email_transport re-exports them under their old names.

def _email_config_candidates(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[tuple]:
    """The current user's config candidate only (no cross-scope fallback)."""
    ec = get_email_config(username, user_scope_id=user_scope_id)
    if isinstance(ec, dict) and (ec.get("accounts") or []):
        return [(ec, user_scope_id)]
    return []


def get_account(
    account_id: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return account metadata for account_id (email or account_id). Optional
    username/user_scope_id for multi-user scope."""
    want = (account_id or "").strip().lower()
    for ec, _ in _email_config_candidates(username, user_scope_id):
        for a in ec.get("accounts") or []:
            if (a.get("account_id") or a.get("email") or "").strip().lower() == want:
                return a
    return None


def get_sender_rules(
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Sender->category rules from config. Each: {"pattern": "twitch.tv",
    "category": "social"}. First match wins."""
    ec = get_email_config(username, user_scope_id=user_scope_id)
    rules = ec.get("sender_category_rules")
    if not isinstance(rules, list):
        return []
    out: List[Dict[str, str]] = []
    for r in rules:
        if isinstance(r, dict) and r.get("pattern") and r.get("category"):
            out.append({
                "pattern": str(r["pattern"]).strip(),
                "category": str(r["category"]).strip().lower().replace(" ", "_")[:64] or "primary",
            })
    return out


def apply_sender_rules_to_category(
    from_str: str,
    current_category: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
    rules: Optional[List[Dict[str, str]]] = None,
) -> str:
    """If any rule's pattern is contained in from_str (case-insensitive), return
    that category; else current_category. Used on sync and on backfill.

    `rules` lets a caller pass a pre-loaded rule list so a per-message loop does
    not re-read the config for every message; the matching itself stays here so
    there is only ever one implementation of it (Rule 2)."""
    if rules is None:
        rules = get_sender_rules(username, user_scope_id=user_scope_id)
    from_lower = (from_str or "").lower()
    for r in rules:
        pattern = (r.get("pattern") or "").lower()
        if pattern and pattern in from_lower:
            return r.get("category") or current_category
    return current_category


def pattern_from_from_addr(from_addr: str) -> str:
    """Derive a sender-rule pattern from a From header
    ('Twitch <no-reply@twitch.tv>' -> 'no-reply@twitch.tv'). Relocated from
    email_routes (P5.4); re-exported there to keep one implementation."""
    s = (from_addr or "").strip()
    if not s:
        return s
    m = re.search(r"<([^>]+@[^>]+)>", s)
    if m:
        return m.group(1).strip().lower()
    if "@" in s:
        return s.lower()
    return s


def upsert_sender_rule(
    pattern: str,
    category: str,
    username: Optional[str] = None,
    user_scope_id: Optional[str] = None,
) -> bool:
    """Add or replace a sender->category rule in the config blob for the given
    identity (last write for a pattern wins). Returns False for an empty pattern.
    Category is normalized like the readers (lowercase/underscores/64-char cap)."""
    pat = (pattern or "").strip().lower()
    if not pat:
        return False
    cat = re.sub(r"\s+", "_", str(category or "").strip().lower())[:64] or "primary"
    ec = get_email_config(username, user_scope_id=user_scope_id)
    rules = [r for r in (ec.get("sender_category_rules") or [])
             if isinstance(r, dict) and (r.get("pattern") or "").strip().lower() != pat]
    rules.append({"pattern": pat, "category": cat})
    ec["sender_category_rules"] = rules
    save_email_config(ec, username, user_scope_id=user_scope_id)
    return True


# ── IMAP/SMTP presets + connection probe (relocated from email_routes, P4.1) ──
#
# ONE table, deliberately. Hosts and "why was my password refused" used to be
# two separate things: IMAP_SMTP_DEFAULTS knew thirteen domains, the failure
# hint knew two of them (Gmail, Outlook). Every other provider answered a
# refused login with the bare server string and nothing the user could act on,
# although the reason is nearly always one of a handful of known ones. Hosts
# are DERIVED from this table below, so a provider can no longer arrive with
# server settings but without guidance.
#
# `auth` names the credential the server actually accepts:
#   "password"       the mailbox password; a provider with 2FA may still issue a
#                    separate app password, which the wording for this case says
#   "app_password"   a generated credential. Once 2FA is on the mailbox password
#                    is refused by construction: IMAP has no second-factor round,
#                    so there is no way for the client to present the code
#   "mail_password"  a distinct mail-program password that exists WITHOUT 2FA
#   "oauth"          basic auth retired; only the provider sign-in works
#   "bridge"         IMAP served only by a local bridge application
#   "none"           the provider offers no IMAP at all
# `enable_imap` marks providers that ship IMAP switched off. The United Internet
# family also switches it back off after a long idle period, which turns a
# working account into an authentication failure with no user action at all.
#
# Every host here answered a TLS IMAP/SMTP greeting on 993/587 when it was
# entered; every alias domain resolves to its operator's MX records.

_PROVIDER_RECORDS = (
    (("gmail.com", "googlemail.com"),
     {"name": "Gmail", "auth": "app_password",
      "help_url": "https://support.google.com/accounts/answer/185833",
      "imap_host": "imap.gmail.com", "smtp_host": "smtp.gmail.com"}),
    (("outlook.com", "outlook.de", "hotmail.com", "hotmail.de",
      "live.com", "live.de", "msn.com"),
     {"name": "Outlook.com", "auth": "oauth",
      "help_url": "https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/deprecation-of-basic-authentication-exchange-online",
      "imap_host": "outlook.office365.com", "smtp_host": "smtp.office365.com"}),
    # Yahoo and AOL share one stack; both refuse the account password outright.
    (("yahoo.com", "yahoo.de", "yahoo.co.uk", "yahoo.fr", "ymail.com", "rocketmail.com"),
     {"name": "Yahoo Mail", "auth": "app_password",
      "help_url": "https://help.yahoo.com/kb/SLN15241.html",
      "imap_host": "imap.mail.yahoo.com", "smtp_host": "smtp.mail.yahoo.com"}),
    (("aol.com", "aol.de"),
     {"name": "AOL Mail", "auth": "app_password",
      "help_url": "https://help.aol.com/articles/create-and-manage-app-password",
      "imap_host": "imap.aol.com", "smtp_host": "smtp.aol.com"}),
    (("icloud.com", "me.com", "mac.com"),
     {"name": "iCloud Mail", "auth": "app_password",
      "help_url": "https://support.apple.com/en-us/102654",
      "imap_host": "imap.mail.me.com", "smtp_host": "smtp.mail.me.com"}),
    # United Internet family: an app password AND an IMAP switch, and the switch
    # turns itself off again when the account is not polled for a long time.
    (("gmx.de", "gmx.net", "gmx.at", "gmx.ch", "gmx.com"),
     {"name": "GMX", "auth": "app_password", "enable_imap": True,
      "help_url": "https://hilfe.gmx.net/sicherheit/2fa/anwendungsspezifisches-passwort.html",
      "imap_host": "imap.gmx.net", "smtp_host": "mail.gmx.net"}),
    (("web.de",),
     {"name": "WEB.DE", "auth": "app_password", "enable_imap": True,
      "help_url": "https://hilfe.web.de/sicherheit/2fa/anwendungsspezifisches-passwort.html",
      "imap_host": "imap.web.de", "smtp_host": "smtp.web.de"}),
    (("mail.com",),
     {"name": "mail.com", "auth": "app_password", "enable_imap": True,
      "help_url": "https://support.mail.com/pop-imap/index.html",
      "imap_host": "imap.mail.com", "smtp_host": "smtp.mail.com"}),
    # Telekom issues a mail-program password that exists without any 2FA, and
    # entering the customer-centre password instead is the usual cause here.
    (("t-online.de",),
     {"name": "Telekom", "auth": "mail_password",
      "help_url": "https://www.telekom.de/hilfe/apps-dienste/e-mail/app-programm-passwort",
      "imap_host": "secureimap.t-online.de", "smtp_host": "securesmtp.t-online.de"}),
    (("ionos.de",),
     {"name": "IONOS", "auth": "password",
      "help_url": "https://www.ionos.de/hilfe/e-mail/",
      "imap_host": "imap.ionos.de", "smtp_host": "smtp.ionos.de"}),
    (("1und1.de",),
     {"name": "1&1", "auth": "password",
      "help_url": "https://www.ionos.de/hilfe/e-mail/",
      "imap_host": "imap.1und1.de", "smtp_host": "smtp.1und1.de"}),
    (("freenet.de",),
     {"name": "freenet", "auth": "password",
      "help_url": "https://kundenservice.freenet.de/",
      "imap_host": "mx.freenet.de", "smtp_host": "mx.freenet.de"}),
    (("posteo.de", "posteo.net", "posteo.eu"),
     {"name": "Posteo", "auth": "password",
      "help_url": "https://posteo.de/en/help",
      "imap_host": "posteo.de", "smtp_host": "posteo.de"}),
    (("mailbox.org",),
     {"name": "mailbox.org", "auth": "app_password",
      "help_url": "https://kb.mailbox.org/",
      "imap_host": "imap.mailbox.org", "smtp_host": "smtp.mailbox.org"}),
    (("zoho.com",),
     {"name": "Zoho Mail", "auth": "app_password", "enable_imap": True,
      "help_url": "https://www.zoho.com/mail/help/imap-access.html",
      "imap_host": "imap.zoho.com", "smtp_host": "smtp.zoho.com"}),
    (("zoho.eu",),
     {"name": "Zoho Mail", "auth": "app_password", "enable_imap": True,
      "help_url": "https://www.zoho.com/mail/help/imap-access.html",
      "imap_host": "imap.zoho.eu", "smtp_host": "smtp.zoho.eu"}),
    (("fastmail.com", "fastmail.fm"),
     {"name": "Fastmail", "auth": "app_password",
      "help_url": "https://www.fastmail.help/hc/en-us/articles/360058752854-App-passwords",
      "imap_host": "imap.fastmail.com", "smtp_host": "smtp.fastmail.com"}),
    # Yandex refuses the account password over IMAP even with 2FA switched off.
    (("yandex.com", "yandex.ru", "ya.ru"),
     {"name": "Yandex Mail", "auth": "app_password", "enable_imap": True,
      "help_url": "https://yandex.com/support/yandex-360/customers/mail/en/mail-clients/others",
      "imap_host": "imap.yandex.com", "smtp_host": "smtp.yandex.com"}),
    (("mail.ru", "inbox.ru", "bk.ru", "list.ru"),
     {"name": "Mail.ru", "auth": "app_password",
      "help_url": "https://help.mail.ru/mail/login/mailer/",
      "imap_host": "imap.mail.ru", "smtp_host": "smtp.mail.ru"}),
    # No host defaults on purpose: Proton's IMAP endpoint is the Bridge on
    # localhost, so there is no server for us to guess. Whoever runs Bridge
    # types 127.0.0.1 under Advanced, and everyone else gets told why.
    (("proton.me", "protonmail.com", "protonmail.ch", "pm.me"),
     {"name": "Proton Mail", "auth": "bridge",
      "help_url": "https://proton.me/support/protonmail-bridge-install"}),
    (("tuta.com", "tutanota.com", "tutanota.de", "tutamail.com", "keemail.me"),
     {"name": "Tuta", "auth": "none",
      "help_url": "https://tuta.com/support"}),
    (("vodafone.de", "arcor.de", "kabelmail.de"),
     {"name": "Vodafone", "auth": "password",
      "help_url": "https://www.vodafone.de/privat/hilfe.html",
      "imap_host": "imap.vodafone.de", "smtp_host": "smtp.vodafone.de"}),
    (("ok.de",),
     {"name": "ok.de", "auth": "password",
      "help_url": "https://www.ok.de/",
      "imap_host": "imap.ok.de", "smtp_host": "smtp.ok.de"}),
    (("bluewin.ch",),
     {"name": "Bluewin", "auth": "password",
      "help_url": "https://www.swisscom.ch/de/privatkunden/hilfe/e-mail.html",
      "imap_host": "imaps.bluewin.ch", "smtp_host": "smtpauths.bluewin.ch"}),
    (("a1.net", "aon.at"),
     {"name": "A1", "auth": "password",
      "help_url": "https://www.a1.net/kontakt",
      "imap_host": "imap.a1.net", "smtp_host": "smtp.a1.net"}),
    (("orange.fr", "wanadoo.fr"),
     {"name": "Orange", "auth": "password",
      "help_url": "https://assistance.orange.fr/",
      "imap_host": "imap.orange.fr", "smtp_host": "smtp.orange.fr"}),
    (("laposte.net",),
     {"name": "La Poste", "auth": "password",
      "help_url": "https://aide.laposte.net/",
      "imap_host": "imap.laposte.net", "smtp_host": "smtp.laposte.net"}),
    (("libero.it", "iol.it", "inwind.it"),
     {"name": "Libero", "auth": "password",
      "help_url": "https://aiuto.libero.it/",
      "imap_host": "imapmail.libero.it", "smtp_host": "smtp.libero.it"}),
    (("seznam.cz", "email.cz"),
     {"name": "Seznam", "auth": "password",
      "help_url": "https://o-seznam.cz/napoveda/",
      "imap_host": "imap.seznam.cz", "smtp_host": "smtp.seznam.cz"}),
)

MAIL_PROVIDERS: Dict[str, Dict[str, Any]] = {
    domain: {"imap_port": 993, "smtp_port": 587, "enable_imap": False, **record}
    for domains, record in _PROVIDER_RECORDS for domain in domains
}

_HOST_FIELDS = ("imap_host", "imap_port", "smtp_host", "smtp_port")

# Derived, never hand-maintained: the host half of MAIL_PROVIDERS for the
# callers that only want server settings. Providers with no reachable server of
# their own (Proton behind Bridge, Tuta without IMAP) are absent by design, so a
# caller defaulting from this dict gets nothing to connect to rather than a
# wrong host.
IMAP_SMTP_DEFAULTS: Dict[str, Dict[str, Any]] = {
    domain: {field: provider[field] for field in _HOST_FIELDS}
    for domain, provider in MAIL_PROVIDERS.items() if provider.get("imap_host")
}

_AUTH_GUIDANCE = {
    "password": ("Check the password for {provider}. With two-factor authentication on, the "
                 "provider may issue a separate app password for mail programs."),
    "app_password": ("{provider} requires an app-specific password once two-factor authentication "
                     "is on. IMAP has no second-factor step, so the normal password is refused."),
    "mail_password": ("{provider} uses a separate password for mail programs, not the account "
                      "password."),
    "oauth": "{provider} no longer accepts a password over IMAP. Use the provider sign-in instead.",
    "bridge": "{provider} serves IMAP only through its local Bridge application.",
    "none": "{provider} offers no IMAP access at all.",
}

_GENERIC_GUIDANCE = (
    "Check the password, and check the provider's own settings for what IMAP needs there: many "
    "providers require an app-specific password once two-factor authentication is on, and some "
    "ship IMAP access switched off."
)

_ENABLE_IMAP_GUIDANCE = "IMAP access has to be switched on in the {provider} settings first."

AUTH_KINDS = tuple(_AUTH_GUIDANCE) + ("unknown",)


def auth_failure_hint(address: str) -> Dict[str, Any]:
    """What to tell a user whose IMAP login was refused, for the domain of
    `address` (a full email address or a bare domain).

    This always answers. An unknown domain gets the generic advice rather than
    nothing, because "authentication failed" straight from the server names no
    action the reader can take, and the two causes behind almost every case
    (an app-specific password, an IMAP switch that is off) are invisible from
    the client side.

    The parts are returned separately - provider, auth, enable_imap, help_url -
    so a localized UI composes its own sentence instead of showing English
    prose; `text` is that same guidance rendered in English for callers that
    only have room for a string.
    """
    domain = (address or "").strip().lower().rsplit("@", 1)[-1]
    provider = MAIL_PROVIDERS.get(domain)
    if not provider:
        return {"provider": None, "auth": "unknown", "enable_imap": False,
                "help_url": None, "text": _GENERIC_GUIDANCE}
    name = provider["name"]
    parts = [_AUTH_GUIDANCE[provider["auth"]].format(provider=name)]
    if provider["enable_imap"]:
        parts.append(_ENABLE_IMAP_GUIDANCE.format(provider=name))
    parts.append(provider["help_url"])
    return {"provider": name, "auth": provider["auth"],
            "enable_imap": provider["enable_imap"], "help_url": provider["help_url"],
            "text": " ".join(parts)}


def _imap_error_text(exc: Exception) -> str:
    """The server's reply, without imaplib's wrapping. imaplib raises with the
    raw bytes, so str(e) reads b'Authentication failed.' - the b and the quotes
    are noise in a panel that is otherwise showing the provider's own words."""
    text = str(exc).strip()
    unwrapped = re.fullmatch(r"b(['\"])(.*)\1", text, re.S)
    return (unwrapped.group(2) if unwrapped else text).strip()


def test_imap_login(email: str, password: str, imap_host: Optional[str] = None,
                    imap_port: Optional[int] = None) -> tuple:
    """Try an IMAP login with the given credentials; saves nothing. Returns
    (success, error_message, hint).

    hint is auth_failure_hint()'s guidance for the address domain, and it rides
    along only when the SERVER refused the login. A DNS, TLS or timeout failure
    is not an app-password problem, and answering one with app-password advice
    sends the reader to the wrong settings page."""
    import imaplib
    import ssl as _ssl

    from vaf.network.binding import assert_safe_remote_host
    email = (email or "").strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    defaults = IMAP_SMTP_DEFAULTS.get(domain, {})
    host = (imap_host or "").strip() or defaults.get("imap_host") or ""
    port = imap_port if imap_port is not None else defaults.get("imap_port", 993)
    hint = auth_failure_hint(domain)["text"]
    if not host:
        # This used to fall back to imap.gmail.com for every unknown domain, so a
        # Proton, Tuta or self-hosted address was probed against a server it was
        # never going to authenticate on and the error came back naming Google.
        return False, "No IMAP server is known for this address. Enter one under Advanced.", hint
    try:
        assert_safe_remote_host(host, allow_private=bool(Config.get("email_allow_private_hosts", False)))
    except ValueError as e:
        return False, str(e), None
    try:
        conn = imaplib.IMAP4_SSL(host, port=port, ssl_context=_ssl.create_default_context(), timeout=30)
        conn.login(email, password)
        conn.noop()
        conn.logout()
        return True, "", None
    except imaplib.IMAP4.error as e:
        return False, (_imap_error_text(e) or "IMAP login failed"), hint
    except Exception as e:
        return False, (str(e).strip() or "Connection failed"), None


# ── account-config CRUD (P4.1): the /api/mail account endpoints build on these ──

def _acct_key(a: Dict[str, Any]) -> str:
    return (a.get("account_id") or a.get("email") or "").strip().lower()


def add_account(entry: Dict[str, Any], username: Optional[str] = None,
                user_scope_id: Optional[str] = None) -> None:
    """Insert or replace an account entry (matched by account_id/email)."""
    ec = dict(get_email_config(username, user_scope_id=user_scope_id) or {})
    k = (entry.get("account_id") or entry.get("email") or "").strip().lower()
    accounts = [a for a in (ec.get("accounts") or []) if _acct_key(a) != k]
    accounts.append(entry)
    ec["accounts"] = accounts
    save_email_config(ec, username, user_scope_id=user_scope_id)


def oauth_provider_for(account_id: str, username: Optional[str] = None,
                       user_scope_id: Optional[str] = None) -> Optional[str]:
    """The OAuth provider already connected for this address, if any.

    Adding a password/IMAP account for an address that is ALREADY connected as
    gmail/microsoft would REPLACE that entry, and `calendar_client` resolves
    calendars by exactly that provider - so the calendar would silently lose the
    account. Callers use this to refuse the overwrite and point the user at the
    sign-in instead (which, since the connect flow requests engine scopes, needs
    no app password at all)."""
    acc = get_account(account_id, username, user_scope_id=user_scope_id)
    provider = (acc or {}).get("provider", "")
    provider = str(provider).lower()
    return provider if provider in ("gmail", "microsoft") else None


def patch_account(account_id: str, fields: Dict[str, Any], username: Optional[str] = None,
                  user_scope_id: Optional[str] = None) -> bool:
    ec = dict(get_email_config(username, user_scope_id=user_scope_id) or {})
    accounts = list(ec.get("accounts") or [])
    k = (account_id or "").strip().lower()
    hit = False
    for a in accounts:
        if _acct_key(a) == k:
            a.update(fields)
            hit = True
    if hit:
        ec["accounts"] = accounts
        save_email_config(ec, username, user_scope_id=user_scope_id)
    return hit


def remove_account(account_id: str, username: Optional[str] = None,
                   user_scope_id: Optional[str] = None) -> bool:
    ec = dict(get_email_config(username, user_scope_id=user_scope_id) or {})
    k = (account_id or "").strip().lower()
    accounts = list(ec.get("accounts") or [])
    kept = [a for a in accounts if _acct_key(a) != k]
    if len(kept) == len(accounts):
        return False
    ec["accounts"] = kept
    save_email_config(ec, username, user_scope_id=user_scope_id)
    return True


def set_mail_enabled(account_id: str, enabled: bool, username: Optional[str] = None,
                     user_scope_id: Optional[str] = None) -> bool:
    """Deliberate: deleting a mail account that ALSO backs Calendar
    keeps its shared OAuth token AND config entry, but flips mail_enabled=False so
    the mail account list hides it while Calendar still resolves it. The marker
    lives in the account dict - no new config key."""
    return patch_account(account_id, {"mail_enabled": bool(enabled)}, username, user_scope_id=user_scope_id)


def list_mail_accounts(username: Optional[str] = None,
                       user_scope_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Accounts to show in the mail UI: everything except mail-disabled entries
    (calendar-only leftovers). A missing mail_enabled means True (existing accounts)."""
    ec = get_email_config(username, user_scope_id=user_scope_id)
    return [a for a in (ec.get("accounts") or []) if a.get("mail_enabled", True) is not False]


def delete_mail_account(account_id: str, username: Optional[str] = None,
                        cred_username: Optional[str] = None,
                        user_scope_id: Optional[str] = None) -> Dict[str, Any]:
    """Calendar-safe account delete (EMAIL_CLIENT.md fail-closed
    lifecycle), centralized so the legacy route and the /api/mail endpoint share
    ONE cascade. Drops the v2 store rows/blobs/FTS/ops for the account and its
    MAIL-only IMAP credential lanes. A gmail/microsoft account shares its OAuth
    token with Calendar (Gmail union token; Microsoft's calendar token) - so the
    shared token AND the config entry are KEPT, the entry just flagged
    mail_enabled=False so the mail list hides it while Calendar still resolves it
    (its `enabled` stays true). A mail-only account (imap/icloud) is fully removed."""
    cred_username = cred_username if cred_username is not None else username
    acc = get_account(account_id, username, user_scope_id=user_scope_id) or {}
    provider = (acc.get("provider") or "imap").lower()
    backs_calendar = provider in ("gmail", "microsoft")
    result: Dict[str, Any] = {"ok": True, "backs_calendar": backs_calendar, "kept_for_calendar": False}
    # Store cascade, best-effort: a never-synced account legitimately has no rows.
    try:
        from vaf.mail.store import MailStore
        scope = (user_scope_id or "").strip() or get_local_admin_scope_id()
        MailStore(scope).delete_account(account_id)
    except Exception as e:  # pragma: no cover - defensive
        result["store_error"] = str(e)
    from vaf.core.credential_store import delete_email_credentials
    if backs_calendar:
        # NEVER revoke the shared OAuth token; drop only the mail-only IMAP lanes.
        for lane in ("imap", "microsoft_imap", "apple", "icloud"):
            delete_email_credentials(account_id, lane, cred_username, user_scope_id=user_scope_id)
        set_mail_enabled(account_id, False, username, user_scope_id=user_scope_id)
        result["kept_for_calendar"] = True
    else:
        delete_email_credentials(account_id, provider=None, username=cred_username, user_scope_id=user_scope_id)
        remove_account(account_id, username, user_scope_id=user_scope_id)
    return result
