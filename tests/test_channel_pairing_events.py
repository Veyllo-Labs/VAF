# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The security log records who was let in through a messenger, by whom.

Turning strangers away is the perimeter's everyday work (channel traffic, see
test_channel_ingress_log.py); opening a door is the audit-worthy half: an owner number
registered for WhatsApp, a LID chat bound to an allowed number, a Telegram whitelist or
relay entry, the Discord admin, a contact given assistant access. Each route that writes
such a change emits `channel_paired` / `channel_unpaired` / `contact_access_changed` with
the acting user, the paired id as `path` (two changes seconds apart stay two events) and what
changed; a write that changes nothing emits nothing, and a replaced owner number or admin is one
`channel_unpaired` for the old id and one `channel_paired` for the new.

MUTATION: drop any one `log_security_event(...)` call in the routes and the matching
assertion below goes red.
"""
import asyncio
from types import SimpleNamespace

import pytest

from vaf.core.config import Config
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"


def _req(username="alice", scope=SCOPE):
    return SimpleNamespace(state=SimpleNamespace(user={"user_scope_id": scope, "username": username}))


@pytest.fixture
def config(monkeypatch, tmp_path):
    """An in-memory config: the routes read through Config.load and write through Config.save."""
    state = {"whatsapp_config": {"enabled": True, "whitelist": []}, "telegram_config": {}, "discord_config": {}}
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {k: (dict(v) if isinstance(v, dict) else v) for k, v in state.items()}))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, cfg: state.update(cfg)))
    return state


def _recorder(monkeypatch, module):
    events = []
    monkeypatch.setattr(module, "log_security_event", lambda kind, **f: events.append((kind, f)))
    return events


def test_whatsapp_owner_numbers_and_lid_bindings_are_recorded_once_per_change(config, monkeypatch):
    from vaf.api import whatsapp_routes as routes
    monkeypatch.setattr(routes, "_is_whatsapp_admin", lambda request: True)
    monkeypatch.setattr("vaf.core.whatsapp_auth.get_linked_phone", lambda username: None)
    events = _recorder(monkeypatch, routes)

    out = asyncio.run(routes.add_whitelist_entry(_req(), routes.WhitelistAddRequest(phone_number="+491700000042")))
    assert out["status"] == "added"
    out = asyncio.run(routes.add_whitelist_entry(_req(), routes.WhitelistAddRequest(phone_number="+491700000042")))
    assert out["status"] == "updated", "the same number again is an update of the entry"
    out = asyncio.run(routes.add_whitelist_entry(_req(), routes.WhitelistAddRequest(phone_number="+491700000043")))
    assert out["status"] == "updated", "the account's entry is replaced: the old number loses access, the new one gains it"
    out = asyncio.run(routes.remove_whitelist_entry(_req(), routes.WhitelistAddRequest(phone_number="+491700000043")))
    assert out["status"] == "removed" and out["whitelist_count"] == 0
    asyncio.run(routes.remove_whitelist_entry(_req(), routes.WhitelistAddRequest(phone_number="+491700000043")))  # nothing left to remove
    asyncio.run(routes.assign_lid_to_number(_req(), routes.LidAssignRequest(lid_jid="12345@lid", phone_number="+491700000042")))
    asyncio.run(routes.assign_lid_to_number(_req(), routes.LidAssignRequest(lid_jid="12345@lid", phone_number="+491700000042")))  # same binding
    asyncio.run(routes.assign_lid_to_number(_req(), routes.LidAssignRequest(lid_jid="12345@lid", phone_number="+491700000043")))

    assert [(k, f["channel"], f["username"], f["path"], f["detail"]) for k, f in events] == [
        ("channel_paired", "whatsapp", "alice", "+491700000042", "owner +491700000042 for alice"),
        ("channel_unpaired", "whatsapp", "alice", "+491700000042", "owner +491700000042"),
        ("channel_paired", "whatsapp", "alice", "+491700000043", "owner +491700000043 for alice"),
        ("channel_unpaired", "whatsapp", "alice", "+491700000043", "owner +491700000043"),
        ("channel_paired", "whatsapp", "alice", "12345@lid", "lid 12345@lid as +491700000042"),
        ("channel_paired", "whatsapp", "alice", "12345@lid", "lid 12345@lid as +491700000043 (was +491700000042)"),
    ]


def test_telegram_whitelist_and_relay_entries_are_recorded_once_per_change(config, monkeypatch):
    from vaf.api import telegram_routes as routes
    monkeypatch.setattr(routes, "_is_telegram_admin", lambda request: True)
    events = _recorder(monkeypatch, routes)
    me = {"user_scope_id": SCOPE, "username": "alice"}
    bob = {"user_scope_id": "22222222-3333-4444-5555-666666666666", "username": "bob"}

    asyncio.run(routes.whitelist_add(routes.WhitelistAddRequest(telegram_user_id="7"), _req(), me))
    asyncio.run(routes.whitelist_add(routes.WhitelistAddRequest(telegram_user_id="7", telegram_username="al"), _req(), me))  # same pairing
    asyncio.run(routes.whitelist_add(routes.WhitelistAddRequest(telegram_user_id="7"), _req("bob", bob["user_scope_id"]), bob))  # moved to bob
    asyncio.run(routes.relay_whitelist_add(_req(), routes.RelayWhitelistAddRequest(telegram_user_id="9")))
    asyncio.run(routes.relay_whitelist_add(_req(), routes.RelayWhitelistAddRequest(telegram_user_id="9")))  # same pairing
    asyncio.run(routes.relay_whitelist_remove(_req(), routes.WhitelistAddRequest(telegram_user_id="9")))
    asyncio.run(routes.relay_whitelist_remove(_req(), routes.WhitelistAddRequest(telegram_user_id="9")))  # already gone

    assert [(k, f["channel"], f["username"], f["path"], f["detail"]) for k, f in events] == [
        ("channel_paired", "telegram", "alice", "7", "owner 7"),
        ("channel_paired", "telegram", "bob", "7", "owner 7"),
        ("channel_paired", "telegram", "alice", "9", "relay 9"),
        ("channel_unpaired", "telegram", "alice", "9", "relay 9"),
    ]


def test_a_contacts_assistant_access_flag_is_recorded_only_when_it_changes(config, monkeypatch):
    from vaf.api import contact_routes as routes
    events = _recorder(monkeypatch, routes)

    plain = asyncio.run(routes.post_contact(_req(), routes.ContactCreate(name="Bob")))
    assert events == [], "a contact without the flag opens no door"
    allowed = asyncio.run(routes.post_contact(_req(), routes.ContactCreate(name="Dana", allow_as_assistant_user=True)))
    asyncio.run(routes.patch_contact(allowed["id"], _req(), routes.ContactUpdate(company="Acme")))
    asyncio.run(routes.patch_contact(allowed["id"], _req(), routes.ContactUpdate(allow_as_assistant_user=True)))   # unchanged
    asyncio.run(routes.patch_contact(allowed["id"], _req(), routes.ContactUpdate(allow_as_assistant_user=False)))
    asyncio.run(routes.patch_contact(plain["id"], _req(), routes.ContactUpdate(allow_as_assistant_user=True)))

    assert [(k, f["username"], f["path"], f["detail"]) for k, f in events] == [
        ("contact_access_changed", "alice", allowed["id"], "granted: Dana"),
        ("contact_access_changed", "alice", allowed["id"], "revoked: Dana"),
        ("contact_access_changed", "alice", plain["id"], "granted: Bob"),
    ]


def test_the_discord_admin_arrives_through_the_config_patch_and_is_recorded_from_the_diff(monkeypatch):
    from vaf.api import config_routes as routes
    events = []
    monkeypatch.setattr("vaf.core.security_events.log_security_event", lambda kind, **f: events.append((kind, f)))
    me = {"username": "alice", "role": "admin"}

    routes._note_discord_admin_change({}, {"discord_config": {"admin_user_id": "42", "verified": True}}, me)
    routes._note_discord_admin_change({"discord_config": {"admin_user_id": "42"}}, {"discord_config": {"admin_user_id": "42", "enabled": False}}, me)
    routes._note_discord_admin_change({"discord_config": {"admin_user_id": "42"}}, {"discord_config": {"admin_user_id": "43"}}, me)   # replaced
    routes._note_discord_admin_change({"discord_config": {"admin_user_id": "43"}}, {"discord_config": {"admin_user_id": None}}, me)
    assert [(k, f["channel"], f["username"], f["path"], f["detail"]) for k, f in events] == [
        ("channel_paired", "discord", "alice", "42", "admin 42"),
        ("channel_unpaired", "discord", "alice", "42", "admin 42"),
        ("channel_paired", "discord", "alice", "43", "admin 43"),
        ("channel_unpaired", "discord", "alice", "43", "admin 43"),
    ]
    # And the route calls it after the save, on the config it saved.
    import inspect
    src = inspect.getsource(routes.patch_config)
    assert src.index("Config.save(merged)") < src.index("_note_discord_admin_change(current, merged, _user)")
