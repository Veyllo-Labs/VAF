# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Inbound on Discord (vaf/api/discord_bridge.py, FRONT_OFFICE.md): the paired admin's direct
message is the full agent; with the channel switched on, a stranger's DM is admitted as a
Front Office contact of the LOCAL ADMIN's book (never a book named after the bridge's literal
"admin" identity), enrolled once with its event, kept out once switched off there; a guild
message is never answered; the dashboard rows carry the inbox's mode. The admission itself is
the shared contacts_store.admit_front_office_sender. Isolated: tmp data dir, in-memory config.

MUTATION: drop the `not is_dm` refusal and the admin's guild message is answered; pass the
literal "admin" identity to the book and the record lands in users/admin/contacts.json."""
from types import SimpleNamespace

import pytest

from vaf.core import channel_message_store as store
from vaf.core import contacts_store
from vaf.core.channel_ingress_policy import set_front_office
from vaf.core.config import Config
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    state = {"local_admin_scope_id": SCOPE, "local_admin_username": "alice",
             "discord_config": {"enabled": True, "admin_user_id": "42", "verified": True},
             "channel_ingress_policy": set_front_office(None, True, "discord")}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    events = []
    import vaf.core.security_events as sec
    monkeypatch.setattr(sec, "log_security_event", lambda kind, **f: events.append((kind, f)))
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    store._reset_announce_state()
    return SimpleNamespace(state=state, events=events, tmp=tmp_path)


def test_a_strangers_dm_is_admitted_into_the_local_admins_book_once_and_kept_out_when_switched_off(world):
    from vaf.api import discord_bridge as dc
    policy = world.state["channel_ingress_policy"]
    assert dc._admit_sender("555", True, "42", "Grace", policy) == (True, "front_office_open", {"from_contact": True, "ingress_reason": "front_office_open"})
    rec = contacts_store.find_contact_by_channel("discord", "555", "alice", SCOPE)
    assert rec and rec["name"] == "Grace" and rec["source"] == "front_office"
    assert contacts_store.contact_access(rec) is None, \
        "the open channel admitted her; the record is not a permission that outlives it"
    assert (world.tmp / "data" / "contacts.json").is_file() and not (world.tmp / "data" / "users" / "admin").exists(), \
        "the local admin's own book, not a book named after the bridge's literal identity"
    assert world.events == [("contact_access_changed", {"channel": "discord", "username": "alice", "path": rec["id"],
                                                        "detail": "added by the open Front Office: Grace"})]
    world.events.clear()
    assert dc._admit_sender("555", True, "42", "Grace", policy)[0] is True and world.events == [], "enrolled once"
    # Taking the decision back changes nothing while the channel stands open; only a denial
    # keeps her out. MUTATION: read the record with bool() and the denial turns into a pass.
    contacts_store.update_contact(rec["id"], "alice", user_scope_id=SCOPE, assistant_access="undecided")
    assert dc._admit_sender("555", True, "42", "Grace", policy)[0] is True
    contacts_store.update_contact(rec["id"], "alice", user_scope_id=SCOPE, assistant_access="denied")
    assert dc._admit_sender("555", True, "42", "Grace", policy) == (False, "contact_denied", {}), \
        "denied in the book: kept out of an open channel"


def test_the_admin_and_guild_messages_and_a_closed_channel(world):
    from vaf.api import discord_bridge as dc
    policy = world.state["channel_ingress_policy"]
    assert dc._admit_sender("42", True, "42", "Owner", policy) == (True, "explicit_pair", {}), "the paired admin is the full agent"
    assert dc._admit_sender("42", False, "42", "Owner", policy) == (False, "not_paired", {}), "never in a guild channel"
    assert dc._admit_sender("555", False, "42", "Grace", policy) == (False, "not_paired", {})
    closed = set_front_office(policy, False, "discord")
    assert dc._admit_sender("555", True, "42", "Grace", closed) == (False, "not_paired", {})
    assert contacts_store.find_contact_by_channel("discord", "555", "alice", SCOPE) is None, "a refused sender is not enrolled"
    # An allowed contact is admitted on the closed channel, by their own permission.
    grace = contacts_store.create_contact("Grace", "alice", user_scope_id=SCOPE,
                                          channels=[{"type": "discord", "value": "555"}],
                                          assistant_access="allowed")
    assert dc._admit_sender("555", True, "42", "Grace", closed) == (
        True, "contact_allowed", {"from_contact": True, "ingress_reason": "contact_allowed"})
    assert grace["id"]


def test_the_dashboard_rows_carry_the_inbox_mode(world, monkeypatch):
    """The row says what the lane does: the paired admin's own chat, a person the owner
    allowed, a person they denied (read-only whatever the switch says) and somebody nobody
    decided about (answered while Inbound is open). MUTATION: let chat_mode fall through the
    denial to the open channel and the denied row turns into a contact."""
    from vaf.api import discord_routes as routes
    contacts_store.create_contact("Grace", "alice", user_scope_id=SCOPE,
                                  channels=[{"type": "discord", "value": "555"}], assistant_access="allowed")
    contacts_store.create_contact("Mara", "alice", user_scope_id=SCOPE,
                                  channels=[{"type": "discord", "value": "888"}], assistant_access="denied")
    for cid, body in (("42", "hi"), ("555", "hello"), ("777", "anyone?"), ("888", "let me in")):
        store.append_message("admin", cid, body, "in", channel="discord", user_scope_id=None)
    rows = {s["chat_id"]: s["type"] for s in routes._store_sessions()}
    assert rows == {"42": "admin", "555": "contact", "777": "contact", "888": "readonly"}
    # With Inbound closed, only the person the owner allowed keeps their lane.
    world.state["channel_ingress_policy"] = set_front_office(world.state["channel_ingress_policy"], False, "discord")
    rows = {s["chat_id"]: s["type"] for s in routes._store_sessions()}
    assert rows == {"42": "admin", "555": "contact", "777": "readonly", "888": "readonly"}
    # And with the book UNREADABLE under an open channel, nobody but the admin is painted as
    # answered: the blocked person would otherwise read "contact" because the denied set came
    # back empty. MUTATION: read the switch without `known` in `inbox.access_inputs` and the
    # row for 888 goes red.
    world.state["channel_ingress_policy"] = set_front_office(world.state["channel_ingress_policy"], True, "discord")
    import vaf.core.contacts_store as cs_mod

    def _broken(*a, **k):
        raise RuntimeError("contacts.json unreadable")
    monkeypatch.setattr(cs_mod, "front_office_endpoints", _broken)
    monkeypatch.setattr(cs_mod, "denied_endpoints", _broken)
    rows = {s["chat_id"]: s["type"] for s in routes._store_sessions()}
    assert rows == {"42": "admin", "555": "readonly", "777": "readonly", "888": "readonly"}


def test_the_no_contact_prefix_calls_the_sender_a_sender_and_not_a_number():
    """The runner's no-contact prefix (vaf/core/headless_runner.py) is the default for every
    Inbound channel: the sender it names is a WhatsApp number, a Telegram user id, a Discord
    author id or an e-mail address, so the sentence explaining it must be channel-neutral.
    Source guard in the idiom of test_chat_namespace_runner.py, since no runner harness
    exists in tests/. MUTATION: write "this number" back into the block and this goes red."""
    from pathlib import Path
    runner = (Path(__file__).resolve().parent.parent / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    start = runner.index("# No contact record: this sender reached Front Office")
    block = runner[start:runner.index('reply_lang_hint = ""', start)]
    assert '"discord_author_id"' in block, "the block resolves the Discord sender too"
    assert "outbound message to this sender opened" in block
    assert "this number" not in block
