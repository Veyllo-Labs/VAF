# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Who may bind a WhatsApp LID chat to a number, and which lanes read the binding.

THE DEFECT, measured on the code before this change (synthetic scopes and numbers, fake Node
processes, an in-memory config):

- `POST /api/whatsapp/lid-assign` checked neither the role nor ownership and wrote the ONE
  map every account's bridge reads. A non-admin bound a LID to the admin's own number; the
  admin's bridge, receiving from that LID, matched the admin's registered number (the owner
  lane, full tools) and saved the LID as the admin's endpoint for proactive sends.
- `whatsapp_send_jid(<admin's number>)` then answered the attacker's LID: every send to the
  admin went to the attacker.
- The sender loop preferred a LID by SUFFIX in both directions: ten bindings to +0 ... +9
  caught sends to three unrelated numbers.
- `GET /api/whatsapp/dashboard` guessed a binding (one allowed contact without messages, one
  LID chat with messages) and wrote it into that same shared map.
- `POST /api/whatsapp/whitelist/remove` authorised a non-admin by the FIRST entry carrying
  the number and then removed every entry with it, the admin's owner entry included.

THE RULE: an admin's binding goes into the shared map; anybody else's into their own map,
read only by their own lane (`contacts_store.whatsapp_lid_map`). Nothing is guessed, the send
path matches the exact number, and a non-admin removes only their own whitelist entries.
"""
import asyncio
import io
import json
import os
import random
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from vaf.api import whatsapp_bridge as wa
from vaf.api import whatsapp_routes as routes
from vaf.core import channel_message_store as store
from vaf.core import contacts_store
from vaf.core.config import Config
from vaf.core.platform import Platform

ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
SECOND_ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000bb"
ALICE_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
BOB_SCOPE = "ab12cd34-0000-4000-8000-000000000002"

LOCAL_ADMIN = {"role": "admin", "user_scope_id": ADMIN_SCOPE, "username": "admin"}
SECOND_ADMIN = {"role": "admin", "user_scope_id": SECOND_ADMIN_SCOPE, "username": "carol"}
ALICE = {"role": "user", "user_scope_id": ALICE_SCOPE, "username": "alice"}
BOB = {"role": "user", "user_scope_id": BOB_SCOPE, "username": "bob"}
SCOPES = {"admin": ADMIN_SCOPE, "carol": SECOND_ADMIN_SCOPE, "alice": ALICE_SCOPE, "bob": BOB_SCOPE}

ADMIN_PHONE = "+4917000000001"
LID = "999999999999999@lid"


def _req(user):
    return SimpleNamespace(state=SimpleNamespace(user=user))


class _State(dict):
    """The config dict, with the security log recorder beside it (not inside: a save
    replaces the dict's values with copies)."""
    events: list


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """An in-memory config the routes and the bridge read and write, scratch stores, and
    a recorder for the security log."""
    state = _State({
        "local_admin_scope_id": ADMIN_SCOPE,
        "local_admin_username": "admin",
        "channel_ingress_policy": None,
        "whatsapp_config": {
            "enabled": True, "inbound_to_agent": True,
            "whitelist": [{"phone_number": ADMIN_PHONE, "user_scope_id": ADMIN_SCOPE, "vaf_username": "admin"}],
        },
    })
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: json.loads(json.dumps(state))))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, c: state.update(json.loads(json.dumps(c)))))
    monkeypatch.setattr("vaf.core.config.scope_id_for_username", lambda name: SCOPES.get(str(name or "")))
    monkeypatch.setattr(wa, "_append_chat_activity", lambda *a, **k: None)
    monkeypatch.setattr(wa, "_wa_pending", {})
    monkeypatch.setattr(wa, "_lid_mappings", {})
    monkeypatch.setattr(wa, "_processes", {})
    monkeypatch.setattr(wa, "_scope_by_user", {})
    events = []
    monkeypatch.setattr(routes, "log_security_event", lambda kind, **f: events.append((kind, f)))
    state.events = events
    return state


def _assign(user, lid=LID, phone=ADMIN_PHONE):
    return asyncio.run(routes.assign_lid_to_number(_req(user), routes.LidAssignRequest(lid_jid=lid, phone_number=phone)))


def _maps(state):
    wc = state["whatsapp_config"]
    return json.dumps({"shared": wc.get("lid_to_e164"), "by_scope": wc.get("lid_to_e164_by_scope")}, sort_keys=True)


