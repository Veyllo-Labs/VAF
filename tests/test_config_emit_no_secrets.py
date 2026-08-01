# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Provider API keys must not travel to ANY browser - and the echo proved why.

Non-admins were always protected: `api_key_` is a secret prefix and the non-admin branch of
`config_for_user` strips it. The hole was the ADMIN branch, which returned the config whole,
estate keys included. That looked like a display quirk (four fields showing dots, one empty)
and was measured doing real damage on 2026-08-01, live:

  GET /api/config handed the admin browser the estate values from config.json. The Settings
  form rendered them as dots. The next Save echoed them back, and `absorb_config_keys`
  stored the echo AS the key. For the five providers whose estate value was raw plaintext
  the echo happened to equal the key - harmless by luck. For the ONE provider whose estate
  was base64-encoded, the store afterwards held the base64 SHELL of the key, and every
  request authenticated with `c2st...` instead of `sk-...`. Poisoned by saving an unrelated
  setting, re-poisoned on every further save, found only because the owner asked why one
  field looked different from the others.

The round-trip test below is the important one: it drives the REAL route with the REAL
echo, because the leak and the poisoning are one defect seen from two sides, and a test
that only checks the GET response would pass while a second emit site kept feeding the
echo. The PATCH response is asserted separately - it used to return the merged config raw,
to non-admins too, which was a second copy of the same leak one save away from any reader.
"""
import asyncio
import base64
from types import SimpleNamespace

import pytest

from vaf.core.api_keys import resolve_api_key, store_api_key
from vaf.core.config import Config


@pytest.fixture()
def _config(monkeypatch):
    """Config on an in-memory dict, so no test run can touch a real config.json."""
    state = {"provider": "deepseek", "server_mode": False}
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: dict(state)))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, cfg: (state.clear(), state.update(cfg))[0]))
    return state


def _patch(body: dict, role: str = "admin"):
    from vaf.api.config_routes import patch_config

    user = {"role": role, "user_scope_id": "ab12cd34-0000-0000-0000-000000000000", "username": "Alice"}
    return asyncio.run(patch_config(body, SimpleNamespace(), user))


def test_the_admin_config_view_carries_no_api_key_values(_config):
    """The emit site itself: an admin's view answers every api_key_* with emptiness."""
    _config["api_key_deepseek"] = base64.b64encode(b"sk-plain").decode()
    _config["api_key_openai"] = "sk-raw-in-estate"

    view = Config.config_for_user(Config.load(), None, "admin")

    for key in ("api_key_deepseek", "api_key_openai"):
        assert view[key] == "", f"{key} travelled to the admin browser"


def test_the_echo_round_trip_cannot_poison_the_store(_config):
    """THE regression - the live defect, replayed end to end.

    Estate holds base64(key), the store holds the migrated plaintext. The admin view is
    echoed back through the real PATCH route, exactly as the Settings form does on save.
    Before the fix the store afterwards held the base64 shell; the provider would have
    refused every request made with it.
    """
    _config["api_key_deepseek"] = base64.b64encode(b"sk-plain").decode()
    store_api_key("deepseek", "sk-plain")

    echo = dict(Config.config_for_user(Config.load(), None, "admin"))
    _patch(echo)

    assert resolve_api_key("deepseek") == "sk-plain", (
        "the echo of the settings page replaced the stored key with its base64 shell"
    )


def test_a_deliberately_typed_key_still_saves_through_the_same_route(_config):
    """The counter-balance: blanking the echo must not blank the WRITE path."""
    _patch({"api_key_openai": "sk-typed-by-a-person"})

    assert resolve_api_key("openai") == "sk-typed-by-a-person"


def test_the_patch_response_carries_no_api_key_values(_config):
    """The second copy of the leak: the save's own answer."""
    _config["api_key_openai"] = "sk-raw-in-estate"

    out = _patch({"theme": "dark"})

    assert out.get("api_key_openai", "") == "", "the PATCH echo handed the estate key back"


def test_the_patch_response_to_a_non_admin_is_the_scoped_view(_config):
    """It used to return the merged config RAW to whoever sent the PATCH.

    For a non-admin that is every secret in the file - estate keys, the KEK - handed out by
    the route whose GET counterpart carefully strips them.
    """
    _config["api_key_openai"] = "sk-raw-in-estate"
    _config["secure_store_kek"] = "the-kek"

    out = _patch({"theme": "dark"}, role="user")

    assert "api_key_openai" not in out or not out["api_key_openai"]
    assert "secure_store_kek" not in out, "the KEK went to a non-admin in a PATCH response"


