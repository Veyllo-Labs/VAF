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