def _inbound(monkeypatch, username, scope, from_jid):
    """One inbound message through the real bridge dispatch; returns what it decided."""
    seen = {"explicit": [], "saved": []}
    real = wa.evaluate_ingress

    def _evaluate(channel, policy, *, explicit_match, access):
        seen["explicit"].append(explicit_match)
        return real(channel, policy, explicit_match=explicit_match, access=access)

    monkeypatch.setattr(wa, "evaluate_ingress", _evaluate)
    monkeypatch.setattr(wa, "save_whatsapp_chat_jid", lambda *a: seen["saved"].append(a))
    monkeypatch.setattr(wa, "_get_allowed_phones_for_user",
                        lambda u, s: ([ADMIN_PHONE], [ADMIN_PHONE]) if u == "admin" else ([], []))
    wa._dispatch_bridge_event(username, scope, "message",
                              {"from": from_jid, "senderJid": from_jid, "body": "hi", "chatType": "dm", "messageId": "m1"})
    rec = wa._wa_pending.get(f"{username}|{from_jid}")
    if rec and rec.get("timer") is not None:
        rec["timer"].cancel()
    return seen


# -- AC 1: a non-admin never writes the shared map ---------------------------------------------

def test_a_non_admins_binding_never_gives_a_stranger_the_admins_owner_lane(cfg, monkeypatch):
    """P1. MUTATION: the non-admin branch writes `lid_to_e164` (the old line back): the admin's
    bridge answers the LID as the admin and saves it as the admin's endpoint."""
    out = _assign(ALICE)
    assert out["status"] == "assigned" and "your account only" in out["message"]
    wc = cfg["whatsapp_config"]
    assert "lid_to_e164" not in wc
    assert wc["lid_to_e164_by_scope"] == {ALICE_SCOPE: {LID: ADMIN_PHONE}}

    seen = _inbound(monkeypatch, "admin", ADMIN_SCOPE, LID)
    assert True not in seen["explicit"], "the admin's bridge read the stranger as the owner"
    assert seen["saved"] == [], "the stranger's LID became the admin's endpoint"
    assert store.get_chat_messages("admin", ADMIN_PHONE, user_scope_id=ADMIN_SCOPE) == []
    assert contacts_store.whatsapp_send_jid(ADMIN_PHONE) == "4917000000001@s.whatsapp.net"
    # Recorded, with who made it and that it is theirs alone.
    assert cfg.events == [("channel_paired", {
        "channel": "whatsapp", "username": "alice", "path": LID,
        "detail": f"lid {LID} as {ADMIN_PHONE} (this account only)"})]


# -- AC 2: the own lane works, and only there -------------------------------------------------

def test_a_non_admins_binding_acts_on_their_own_lane_and_nowhere_else(cfg, monkeypatch):
    """MUTATION: `whatsapp_lid_map` overlays every scope's map, or ignores its scope argument."""
    contacts_store.create_contact("Dana", "alice", user_scope_id=ALICE_SCOPE, whatsapp_phone="+491700000077",
                                  allow_as_assistant_user=True)
    contacts_store.create_contact("Dana", "bob", user_scope_id=BOB_SCOPE, whatsapp_phone="+491700000077",
                                  allow_as_assistant_user=True)
    _assign(ALICE, phone="+491700000077")

    assert contacts_store.whatsapp_lid_map(ALICE_SCOPE) == {LID: "+491700000077"}
    assert contacts_store.whatsapp_lid_map(BOB_SCOPE) == {}
    assert contacts_store.whatsapp_lid_map(None) == {}
    assert wa._lid_map_e164(LID, ALICE_SCOPE) == "+491700000077"
    assert wa._lid_map_e164(LID, BOB_SCOPE) == ""
    assert wa._jid_to_chat_id("alice", LID, ALICE_SCOPE) == "+491700000077"
    assert wa._jid_to_chat_id("bob", LID, BOB_SCOPE) == ""
    assert contacts_store.find_contact_by_channel("whatsapp", LID, "alice", ALICE_SCOPE)["name"] == "Dana"
    assert contacts_store.find_contact_by_channel("whatsapp", LID, "bob", BOB_SCOPE) is None
    assert contacts_store.find_contact_by_channel("whatsapp", LID, "admin", ADMIN_SCOPE) is None

    # Her own bridge files the LID's message under the number; Bob's keeps it under the LID.
    _inbound(monkeypatch, "alice", ALICE_SCOPE, LID)
    _inbound(monkeypatch, "bob", BOB_SCOPE, LID)
    assert [m["body"] for m in store.get_chat_messages("alice", "+491700000077", user_scope_id=ALICE_SCOPE)] == ["hi"]
    assert store.get_chat_messages("bob", "+491700000077", user_scope_id=BOB_SCOPE) == []
    assert [m["body"] for m in store.get_chat_messages("bob", LID, user_scope_id=BOB_SCOPE)] == ["hi"]
    # The store's own LID lookup follows the lane too.
    assert len(store.get_chat_messages("alice", LID, user_scope_id=ALICE_SCOPE)) == 1


