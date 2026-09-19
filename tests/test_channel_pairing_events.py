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


def test_a_contacts_decision_is_recorded_with_the_word_that_was_taken(config, monkeypatch):
    """Three states, three words, and each one recorded exactly when it changes. The two that
    matter most were invisible before: a DENIAL read as truthy through bool(), so switching a
    person off compared equal to switching them on and the single most security-relevant change
    on a contact never reached its own log.
    MUTATION: compare the two states with bool() in patch_contact and the "blocked" line
    disappears."""
    from vaf.api import contact_routes as routes
    events = _recorder(monkeypatch, routes)

    plain = asyncio.run(routes.post_contact(_req(), routes.ContactCreate(name="Bob")))
    assert events == [], "a contact nobody decided about opens no door"
    allowed = asyncio.run(routes.post_contact(_req(), routes.ContactCreate(name="Dana", assistant_access="allowed")))
    asyncio.run(routes.patch_contact(allowed["id"], _req(), routes.ContactUpdate(company="Acme")))
    asyncio.run(routes.patch_contact(allowed["id"], _req(), routes.ContactUpdate(assistant_access="allowed")))   # unchanged
    asyncio.run(routes.patch_contact(allowed["id"], _req(), routes.ContactUpdate(assistant_access="denied")))
    asyncio.run(routes.patch_contact(allowed["id"], _req(), routes.ContactUpdate(assistant_access="undecided")))
    asyncio.run(routes.patch_contact(plain["id"], _req(), routes.ContactUpdate(assistant_access="allowed")))
    # The legacy bool still works and can only ever mean "allowed" or "nobody decided".
    asyncio.run(routes.patch_contact(plain["id"], _req(), routes.ContactUpdate(allow_as_assistant_user=False)))

    assert [(k, f["username"], f["path"], f["detail"]) for k, f in events] == [
        ("contact_access_changed", "alice", allowed["id"], "granted: Dana"),
        ("contact_access_changed", "alice", allowed["id"], "blocked: Dana"),
        ("contact_access_changed", "alice", allowed["id"], "cleared: Dana"),
        ("contact_access_changed", "alice", plain["id"], "granted: Bob"),
        ("contact_access_changed", "alice", plain["id"], "cleared: Bob"),
    ]


def test_a_field_that_was_not_filled_in_changes_no_decision(config, monkeypatch):
    """`allow_as_assistant_user: null` is "not sent": the type has no room for a third state,
    so None can only mean "no answer here". Left in the update it reached the store's legacy
    branch, where `bool(None)` cleared the decision, and a blocked contact came back unblocked
    through a field nobody filled in.

    MUTATION: drop the `updates.pop("allow_as_assistant_user")` guard in patch_contact and the
    state assertion goes red (and a "cleared" line appears in the security log).
    """
    from vaf.api import contact_routes as routes
    from vaf.core import contacts_store
    events = _recorder(monkeypatch, routes)

    blocked = asyncio.run(routes.post_contact(_req(), routes.ContactCreate(name="Mara", assistant_access="denied")))
    events.clear()
    asyncio.run(routes.patch_contact(blocked["id"], _req(),
                                     routes.ContactUpdate(company="Acme", allow_as_assistant_user=None)))
    stored = contacts_store.get_contact_by_id(blocked["id"], "alice", user_scope_id=SCOPE)
    assert contacts_store.contact_access(stored) == "denied" and stored.get("company") == "Acme"
    assert events == [], "nothing about the decision changed, so nothing is recorded"
    # The three-state field's own null is the same case: "undecided" is the word that clears,
    # null is a field nobody filled in. MUTATION: drop the `assistant_access` pop in
    # patch_contact and this goes red with the block cleared.
    asyncio.run(routes.patch_contact(blocked["id"], _req(),
                                     routes.ContactUpdate(role="Buyer", assistant_access=None)))
    stored = contacts_store.get_contact_by_id(blocked["id"], "alice", user_scope_id=SCOPE)
    assert contacts_store.contact_access(stored) == "denied" and stored.get("role") == "Buyer"
    assert events == []
    # And the word itself still clears, recorded as "cleared".
    asyncio.run(routes.patch_contact(blocked["id"], _req(), routes.ContactUpdate(assistant_access="undecided")))
    assert contacts_store.contact_access(
        contacts_store.get_contact_by_id(blocked["id"], "alice", user_scope_id=SCOPE)) is None
    assert [e[1]["detail"] for e in events] == ["cleared: Mara"]


