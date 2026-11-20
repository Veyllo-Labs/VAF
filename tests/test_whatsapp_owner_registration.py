# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A WhatsApp number an admin registers for another account belongs to that account.

THE DEFECT, measured with synthetic scopes on the code before this change (the local admin is
"dana", Alice an ordinary account, B an ordinary account registered as "admin"): an admin who
registered a number for Alice on the WhatsApp window sent only the name, and
`POST /api/whatsapp/whitelist/add` filed it under the ADMIN's scope with Alice's name. Every
reader goes by scope, so the admin's lanes, the ones without a scope included, delivered the
admin's notes to Alice's number and Alice's own lane got nothing (row A). The route's update
loop matched an existing entry by scope OR name, first in whitelist order, so the same call
overwrote the admin's own entry (row B) or moved Alice's (row C), and B, by registering its own
number, overwrote the admin's entry filed under "admin" (row H). An unknown name was accepted
and the number became the admin's (row D).

THE RULE: the route resolves the named account (`config.account_for_username`, off the event
loop), refuses an unknown name (404), a scope that is not the named account's (400) and a
non-admin naming anybody else (403); the writer `channel_pairing.pair_whatsapp_number` changes
only the target scope's entry. Entries older versions filed under an admin's scope are moved at
the bridge start (`channel_pairing.refile_whatsapp_owner_numbers`), each move logged. A seeded
fuzz holds the writer and the refile to the generator's own labels.
"""
import asyncio
import json
import os
import random
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from vaf.api import whatsapp_bridge as wa
from vaf.api import whatsapp_routes as routes
from vaf.core import channel_pairing
from vaf.core import messaging_connections as mc
from vaf.core.channel_ingress_policy import set_front_office
from vaf.core.config import Config
from vaf.core.contacts_store import phone_digits_canonical
from vaf.core.platform import Platform
from vaf.core.tool_dispatch import get_account_directory_resolver, set_account_directory_resolver

ADM = "ab12cd34-0000-4000-8000-0000000000aa"   # the local admin, named "dana"
ALI = "ab12cd34-0000-4000-8000-000000000001"   # Alice, an ordinary account
B = "ab12cd34-0000-4000-8000-000000000003"     # an ordinary account that registered as "admin"
EVE = "ab12cd34-0000-4000-8000-000000000004"   # a second admin
CAR = "ab12cd34-0000-4000-8000-000000000005"   # Carl, an ordinary account
INA = "ab12cd34-0000-4000-8000-000000000006"   # an inactive account
DANA_P, ALICE_P, B_P, N = "+4917000000010", "+4917000000011", "+4917000000012", "+4917000000020"
ALICE_JID, DANA_JID = ALICE_P[1:] + "@s.whatsapp.net", DANA_P[1:] + "@s.whatsapp.net"

DIRECTORY = [
    {"username": "alice", "user_scope_id": ALI, "active": True, "role": "user"},
    {"username": "admin", "user_scope_id": B, "active": True, "role": "user"},
    {"username": "eve", "user_scope_id": EVE, "active": True, "role": "admin"},
    {"username": "carl", "user_scope_id": CAR, "active": True, "role": "user"},
    {"username": "ina", "user_scope_id": INA, "active": False, "role": "user"},
]


def _e(phone, scope, name):
    out = {"phone_number": phone, "vaf_username": name}
    if scope is not None:
        out["user_scope_id"] = scope
    return out


DANA_E = _e(DANA_P, ADM, "dana")
ALICE_E = _e(ALICE_P, ALI, "alice")


@pytest.fixture
def world(monkeypatch, tmp_path):
    state = {"local_admin_scope_id": ADM, "local_admin_username": "dana",
             "whatsapp_config": {"enabled": True, "whitelist": []},
             "channel_ingress_policy": set_front_office(None, False, "whatsapp")}
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "app" / "config.json")
    monkeypatch.setattr(Config, "_filelock", None)
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: json.loads(json.dumps(state))))

    def save(cls, cfg):
        state.clear()
        state.update(json.loads(json.dumps(cfg)))
    monkeypatch.setattr(Config, "save", classmethod(save))
    events = []
    import vaf.core.security_events as sec
    monkeypatch.setattr(sec, "log_security_event", lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(routes, "log_security_event", lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr("vaf.core.whatsapp_auth.get_linked_phone", lambda username: None)
    rows = [dict(r) for r in DIRECTORY]
    lookups = []

    def directory():
        lookups.append(1)
        return [dict(r) for r in rows]
    previous = get_account_directory_resolver()
    set_account_directory_resolver(directory)
    # The scope -> name lookup of the security events reads the directory here too, never an
    # auth database.
    monkeypatch.setattr("vaf.core.thinking_mode._resolve_username_for_scope",
                        lambda scope: next((r["username"] for r in rows if r["user_scope_id"] == scope), None))
    store = tmp_path / "data" / "messaging_endpoints.json"
    monkeypatch.setattr(mc, "_ENDPOINTS_FILE", store)

    def whitelist(*entries):
        state["whatsapp_config"]["whitelist"] = [dict(e) for e in entries]

    def stored(by_scope=None, by_name=None):
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"by_scope": {}, "by_username": {},
                                     "whatsapp_by_scope": dict(by_scope or {}),
                                     "whatsapp_by_username": dict(by_name or {})}), encoding="utf-8")

    yield SimpleNamespace(state=state, events=events, rows=rows, lookups=lookups, whitelist=whitelist,
                          stored=stored, wl=lambda: state["whatsapp_config"]["whitelist"])
    set_account_directory_resolver(previous)


def _req(scope, name, role="user"):
    return SimpleNamespace(state=SimpleNamespace(user={"user_scope_id": scope, "username": name, "role": role}))


DANA = (ADM, "dana", "admin")
ALICE = (ALI, "alice", "user")
B_ACCOUNT = (B, "admin", "user")


def _add(caller, phone, name=None, scope=None):
    """(status, response) of the real route; an HTTPException becomes its status code."""
    body = routes.WhitelistAddRequest(phone_number=phone, vaf_username=name, user_scope_id=scope)
    try:
        return 200, asyncio.run(routes.add_whitelist_entry(_req(*caller), body))
    except HTTPException as exc:
        return exc.status_code, exc.detail


# -- AC1 the named account's scope ---------------------------------------------------------------

@pytest.mark.parametrize("typed", ["alice", " Alice ", "ALICE"])
def test_an_admin_files_a_number_under_the_named_accounts_scope(world, typed):
    """Row A, any letter case: the entry is Alice's, the admin's own entry is untouched.
    MUTATION: file a named target under the caller's own scope again (the old
    `body.user_scope_id or user_info["user_scope_id"]`) and the admin's entry is replaced."""
    world.whitelist(DANA_E)
    status, out = _add(DANA, N, name=typed)
    assert status == 200 and out["status"] == "added" and out["vaf_username"] == "alice"
    assert world.wl() == [DANA_E, _e(N, ALI, "alice")]