def _dashboard(monkeypatch, user):
    monkeypatch.setattr(wa, "get_whatsapp_chats", lambda *a, **k: [])
    monkeypatch.setattr(wa, "get_lid_mappings", lambda *a, **k: [])
    monkeypatch.setattr(wa, "get_contact_names", lambda *a, **k: {})
    monkeypatch.setattr(wa, "get_connection_status", lambda *a, **k: False)
    monkeypatch.setattr(wa, "is_bridge_running", lambda: False)
    return asyncio.run(routes.get_whatsapp_dashboard(_req(user)))


def test_the_dashboard_merges_a_chat_by_the_callers_own_binding_only(cfg, monkeypatch):
    for who in ("alice", "bob"):
        store.append_message(who, LID, "from the lid", direction="in", user_scope_id=SCOPES[who], ts=100.0)
    _assign(ALICE, phone="+491700000077")
    alice = {s["chat_id"]: s for s in _dashboard(monkeypatch, ALICE)["sessions"]}
    bob = {s["chat_id"]: s for s in _dashboard(monkeypatch, BOB)["sessions"]}
    assert "+491700000077" in alice and LID not in alice
    assert LID in bob and bob[LID]["needs_assign"] is True and "+491700000077" not in bob


# -- AC 3: an admin's binding is installation-wide, as before ---------------------------------

@pytest.mark.parametrize("admin", [LOCAL_ADMIN, SECOND_ADMIN], ids=["local_admin", "second_admin"])
def test_an_admins_binding_is_shared_as_before(cfg, monkeypatch, admin):
    """MUTATION: `caller_is_admin` replaced by `is_local_admin_lane` (the second admin goes red)."""
    out = _assign(admin)
    assert "every account" in out["message"]
    assert cfg["whatsapp_config"]["lid_to_e164"] == {LID: ADMIN_PHONE}
    assert "lid_to_e164_by_scope" not in cfg["whatsapp_config"]
    assert cfg.events[-1][1]["detail"] == f"lid {LID} as {ADMIN_PHONE}"
    # The positive control for AC 1: the probe sees the owner lane when the binding is shared.
    seen = _inbound(monkeypatch, "admin", ADMIN_SCOPE, LID)
    assert seen["explicit"] == [True] and seen["saved"] == [(ADMIN_SCOPE, "admin", LID)]
    assert contacts_store.whatsapp_lid_map(BOB_SCOPE) == {LID: ADMIN_PHONE}


# -- AC 4: validation -------------------------------------------------------------------------

@pytest.mark.parametrize("user, lid, phone", [
    (ALICE, "1234@lid:5", ADMIN_PHONE),
    (ALICE, "abc@lid", ADMIN_PHONE),
    (ALICE, "12345@s.whatsapp.net", ADMIN_PHONE),
    (ALICE, "1" * 25 + "@lid", ADMIN_PHONE),
    (LOCAL_ADMIN, "1" * 25 + "@lid", ADMIN_PHONE),
    (ALICE, LID, "+1"),
    (ALICE, LID, "+" + "4" * 16),
    (ALICE, LID, "+49 170 1234567 ext 9"),
    (LOCAL_ADMIN, LID, "+49 170 1234567 ext 9"),
    (ALICE, "4917000000001@lid", ADMIN_PHONE),
    (LOCAL_ADMIN, "4917000000001@lid", "0049 170 00000001"),
    ({"role": "user", "user_scope_id": "", "username": "dave"}, LID, ADMIN_PHONE),
], ids=["device-suffix", "letters", "phone-jid", "25-digits", "25-digits-admin", "too-short",
        "16-digits", "extension", "extension-admin", "fake-lid", "fake-lid-admin", "no-scope"])
