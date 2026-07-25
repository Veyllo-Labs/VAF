# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""P7.2: what /api/email is allowed to be after the legacy teardown.

The router kept exactly one job - the shared OAuth + account hub - and that job is
NOT mail-specific: the Calendar wizard mints its consent through `/oauth/start`
(calendar_routes has no OAuth endpoint of its own), and the Connections tile and
Calendar dashboard read `/accounts`. Deleting one of those by accident is close to
silent: web_server swallows a router ImportError into a single log line and starts
normally, so the whole prefix would just be missing at runtime.

The registered redirect_uri is the sharpest edge: `/api/email/oauth/callback` is
configured verbatim at Google and Azure, so a rename by one character breaks
sign-in for mail, calendar and cloud at once. This test states the surface as data
rather than prose (CLAUDE.md Rule 2: prefer a guard over a rule)."""
import vaf.api.email_routes as email_routes

_KEEP = {
    "/api/email/oauth/start",
    "/api/email/oauth/callback",          # registered at Google/Azure - never rename
    "/api/email/oauth-status",
    "/api/email/accounts",
    "/api/email/accounts/test",
    "/api/email/accounts/{account_id}",
    "/api/email/accounts/{account_id}/verify",
}


def _paths() -> set:
    return {r.path for r in email_routes.router.routes}


def test_surface_is_exactly_the_oauth_and_accounts_hub():
    assert _paths() == _KEEP


def test_the_registered_oauth_callback_path_still_exists():
    """Its own assertion: this is the one path an external system knows by heart."""
    assert "/api/email/oauth/callback" in _paths()


def test_no_message_or_sync_surface_survives():
    """The legacy viewer + the 30-minute auto-sync engine are gone; mail data is
    served by /api/mail only."""
    for path in _paths():
        assert "messages" not in path, f"legacy message route survived: {path}"
        assert "categories" not in path, f"legacy category route survived: {path}"
        assert not path.endswith("/sync"), f"legacy sync route survived: {path}"
    assert not hasattr(email_routes, "run_auto_sync_all_accounts")
    assert not hasattr(email_routes, "EMAIL_AUTO_SYNC_INTERVAL_SEC")


def test_the_auto_sync_loop_is_gone_from_the_web_server():
    """It ran unconditionally next to the engine supervisor, fetching every INBOX a
    second time into the legacy store."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert "_email_auto_sync_loop" not in src
    assert "run_auto_sync_all_accounts" not in src
    assert "MailSyncSupervisor" in src        # the remaining sync lane stays


def test_account_test_endpoint_stays_rate_limited():
    """The probe takes a password, so it must remain on the auth rate-limit
    allowlist after the trim."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "vaf" / "auth" / "rate_limit.py").read_text(encoding="utf-8")
    assert "/api/email/accounts/test" in src