# -- AC2 an unknown name ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["unknown", "inactive", "raising"])
def test_an_unknown_name_is_refused_and_nothing_is_written(world, case):
    """MUTATION: drop the `account is None` refusal (fall back to the caller's own scope)."""
    world.whitelist(DANA_E)
    before = json.dumps(world.state, sort_keys=True)
    name = {"unknown": "nobody", "inactive": "ina", "raising": "alice"}[case]
    if case == "raising":
        def broken():
            raise RuntimeError("the account store is down")
        set_account_directory_resolver(broken)
    status, detail = _add(DANA, N, name=name)
    assert status == 404 and name in detail
    assert json.dumps(world.state, sort_keys=True) == before
    assert world.events == []


# -- AC3 the writer goes by scope only -------------------------------------------------------------

@pytest.mark.parametrize("row, caller, name, seed, expected", [
    ("B", DANA, "alice", [DANA_E, ALICE_E], [DANA_E, _e(N, ALI, "alice")]),
    ("C", DANA, "alice", [ALICE_E, DANA_E], [_e(N, ALI, "alice"), DANA_E]),
    ("H", B_ACCOUNT, None, [_e(DANA_P, ADM, "admin")], [_e(DANA_P, ADM, "admin"), _e(N, B, "admin")]),
    ("I", ALICE, None, [_e(ALICE_P, None, "alice")], [_e(ALICE_P, None, "alice"), _e(N, ALI, "alice")]),
])
def test_the_writer_changes_only_the_target_scopes_entry(world, row, caller, name, seed, expected):
    """Rows B, C, H, I. MUTATION: let the writer match an entry by name as well
    (`or e.get("vaf_username") == username`): H overwrites the admin's entry, I adopts the
    scopeless one."""
    world.whitelist(*seed)
    status, _out = _add(caller, N, name=name)
    assert status == 200
    assert world.wl() == expected, row