def test_a_binding_that_is_not_a_lid_and_a_number_is_refused(cfg, user, lid, phone):
    """MUTATION: drop the LID pattern, the number check, the fake-LID check or the scope check."""
    before = _maps(cfg)
    with pytest.raises(HTTPException) as err:
        _assign(user, lid=lid, phone=phone)
    assert err.value.status_code == 400
    assert _maps(cfg) == before and cfg.events == []


def test_the_stored_number_is_the_canonical_form(cfg):
    _assign(LOCAL_ADMIN, phone="0049 170 00000001")
    assert cfg["whatsapp_config"]["lid_to_e164"] == {LID: "+4917000000001"}


def test_one_account_binds_at_most_a_thousand_lids(cfg):
    """MUTATION: `>` for `>=` in the cap (the 1001st is accepted)."""
    own = {f"{100000000000 + i}@lid": "+491700000077" for i in range(routes.LID_BINDINGS_PER_ACCOUNT)}
    cfg["whatsapp_config"]["lid_to_e164_by_scope"] = {ALICE_SCOPE: dict(own)}
    with pytest.raises(HTTPException) as err:
        _assign(ALICE, lid="777777777777777@lid")
    assert err.value.status_code == 409
    assert cfg["whatsapp_config"]["lid_to_e164_by_scope"][ALICE_SCOPE] == own
    first = next(iter(own))
    assert _assign(ALICE, lid=first, phone="+491700000078")["status"] == "assigned"
    assert cfg["whatsapp_config"]["lid_to_e164_by_scope"][ALICE_SCOPE][first] == "+491700000078"
    assert len(cfg["whatsapp_config"]["lid_to_e164_by_scope"][ALICE_SCOPE]) == routes.LID_BINDINGS_PER_ACCOUNT


# -- AC 5: a GET writes no binding ------------------------------------------------------------

@pytest.mark.parametrize("user", [ALICE, LOCAL_ADMIN], ids=["non_admin", "admin"])
def test_opening_the_dashboard_binds_nothing(cfg, monkeypatch, user):
    """P8: one allowed contact without messages and one LID chat with messages. MUTATION: the
    inference block restored."""
    who, scope = user["username"], user["user_scope_id"]
    cfg["whatsapp_config"]["whitelist"] = []
    contacts_store.create_contact("Owner", who, user_scope_id=scope, whatsapp_phone=ADMIN_PHONE,
                                  allow_as_assistant_user=True)
    store.append_message(who, LID, "guess me", direction="in", user_scope_id=scope, ts=100.0)
    before = _maps(cfg)
    _dashboard(monkeypatch, user)
    assert _maps(cfg) == before


# -- AC 6: the send path matches the exact number ---------------------------------------------

def _run_sender(monkeypatch, tmp_path, items):
    """Drive the real sender loop over `items` with a fake admin process; returns the commands."""
    import queue as _queue
    written = io.StringIO()
    monkeypatch.setattr(wa, "_processes", {"admin": SimpleNamespace(stdin=written, poll=lambda: None)})
    monkeypatch.setattr(wa, "_scope_by_user", {"admin": ADMIN_SCOPE})
    q = _queue.Queue()
    for it in items:
        q.put(it)
    q.put(None)
    monkeypatch.setattr(wa, "_outgoing_queue", q)
    wa._sender_loop()
    return [json.loads(line) for line in written.getvalue().splitlines()]


UNRELATED = ["491511112220", "4930123451", "33612345679"]


def _suffix_bait(cfg):
    """P2's map, as a binding made before the update would still sit in the shared map."""
    cfg["whatsapp_config"]["lid_to_e164"] = {f"7777777777777{i:02d}@lid": f"+{i}" for i in range(10)}


def test_text_sends_keep_their_number_whatever_a_map_entry_ends_with(cfg, monkeypatch, tmp_path):
    """MUTATION: the suffix matcher restored in the text branch."""
    _suffix_bait(cfg)
    cmds = _run_sender(monkeypatch, tmp_path, [("admin", f"{n}@s.whatsapp.net", "hi") for n in UNRELATED])
    assert [c["to"] for c in cmds] == [f"{n}@s.whatsapp.net" for n in UNRELATED]


