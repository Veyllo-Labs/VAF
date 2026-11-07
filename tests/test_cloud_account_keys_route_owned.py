# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The cloud account lists change only through the cloud routes, and each account sees
only its own.

Neither `cloud_config` nor `cloud_config_by_user` was an admin-only key, so a settings save
by any signed-in account was merged as sent: measured, a non-admin body replaced the owner's
list and removed another account's, and could add an entry for a provider no route
connects (an iCloud entry, which reads the host's iCloud Drive). The settings view handed
every account the owner's list and every other account's. The background sync settings are
one loop for the whole instance and were writable by anyone too. Synthetic identities only.
"""
import asyncio
import inspect
import types

import pytest

TENANT_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
OTHER_SCOPE = "ab12cd34-0000-4000-8000-000000000002"
ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"

OWNER_LIST = {"accounts": [{"provider": "google_drive", "account_id": "owner@example.com"}]}
BY_USER = {
    "alice": {"accounts": [
        {"provider": "onedrive", "account_id": "alice@example.com",
         "user_scope_id": TENANT_SCOPE},
        {"provider": "dropbox", "account_id": "legacy@example.com"},
    ]},
    "bob": {"accounts": [
        {"provider": "google_drive", "account_id": "bob@example.com",
         "user_scope_id": OTHER_SCOPE},
    ]},
}


@pytest.fixture
def stored(monkeypatch):
    from vaf.core.config import Config
    import copy
    state = {"cloud_config": copy.deepcopy(OWNER_LIST),
             "cloud_config_by_user": copy.deepcopy(BY_USER),
             "cloud_sync_enabled": False, "server_mode": False}
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: copy.deepcopy(state)))
    monkeypatch.setattr(Config, "save", classmethod(
        lambda cls, cfg: (state.clear(), state.update(copy.deepcopy(cfg)))[0]))
    return state


def _patch(body, user):
    from vaf.api import config_routes
    request = types.SimpleNamespace(state=types.SimpleNamespace(user=user))
    return asyncio.run(config_routes.patch_config(body, request, user))


HOSTILE = {
    "cloud_config": {"accounts": [{"provider": "icloud", "account_id": "icloud"}]},
    "cloud_config_by_user": {"alice": {"accounts": [
        {"provider": "icloud", "account_id": "icloud", "user_scope_id": TENANT_SCOPE}]}},
    "tts_auto_speak": True,
}


@pytest.mark.parametrize("user", [
    {"role": "user", "user_scope_id": TENANT_SCOPE, "username": "alice"},
    {"role": "admin", "user_scope_id": ADMIN_SCOPE, "username": "Admin"},
])
def test_save_lanes_drop_cloud_account_keys(stored, user):
    """PATCH /api/config here, for a non-admin AND an admin; the WebSocket save by its
    source (its handler is inline in the socket loop).

    MUTATION: remove the call in `patch_config` - red. MUTATION: remove it in the WebSocket
    lane - the source assertion red."""
    _patch(dict(HOSTILE), user)
    assert stored["cloud_config"] == OWNER_LIST
    assert stored["cloud_config_by_user"] == BY_USER
    assert stored["tts_auto_speak"] is True

    from vaf.core import web_server
    source = inspect.getsource(web_server)
    lane = source[source.index('elif type == "save_config":'):]
    lane = lane[:lane.index("Config.split_connection_toggles")]
    assert "new_config = without_cloud_account_keys(new_config)" in lane


def test_without_cloud_account_keys_leaves_other_keys_alone():
    from vaf.cloud.base import without_cloud_account_keys
    body = {"language": "de"}
    assert without_cloud_account_keys(body) is body
    assert without_cloud_account_keys(dict(HOSTILE)) == {"tts_auto_speak": True}


def test_non_admin_view_holds_only_own_cloud_accounts():
    """MUTATION: delete the cloud lines in `config_for_user`'s non-admin branch - red."""
    from vaf.core.config import Config
    cfg = {"cloud_config": OWNER_LIST, "cloud_config_by_user": BY_USER}

    view = Config.config_for_user(cfg, TENANT_SCOPE, "user")
    assert view["cloud_config"] is None
    assert view["cloud_config_by_user"] == {"alice": {"accounts": [
        BY_USER["alice"]["accounts"][0]]}}

    assert Config.config_for_user(cfg, OTHER_SCOPE, "user")["cloud_config_by_user"] == {
        "bob": BY_USER["bob"]}
    assert Config.config_for_user(cfg, None, "user")["cloud_config_by_user"] == {}

    admin = Config.config_for_user(cfg, ADMIN_SCOPE, "admin")
    assert admin["cloud_config"] == OWNER_LIST
    assert admin["cloud_config_by_user"] == BY_USER


def test_cloud_sync_keys_are_admin_only(stored):
    """MUTATION: remove "cloud_sync_" from GLOBAL_CONFIG_KEY_PREFIXES - red."""
    from vaf.core.config import Config
    for key in ("cloud_sync_enabled", "cloud_sync_interval_minutes",
                "cloud_sync_max_file_size_mb", "cloud_sync_conflict_resolution"):
        assert Config.is_global_config_key(key), key
    _patch({"cloud_sync_enabled": True, "tts_auto_speak": True},
           {"role": "user", "user_scope_id": TENANT_SCOPE, "username": "alice"})
    assert stored["cloud_sync_enabled"] is False
    assert stored["tts_auto_speak"] is True


def test_a_non_admin_save_cannot_set_the_limits_of_any_sync(stored, monkeypatch):
    """The manual sync route read `cloud_max_file_size` and `cloud_conflict_strategy`, keys
    registered nowhere, so a non-admin save set them for every account. Both sync lanes now
    read the admin-only `cloud_sync_` pair through one reader.

    MUTATION: the route back to `Config.get("cloud_max_file_size", ...)` - red (source).
    MUTATION: `sync_settings` reading the old keys - red (the non-admin's values win)."""
    from vaf.api import cloud_routes
    from vaf.cloud import sync_worker
    from vaf.cloud.sync_engine import sync_settings
    from vaf.core.config import Config

    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: stored.get(key, default)))
    _patch({"cloud_max_file_size": 1, "cloud_conflict_strategy": "keep_both",
            "cloud_sync_max_file_size_mb": 1, "cloud_sync_conflict_resolution": "keep_both"},
           {"role": "user", "user_scope_id": TENANT_SCOPE, "username": "alice"})
    assert sync_settings() == (100 * 1024 * 1024, "last_write_wins")

    for fn in (cloud_routes.trigger_sync, sync_worker._run_sync_for_account):
        src = inspect.getsource(fn)
        assert "sync_settings()" in src, fn.__name__
        assert "cloud_max_file_size" not in src and "cloud_conflict_strategy" not in src