def test_the_writer_reports_added_updated_and_unchanged(world):
    world.whitelist(DANA_E)
    write = channel_pairing.pair_whatsapp_number
    assert write(DANA_P, user_scope_id=ADM, username="dana", changed_by="dana") == "unchanged"
    assert world.events == []
    assert write(N, user_scope_id=ADM, username="dana", changed_by="dana") == "updated"
    assert write(ALICE_P, user_scope_id=ALI, username="alice", changed_by="dana") == "added"
    assert world.wl() == [_e(N, ADM, "dana"), ALICE_E]
    with pytest.raises(ValueError):
        write("", user_scope_id=ADM, username="dana", changed_by="dana")
    with pytest.raises(ValueError):
        write(N, user_scope_id=" ", username="dana", changed_by="dana")


# -- AC4 a non-admin names nobody else -------------------------------------------------------------

@pytest.mark.parametrize("name, scope, expected", [
    ("dana", None, 403), (None, ADM, 403), ("ALICE", None, 200), (None, None, 200), (" alice ", ALI, 200),
])
def test_a_non_admin_registers_only_their_own_number(world, name, scope, expected):
    """MUTATION: drop the non-admin 403 branch (rows 1, 2 go 200); separately, compare the
    own name case-sensitively (row 3 goes 403)."""
    world.whitelist(DANA_E)
    status, _out = _add(ALICE, N, name=name, scope=scope)
    assert status == expected
    if expected == 403:
        assert world.wl() == [DANA_E] and world.events == []
    else:
        assert world.wl() == [DANA_E, _e(N, ALI, "alice")]


# -- AC5 a scope that is not the named account's ---------------------------------------------------

@pytest.mark.parametrize("name, scope", [("dana", ALI), ("alice", ADM), (None, ALI)])
def test_an_admin_whose_scope_disagrees_with_the_target_is_refused(world, name, scope):
    """Row G. MUTATION: ignore `user_scope_id` for an admin (the number lands under ALI as
    "dana", or under ADM)."""
    world.whitelist(DANA_E)
    status, _detail = _add(DANA, N, name=name, scope=scope)
    assert status == 400
    assert world.wl() == [DANA_E] and world.events == []


# -- AC6 the lookup runs off the event loop --------------------------------------------------------

def test_the_directory_lookup_runs_off_the_event_loop(world):
    """MUTATION: call `account_for_username` directly in the handler (it runs on the loop)."""
    on_loop = []

    def directory():
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return [dict(r) for r in world.rows]
    set_account_directory_resolver(directory)
    assert _add(DANA, N, name="alice")[0] == 200
    assert on_loop and not any(on_loop)


# -- AC8 the refile at the bridge start ------------------------------------------------------------

P = ["+49170000001%02d" % i for i in range(10)]