def test_voice_sends_keep_their_number_whatever_a_map_entry_ends_with(cfg, monkeypatch, tmp_path):
    """MUTATION: the suffix matcher restored in the voice branch."""
    _suffix_bait(cfg)
    voice = tmp_path / "v.ogg"
    voice.write_bytes(b"OggS")
    cmds = _run_sender(monkeypatch, tmp_path, [("admin", f"{n}@s.whatsapp.net", "", str(voice)) for n in UNRELATED])
    assert [(c["cmd"], c["to"]) for c in cmds] == [("send_voice", f"{n}@s.whatsapp.net") for n in UNRELATED]


@pytest.mark.parametrize("kind", ["text", "voice"])
def test_a_lid_is_used_for_exactly_its_number(cfg, monkeypatch, tmp_path, kind):
    cfg["whatsapp_config"]["lid_to_e164"] = {
        "111111111111111@lid": "+4917000000001",      # the exact number
        "222222222222222@lid": "+491700000000",       # one digit shorter
        "333333333333333@lid": "+7000000002",         # a suffix of the next recipient
        "4917000000003@lid": "+4917000000003",        # a fake LID: the number itself
    }
    voice = tmp_path / "v.ogg"
    voice.write_bytes(b"OggS")
    extra = ("", str(voice)) if kind == "voice" else ("hi",)
    recipients = ["4917000000001", "4917000000000", "4917000000002", "4917000000003"]
    items = [("admin", f"{n}@s.whatsapp.net", *extra) for n in recipients]
    if kind == "text":
        items = [("admin", f"{n}@s.whatsapp.net", "hi") for n in recipients]
    cmds = _run_sender(monkeypatch, tmp_path, items)
    assert [c["to"] for c in cmds] == ["111111111111111@lid", "4917000000000@s.whatsapp.net",
                                      "4917000000002@s.whatsapp.net", "4917000000003@s.whatsapp.net"]


# -- AC 8: whitelist/remove -------------------------------------------------------------------

def _remove(user, phone):
    return asyncio.run(routes.remove_whitelist_entry(_req(user), routes.WhitelistAddRequest(phone_number=phone)))


ALICE_ENTRY = {"phone_number": ADMIN_PHONE, "user_scope_id": ALICE_SCOPE, "vaf_username": "alice"}
ADMIN_ENTRY = {"phone_number": ADMIN_PHONE, "user_scope_id": ADMIN_SCOPE, "vaf_username": "admin"}
BOB_ENTRY = {"phone_number": "+491700000088", "user_scope_id": BOB_SCOPE, "vaf_username": "bob"}


def test_a_non_admin_removes_only_their_own_entry_for_a_shared_number(cfg):
    """P4. MUTATION: the old first-match check restored (the admin's entry goes too)."""
    cfg["whatsapp_config"]["whitelist"] = [ALICE_ENTRY, ADMIN_ENTRY, BOB_ENTRY]
    assert _remove(ALICE, ADMIN_PHONE)["whitelist_count"] == 2
    assert cfg["whatsapp_config"]["whitelist"] == [ADMIN_ENTRY, BOB_ENTRY]
    with pytest.raises(HTTPException) as err:
        _remove(ALICE, BOB_ENTRY["phone_number"])
    assert err.value.status_code == 403
    assert cfg["whatsapp_config"]["whitelist"] == [ADMIN_ENTRY, BOB_ENTRY]


def test_an_admin_removes_every_entry_with_the_number(cfg):
    cfg["whatsapp_config"]["whitelist"] = [ALICE_ENTRY, ADMIN_ENTRY, BOB_ENTRY]
    assert _remove(LOCAL_ADMIN, ADMIN_PHONE)["whitelist_count"] == 1
    assert cfg["whatsapp_config"]["whitelist"] == [BOB_ENTRY]


def test_a_non_admin_without_a_scope_changes_no_entry(cfg):
    """`get_current_vaf_user` reads a scope-less account as the local admin; a write must not."""
    cfg["whatsapp_config"]["whitelist"] = [ADMIN_ENTRY]
    nobody = {"role": "user", "user_scope_id": "", "username": "dave"}
    for call in (lambda: _remove(nobody, ADMIN_PHONE),
                 lambda: asyncio.run(routes.add_whitelist_entry(
                     _req(nobody), routes.WhitelistAddRequest(phone_number="+491700000099")))):
        with pytest.raises(HTTPException) as err:
            call()
        assert err.value.status_code == 400
    assert cfg["whatsapp_config"]["whitelist"] == [ADMIN_ENTRY]