# ── the widening: from an api_key_ carve-out to the classifier ──────────────────────

def test_the_admin_view_blanks_every_classified_secret(_config):
    """The api_key_-only cut was an enumeration bought one incident at a time.

    The OAuth client secrets travelled by the identical mechanism and were one save away
    from the identical damage; the JWT signing secret rode along too. One classifier
    (`is_secret_config_key`) now decides what never travels, instead of a prefix list that
    grows a member per incident.
    """
    _config["email_oauth_google_client_secret"] = "GOCSPX-demo-secret"
    _config["local_network_jwt_secret"] = "jwt-demo"

    view = Config.config_for_user(Config.load(), None, "admin")

    assert view["email_oauth_google_client_secret"] == ""
    assert view["local_network_jwt_secret"] == ""


def test_the_echo_cannot_wipe_an_oauth_secret(_config):
    """The regression that exists BETWEEN the two halves of the widening.

    Blanking the admin view alone would make every save echo "" for every OAuth secret the
    form did not retype - and the old merge guard only protected `api_key_*`, so the first
    unrelated settings save would have wiped the client secrets from config.json. The two
    halves (blank at the emit, keep-on-blank in the merge) are one change; this is the test
    that fails if they are ever separated.
    """
    _config["email_oauth_google_client_secret"] = "GOCSPX-demo-secret"

    echo = dict(Config.config_for_user(Config.load(), None, "admin"))
    _patch(echo)

    assert Config.load().get("email_oauth_google_client_secret") == "GOCSPX-demo-secret", (
        "an unrelated settings save wiped an OAuth client secret"
    )


def test_a_deliberately_typed_oauth_secret_still_saves(_config):
    _patch({"github_oauth_client_secret": "ghp-typed-by-a-person"})

    assert Config.load().get("github_oauth_client_secret") == "ghp-typed-by-a-person"


def test_every_ui_managed_secret_is_actually_classified_secret():
    """The allowlist rides on the classifier; an entry the classifier does not cover would
    travel to the browser with a hint saying it is protected."""
    for key in Config.UI_MANAGED_SECRET_KEYS:
        assert Config.is_secret_config_key(key), f"{key} is UI-managed but not classified secret"
        assert not key.startswith("api_key_"), (
            f"{key} belongs to the provider-key lane, which has its own revocation ordering"
        )


def test_deleting_a_ui_managed_secret_clears_it(_config):
    import vaf.api.config_routes as routes

    _config["github_oauth_client_secret"] = "ghp-old"

    out = asyncio.run(routes.delete_config_secret("github_oauth_client_secret", _={"role": "admin"}))

    assert out["status"] == "deleted"
    assert not Config.load().get("github_oauth_client_secret")


@pytest.mark.parametrize("key", ["secure_store_kek", "api_key_openai", "language"])
def test_the_delete_endpoint_refuses_everything_off_the_allowlist(key, _config):
    """The KEK from a settings page would be an outage button; provider keys have their own
    ordered revocation; plain settings are not secrets at all."""
    import vaf.api.config_routes as routes
    from fastapi import HTTPException

    _config[key] = "something"
    with pytest.raises(HTTPException):
        asyncio.run(routes.delete_config_secret(key, _={"role": "admin"}))


def test_the_listing_hints_ui_managed_secrets_and_never_infrastructure(_config):
    """A hint of the KEK would be a leak with no user need behind it."""
    import vaf.api.config_routes as routes

    _config["email_oauth_google_client_secret"] = "GOCSPX-demo-secret-long-enough"
    _config["secure_store_kek"] = "kek-value-demo-long-enough-000"

    out = asyncio.run(routes.list_api_keys(_={"role": "admin"}))

    assert out["secrets"].get("email_oauth_google_client_secret", "").startswith("GOCSPX-dem")
    assert "GOCSPX-demo-secret-long-enough" not in str(out), "the full secret left the server"
    assert "secure_store_kek" not in out["secrets"], "the KEK got a hint"


def test_the_websocket_config_path_routes_through_the_same_funnel():
    """Static, and honestly labelled as such: the WS handler cannot be driven headlessly
    here, so this pins that its config answer goes through `config_for_user` rather than
    `Config.load()` raw - the funnel the two assertions above prove. By AST, not substring:
    a comment naming the function must neither satisfy nor break this.
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_bytes()
    calls = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "config_for_user"
    ]
    assert calls, "web_server no longer filters the config it sends over the websocket"