def test_the_refile_moves_exactly_the_misfiled_entries(world):
    """MUTATION: drop the `is_admin_account` condition (P[4] moves); drop the "admin"
    ambiguity condition (P[5] moves to B); drop the dedupe (P[3] is moved, not removed); drop
    either event; drop the `target == scope` check (correct entries are touched)."""
    seed = [
        _e(P[1], ADM, "alice"),            # misfiled by the local admin
        _e(P[2], EVE, "Carl"),             # misfiled by a second admin, another letter case
        _e("017000000103", ADM, "carl"),    # misfiled, and Carl's own entry has that number
        _e(P[3], CAR, "carl"),
        _e(P[4], ALI, "carl"),             # a non-admin scope with another name: a hand edit
        _e(P[5], ADM, "admin"),            # the ambiguous name
        _e(P[6], ADM, "nobody"),           # an unknown name
        _e(P[7], ALI, "alice"),            # already right
        _e(P[8], None, "carl"),            # no scope
        _e(P[9], ADM, "ina"),              # an inactive account
        DANA_E,
    ]
    world.whitelist(*seed)
    changes = channel_pairing.refile_whatsapp_owner_numbers()
    assert [(c["action"], c["phone_number"], c["to_scope"]) for c in changes] == [
        ("moved", P[1], ALI), ("moved", P[2], CAR), ("removed", "017000000103", CAR)]
    assert world.wl() == [_e(P[1], ALI, "alice"), _e(P[2], CAR, "Carl")] + seed[3:]
    assert [(k, f["username"], f["path"]) for k, f in world.events] == [
        ("channel_unpaired", "dana", P[1]), ("channel_paired", "alice", P[1]),
        ("channel_unpaired", "eve", P[2]), ("channel_paired", "Carl", P[2]),
        ("channel_unpaired", "dana", "017000000103"),
    ]
    assert all(f["channel"] == "whatsapp" for _k, f in world.events)
    assert world.events[0][1]["detail"] == (f"owner {P[1]}, registered for alice under this account; "
                                            "corrected at WhatsApp start")
    assert world.events[1][1]["detail"] == f"owner {P[1]} for alice, moved from the account it was filed under"
    # A second pass finds nothing to do and says nothing.
    after = json.dumps(world.state, sort_keys=True)
    assert channel_pairing.refile_whatsapp_owner_numbers() == []
    assert json.dumps(world.state, sort_keys=True) == after and len(world.events) == 5


def test_the_name_admin_moves_when_the_local_admin_is_named_so(world):
    """An admin named "admin" makes the name unambiguous: an entry a second admin filed under
    it is the local admin's, and the local admin's own entry stays."""
    world.state["local_admin_username"] = "admin"
    world.rows[:] = [r for r in world.rows if r["username"] != "admin"]
    world.whitelist(_e(P[1], EVE, "admin"), _e(P[2], ADM, "admin"))
    changes = channel_pairing.refile_whatsapp_owner_numbers()
    assert [(c["action"], c["to_scope"]) for c in changes] == [("moved", ADM)]
    assert world.wl() == [_e(P[1], ADM, "admin"), _e(P[2], ADM, "admin")]


def test_the_refile_never_raises(world, monkeypatch):
    world.whitelist(_e(P[1], ADM, "alice"))
    monkeypatch.setattr("vaf.core.config.is_admin_account",
                        lambda scope: (_ for _ in ()).throw(RuntimeError("boom")))
    assert channel_pairing.refile_whatsapp_owner_numbers() == []
    assert world.wl() == [_e(P[1], ADM, "alice")]


def test_an_entry_changed_since_it_was_read_is_left_to_the_newer_write(world, monkeypatch):
    """The candidates are read without the lock; under it an entry that changed is skipped."""
    world.whitelist(_e(P[1], ADM, "alice"))
    real_load = Config.load
    calls = []

    def load(cls):
        calls.append(1)
        if len(calls) == 2:   # the reload under the lock sees a newer write
            world.whitelist(_e(P[2], ADM, "alice"))
        return real_load()
    monkeypatch.setattr(Config, "load", classmethod(load))
    assert channel_pairing.refile_whatsapp_owner_numbers() == []
    assert world.wl() == [_e(P[2], ADM, "alice")] and world.events == []


# -- AC9 the call site -----------------------------------------------------------------------------

@pytest.mark.parametrize("raises", [False, True])
def test_the_bridge_start_refiles_before_it_looks_for_users(world, monkeypatch, raises):
    """Also when nobody is linked, and a failure does not stop the bridge. MUTATION: remove the
    call; move it after the early return; let its exception escape."""
    order = []

    def refile():
        order.append("refile")
        if raises:
            raise RuntimeError("boom")
        return []
    monkeypatch.setattr(channel_pairing, "refile_whatsapp_owner_numbers", refile)
    monkeypatch.setattr(wa, "_get_users_to_run", lambda: order.append("users") or [])
    monkeypatch.setattr(wa, "_write_bridge_state", lambda running: None)
    wa._run_bridge()
    assert order == ["refile", "users"]