# -- fuzz -------------------------------------------------------------------------------------

FUZZ_DRAWS = int(os.environ.get("VAF_F422_FUZZ_DRAWS", "300"))


def _digits(rng, lo, hi):
    return "".join(rng.choice("0123456789") for _ in range(rng.randint(lo, hi)))


def _fuzz_value(rng, numbers):
    pick = rng.random()
    n = rng.choice(numbers)
    if pick < 0.35:
        return "+" + n
    if pick < 0.45:
        return "00" + n
    if pick < 0.55:
        return "0" + n[2:]
    if pick < 0.65:
        return n
    if pick < 0.75:
        return "+" + _digits(rng, 1, 4)
    if pick < 0.8:
        return "+" + n[rng.randint(1, max(1, len(n) - 1)):]
    if pick < 0.85:
        return int(n) if n else 0
    return rng.choice([None, ["+" + n], {"n": n}, True, "", " + " + n, "+" + _digits(rng, 16, 20)])


def _fuzz_key(rng, numbers):
    pick = rng.random()
    if pick < 0.75:
        return _digits(rng, 1, 24) + "@lid"
    if pick < 0.85:
        return rng.choice(numbers) + "@lid"                      # a fake LID
    return rng.choice(["abc@lid", "12a@lid", "@lid", "123@s.whatsapp.net", "", "123", "١٢٣@lid"])


def _reference_choice(merged, recipient):
    """The oracle, written from the rule: the first genuine LID in map order whose value is a
    number with exactly the recipient's canonical digits, else the recipient itself."""
    from vaf.core.contacts_store import phone_digits_canonical
    if recipient.endswith("@lid"):
        return recipient
    want = phone_digits_canonical(recipient)
    if not want:
        return recipient
    for key, value in merged.items():
        k = str(key).strip()
        user, _, server = k.partition("@")
        if server != "lid" or not user or not all(c in "0123456789" for c in user):
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        if phone_digits_canonical(str(value)) == want and user != want:
            return k
    return recipient


def test_fuzz_the_send_path_picks_only_an_exact_genuine_lid(cfg, monkeypatch):
    """Seeded: random maps (0 to 200 entries, junk keys, fake LIDs, every notation and some
    non-strings as values) and random recipients, against the reference above."""
    rng = random.Random(4220)
    monkeypatch.setattr(wa, "_scope_by_user", {"admin": ADMIN_SCOPE})
    mismatches = []
    for draw in range(FUZZ_DRAWS):
        numbers = [_digits(rng, 7, 15) for _ in range(rng.randint(1, 6))]
        shared = {_fuzz_key(rng, numbers): _fuzz_value(rng, numbers) for _ in range(rng.randint(0, 200))}
        own = {_fuzz_key(rng, numbers): _fuzz_value(rng, numbers) for _ in range(rng.randint(0, 20))}
        cfg["whatsapp_config"]["lid_to_e164"] = shared
        cfg["whatsapp_config"]["lid_to_e164_by_scope"] = {ADMIN_SCOPE: own, BOB_SCOPE: {"5@lid": "+" + numbers[0]}}
        n = rng.choice(numbers + [_digits(rng, 7, 15)])
        recipient = rng.choice([f"{n}@s.whatsapp.net", f"{n}:3@s.whatsapp.net", f"{n}@lid", f"{n}@g.us"])
        got = wa._preferred_lid_jid("admin", recipient, "text")
        want = _reference_choice({**shared, **own}, recipient)
        if got != want:
            mismatches.append((draw, recipient, got, want))
    assert mismatches == []