def test_the_agent_tool_refuses_a_word_it_does_not_know(config):
    """The store clears the decision for any word it does not recognise, so a tool that passed
    one on would UN-block the person it was asked to block. MUTATION: pass the word through
    again and the first assertion goes red."""
    from vaf.core import contacts_store
    from vaf.tools.update_contact import UpdateContactTool

    rec = contacts_store.create_contact("Mara", "alice", user_scope_id=SCOPE, assistant_access="denied")
    out = UpdateContactTool().run(contact_id=rec["id"], assistant_access="block",
                                  username="alice", user_scope_id=SCOPE)
    assert "must be" in out and "block" in out
    assert contacts_store.contact_access(
        contacts_store.get_contact_by_id(rec["id"], "alice", user_scope_id=SCOPE)) == "denied"
    # The three words it does know still work, and "undecided" is the one that clears.
    for word, expected in (("allowed", "allowed"), ("denied", "denied"), ("undecided", None)):
        UpdateContactTool().run(contact_id=rec["id"], assistant_access=word,
                                username="alice", user_scope_id=SCOPE)
        assert contacts_store.contact_access(
            contacts_store.get_contact_by_id(rec["id"], "alice", user_scope_id=SCOPE)) == expected, word


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


def test_the_three_state_word_outranks_the_legacy_bool_whatever_order_they_arrive_in(config):
    """`update_contact` takes both spellings of the decision, and a caller may send both: the
    PATCH body has both fields, and a tool built on the old bool may add the word later. The
    word IS the decision; the bool speaks only when the word is absent. It used to depend on
    the order of the keys, so `assistant_access="denied", allow_as_assistant_user=True` came
    out as a grant.

    MUTATION: handle the two keys inside the field loop again and the first assertion goes red.
    """
    from vaf.core import contacts_store
    rec = contacts_store.create_contact("Mara", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000077")
    cid = rec["id"]
    out = contacts_store.update_contact(cid, "alice", user_scope_id=SCOPE,
                                        assistant_access="denied", allow_as_assistant_user=True)
    assert contacts_store.contact_access(out) == "denied"
    out = contacts_store.update_contact(cid, "alice", user_scope_id=SCOPE,
                                        allow_as_assistant_user=True, assistant_access="denied")
    assert contacts_store.contact_access(out) == "denied"
    # The bool alone still speaks: True is a grant, False takes the decision back.
    out = contacts_store.update_contact(cid, "alice", user_scope_id=SCOPE, allow_as_assistant_user=True)
    assert contacts_store.contact_access(out) == "allowed"
    out = contacts_store.update_contact(cid, "alice", user_scope_id=SCOPE, allow_as_assistant_user=False)
    assert contacts_store.contact_access(out) is None
    # And neither key leaks into the record as a raw field: the writer keeps them in step.
    out = contacts_store.update_contact(cid, "alice", user_scope_id=SCOPE, assistant_access="allowed", name="Mara B.")
    assert contacts_store.contact_access(out) == "allowed" and out["name"] == "Mara B."
    assert out.get("allow_as_assistant_user") is True


def test_the_tool_says_what_no_decision_means(config):
    """`get_contact` answers the question "can this person reach my assistant" with three
    words, and the third is a rule rather than silence: nobody decided, so the channel's
    Inbound switch answers, and only while it stands open. Silence read as "no" to the model.

    MUTATION: drop the else branch in get_contact.py and this goes red.
    """
    from vaf.core import contacts_store
    from vaf.tools.get_contact import GetContactTool
    contacts_store.create_contact("Nils", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000078")
    text = GetContactTool().run(name="Nils", username="alice", user_scope_id=SCOPE)
    assert "Can reach your assistant: not decided" in text and "Inbound is open" in text
    contacts_store.create_contact("Olga", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000079",
                                  assistant_access="denied")
    assert "Can reach your assistant: no" in GetContactTool().run(name="Olga", username="alice", user_scope_id=SCOPE)