# -- AC10 the delivery lanes after the bridge start ------------------------------------------------

LANES = [(None, "admin"), (None, None), (ADM, "dana")]


def _bridge_start(monkeypatch):
    monkeypatch.setattr(wa, "_get_users_to_run", lambda: [])
    monkeypatch.setattr(wa, "_write_bridge_state", lambda running: None)
    wa._run_bridge()


@pytest.mark.parametrize("inbound", [False, True])
@pytest.mark.parametrize("with_dana", [False, True])
def test_after_the_bridge_start_alices_number_is_alices(world, monkeypatch, inbound, with_dana):
    """Whitelist `[{Alice's number, ADM, "alice"}]`, an empty store. MUTATION: remove the
    refile call from `_run_bridge` and the admin's lanes return Alice's number."""
    world.state["channel_ingress_policy"] = set_front_office(None, inbound, "whatsapp")
    world.whitelist(_e(ALICE_P, ADM, "alice"), *([DANA_E] if with_dana else []))
    _bridge_start(monkeypatch)
    for scope, name in LANES:
        assert mc.get_whatsapp_chat_jid(scope, name) == (DANA_JID if with_dana else None), (scope, name)
    assert mc.get_whatsapp_chat_jid(ALI, "alice") == ALICE_JID
    assert [k for k, _f in world.events if k == "owner_endpoint_reset"] == []


@pytest.mark.parametrize("inbound", [False, True])
def test_the_admins_saved_chat_holding_alices_number_is_reset_once(world, monkeypatch, inbound):
    """Row K: the admin lane saved Alice's number as the admin's chat before the change."""
    world.state["channel_ingress_policy"] = set_front_office(None, inbound, "whatsapp")
    world.whitelist(_e(ALICE_P, ADM, "alice"))
    world.stored(by_scope={ADM: ALICE_JID})
    _bridge_start(monkeypatch)
    assert mc.get_whatsapp_chat_jid(ADM, "dana") is None
    assert mc.get_whatsapp_chat_jid(None, "admin") is None
    assert mc.get_whatsapp_chat_jid(ALI, "alice") == ALICE_JID
    assert [k for k, _f in world.events].count("owner_endpoint_reset") == 1


# -- AC11 cost -------------------------------------------------------------------------------------

def test_one_lookup_per_distinct_name_and_scope(world, monkeypatch):
    """MUTATION: remove the per-pass cache (50 lookups each)."""
    import vaf.core.config as config_mod
    counts = Counter()
    real_scope, real_admin = config_mod.scope_id_for_username, config_mod.is_admin_account

    def scope_for(name):
        counts["name"] += 1
        return real_scope(name)

    def admin(scope):
        counts["scope"] += 1
        return real_admin(scope)
    monkeypatch.setattr(config_mod, "scope_id_for_username", scope_for)
    monkeypatch.setattr(config_mod, "is_admin_account", admin)
    names = ["alice", "Carl", "nobody", "eve", "ina"]
    scopes = [ADM, EVE, CAR]
    world.whitelist(*[_e("+491700002%03d" % i, scopes[i % 3], names[i % 5]) for i in range(50)])
    channel_pairing.refile_whatsapp_owner_numbers()
    assert 0 < counts["name"] <= 5 and 0 < counts["scope"] <= 3, counts

    counts.clear()
    world.whitelist(*[_e("+491700003%03d" % i, None if i % 2 else ADM, "alice" if i % 2 else "")
                      for i in range(50)])
    channel_pairing.refile_whatsapp_owner_numbers()
    assert counts == Counter()


# -- AC12 the dashboard says who may name another account -------------------------------------------

@pytest.mark.parametrize("caller, expected", [(DANA, True), (ALICE, False), ((EVE, "eve", "admin"), True)])
def test_the_dashboard_offers_the_name_field_to_an_admin_only(world, monkeypatch, caller, expected):
    """MUTATION: hard-code `may_register_for_others` to True."""
    monkeypatch.setattr(wa, "get_whatsapp_chats", lambda *a, **k: [])
    monkeypatch.setattr(wa, "is_bridge_running", lambda: False)
    monkeypatch.setattr(wa, "get_connection_status", lambda *a, **k: False)
    monkeypatch.setattr(wa, "get_lid_mappings", lambda *a, **k: [])
    monkeypatch.setattr(wa, "get_contact_names", lambda *a, **k: {})
    out = asyncio.run(routes.get_whatsapp_dashboard(_req(*caller)))
    assert out["may_register_for_others"] is expected


