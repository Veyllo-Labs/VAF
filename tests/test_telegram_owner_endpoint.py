# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Where VAF reaches an account on Telegram is only ever that account's own paired chat.

Measured on the code before this change: the Telegram bridge stored the chat of EVERY sender
it admitted as the owner's endpoint (`save_telegram_chat_id` at its three inbound sites), and
three of the four kinds of sender it admits are not the owner: an allowed contact, a relay
contact and a stranger the open Front Office answers all resolve to an entry that carries the
OWNER's scope and name. After one message from a contact, the owner's reminders, automation
results, background questions and the Front Office notes about other contacts went to that
contact, and were mirrored into the contact's own chat session. The reader returned whatever
was stored, ahead of the pairing, so the wrong value never went away.

The rule lives in the store, both halves: the writer refuses any chat that is not a paired id
of the account, and the reader drops one it finds (an install that ran a released version can
hold a contact's chat) and records it once as `owner_endpoint_reset`.
"""
import json
from types import SimpleNamespace

import pytest

from vaf.core import messaging_connections as mc
from vaf.core.channel_ingress_policy import set_front_office
from vaf.core.config import Config
from vaf.core.platform import Platform

DANA = "d0d0d0d0-0000-4000-8000-000000000199"     # the local admin, Dana Example
ALICE = "a1a1a1a1-0000-4000-8000-000000000401"    # a second account on the same bot
ADMIN = "ad0ad0ad-0000-4000-8000-000000000501"    # a local admin named "admin"
OWNER_TG = "7000000199"
OWNER_TG_2 = "7000000198"                         # a second Telegram Dana paired
CAROL_TG = "7000000101"                           # Carol Example, an allowed contact
STRANGER_TG = "7000000102"                        # nobody decided about them
RELAY_TG = "7000000301"                           # Dana's relay contact
ALICE_TG = "7000000401"
ADMIN_TG = "7000000501"
GROUP_CHAT = "-1001234567890"


def _owner_entry(tid=OWNER_TG, scope=DANA, name="dana"):
    return {"telegram_user_id": tid, "user_scope_id": scope, "vaf_username": name}


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "local_admin_scope_id": DANA, "local_admin_username": "dana",
        "telegram_config": {"enabled": True, "verified": True, "whitelist": [_owner_entry()]},
    }), encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    # The store path is cached module-globally and the conftest store dir is shared by the
    # whole session, so each test gets its own file.
    store = tmp_path / "data" / "messaging_endpoints.json"
    monkeypatch.setattr(mc, "_ENDPOINTS_FILE", store)
    events = []
    import vaf.core.security_events as sec
    monkeypatch.setattr(sec, "log_security_event", lambda kind, **f: events.append((kind, f)))

    def seed(**blocks):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(blocks)
        path.write_text(json.dumps(data), encoding="utf-8")

    def stored(**maps):
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"by_scope": maps.get("by_scope", {}),
                                     "by_username": maps.get("by_username", {})}), encoding="utf-8")

    def on_disk():
        return json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}

    def resets():
        return [f for kind, f in events if kind == "owner_endpoint_reset"]

    return SimpleNamespace(seed=seed, stored=stored, on_disk=on_disk, store=store, resets=resets,
                           events=events)


def _add_carol():
    from vaf.core import contacts_store
    return contacts_store.create_contact("Carol Example", "dana", user_scope_id=DANA,
                                         telegram_user_id=CAROL_TG, assistant_access="allowed")


# -- the writer --------------------------------------------------------------------------

def _allowed_contact(w):
    _add_carol()
    return CAROL_TG, CAROL_TG


def _relay_contact(w):
    w.seed(telegram_config={"enabled": True, "verified": True, "whitelist": [_owner_entry()],
                            "relay_whitelist": [{"telegram_user_id": RELAY_TG, "user_scope_id": DANA,
                                                 "vaf_username": "dana"}]})
    return RELAY_TG, RELAY_TG


def _open_front_office_stranger(w):
    w.seed(channel_ingress_policy=set_front_office(None, True, "telegram"))
    return STRANGER_TG, STRANGER_TG


def _the_owner_in_a_group(w):
    return OWNER_TG, GROUP_CHAT


@pytest.mark.parametrize("sender", [_allowed_contact, _relay_contact, _open_front_office_stranger,
                                    _the_owner_in_a_group])
def test_a_non_owner_chat_never_sets_the_owner_endpoint(world, sender):
    """AC1/AC2. MUTATION: drop the paired-id check in `save_telegram_chat_id` -> red."""
    from vaf.api import telegram_bridge as tb
    assert mc.save_telegram_chat_id(DANA, "dana", OWNER_TG) is True, "the owner's own chat"
    before = world.store.read_bytes()
    user_id, chat_id = sender(world)

    entry, _relay = tb._resolve_telegram_user(user_id, SimpleNamespace(id=int(user_id), full_name="Bob Example",
                                                                       username="bob"))
    assert entry is not None and entry["user_scope_id"] == DANA, "the bridge admits them as Dana's"
    # Exactly what the bridge does at its three inbound sites.
    saved = mc.save_telegram_chat_id(entry.get("user_scope_id"), entry.get("vaf_username") or "admin", str(chat_id))

    assert saved is False
    assert world.store.read_bytes() == before, "the store was written"
    assert mc.get_telegram_chat_id(DANA, "dana") == OWNER_TG
    assert world.resets() == []


def test_the_bridge_saves_through_the_store_at_its_three_sites():
    """AC9: the test above calls the store the way the bridge does; this pins that the bridge
    still does it that way and nowhere else, so the rule has no hand copy to forget."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "vaf" / "api" / "telegram_bridge.py").read_text(encoding="utf-8")
    assert source.count("save_telegram_chat_id(user_scope_id, vaf_username, str(chat_id))") == 3
    assert source.count("save_telegram_chat_id(") == 3


def test_the_owners_own_chats_set_it_and_the_last_used_wins(world):
    """AC2 owner half, AC3. MUTATION: check against `get_telegram_chat_id_from_whitelist`
    (the first paired id only) -> the second Telegram is refused -> red."""
    world.seed(telegram_config={"enabled": True, "verified": True,
                                "whitelist": [_owner_entry(), _owner_entry(OWNER_TG_2)]})
    assert mc.telegram_paired_ids(DANA, "dana") == [OWNER_TG, OWNER_TG_2]
    assert mc.save_telegram_chat_id(DANA, "dana", OWNER_TG_2) is True
    assert mc.get_telegram_chat_id(DANA, "dana") == OWNER_TG_2
    assert world.on_disk() == {"by_scope": {DANA: OWNER_TG_2}, "by_username": {"dana": OWNER_TG_2},
                               "whatsapp_by_scope": {}, "whatsapp_by_username": {}}
    assert mc.save_telegram_chat_id(DANA, "dana", OWNER_TG) is True
    assert mc.get_telegram_chat_id(DANA, "dana") == OWNER_TG
    assert world.resets() == []


def test_an_entry_without_an_id_does_not_end_the_search(world):
    """A half-filled row of the account used to stop the whitelist lookup with None.
    MUTATION: stop at the first matching entry -> red."""
    world.seed(telegram_config={"enabled": True, "verified": True,
                                "whitelist": [_owner_entry(""), _owner_entry()]})
    assert mc.get_telegram_chat_id_from_whitelist(DANA, "dana") == OWNER_TG
    assert mc.save_telegram_chat_id(DANA, "dana", OWNER_TG) is True


def test_another_accounts_pairing_is_never_the_local_admins(world):
    """AC4. MUTATION: decide by `owner_endpoints("telegram", ...)`, which gives the local admin
    every entry on the bot -> Alice's chat is accepted as Dana's -> red."""
    world.seed(telegram_config={"enabled": True, "verified": True,
                                "whitelist": [_owner_entry(), _owner_entry(ALICE_TG, ALICE, "alice")]})
    assert mc.save_telegram_chat_id(DANA, "dana", ALICE_TG) is False
    assert not world.store.exists()

    world.stored(by_scope={DANA: ALICE_TG}, by_username={"dana": ALICE_TG})
    assert mc.telegram_chat_id_of(DANA, "dana") == OWNER_TG
    assert world.on_disk()["by_scope"] == {DANA: OWNER_TG}
    assert world.on_disk()["by_username"] == {"dana": OWNER_TG}
    assert [r["path"] for r in world.resets()] == [ALICE_TG]
    assert mc.telegram_chat_id_of(ALICE, "alice") == ALICE_TG, "Alice still reaches her own"


# -- the reader, and the repair of what a released version stored -------------------------

def test_a_stored_contact_chat_is_put_back_to_the_paired_one(world):
    """AC5/AC6. MUTATION: return the stored value without the paired-id check -> red.
    MUTATION: record the event but keep the value -> the second read records it again -> red."""
    _add_carol()
    world.stored(by_scope={DANA: CAROL_TG}, by_username={"dana": CAROL_TG})

    assert mc.telegram_chat_id_of(DANA, "dana") == OWNER_TG
    assert world.on_disk()["by_scope"] == {DANA: OWNER_TG}
    assert world.on_disk()["by_username"] == {"dana": OWNER_TG}
    assert world.resets() == [{"channel": "telegram", "username": "dana", "path": CAROL_TG,
                               "detail": "contact Carol Example; reset to the paired chat"}]

    assert mc.telegram_chat_id_of(DANA, "dana") == OWNER_TG
    assert len(world.resets()) == 1, "the second read found something to reset again"


def test_a_discarded_chat_is_gone_so_the_next_read_records_nothing(world):
    """AC6, where only the removal clears it: the paired chat is found first, so nothing is
    written back over the contact's chat under the name. MUTATION: record the event but keep
    the value -> the second read records it again -> red."""
    _add_carol()
    world.stored(by_scope={DANA: OWNER_TG}, by_username={"dana": CAROL_TG})

    assert mc.telegram_chat_id_of(DANA, "dana") == OWNER_TG
    assert world.on_disk()["by_username"] == {}
    assert mc.telegram_chat_id_of(DANA, "dana") == OWNER_TG
    assert [r["path"] for r in world.resets()] == [CAROL_TG]


def test_an_unpaired_account_loses_its_stale_endpoint(world):
    """AC5 without a pairing. MUTATION: keep returning the stored value when the account has
    no paired id -> red."""
    world.seed(telegram_config={"enabled": True, "verified": True, "whitelist": []})
    world.stored(by_scope={DANA: OWNER_TG}, by_username={"dana": OWNER_TG})

    assert mc.telegram_chat_id_of(DANA, "dana") is None
    assert world.on_disk()["by_scope"] == {} and world.on_disk()["by_username"] == {}
    assert world.resets() == [{"channel": "telegram", "username": "dana", "path": OWNER_TG,
                               "detail": "not a paired Telegram of this account; no paired chat"}]


@pytest.mark.parametrize("username", [None, "admin"])
def test_a_caller_with_the_scope_but_no_username_keeps_the_paired_chat(world, username):
    """AC7: a background lane often has the scope but not the name. MUTATION: keep requiring
    the username when both sides carry a scope -> the paired chat is discarded -> red."""
    world.stored(by_scope={DANA: OWNER_TG}, by_username={"dana": OWNER_TG})
    before = world.store.read_bytes()

    assert mc.telegram_chat_id_of(DANA, username) == OWNER_TG
    assert mc.get_telegram_chat_id_from_whitelist(DANA, username) == OWNER_TG
    assert world.store.read_bytes() == before
    assert world.resets() == []


def test_another_accounts_name_is_neither_read_as_this_ones_nor_overwritten(world):
    """A lane of Dana's that has her scope but no username passes the local admin's name
    (thinking mode, the room watchman). The store keeps the admin's chat under that name,
    so it must neither count as Dana's (it would be discarded, with a false event) nor be
    overwritten with Dana's chat.
    MUTATION: treat the name asked with as the account's own whatever entry carries it -> red.
    MUTATION: write the name asked with instead of the name the account is paired under -> red."""
    world.seed(local_admin_scope_id=ADMIN, local_admin_username="admin",
               telegram_config={"enabled": True, "verified": True,
                                "whitelist": [_owner_entry(ADMIN_TG, ADMIN, "admin"), _owner_entry()]})
    world.stored(by_username={"admin": ADMIN_TG})

    assert mc.telegram_chat_id_of(DANA, "admin") == OWNER_TG
    assert world.on_disk()["by_username"] == {"admin": ADMIN_TG, "dana": OWNER_TG}
    assert world.on_disk()["by_scope"] == {DANA: OWNER_TG}
    assert mc.telegram_chat_id_of(None, "admin") == ADMIN_TG, "the admin's scope-less lane"
    assert world.resets() == []


@pytest.mark.parametrize("asked_with, admin_lane, stale", [
    ("dana", (DANA, "dana"), {"by_scope": {DANA: CAROL_TG}, "by_username": {"dana": CAROL_TG}}),
    (None, (None, None), {"by_username": {"admin": CAROL_TG}}),
])
def test_the_local_admins_name_is_never_another_accounts_key(world, asked_with, admin_lane, stale):
    """A lane of Alice's that falls back to the local admin's name (or to "admin", the name a
    lane without one is read as) must leave the admin's key alone even when the admin has
    no Telegram entry to say whose name it is: the admin was paired, a contact wrote before
    the fix, and the pairing was removed. The admin's own lane clears it, once, under the
    admin's name. MUTATION: leave the local admin's names out of the ones another account
    cannot claim -> Alice's lane clears it and records it under her name -> red."""
    _add_carol()
    world.seed(telegram_config={"enabled": True, "verified": True,
                                "whitelist": [_owner_entry(ALICE_TG, ALICE, "alice")]})
    world.stored(**stale)

    assert mc.telegram_chat_id_of(ALICE, asked_with) == ALICE_TG
    after = world.on_disk()
    for key, value in stale.get("by_username", {}).items():
        assert after["by_username"].get(key) == value, "Alice's lane cleared the admin's key"
    assert world.resets() == []

    assert mc.telegram_chat_id_of(*admin_lane) is None
    assert [(r["username"], r["path"]) for r in world.resets()] == [(admin_lane[1] or "admin", CAROL_TG)]
    assert mc.telegram_chat_id_of(*admin_lane) is None
    assert len(world.resets()) == 1


def test_a_name_on_an_entry_without_a_scope_is_not_a_scoped_callers_key(world):
    """An entry without a scope (none has been written since the first release, the matcher
    still reads one) is filed under its name for everybody, so that name is not a scoped
    caller's own key. MUTATION: skip entries without a scope when collecting the names
    another account holds -> Alice's lane clears Bob's key -> red."""
    world.seed(telegram_config={"enabled": True, "verified": True,
                                "whitelist": [_owner_entry(), _owner_entry(ALICE_TG, ALICE, "alice"),
                                              {"telegram_user_id": "7000000601", "vaf_username": "bob"}]})
    world.stored(by_scope={ALICE: ALICE_TG}, by_username={"bob": CAROL_TG})

    assert mc.telegram_chat_id_of(ALICE, "bob") == ALICE_TG
    assert world.on_disk()["by_username"] == {"bob": CAROL_TG}
    assert world.resets() == []


def test_the_reminder_reaches_the_owner_and_stays_out_of_the_contacts_chat(world, monkeypatch):
    """AC8: the delivery lane every reminder, automation and `send_to_user` takes. MUTATION:
    return the stored value without the paired-id check -> the reminder goes to Carol -> red."""
    _add_carol()
    world.stored(by_scope={DANA: CAROL_TG}, by_username={"dana": CAROL_TG})
    monkeypatch.setattr(mc, "get_messaging_connections",
                        lambda **kw: {"available": ["telegram"], "outbound": [], "main_messenger": "telegram"})
    sent, sessions, store_rows = [], [], []
    import vaf.core.telegram_reply as tr
    monkeypatch.setattr(tr, "send_telegram_reply", lambda chat_id, text, **kw: sent.append((str(chat_id), text)) or True)

    class _Sessions:
        def append_background_message(self, session_id, text, **kw):
            sessions.append(session_id)
    import vaf.core.session as session_mod
    monkeypatch.setattr(session_mod, "SessionManager", _Sessions)
    import vaf.core.channel_message_store as cms
    monkeypatch.setattr(cms, "append_message", lambda **kw: store_rows.append(kw["chat_id"]))

    assert mc.send_to_main_messenger(DANA, "dana", "Reminder: call the bank at 3") == (True, "telegram")
    assert sent == [(OWNER_TG, "Reminder: call the bank at 3")]
    assert sessions == [f"telegram_{OWNER_TG}"]
    assert store_rows == [OWNER_TG]
    assert f"telegram_{CAROL_TG}" not in sessions
