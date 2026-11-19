# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A stored WhatsApp chat of an account is never another account's registered number.

THE DEFECT, measured with synthetic scopes and numbers on the code before this change: while a
scoped caller without a name was read as "admin", the endpoint store took two kinds of wrong
value, and `get_whatsapp_chat_jid` returned both as stored:
(a) `whatsapp_by_scope[<a non-admin's scope>]` holding the admin's registered number (also as
    a LID the shared map binds to it), returned for that account;
(b) `whatsapp_by_username["admin"]` holding a non-admin's number, returned to the scoped admin
    read while the admin had no scope key, and to the lane without a scope ALWAYS, scope key
    or not.
With Inbound open, a later process of the right account sends a private note there.

THE RULE: the reader drops a stored JID whose number is another account's registered number
from the keys it consults (both, on every read), records it once as `owner_endpoint_reset`,
and falls back to the account's own registered number. The account's own numbers, numbers
nobody registered and entries nobody can be attributed (a scopeless entry filed under a
non-admin's name) are kept. A name key another account holds is never cleared or written.
"""
import json
import os
import random
import queue
import io
from types import SimpleNamespace

import pytest

from vaf.core import messaging_connections as mc
from vaf.core.config import Config
from vaf.core.platform import Platform

ADM = "ab12cd34-0000-4000-8000-0000000000aa"      # the local admin, named "admin"
ALI = "ab12cd34-0000-4000-8000-000000000001"      # Alice, an ordinary account
BOB = "ab12cd34-0000-4000-8000-000000000002"      # Bob, an ordinary account
B_ADMIN = "ab12cd34-0000-4000-8000-000000000003"  # an ordinary account that registered as "admin"
ADMIN_PHONE, ALICE_PHONE, BOB_PHONE = "+4917000000010", "+4917000000011", "+4917000000012"
ADMIN_JID, ALICE_JID, BOB_JID = (p[1:] + "@s.whatsapp.net" for p in (ADMIN_PHONE, ALICE_PHONE, BOB_PHONE))
CAROL_PHONE = "+4917000000013"                    # filed under "alice" without a scope
CAROL_JID = CAROL_PHONE[1:] + "@s.whatsapp.net"
STRANGER_JID = "4917000000099@s.whatsapp.net"    # registered by nobody
ADMIN_LID = "100000000000010@lid"
UNMAPPED_LID = "100000000000077@lid"
GROUP_JID = "120363000000000001@g.us"


def _entry(phone, scope, name):
    out = {"phone_number": phone, "vaf_username": name}
    if scope is not None:
        out["user_scope_id"] = scope
    return out


ADMIN_ENTRY = _entry(ADMIN_PHONE, ADM, "admin")
ALICE_ENTRY = _entry(ALICE_PHONE, ALI, "alice")
BOB_ENTRY = _entry(BOB_PHONE, BOB, "bob")


@pytest.fixture
def world(monkeypatch, tmp_path):
    state = {"local_admin_scope_id": ADM, "local_admin_username": "admin",
             "whatsapp_config": {"enabled": True, "whitelist": [dict(ADMIN_ENTRY), dict(ALICE_ENTRY)]}}
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    store = tmp_path / "data" / "messaging_endpoints.json"
    monkeypatch.setattr(mc, "_ENDPOINTS_FILE", store)
    events = []
    import vaf.core.security_events as sec
    monkeypatch.setattr(sec, "log_security_event", lambda kind, **f: events.append((kind, f)))

    def whitelist(*entries):
        state["whatsapp_config"]["whitelist"] = [dict(e) for e in entries]

    def stored(by_scope=None, by_name=None):
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"by_scope": {}, "by_username": {},
                                     "whatsapp_by_scope": dict(by_scope or {}),
                                     "whatsapp_by_username": dict(by_name or {})}, indent=2), encoding="utf-8")

    def on_disk():
        return json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}

    def resets():
        return [f for kind, f in events if kind == "owner_endpoint_reset"]

    return SimpleNamespace(state=state, whitelist=whitelist, stored=stored, on_disk=on_disk,
                           store=store, resets=resets)


# -- AC1 kind (a) ---------------------------------------------------------------------------

@pytest.mark.parametrize("admin_entry", [
    ADMIN_ENTRY, _entry(ADMIN_PHONE, None, ""), _entry(ADMIN_PHONE, None, "admin"),
    _entry(ADMIN_PHONE, None, "Dana"),
], ids=["scoped", "scopeless-nameless", "scopeless-admin", "scopeless-local-admin-name"])
def test_kind_a_resets_to_the_own_number(world, admin_entry):
    """MUTATION: return the stored scope value without the foreign check -> red. MUTATION:
    read a scopeless entry under the admin's name as nobody's -> the scopeless rows go red."""
    world.state["local_admin_username"] = "Dana"
    world.whitelist(admin_entry, ALICE_ENTRY)
    world.stored(by_scope={ALI: ADMIN_JID})

    assert mc.get_whatsapp_chat_jid(ALI, "alice") == ALICE_JID
    assert world.on_disk()["whatsapp_by_scope"][ALI] == ALICE_JID
    assert world.resets() == [{"channel": "whatsapp", "username": "alice", "path": ADMIN_JID,
                               "detail": f"registered number of account {admin_entry['vaf_username']}; "
                                         "reset to the registered number"
                               if admin_entry["vaf_username"] else
                               "registered number of another account; reset to the registered number"}]


def test_kind_a_without_own_number_returns_none(world):
    """MUTATION: skip the delete -> the file still holds the admin's JID -> red."""
    world.whitelist(ADMIN_ENTRY)
    world.stored(by_scope={ALI: ADMIN_JID})

    assert mc.get_whatsapp_chat_jid(ALI, "alice") is None
    assert ALI not in world.on_disk()["whatsapp_by_scope"]
    assert "alice" not in world.on_disk()["whatsapp_by_username"]
    [event] = world.resets()
    assert event["path"] == ADMIN_JID and event["detail"].endswith("no registered number")


# -- AC2 kind (a) through a LID -------------------------------------------------------------

@pytest.mark.parametrize("where, stored", [
    ("shared", ADMIN_LID), ("own", ADMIN_LID), ("shared", ADMIN_LID.replace("@lid", ":3@lid")),
], ids=["shared-map", "own-map", "device-suffix"])
def test_kind_a_through_a_lid(world, where, stored):
    """The LID's number comes from the lane's own view: the shared map plus the lane's own
    bindings, and a stored `<lid>:<device>@lid` is the LID without its device.
    MUTATION: read the number with `phone_digits_canonical(jid)` only -> red.
    MUTATION: read the shared map only (`whatsapp_lid_map(None)`) -> the own-map row is kept -> red.
    MUTATION: no second lookup without the device -> the device-suffix row is kept -> red."""
    if where == "shared":
        world.state["whatsapp_config"]["lid_to_e164"] = {ADMIN_LID: ADMIN_PHONE}
    else:
        world.state["whatsapp_config"]["lid_to_e164_by_scope"] = {ALI: {ADMIN_LID: ADMIN_PHONE}}
    world.stored(by_scope={ALI: stored})

    assert mc.get_whatsapp_chat_jid(ALI, "alice") == ALICE_JID
    assert [r["path"] for r in world.resets()] == [stored]
    assert mc.get_whatsapp_chat_jid(ALI, "alice") == ALICE_JID
    assert len(world.resets()) == 1


# -- AC3 kind (b) on both lanes -------------------------------------------------------------

@pytest.mark.parametrize("admin_scope_key", [False, True], ids=["no-admin-scope-key", "admin-scope-key"])
@pytest.mark.parametrize("first", [(ADM, "admin"), (None, None)], ids=["scoped-admin", "scopeless"])
def test_kind_b_on_the_scoped_and_the_scopeless_lane(world, admin_scope_key, first):
    """MUTATION: decide "own" with `_entry_is_mine` (the admin owns every entry) -> red.
    MUTATION: stop at the first kept value -> the scoped admin read leaves the foreign name
    key for the scopeless lane, which then reports it a second time -> red. MUTATION: report
    the fallback as the outcome although another stored chat was kept -> red."""
    world.stored(by_scope={ADM: ADMIN_JID} if admin_scope_key else {}, by_name={"admin": ALICE_JID})

    got = mc.get_whatsapp_chat_jid(*first)
    assert got != ALICE_JID
    if admin_scope_key or first == (ADM, "admin"):
        assert got == ADMIN_JID
    assert world.on_disk()["whatsapp_by_username"].get("admin") != ALICE_JID
    kept = admin_scope_key and first == (ADM, "admin")
    outcome = "kept its other saved chat" if kept else "reset to the registered number"
    assert world.resets() == [{"channel": "whatsapp", "username": "admin", "path": ALICE_JID,
                               "detail": f"registered number of account alice; {outcome}"}]
    for lane in [(ADM, "admin"), (None, None)]:
        assert mc.get_whatsapp_chat_jid(*lane) != ALICE_JID
    assert len(world.resets()) == 1, "reported again"


# -- AC4 the admin's own endpoint -----------------------------------------------------------

def _device_suffix(w):
    w.stored(by_scope={ADM: "4917000000010:7@s.whatsapp.net"})
    return (ADM, "admin"), "4917000000010:7@s.whatsapp.net"


def _formatted_whitelist(w):
    w.whitelist(_entry("0049 170 00000010", ADM, "admin"), _entry("+49 170 00000011", ALI, "alice"))
    w.stored(by_scope={ADM: ADMIN_JID}, by_name={"admin": ADMIN_JID})
    return (None, None), ADMIN_JID


def _own_lid(w):
    w.state["whatsapp_config"]["lid_to_e164"] = {ADMIN_LID: "+" + ADMIN_JID.split("@")[0]}
    w.stored(by_scope={ADM: ADMIN_LID})
    return (ADM, "admin"), ADMIN_LID


def _scopeless_admin_entry(name):
    def shape(w):
        w.state["local_admin_username"] = "Dana"
        # Alice registered the same number too: only the admin's own entry keeps it the admin's.
        w.whitelist(_entry(ADMIN_PHONE, None, name), ALICE_ENTRY, _entry(ADMIN_PHONE, ALI, "alice"))
        w.stored(by_scope={ADM: ADMIN_JID}, by_name={"admin": ADMIN_JID})
        return (ADM, "admin"), ADMIN_JID
    return shape


def _shared_number(w):
    w.whitelist(ADMIN_ENTRY, _entry(ADMIN_PHONE, ALI, "alice"))
    w.stored(by_scope={ADM: ADMIN_JID, ALI: ADMIN_JID})
    return (ADM, "admin"), ADMIN_JID


@pytest.mark.parametrize("shape", [
    _device_suffix, _formatted_whitelist, _own_lid, _scopeless_admin_entry(""),
    _scopeless_admin_entry("admin"), _scopeless_admin_entry("dana"), _shared_number,
], ids=["device", "formatted", "lid", "scopeless-nameless", "scopeless-admin", "scopeless-local-name",
        "shared-number"])
def test_the_admins_own_endpoint_is_never_dropped(world, shape):
    """MUTATION: read a scopeless admin entry as nobody's -> the scopeless rows go red.
    MUTATION: leave out the `- own` subtraction -> the shared-number and scopeless rows go red."""
    lane, expected = shape(world)
    before = world.store.read_bytes()
    assert mc.get_whatsapp_chat_jid(*lane) == expected
    assert world.store.read_bytes() == before
    assert world.resets() == []
    if shape is _shared_number:
        assert mc.get_whatsapp_chat_jid(ALI, "alice") == ADMIN_JID, "her registration of it is hers"
        assert world.store.read_bytes() == before and world.resets() == []


# -- AC5 what nobody registered, and entries nobody can be attributed -----------------------

@pytest.mark.parametrize("value", [STRANGER_JID, UNMAPPED_LID, GROUP_JID, CAROL_JID],
                         ids=["unregistered", "unmapped-lid", "group", "unattributed"])
def test_unregistered_and_unattributed_values_are_kept(world, value):
    """MUTATION: read a scopeless entry with a non-admin name as the local admin's -> Carol's
    number becomes the admin's, foreign to Alice -> red. MUTATION: drop every JID that is not
    the lane's own -> every row goes red."""
    world.whitelist(ADMIN_ENTRY, ALICE_ENTRY, _entry(CAROL_PHONE, None, "alice"))
    world.stored(by_scope={ALI: value, ADM: value})
    before = world.store.read_bytes()
    assert mc.get_whatsapp_chat_jid(ALI, "alice") == value
    assert mc.get_whatsapp_chat_jid(ADM, "admin") == value
    assert world.store.read_bytes() == before
    assert world.resets() == []


# -- AC6 only the consulted keys ------------------------------------------------------------

def test_other_accounts_keys_are_untouched(world):
    """MUTATION: delete by number across every key of the block -> Bob's keys go -> red."""
    world.whitelist(ADMIN_ENTRY, ALICE_ENTRY, BOB_ENTRY)
    world.stored(by_scope={ALI: ADMIN_JID, BOB: ADMIN_JID},
                 by_name={"alice": ADMIN_JID, "bob": ADMIN_JID, "admin": ALICE_JID})
    assert mc.get_whatsapp_chat_jid(ALI, "alice") == ALICE_JID
    disk = world.on_disk()
    assert disk["whatsapp_by_scope"] == {ALI: ALICE_JID, BOB: ADMIN_JID}
    assert disk["whatsapp_by_username"] == {"alice": ALICE_JID, "bob": ADMIN_JID, "admin": ALICE_JID}
    assert [r["path"] for r in world.resets()] == [ADMIN_JID]


ADMIN_SELF_CHAT = "4917000000555@s.whatsapp.net"   # the admin's old self-chat, registered by nobody


@pytest.mark.parametrize("admin_value", [ADMIN_JID, ADMIN_SELF_CHAT], ids=["admin-number", "self-chat"])
@pytest.mark.parametrize("alice_registered", [True, False], ids=["alice-registered", "alice-unregistered"])
def test_another_accounts_name_key_is_not_read(world, admin_value, alice_registered):
    """A background lane of Alice that passes the local admin's name (`thinking_mode` reads
    `_current_username or` the admin's name) must not read the admin's key at all: neither the
    admin's number nor the admin's unregistered self-chat, and it neither clears nor overwrites
    that key. MUTATION: consult the name key whoever holds it (only refusing to clear it) ->
    the self-chat rows return the admin's chat -> red. MUTATION: treat every consulted name key
    as the lane's own -> the admin's key is deleted and reported -> red. MUTATION: save the
    fallback under the given name -> the admin's key holds Alice's number -> red."""
    if not alice_registered:
        world.whitelist(ADMIN_ENTRY, BOB_ENTRY)
    world.stored(by_name={"admin": admin_value})
    expected = ALICE_JID if alice_registered else None
    assert mc.get_whatsapp_chat_jid(ALI, "admin") == expected
    disk = world.on_disk()
    assert disk["whatsapp_by_username"] == {"admin": admin_value}
    assert disk["whatsapp_by_scope"] == ({ALI: ALICE_JID} if alice_registered else {})
    assert world.resets() == []
    assert mc.get_whatsapp_chat_jid(None, None) == admin_value


def test_an_ordinary_account_named_admin(world):
    """The local admin is "dana"; account B registered as "admin". B's read clears the local
    admin's number from the "admin" key it owns. The admin's scopeless lane does not read B's
    key, rejects B's number found by name and uses Dana's own registered number, without saving
    it under B's name, so nothing is reported in a loop. MUTATION: drop the fallback check ->
    the scopeless lane returns B's number -> red. MUTATION: no own-number stand-in -> None -> red."""
    world.state["local_admin_username"] = "dana"
    world.whitelist(_entry(ADMIN_PHONE, ADM, "dana"), _entry(BOB_PHONE, B_ADMIN, "admin"))
    world.stored(by_name={"admin": ADMIN_JID})
    assert mc.get_whatsapp_chat_jid(B_ADMIN, "admin") == BOB_JID
    assert [(r["username"], r["path"]) for r in world.resets()] == [("admin", ADMIN_JID)]
    assert world.on_disk()["whatsapp_by_username"] == {"admin": BOB_JID}
    # The scopeless lane is the local admin's; the "admin" key is B's, so it is left alone.
    before = world.store.read_bytes()
    assert mc.get_whatsapp_chat_jid(None, None) == ADMIN_JID
    assert world.store.read_bytes() == before
    world.stored()
    assert mc.get_whatsapp_chat_jid(None, None) == ADMIN_JID
    assert world.on_disk()["whatsapp_by_username"] == {}, "saved under B's name"
    assert len(world.resets()) == 1


B_OWN_LID = "100000000000014@lid"


@pytest.mark.parametrize("dana_registered", [True, False], ids=["dana-registered", "dana-unregistered"])
def test_the_discord_lane_never_takes_an_ordinary_admin_accounts_lid(world, dana_registered):
    """The Discord bridge's identity is `(None, "admin")`, the local admin's lane. With the
    local admin named "dana" and an ordinary account named "admin" whose own chat is a LID
    bound only in its own map, that lane got the account's LID. MUTATION: consult the name key
    whoever holds it -> the LID is returned -> red."""
    world.state["local_admin_username"] = "dana"
    world.state["whatsapp_config"]["lid_to_e164_by_scope"] = {B_ADMIN: {B_OWN_LID: BOB_PHONE}}
    entries = [_entry(BOB_PHONE, B_ADMIN, "admin")]
    if dana_registered:
        entries.insert(0, _entry(ADMIN_PHONE, ADM, "dana"))
    world.whitelist(*entries)
    world.stored(by_scope={B_ADMIN: B_OWN_LID}, by_name={"admin": B_OWN_LID})
    before = world.store.read_bytes()
    assert mc.get_whatsapp_chat_jid(None, "admin") == (ADMIN_JID if dana_registered else None)
    assert world.store.read_bytes() == before and world.resets() == []
    assert mc.get_whatsapp_chat_jid(B_ADMIN, "admin") == B_OWN_LID, "B keeps its own chat"


def test_a_scopeless_caller_naming_another_account_repairs_nothing(world):
    """Named boundary: the store cannot tell such a lane's scope. MUTATION: read every caller
    without a scope as the local admin's -> Alice's own number is withheld from her name lane."""
    world.stored(by_name={"alice": ALICE_JID})
    before = world.store.read_bytes()
    assert mc.get_whatsapp_chat_jid(None, "alice") == ALICE_JID
    assert world.store.read_bytes() == before and world.resets() == []


# -- AC7 idempotent -------------------------------------------------------------------------

def test_second_read_is_silent(world):
    """MUTATION: delete by number across every key of the block -> the second read differs
    (the fallback refills only the keys it consults) -> red."""
    world.whitelist(ADMIN_ENTRY, ALICE_ENTRY, BOB_ENTRY)
    world.stored(by_scope={ALI: ADMIN_JID, BOB: ALICE_JID}, by_name={"admin": BOB_JID, "alice": BOB_JID})
    assert mc.get_whatsapp_chat_jid(ALI, "alice") == ALICE_JID
    assert mc.get_whatsapp_chat_jid(None, None) == ADMIN_JID
    assert len(world.resets()) == 3
    after = world.store.read_bytes()
    assert mc.get_whatsapp_chat_jid(ALI, "alice") == ALICE_JID
    assert mc.get_whatsapp_chat_jid(None, None) == ADMIN_JID
    assert world.store.read_bytes() == after
    assert len(world.resets()) == 3


# -- AC9 the delivery -----------------------------------------------------------------------

def test_nameless_delivery_after_repair_reaches_her_own_number(world, monkeypatch):
    """The fake-process pattern of tests/test_whatsapp_nameless_delivery.py: only the admin's
    Node process runs. MUTATION: return the stored scope value unchecked, or drop it without
    saving -> her notification is queued to the admin's number -> red."""
    from vaf.api import whatsapp_bridge as wa
    from vaf.core import whatsapp_reply
    world.whitelist(ADMIN_ENTRY)
    world.stored(by_scope={ALI: ADMIN_JID})
    written = io.StringIO()
    monkeypatch.setattr(wa, "_processes", {"admin": SimpleNamespace(stdin=written, poll=lambda: None)})
    monkeypatch.setattr(wa, "_scope_by_user", {"admin": ADM})
    monkeypatch.setattr(wa, "_append_chat_activity", lambda *a, **k: None)
    monkeypatch.setattr(wa, "_outgoing_queue", queue.Queue())
    monkeypatch.setattr(whatsapp_reply, "_send_callback", wa._enqueue_reply)
    monkeypatch.setattr("vaf.core.thinking_mode._resolve_username_for_scope",
                        lambda scope: {ALI: "alice"}.get(scope))
    monkeypatch.setattr(mc, "get_messaging_connections",
                        lambda username=None, user_scope_id=None: {"main_messenger": "whatsapp"})

    assert mc.send_to_main_messenger(ALI, None, "alice's private reminder") == (False, None)
    assert list(wa._outgoing_queue.queue) == []
    world.whitelist(ADMIN_ENTRY, ALICE_ENTRY)
    assert mc.send_to_main_messenger(ALI, None, "alice's private reminder") == (True, "whatsapp")
    assert [(q[0], q[1], q[8]) for q in wa._outgoing_queue.queue] == [("alice", ALICE_JID, ALI)]
    assert [r["path"] for r in world.resets()] == [ADMIN_JID]


# -- Fuzz -----------------------------------------------------------------------------------

def _main_reader(user_scope_id, username):
    """The reader as it was before the repair, frozen as the differential reference."""
    with mc._ENDPOINTS_LOCK:
        data = mc._load_endpoints()
        if user_scope_id is not None:
            jid = data["whatsapp_by_scope"].get(str(user_scope_id))
            if jid:
                return jid
        uname = (username or "").strip() or ("admin" if user_scope_id is None else "")
        jid = data["whatsapp_by_username"].get(uname) if uname else None
        if jid:
            return jid
    jid = mc.get_whatsapp_chat_jid_from_whitelist(user_scope_id, username)
    if jid:
        mc.save_whatsapp_chat_jid(user_scope_id, username, jid)
    return jid


PEOPLE = {"admin": (ADM, "4917000000101"), "alice": (ALI, "4917000000102"), "bob": (BOB, "4917000000103")}
UNREG_DIGITS = "4917000000199"
CAROL_DIGITS = "4917000000104"   # filed under "alice" without a scope: nobody's
LIDS = ["200000000000001@lid", "200000000000002@lid"]
CALLERS = [(ADM, "admin"), (ALI, "alice"), (BOB, "bob"), (None, None), (ALI, None), (ADM, None),
           (ALI, "admin"), (None, "alice")]
# The lane's person, by construction; None for a lane the reader does not repair. A lane
# owns the name key of its person (the fuzz's names are the people's names).
LANE_PERSON = {(ADM, "admin"): "admin", (ALI, "alice"): "alice", (BOB, "bob"): "bob",
               (None, None): "admin", (ALI, None): "alice", (ADM, None): "admin",
               (ALI, "admin"): "alice", (None, "alice"): None}


def _render_phone(rng, digits):
    return rng.choice([f"+{digits}", f"00{digits}", f"+{digits[:2]} {digits[2:5]} {digits[5:]}", digits])


def _render_jid(rng, digits):
    return rng.choice([f"{digits}@s.whatsapp.net", f"{digits}:3@s.whatsapp.net", f"+{digits}", digits])


def _draw(rng):
    """(whitelist, lid map, its labels, store, labels of every stored value, caller, the
    registered people). Every label is the person a value was rendered from."""
    registered = {p for p in PEOPLE if rng.random() < 0.8}
    whitelist = []
    # Declared order, never the set's: string hashes differ between processes, and the draws
    # must replay from the seed.
    for person in (p for p in PEOPLE if p in registered):
        scope, digits = PEOPLE[person]
        if person == "admin" and rng.random() < 0.3:
            whitelist.append(_entry(_render_phone(rng, digits), None, rng.choice(["admin", ""])))
        else:
            whitelist.append(_entry(_render_phone(rng, digits), scope, person))
    if rng.random() < 0.3:
        whitelist.append(_entry(_render_phone(rng, CAROL_DIGITS), None, "alice"))
    rng.shuffle(whitelist)
    lid_map, lid_label = {}, {}
    for lid in LIDS:
        if rng.random() < 0.5:
            target = rng.choice(list(PEOPLE) + ["stranger"])
            lid_map[lid] = "+" + (PEOPLE[target][1] if target in PEOPLE else UNREG_DIGITS)
            lid_label[lid] = target
        else:
            lid_label[lid] = "stranger"

    def value():
        kind = rng.randrange(6)
        if kind == 0:
            return None, None
        if kind <= 2:
            person = rng.choice(list(PEOPLE))
            return _render_jid(rng, PEOPLE[person][1]), person
        if kind == 3:
            return rng.choice([(_render_jid(rng, UNREG_DIGITS), "stranger"),
                               (_render_jid(rng, CAROL_DIGITS), "carol")])
        if kind == 4:
            lid = rng.choice(LIDS)
            return lid, lid_label[lid]
        return GROUP_JID, "stranger"

    by_scope, by_name, labels = {}, {}, {}
    for person, (scope, _) in PEOPLE.items():
        for block, key in (("whatsapp_by_scope", scope), ("whatsapp_by_username", person)):
            jid, label = value()
            if jid is not None:
                (by_scope if block == "whatsapp_by_scope" else by_name)[key] = jid
                labels[(block, key)] = label
    return whitelist, lid_map, lid_label, by_scope, by_name, labels, rng.choice(CALLERS), registered


def _consulted(caller):
    scope, name = caller
    out = [("whatsapp_by_scope", scope)] if scope is not None else []
    uname = (name or "").strip() or ("admin" if scope is None else "")
    return out + ([("whatsapp_by_username", uname)] if uname else [])


def _run_fuzz(world, seed, draws):
    rng = random.Random(seed)
    violations = []
    for i in range(draws):
        whitelist, lid_map, lid_label, by_scope, by_name, labels, caller, registered = _draw(rng)
        world.whitelist(*whitelist)
        world.state["whatsapp_config"]["lid_to_e164"] = lid_map
        lane_person = LANE_PERSON[caller]

        def foreign(label):
            return lane_person is not None and label in registered and label != lane_person

        def label_of(jid):
            """A stored value's label, else the label of a JID the whitelist fallback built."""
            for (block, key), label in labels.items():
                if (by_scope if block == "whatsapp_by_scope" else by_name).get(key) == jid:
                    return label
            if jid in lid_label:
                return lid_label[jid]
            return next((person for person, (_, digits) in PEOPLE.items()
                         if jid.split("@")[0].split(":")[0].lstrip("+") == digits), "stranger")

        world.stored(by_scope, by_name)
        reference = _main_reader(*caller)
        world.stored(by_scope, by_name)
        before = world.on_disk()
        del_events = len(world.resets())
        got = mc.get_whatsapp_chat_jid(*caller)
        after = world.on_disk()
        events = world.resets()[del_events:]
        problems = []
        # 1. Never another registered person's number.
        got_label = label_of(got) if got else None
        if got and foreign(got_label):
            problems.append(f"returned {got_label}'s {got}")
        consulted = _consulted(caller)

        def owns(block, key):
            return lane_person is None or block == "whatsapp_by_scope" or key == lane_person

        # 5. A name key the lane does not own is not read: its value never comes back unless
        #    it is the lane's own person's anyway, or a key the lane owns holds it too.
        unowned = [(b, k) for b, k in consulted if not owns(b, k) and before[b].get(k)]
        owned_values = {before[b].get(k) for b, k in consulted if owns(b, k)}
        for b, k in unowned:
            if got == before[b][k] and labels.get((b, k)) != lane_person and got not in owned_values:
                problems.append(f"returned {b}[{k}] ({labels.get((b, k))}), a key the lane does not own")
        for block, key in consulted:
            label = labels.get((block, key))
            own_key = owns(block, key)
            changed = after[block].get(key) != before[block].get(key)
            # 2. A name key the lane does not own is never changed; a value of the lane's own
            #    person, or of nobody registered, is never dropped; a foreign one in a key the
            #    lane owns always is.
            if changed and (not own_key or (label is not None and not foreign(label))):
                problems.append(f"changed {block}[{key}] holding {label}")
            if label is not None and foreign(label) and own_key and not changed:
                problems.append(f"kept foreign {block}[{key}] ({label})")
        for event in events:
            if not any(event["path"] == before[b].get(k) and foreign(labels.get((b, k)))
                       for b, k in consulted if owns(b, k)):
                problems.append(f"reported {event['path']}")
        # 4. Keys the read does not consult are byte-identical.
        for block in ("whatsapp_by_scope", "whatsapp_by_username"):
            for key in set(before[block]) | set(after[block]):
                if (block, key) not in consulted and before[block].get(key) != after[block].get(key):
                    problems.append(f"touched {block}[{key}]")
        # 3. A second read is silent.
        settled = world.store.read_bytes()
        n = len(world.resets())
        mc.get_whatsapp_chat_jid(*caller)
        if len(world.resets()) != n or world.store.read_bytes() != settled:
            problems.append("second read not silent")
        # Differential: with no foreign value consulted and no key the lane does not own read
        # before, exactly what the reader returned before.
        if not unowned and not any(foreign(labels.get(ck)) for ck in consulted) and got != reference:
            problems.append(f"differs from before: {got!r} != {reference!r}")
        if problems:
            violations.append((i, caller, problems))
    return violations


def test_fuzz_the_reader_never_returns_another_accounts_number(world):
    """Seeded fuzz with an oracle from the generator's labels, not from the canonicaliser.
    `VAF_FUZZ_DRAWS` raises the draw count outside the suite."""
    draws = int(os.environ.get("VAF_FUZZ_DRAWS") or 2000)
    violations = _run_fuzz(world, 4311, draws)
    assert violations == [], violations[:5]