# -- AC13 the dashboard component ------------------------------------------------------------------

WEB = Path(__file__).resolve().parent.parent / "web"


def test_the_card_shows_the_name_field_only_where_the_server_accepts_it():
    """MUTATION: render the name input unconditionally; restore the config mirror in the add or
    the remove handler."""
    src = (WEB / "components" / "connections" / "WhatsAppDashboard.tsx").read_text(encoding="utf-8")
    inputs = re.findall(r"<input[^>]*ownerUserPlaceholder", src)
    assert len(inputs) == 1
    assert re.search(r"\{data\?\.may_register_for_others && \(\s*<input[^>]*ownerUserPlaceholder", src)
    handlers = src[src.index("const handleOwnerAdd"):src.index("const handleAssign")]
    assert "onConfigChange" not in handlers, "the page's copy of the whitelist has no reader"
    assert "t('ownerUnknownAccount', { name: forName })" in handlers
    for path in sorted((WEB / "messages").glob("*.json")):
        text = json.loads(path.read_text(encoding="utf-8"))["settings"]["whatsappDashboard"]["ownerUnknownAccount"]
        assert "{name}" in text and "\u2014" not in text, path.name


# -- fuzz: the writer and the refile against the generator's labels ---------------------------------

FUZZ_DRAWS = int(os.environ.get("VAF_F447_FUZZ_DRAWS", "300"))
FUZZ_SEED = 4470
ADMIN_NAMES = ["dana", "admin", "Straße"]


def _variant(rng, name):
    return rng.choice([name, name.upper(), name.capitalize(), f" {name} "])


def _gen_world(rng):
    """(admin_name, accounts, directory, entries with labels, deleted names)."""
    admin_name = rng.choice(ADMIN_NAMES)
    accounts = [{"name": admin_name, "scope": ADM, "admin": True, "active": True}]
    for i, name in enumerate(rng.sample(["eve", "frank"], rng.randint(0, 2))):
        accounts.append({"name": name, "scope": f"ab12cd34-0000-4000-8000-0000000002{i:02d}", "admin": True,
                         "active": True})
    pool = ["alice", "bob", "carl"] + ([] if admin_name == "admin" else ["admin"])
    for i, name in enumerate(rng.sample(pool, rng.randint(1, 3))):
        accounts.append({"name": name, "scope": f"ab12cd34-0000-4000-8000-0000000003{i:02d}", "admin": False,
                         "active": True})
    if rng.random() < 0.4:
        accounts.append({"name": "ina", "scope": INA, "admin": False, "active": False})
    directory = [{"username": a["name"], "user_scope_id": a["scope"], "active": a["active"],
                  "role": "admin" if a["admin"] else "user"} for a in accounts[1:]]
    return admin_name, accounts, directory


def _number(rng):
    i = rng.randint(0, 11)
    return rng.choice(["+4917000004%02d" % i, "+4917000004%02d" % i, "017000004%02d" % i])