def test_fuzz_one_accounts_bindings_never_reach_another_lane(cfg):
    """Seeded: random per-scope maps over 1 to 5 scopes plus a shared map."""
    rng = random.Random(4221)
    for _ in range(FUZZ_DRAWS):
        scopes = [f"ab12cd34-0000-4000-8000-{i:012d}" for i in range(rng.randint(1, 5))]
        shared = {f"{_digits(rng, 3, 15)}@lid": f"+{_digits(rng, 7, 15)}" for _ in range(rng.randint(0, 30))}
        by_scope = {s: {f"{_digits(rng, 3, 15)}@lid": f"+{_digits(rng, 7, 15)}" for _ in range(rng.randint(0, 30))}
                    for s in scopes}
        cfg["whatsapp_config"]["lid_to_e164"] = shared
        cfg["whatsapp_config"]["lid_to_e164_by_scope"] = by_scope
        assert contacts_store.whatsapp_lid_map(None) == shared
        assert contacts_store.whatsapp_lid_map("") == shared
        for s in scopes + ["ab12cd34-0000-4000-8000-ffffffffffff"]:
            got = contacts_store.whatsapp_lid_map(s)
            assert got == {**shared, **by_scope.get(s, {})}
            own = by_scope.get(s, {})
            for other in scopes:
                if other == s:
                    continue
                for key, value in by_scope[other].items():
                    if key not in shared and key not in own:
                        assert key not in got, (s, other, key)
                    if value not in shared.values() and value not in own.values():
                        assert value not in got.values(), (s, other, value)


def test_odd_shapes_count_as_empty(cfg):
    for wc in ({"lid_to_e164": ["x"], "lid_to_e164_by_scope": {ALICE_SCOPE: ["y"]}},
               {"lid_to_e164": None, "lid_to_e164_by_scope": "z"},
               "not a dict"):
        cfg["whatsapp_config"] = wc
        assert contacts_store.whatsapp_lid_map(ALICE_SCOPE) == {}
        assert contacts_store.whatsapp_lid_map(None) == {}


def test_the_send_path_stays_fast_on_a_large_shared_map(cfg, monkeypatch):
    """Linear in the map; measured outside the suite at 10k and 100k entries."""
    monkeypatch.setattr(wa, "_scope_by_user", {"admin": ADMIN_SCOPE})
    cfg["whatsapp_config"]["lid_to_e164"] = {f"{100000000000 + i}@lid": f"+491700{i:07d}" for i in range(10000)}
    start = time.perf_counter()
    assert wa._preferred_lid_jid("admin", "4917000009999@s.whatsapp.net", "text") == "100000009999@lid"
    assert time.perf_counter() - start < 1.0


# -- the other readers of the lane ------------------------------------------------------------

def test_the_inbox_counts_an_own_binding_in_its_own_lane_only(cfg):
    """MUTATION: `inbox._messenger_rows` reads `whatsapp_lid_map(None)`."""
    from vaf.core import inbox
    for who in ("alice", "bob"):
        store.append_message(who, LID, "hi", direction="in", user_scope_id=SCOPES[who], ts=100.0)
    _assign(ALICE, phone="+491700000077")

    def mode(who):
        rows = inbox.list_conversations(who, SCOPES[who], channels=["whatsapp"], include_done=True)["rows"]
        return next(r["mode"] for r in rows if r["id"] == LID)

    assert mode("alice") != "needs_assign"
    assert mode("bob") == "needs_assign"


def test_contact_history_reads_the_callers_own_bindings_once(cfg):
    """MUTATION: drop the lane read from `contact_timeline` or `contact_activity_stats` (the
    LID-addressed message disappears for Alice), or the timeline's per-row dedupe (her
    number's message is listed twice: the LID key reads the number's rows too)."""
    dana = {who: contacts_store.create_contact("Dana", who, user_scope_id=SCOPES[who],
                                               whatsapp_phone="+491700000077") for who in ("alice", "bob")}
    for who in ("alice", "bob"):
        store.append_message(who, "+491700000077", "by number", direction="in", user_scope_id=SCOPES[who],
                             ts=100.0, message_id=f"n-{who}")
        store.append_message(who, LID, "to the lid", direction="out", user_scope_id=SCOPES[who], ts=200.0,
                             message_id=f"l-{who}")
    _assign(ALICE, phone="+491700000077")

    def bodies(who):
        items = contacts_store.contact_timeline(dana[who], who, SCOPES[who], kinds={"message"})["items"]
        return sorted(i["body"] for i in items)

    assert bodies("alice") == ["by number", "to the lid"]
    assert bodies("bob") == ["by number"]
    assert contacts_store.contact_activity_stats(dana["alice"], "alice", ALICE_SCOPE)["messages"] == 2
    assert contacts_store.contact_activity_stats(dana["bob"], "bob", BOB_SCOPE)["messages"] == 1
