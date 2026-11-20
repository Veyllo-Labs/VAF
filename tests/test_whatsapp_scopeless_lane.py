# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A WhatsApp send without a scope is the local admin's, through the admin's own process.

THE DEFECT, measured with synthetic scopes and numbers on the code before this change (the
local admin is "dana", an ordinary account B registered as "admin"): the Discord bridge and
reminders saved by older versions send as `(None, "admin")`. The bridge settled such a request
by its name (`_process_for("admin", None)` is B's process, `_is_reply_allowed` resolved the
scope from the name), so the admin's note left through B's WhatsApp: in both Inbound states
when the admin's entry was filed under "admin", and in 8 of 8 shapes where B had no whitelist
entry at all. The endpoint reader read the literal "admin" key for that lane, which B's lane
owns too, so the two lanes reset each other's saved chat on every read (12 security events in
10 alternations).

THE RULE: `config.messenger_lane_identity` decides once whose lane a request is. No scope (or
the admin's) with an admin name is the local admin's own scope and name; no scope with any
other name is refused; every other scoped request stands as given. `send_to_main_messenger`,
`get_whatsapp_chat_jid` and both bridge send entries (and the sender loop, for a request file
of older code) apply it. Fake Node processes only; no Node, no network.
"""
import io
import json
import queue
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.api import whatsapp_bridge as wa
from vaf.core import config as config_mod
from vaf.core import messaging_connections as mc
from vaf.core import whatsapp_reply
from vaf.core.channel_ingress_policy import set_front_office
from vaf.core.config import Config, is_local_admin_name, messenger_lane_identity
from vaf.core.platform import Platform

ADM = "ab12cd34-0000-4000-8000-0000000000aa"   # the local admin, named "dana"
ALI = "ab12cd34-0000-4000-8000-000000000001"   # Alice, an ordinary account
B = "ab12cd34-0000-4000-8000-000000000003"     # an ordinary account that registered as "admin"
DANA_PHONE, BOB_PHONE, ALICE_PHONE = "+4917000000010", "+4917000000012", "+4917000000011"
DANA_JID, BOB_JID, ALICE_JID = (p[1:] + "@s.whatsapp.net" for p in (DANA_PHONE, BOB_PHONE, ALICE_PHONE))
B_OWN_LID = "100000000000014@lid"
SELF_CHAT = "4917000000555@s.whatsapp.net"     # registered by nobody


def _entry(phone, scope, name):
    out = {"phone_number": phone, "vaf_username": name}
    if scope is not None:
        out["user_scope_id"] = scope
    return out


B_ENTRY = _entry(BOB_PHONE, B, "admin")


@pytest.fixture
def world(monkeypatch, tmp_path):
    state = {"local_admin_scope_id": ADM, "local_admin_username": "dana",
             "whatsapp_config": {"enabled": True, "whitelist": [_entry(DANA_PHONE, ADM, "dana"), dict(B_ENTRY)]},
             "channel_ingress_policy": set_front_office(None, True, "whatsapp")}
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    store = tmp_path / "data" / "messaging_endpoints.json"
    monkeypatch.setattr(mc, "_ENDPOINTS_FILE", store)
    events = []
    import vaf.core.security_events as sec
    monkeypatch.setattr(sec, "log_security_event", lambda kind, **f: events.append((kind, f)))
    by = {"dana": io.StringIO(), "admin": io.StringIO()}
    monkeypatch.setattr(wa, "_processes", {name: SimpleNamespace(stdin=out, poll=lambda: None)
                                           for name, out in by.items()})
    monkeypatch.setattr(wa, "_scope_by_user", {"dana": ADM, "admin": B})
    monkeypatch.setattr(wa, "_append_chat_activity", lambda *a, **k: None)
    monkeypatch.setattr(wa, "_outgoing_queue", queue.Queue())
    monkeypatch.setattr(wa, "_pending_sends", {})
    monkeypatch.setattr(whatsapp_reply, "_send_callback", wa._enqueue_reply)
    monkeypatch.setattr("vaf.core.config.scope_id_for_username",
                        lambda name: {"dana": ADM, "admin": B, "alice": ALI}.get(str(name or "").lower()))
    ipc = []
    monkeypatch.setattr(wa, "_read_bridge_state", lambda: {"running": True, "usernames": ["dana", "admin"]})
    monkeypatch.setattr(wa, "_write_json_atomic", lambda path, data: ipc.append(data))
    completed = {}
    monkeypatch.setattr(wa, "_complete_send_request",
                        lambda req_id, ok, err=None, **k: completed.__setitem__(req_id, (ok, err)))
    connections = []

    def fake_connections(username=None, user_scope_id=None):
        connections.append((username, user_scope_id))
        return {"main_messenger": "whatsapp"}
    monkeypatch.setattr(mc, "get_messaging_connections", fake_connections)
    mirrored = []
    monkeypatch.setattr(mc, "_record_outbound",
                        lambda channel, endpoint, text, uname, scope, *a, **k: mirrored.append((endpoint, uname, scope)))
    # The confirmation path waits for Node's answer, which a fake process never gives.
    real_confirm = wa.send_whatsapp_with_confirmation
    monkeypatch.setattr(wa, "send_whatsapp_with_confirmation",
                        lambda *a, **k: real_confirm(*a, **{**k, "timeout": 0.05}))

    def whitelist(*entries):
        state["whatsapp_config"]["whitelist"] = [dict(e) for e in entries]

    def stored(by_scope=None, by_name=None):
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"by_scope": {}, "by_username": {},
                                     "whatsapp_by_scope": dict(by_scope or {}),
                                     "whatsapp_by_username": dict(by_name or {})}, indent=2), encoding="utf-8")

    def drain():
        wa._outgoing_queue.put(None)
        wa._sender_loop()

    def written(name):
        return [(c["to"], c.get("text")) for c in map(json.loads, by[name].getvalue().splitlines())]

    def resets():
        return [f for kind, f in events if kind == "owner_endpoint_reset"]

    return SimpleNamespace(state=state, whitelist=whitelist, stored=stored, store=store, drain=drain,
                           written=written, resets=resets, ipc=ipc, completed=completed,
                           connections=connections, mirrored=mirrored, real_confirm=real_confirm)


def _inbound(world, is_open):
    world.state["channel_ingress_policy"] = set_front_office(None, is_open, "whatsapp")


# -- AC1 the helper -------------------------------------------------------------------------

DANA = (ADM, "dana")


@pytest.mark.parametrize("scope, name, expected", [
    (None, None, DANA), (None, "", DANA), (None, "admin", DANA), (None, "Admin", DANA),
    (None, "dana", DANA), (None, " DANA ", DANA), ("", "admin", DANA), ("  ", None, DANA),
    (ADM, "admin", DANA), (ADM, None, DANA), (ADM, "", DANA), (ADM, "Dana", DANA),
    (ALI, "admin", (ALI, "admin")), (ALI, None, (ALI, None)), (ALI, "alice", (ALI, "alice")),
    (B, "admin", (B, "admin")), (ADM, "alice", (ADM, "alice")),
    (None, "alice", None), (None, "carl", None), ("", "alice", None), ("  ", "carl", None),
])
def test_the_lane_identity_table(world, scope, name, expected):
    """MUTATION: drop "admin" from the admin names -> the "admin" rows go red. MUTATION: give
    a scopeless request any name the admin's -> the refused rows go red. MUTATION: return a
    scoped admin request unchanged -> the admin-scope rows go red."""
    got = messenger_lane_identity(scope, name)
    assert got == expected
    if expected is not None and expected != DANA:
        assert got[0] is scope and got[1] is name, "a scoped request comes back byte-identical"


def test_one_admin_name_rule():
    """The admin-name set has one copy, `config.is_local_admin_name`; Telegram's "taken names"
    union in `_telegram_account_names` is the one other set and is named in the spec.
    MUTATION: leave a hand copy in the WhatsApp reader -> red."""
    src = Path(mc.__file__).read_text(encoding="utf-8")
    sets = re.findall(r"\{[^{}\n]*(?:\"admin\"[^{}\n]*get_local_admin_username|get_local_admin_username"
                      r"[^{}\n]*\"admin\")[^{}\n]*\}", src)
    assert len(sets) == 1, sets
    telegram = src[src.index("def _telegram_account_names("):src.index("\ndef ", src.index("def _telegram_account_names(") + 1)]
    assert sets[0] in telegram
    assert "def _whatsapp_lane(" not in src


# -- Fuzz 1: the classifier, exhaustive -----------------------------------------------------

SCOPES = [None, "", "  ", ADM, " " + ADM + " ", ALI, B]
ADMIN_SCOPES = {None, "", "  ", ADM, " " + ADM + " "}   # read as the local admin's lane
ADMIN_NAMES = ["admin", "dana", "Dana", "Straße"]


def _names(admin_name):
    """(name, is it an admin name), spelled out: the first eight always are, the last five never
    (dotless i and dotted capital I are other letters)."""
    aliases = [None, "", "admin", "ADMIN", " Admin ", admin_name, admin_name.upper(), admin_name.lower()]
    others = ["alice", "carl", "scope_ab12cd34", "admın", "ADMİN"]
    return [(n, True) for n in aliases] + [(n, False) for n in others]


def test_the_lane_identity_exhaustive(world):
    """7 scopes x 13 names x 4 admin names (364 cases), against the table above. MUTATION: compare with
    `.lower()` instead of casefold -> the "Straße" rows go red. MUTATION: no strip of the scope
    -> the padded admin scope rows go red."""
    cases = mismatches = 0
    for admin_name in ADMIN_NAMES:
        world.state["local_admin_username"] = admin_name
        for scope in SCOPES:
            for name, alias in _names(admin_name):
                cases += 1
                if scope in ADMIN_SCOPES and alias:
                    expected = (ADM, admin_name)
                elif scope is not None and scope.strip():
                    expected = (scope, name)
                else:
                    expected = None
                got = messenger_lane_identity(scope, name)
                ok = got == expected
                if expected is not None and not (scope in ADMIN_SCOPES and alias):
                    ok = ok and got[0] is scope and got[1] is name
                # Invariants: a scopeless request never gets a non-admin scope; the admin's
                # identity only for an admin name; a scoped non-admin request is unchanged.
                if got is not None and scope in ADMIN_SCOPES and got[0] != ADM and got != (scope, name):
                    ok = False
                if got == (ADM, admin_name) and not alias and (scope, name) != (ADM, admin_name):
                    ok = False
                mismatches += not ok
    assert cases == 4 * 7 * 13 and mismatches == 0


def test_the_admin_name_rule_follows_the_configured_name(world):
    world.state["local_admin_username"] = "Straße"
    assert is_local_admin_name("STRASSE") and is_local_admin_name(" admin ") and is_local_admin_name(None)
    assert not is_local_admin_name("dana") and not is_local_admin_name("admın")


# -- AC2 the Discord lane -------------------------------------------------------------------

@pytest.mark.parametrize("inbound_open", [True, False], ids=["inbound-open", "inbound-closed"])
@pytest.mark.parametrize("dana_entry", [
    _entry(DANA_PHONE, ADM, "dana"), _entry(DANA_PHONE, ADM, "admin"), _entry(DANA_PHONE, None, "admin"),
], ids=["scoped-dana", "scoped-admin", "scopeless-admin"])
def test_the_discord_lane(world, dana_entry, inbound_open):
    """`send_to_main_messenger(None, "admin", ...)`, the Discord bridge's `send_to_user`.
    Named boundary: an entry without a scope filed under "admin" is not the admin's registered
    number to the bridge's reply check (`_get_allowed_phones_for_user` matches name or scope),
    so with Inbound closed that one row goes to the web UI. MUTATION: remove the conversion
    from `send_to_main_messenger` -> the connections are looked up as "admin" -> red; with the
    reader and bridge conversions also gone, B's process writes."""
    _inbound(world, inbound_open)
    world.whitelist(dana_entry, B_ENTRY)
    world.stored(by_scope={B: BOB_JID}, by_name={"admin": BOB_JID})

    result = mc.send_to_main_messenger(None, "admin", "dana's private note")
    world.drain()
    assert world.written("admin") == [], "sent through the ordinary admin account's process"
    assert world.connections == [("dana", ADM)]
    if dana_entry.get("user_scope_id") is None and not inbound_open:
        assert result == (False, None) and world.written("dana") == []
        return
    assert world.written("dana") == [(DANA_JID, "dana's private note")]
    assert result == (True, "whatsapp")
    assert world.mirrored == [(DANA_JID, "dana", ADM)], "mirrored into the admin's own session"


# -- AC3 old reminders ----------------------------------------------------------------------

@pytest.mark.parametrize("name", [None, "admin", "carl"])
def test_old_reminders(world, monkeypatch, name):
    """A reminder an older version saved without a scope. MUTATION: read a scopeless "carl" as
    the admin's -> Dana's process sends carl's text -> red."""
    from vaf.core import reminders
    notes = []
    monkeypatch.setattr("vaf.core.user_notifications.append_notification",
                        lambda scope, **k: notes.append((scope, k.get("summary"))))
    reminders._deliver({"user_scope_id": None, "username": name, "message": "take the pills"})
    world.drain()
    assert world.written("admin") == []
    if name == "carl":
        assert world.written("dana") == [] and notes == [(None, "take the pills")]
    else:
        assert world.written("dana") == [(DANA_JID, "take the pills")] and notes == []


# -- AC4 the scopeless bridge sends ---------------------------------------------------------

def _reply(world, name):
    ok = whatsapp_reply.send_whatsapp_reply(name, DANA_JID, "note")
    return ok, [(q[0], q[8]) for q in list(wa._outgoing_queue.queue)]


def _confirm(world, name):
    out = wa.send_whatsapp_with_confirmation(name, DANA_JID, "note")
    return out, [(q[0], q[8]) for q in list(wa._outgoing_queue.queue)]


def _tool(to_phone):
    def call(world, name):
        from vaf.tools.send_whatsapp import SendWhatsAppTool
        kwargs = {"message": "note", "user_scope_id": None, "username": name}
        if to_phone:
            kwargs["to_phone"] = DANA_PHONE
        out = SendWhatsAppTool().run(**kwargs)
        return out, [(q[0], q[8]) for q in list(wa._outgoing_queue.queue)]
    return call


def _loop_item(world, name):
    wa._outgoing_queue.put((name, DANA_JID, "note", None, "rq"))
    return None, [(name, None)]


ENTRIES = {"reply": _reply, "confirmation": _confirm, "tool-default": _tool(False),
           "tool-to-phone": _tool(True), "sender-loop": _loop_item}


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_scopeless_bridge_sends_go_through_the_admins_process(world, entry):
    """Each entry converts on its own: the queued item already carries the admin's scope and
    name. MUTATION: remove the helper from `_enqueue_reply`, from
    `send_whatsapp_with_confirmation` or from the sender loop -> that entry's row goes red (a
    queued item without the scope, or B's process writing)."""
    _, queued = ENTRIES[entry](world, "admin")
    if entry != "sender-loop":
        assert queued == [("dana", ADM)], "queued without the admin's scope and name"
    world.drain()
    assert world.written("admin") == [], "sent through the ordinary admin account's process"
    assert world.written("dana") == [(DANA_JID, "note")]
    assert world.ipc == []


@pytest.mark.parametrize("name", ["alice", "carl"])
@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_scopeless_bridge_sends_naming_anybody_else_are_refused(world, entry, name):
    """MUTATION: remove the helper from the entry -> the row queues, writes a request file, or
    reaches a process -> red."""
    out, queued = ENTRIES[entry](world, name)
    if entry == "sender-loop":
        world.drain()
        assert world.completed == {"rq": (False, wa.NO_ACCOUNT_FOR_REQUEST)}
    else:
        assert queued == [], "queued"
        world.drain()
    if entry == "reply":
        assert out is False
    elif entry in ("confirmation", "tool-to-phone"):
        assert out == wa.NO_ACCOUNT_FOR_REQUEST
    elif entry == "tool-default":
        assert "No main-user WhatsApp number" in out
    assert world.ipc == [], "wrote a request file"
    assert world.written("admin") == [] and world.written("dana") == []


# -- AC5 no mutual reset --------------------------------------------------------------------

@pytest.mark.parametrize("caller", [(None, "admin"), (ADM, "admin")], ids=["scopeless", "admin-scope"])
@pytest.mark.parametrize("dana_entry", [_entry(DANA_PHONE, None, "admin"), _entry(DANA_PHONE, ADM, "admin")],
                         ids=["scopeless-entry", "scoped-entry"])
def test_no_mutual_reset(world, dana_entry, caller):
    """The local admin's entry filed under "admin", next to B's own "admin" entry and B's number
    under the "admin" key. MUTATION: remove the conversion from `get_whatsapp_chat_jid` -> the
    admin's lane reads B's key as its own and B's lane resets it back -> 12 events -> red."""
    world.whitelist(dana_entry, B_ENTRY)
    world.stored(by_name={"admin": BOB_JID})
    for _ in range(10):
        assert mc.get_whatsapp_chat_jid(*caller) == DANA_JID
        assert mc.get_whatsapp_chat_jid(B, "admin") == BOB_JID
    assert world.resets() == []
    disk = json.loads(world.store.read_text(encoding="utf-8"))
    assert disk["whatsapp_by_username"]["admin"] == BOB_JID


# -- AC6 an ordinary "admin" account with no whitelist entry --------------------------------

def _scopeless_send(world):
    return mc.send_to_main_messenger(None, "admin", "dana's note")


def _scopeless_tool(world):
    from vaf.tools.send_whatsapp import SendWhatsAppTool
    return SendWhatsAppTool().run(message="dana's note", user_scope_id=None, username="admin")


@pytest.mark.parametrize("lane", [_scopeless_send, _scopeless_tool], ids=["send-to-user", "send-whatsapp"])
@pytest.mark.parametrize("dana_registered", [True, False], ids=["dana-registered", "dana-unregistered"])
@pytest.mark.parametrize("admin_key", [BOB_JID, B_OWN_LID, DANA_JID, SELF_CHAT],
                         ids=["b-number", "b-own-lid", "dana-number", "self-chat"])
def test_an_admin_named_account_without_an_entry(world, admin_key, dana_registered, lane):
    """B is named "admin", runs a process and has no whitelist entry; the "admin" key holds one
    of four values; Inbound is open. MUTATION: remove the reader conversion (keep the bridge
    one) -> Dana's process sends to B's value -> red. MUTATION: remove the bridge conversion ->
    the send-whatsapp rows go through B's process -> red."""
    world.state["whatsapp_config"]["lid_to_e164_by_scope"] = {B: {B_OWN_LID: BOB_PHONE}}
    world.whitelist(*([_entry(DANA_PHONE, ADM, "dana")] if dana_registered else []))
    world.stored(by_name={"admin": admin_key})
    lane(world)
    world.drain()
    assert world.written("admin") == [], "sent through the ordinary admin account's process"
    assert world.written("dana") == ([(DANA_JID, "dana's note")] if dana_registered else [])


# -- AC7 the reader on converted lanes ------------------------------------------------------

def test_a_scopeless_lane_naming_another_account_reads_nothing(world):
    """MUTATION: keep the old read by name -> Alice's stored chat comes back -> red."""
    world.stored(by_scope={ALI: ALICE_JID}, by_name={"alice": ALICE_JID})
    before = world.store.read_bytes()
    assert mc.get_whatsapp_chat_jid(None, "alice") is None
    assert world.store.read_bytes() == before and world.resets() == []


def test_the_scopeless_lane_reads_the_admins_scope_key(world):
    world.stored(by_scope={ADM: SELF_CHAT}, by_name={"admin": BOB_JID})
    assert mc.get_whatsapp_chat_jid(None, None) == SELF_CHAT


@pytest.mark.parametrize("admin_name, filed", [("admin", "admin"), ("admin", ""), ("dana", "admin"),
                                               ("dana", ""), ("dana", "Dana")])
def test_a_legacy_scopeless_entry_is_still_the_admins_number(world, admin_name, filed):
    """An older install's whitelist entry without a scope, filed under an admin name, with an
    empty store. MUTATION: drop the admin-scope match of a scopeless entry in
    `get_whatsapp_chat_jid_from_whitelist` -> None -> red."""
    world.state["local_admin_username"] = admin_name
    world.whitelist(_entry(DANA_PHONE, None, filed))
    assert mc.get_whatsapp_chat_jid(None, None) == DANA_JID
    assert mc.get_whatsapp_chat_jid_from_whitelist(ALI, "alice") is None, "only the admin's scope"


def test_the_helpers_live_in_the_framework():
    """The rule is decided once, in `vaf.core`: the bridge imports it rather than a private
    helper of the messenger module."""
    assert wa.messenger_lane_identity is config_mod.messenger_lane_identity
    assert mc.messenger_lane_identity is config_mod.messenger_lane_identity