def _gen_entries(rng, admin_name, accounts):
    """Entries, each with its label: the scope the refile must move it to, or None."""
    active = [a for a in accounts if a["active"]]
    admins = [a for a in active if a["admin"]]
    ordinary = [a for a in active if not a["admin"]]
    former = "ab12cd34-0000-4000-8000-000000000099"
    out = []
    for _ in range(rng.randint(0, 8)):
        cause = rng.choice(["own", "f447", "f447", "hand", "scopeless", "deleted", "former", "dup"])
        if cause == "dup" and out:
            out.append(dict(rng.choice(out)))
            continue
        if cause == "own" or cause == "dup":
            who = rng.choice(active)
            name = rng.choice([who["name"], who["name"], "admin"]) if who["scope"] == ADM else who["name"]
            out.append({"entry": _e(_number(rng), who["scope"], name), "to": None})
        elif cause == "f447":
            by = rng.choice(admins)
            target = rng.choice([a for a in accounts if a["scope"] != by["scope"]] or [by])
            if target is by:
                continue
            resolvable = target["active"]
            ambiguous = target["name"].casefold() == "admin" and admin_name.casefold() != "admin"
            to = target["scope"] if resolvable and not ambiguous else None
            out.append({"entry": _e(_number(rng), by["scope"], _variant(rng, target["name"])), "to": to})
        elif cause == "hand" and ordinary:
            who = rng.choice(ordinary)
            other = rng.choice([a for a in accounts if a is not who])
            out.append({"entry": _e(_number(rng), who["scope"], other["name"]), "to": None})
        elif cause == "scopeless":
            entry = {"phone_number": _number(rng)}
            name = rng.choice(["admin", "", None, "MISSING", rng.choice(accounts)["name"]])
            if name != "MISSING":
                entry["vaf_username"] = name
            out.append({"entry": entry, "to": None})
        elif cause == "deleted":
            out.append({"entry": _e(_number(rng), rng.choice(admins)["scope"], "gone"), "to": None})
        elif cause == "former":
            out.append({"entry": _e(_number(rng), former, rng.choice(accounts)["name"]), "to": None})
    return out


def _lookup(accounts, name):
    folded = (name or "").strip().casefold()
    return next((a for a in accounts if a["active"] and a["name"].casefold() == folded), None)


def _same(a, b):
    ca, cb = phone_digits_canonical(a), phone_digits_canonical(b)
    return ca == cb if ca and cb else a.strip() == b.strip()


def _old_route_non_admin(whitelist, phone, scope, name):
    """The route as it was, for a non-admin's own registration: the differential reference."""
    for i, e in enumerate(whitelist):
        if isinstance(e, dict) and (str(e.get("user_scope_id")) == str(scope) or e.get("vaf_username") == name):
            whitelist[i] = {**e, "phone_number": phone, "user_scope_id": scope, "vaf_username": name}
            return whitelist
    return whitelist + [{"phone_number": phone, "user_scope_id": scope, "vaf_username": name}]


def _route_call(world, rng, accounts, labelled):
    caller = rng.choice([a for a in accounts if a["active"]])
    other = rng.choice(accounts)
    name = rng.choice([None, caller["name"], _variant(rng, other["name"]), "nobody", "ina"])
    scope = rng.choice([None, None, caller["scope"], other["scope"]])
    phone = _number(rng)
    before = [dict(x["entry"]) for x in labelled]
    events_before = len(world.events)
    status, out = _add((caller["scope"], caller["name"], "admin" if caller["admin"] else "user"), phone,
                       name=name, scope=scope)
    after = world.wl()
    assert status in (200, 400, 403, 404), (status, out)
    if status != 200:
        assert after == before and len(world.events) == events_before
        return status
    target = _lookup(accounts, out["vaf_username"])
    assert target is not None
    # (b) a non-admin writes only their own scope.
    if not caller["admin"]:
        assert target["scope"] == caller["scope"]
    # (a) only the target scope's first entry changes, or one entry is appended.
    if len(after) == len(before) + 1:
        assert after[:-1] == before and not any(e.get("user_scope_id") == target["scope"] for e in before)
        labelled.append({"entry": dict(after[-1]), "to": None})
    else:
        assert len(after) == len(before)
        index = next(i for i, e in enumerate(before) if str(e.get("user_scope_id") or "").strip() == target["scope"])
        assert [e for i, e in enumerate(after) if i != index] == [e for i, e in enumerate(before) if i != index]
        labelled[index] = {"entry": dict(after[index]), "to": None}
    assert after[[i for i, e in enumerate(after) if e.get("user_scope_id") == target["scope"]][0]]["phone_number"] \
        == phone.strip()
    return status


def _one_world(world, rng, seen):
    admin_name, accounts, directory = _gen_world(rng)
    world.state["local_admin_username"] = admin_name
    world.rows[:] = directory
    world.events.clear()
    labelled = _gen_entries(rng, admin_name, accounts)
    world.whitelist(*[x["entry"] for x in labelled])

    # Differential: without name collisions the old route and the new agree on a non-admin's
    # own registration.
    plain = (all(a["name"] != "admin" for a in accounts)
             and all(x["entry"].get("user_scope_id") and _lookup(accounts, x["entry"].get("vaf_username"))
                     and _lookup(accounts, x["entry"]["vaf_username"])["scope"] == x["entry"]["user_scope_id"]
                     and x["entry"]["vaf_username"] == _lookup(accounts, x["entry"]["vaf_username"])["name"]
                     for x in labelled))
    ordinary = [a for a in accounts if a["active"] and not a["admin"]]
    if plain and ordinary:
        who = rng.choice(ordinary)
        phone = _number(rng)
        expected = _old_route_non_admin([dict(x["entry"]) for x in labelled], phone, who["scope"], who["name"])
        assert _add((who["scope"], who["name"], "user"), phone)[0] == 200
        assert world.wl() == expected
        labelled = [{"entry": dict(e), "to": None} for e in world.wl()]
        world.events.clear()
        seen["differential"] += 1

    for _ in range(rng.randint(0, 3)):
        seen[_route_call(world, rng, accounts, labelled)] += 1
    world.events.clear()

    before = [x["entry"] for x in labelled]
    eligible = [(x["entry"], x["to"]) for x in labelled if x["to"]]
    keep = [x["entry"] for x in labelled if not x["to"]]
    changes = channel_pairing.refile_whatsapp_owner_numbers()
    after = world.wl()
    # (c) nothing but the labelled entries changes: every kept entry is still there, the rest
    # of the list is the labelled entries moved to their account.
    rest = Counter(json.dumps(e, sort_keys=True) for e in after)
    rest.subtract(Counter(json.dumps(e, sort_keys=True) for e in keep))
    assert all(v >= 0 for v in rest.values()), "a kept entry changed or vanished"
    moved = list((rest - Counter()).elements())
    pool = Counter(json.dumps({**e, "user_scope_id": to}, sort_keys=True) for e, to in eligible)
    assert not (Counter(moved) - pool), "an entry moved somewhere it was not labelled for"
    removed = len(eligible) - len(moved)
    assert removed >= 0 and len(after) == len(before) - removed
    # (d) no number is lost: every labelled number has an entry under its account.
    for e, to in eligible:
        assert any(x.get("user_scope_id") == to and _same(str(x.get("phone_number")), e["phone_number"])
                   for x in after), (e, to)
    # A move never makes a duplicate: per account, a number is held no more often than before
    # (or once, where it is new to the account).
    def held(entries):
        return Counter((str(x.get("user_scope_id") or ""), phone_digits_canonical(str(x.get("phone_number") or "")))
                       for x in entries)
    was, now = held(before), held(after)
    assert all(n <= max(1, was[key]) for key, n in now.items()), (before, after)
    # (e) two events per move, one per removal.
    kinds = Counter(k for k, _f in world.events)
    assert kinds == Counter({"channel_unpaired": len(eligible), "channel_paired": len(moved)}) - Counter()
    assert len(changes) == len(eligible)
    # (g) every labelled number is attributed to its account.
    attributed = mc._whatsapp_registered_entries()
    for e, to in eligible:
        assert (phone_digits_canonical(e["phone_number"]), to) in {(c, s) for c, s, _n in attributed}
    # (f) a second pass changes nothing and says nothing.
    events = len(world.events)
    assert channel_pairing.refile_whatsapp_owner_numbers() == []
    assert world.wl() == after and len(world.events) == events
    seen["moved"] += len(moved)
    seen["removed"] += removed
    seen["kept"] += len(keep)


def test_fuzz_owner_registration_and_refile(world):
    """FUZZ_DRAWS seeded worlds (labelled causes, then route calls, then the refile).
    MUTATION: any of the AC3 / AC8 mutations turns a draw red."""
    rng = random.Random(FUZZ_SEED)
    seen = Counter()
    for _draw in range(FUZZ_DRAWS):
        _one_world(world, rng, seen)
    # Every outcome is reached, so the oracle is not vacuous.
    for outcome in ("moved", "removed", "kept", "differential", 200, 400, 403, 404):
        assert seen[outcome] > 0, seen
    print(f"fuzz seed={FUZZ_SEED} draws={FUZZ_DRAWS} {dict(seen)}")
