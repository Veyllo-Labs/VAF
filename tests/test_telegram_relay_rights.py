# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Who may make a Telegram id a relay contact, and who may read a Telegram chat's history.

Measured on the code before this change, with synthetic accounts on one shared bot:
- `POST /api/telegram/relay-whitelist-add` was open to every account and took the id from
  the body. The bridge reads the relay list before the contact books, so Alice adding the
  admin's allowed contact made that person's messages go to Alice ("I'll pass that on to
  alice").
- The history route admitted a non-admin for any chat id on their own whitelist OR relay
  entries, and a Telegram session is keyed by the Telegram id alone: Alice adding the
  admin's own id read the admin's whole conversation with the agent.
- The Telegram routes read the caller leniently: a sign-in with a name and no scope was the
  local admin, removed the admin's relay contacts and wrote new ones under the admin's name.

Now a relay contact is proven: by a one-time code the person sends to the bot
(`POST /relay-pair`), or by an admin's add by id; one writer
(`channel_pairing.pair_telegram_relay`); the history route asks the one session ownership
rule; every Telegram route reads the caller strictly (`user_routes.request_account`).

FUZZ: `test_relay_writes_match_the_model` runs `VAF_F425_FUZZ_DRAWS` draws (300 by default)
of random operation sequences against a reference model of the rules.
"""
import asyncio
import json
import os
import random
import re
from types import SimpleNamespace

import pytest

from vaf.core.config import Config
from vaf.core.platform import Platform

ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
SECOND_ADMIN_SCOPE = "ab12cd34-0000-4000-8000-000000000003"
ALICE_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
BOB_SCOPE = "ab12cd34-0000-4000-8000-000000000002"
ADMIN = {"username": "admin", "role": "admin", "user_scope_id": ADMIN_SCOPE}
SECOND_ADMIN = {"username": "carol", "role": "admin", "user_scope_id": SECOND_ADMIN_SCOPE}
ALICE = {"username": "alice", "role": "user", "user_scope_id": ALICE_SCOPE}
BOB = {"username": "bob", "role": "user", "user_scope_id": BOB_SCOPE}

ADMIN_TG = "111"
ALICE_TG = "222"
CONTACT_TG = "555"
OWNERS = [{"telegram_user_id": ADMIN_TG, "telegram_username": "boss", "user_scope_id": ADMIN_SCOPE,
           "vaf_username": "admin"},
          {"telegram_user_id": ALICE_TG, "telegram_username": "al", "user_scope_id": ALICE_SCOPE,
           "vaf_username": "alice"}]
BOB_RELAY = {"telegram_user_id": "777", "telegram_username": None, "user_scope_id": BOB_SCOPE,
             "vaf_username": "bob", "paired_by": "code"}

RELAY_DONE = "Done: what you write here is now passed on to alice."
RELAY_OWNER = "This Telegram account is paired with a VAF account, so it cannot be a relay contact."


@pytest.fixture
def world(tmp_path, monkeypatch):
    import vaf.api.telegram_routes as routes
    import vaf.core.security_events as sec
    import vaf.core.session as session_module
    from vaf.core import channel_pairing as cp
    from vaf.core import tool_dispatch

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "local_admin_scope_id": ADMIN_SCOPE, "local_admin_username": "admin",
        "telegram_config": {"enabled": True, "verified": True, "whitelist": OWNERS, "relay_whitelist": []},
    }), encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(session_module, "default_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_module, "_manager", None)
    monkeypatch.setattr(cp, "_codes", {})
    monkeypatch.setattr(cp, "_spent", {})
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", lambda: [
        {"username": u["username"], "user_scope_id": u["user_scope_id"], "active": True, "role": u["role"]}
        for u in (ADMIN, SECOND_ADMIN, ALICE, BOB)])
    events = []
    monkeypatch.setattr(sec, "log_security_event", lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(routes, "log_security_event", lambda kind, **f: events.append((kind, f)))

    def seed(**blocks):
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in blocks.items():
            if key == "telegram_config":
                data[key] = {**data.get(key, {}), **value}
            else:
                data[key] = value
        path.write_text(json.dumps(data), encoding="utf-8")

    def on_disk():
        return json.loads(path.read_text(encoding="utf-8"))

    def relay():
        return (on_disk().get("telegram_config") or {}).get("relay_whitelist") or []

    return SimpleNamespace(seed=seed, on_disk=on_disk, relay=relay, events=events, sessions=sessions)


def _client(who):
    """The real Telegram router behind a signed-in `who["user"]` (None: the tokenless desktop)."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from vaf.api import telegram_routes

    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request, call_next):
        if who.get("user") is not None:
            request.state.user = who["user"]
        return await call_next(request)

    app.include_router(telegram_routes.router)
    return TestClient(app)


def _session(sid, scope, *texts):
    from vaf.core.session import Message, Session, SessionManager
    session = Session(id=sid, name=sid, metadata={"user_scope_id": scope} if scope else {})
    session.messages = [Message(role="user" if i % 2 == 0 else "assistant", content=t) for i, t in enumerate(texts)]
    SessionManager().save(session, sync_state=False)


def _history(client, sid):
    return client.get(f"/api/telegram/session/{sid}/history")


def _bot_running(monkeypatch):
    import vaf.api.telegram_bridge as tb
    import vaf.api.telegram_routes as routes
    from vaf.core import channel_secrets as cs
    monkeypatch.setattr(tb, "is_bridge_running", lambda: True)
    monkeypatch.setattr(routes, "_get_bot_username", lambda: "vaf_bot")
    cs.set_channel_secret("telegram", "bot_token", "123:" + "A" * 35)


def _admins_contact():
    from vaf.core import contacts_store
    return contacts_store.create_contact("Carol Example", "admin", user_scope_id=ADMIN_SCOPE,
                                         telegram_user_id=CONTACT_TG, assistant_access="allowed")


# -- AC1: no takeover by id ---------------------------------------------------------------------

def test_a_non_admin_cannot_take_the_admins_contact_by_id(world):
    """P1. MUTATION: drop `Depends(require_admin)` from `relay_whitelist_add` -> 200, and the
    contact resolves to Alice as a relay."""
    from vaf.api import telegram_bridge as tb
    _admins_contact()
    before = tb._resolve_telegram_user(CONTACT_TG)
    assert before[0] is not None and before[0]["user_scope_id"] == ADMIN_SCOPE and before[0].get("from_contact")
    assert before[1] is False

    res = _client({"user": ALICE}).post("/api/telegram/relay-whitelist-add", json={"telegram_user_id": CONTACT_TG})

    assert res.status_code == 403
    assert world.relay() == []
    assert tb._resolve_telegram_user(CONTACT_TG) == before
    assert world.events == []


# -- AC2: no read through a relay entry -------------------------------------------------------

@pytest.mark.parametrize("entry", [
    None,
    {"paired_by": "admin"},
    {},   # an entry written before relay entries recorded their proof
], ids=["no-entry", "admin-added", "legacy"])
@pytest.mark.parametrize("chat", [ADMIN_TG, CONTACT_TG])
def test_a_relay_entry_does_not_open_another_accounts_history(world, entry, chat):
    """P2, P3. MUTATION: restore the whitelist-or-relay membership check in place of
    `session_access_allowed` (the two seeded entries go 200 with the admin's messages), or
    pass `is_admin=True` for a non-admin (all go 200)."""
    _session(f"telegram_{ADMIN_TG}", ADMIN_SCOPE, "the admin's plan", "noted")
    _session(f"telegram_{CONTACT_TG}", ADMIN_SCOPE, "hello from Carol", "hi Carol")
    if entry is not None:
        world.seed(telegram_config={"relay_whitelist": [{"telegram_user_id": chat, "telegram_username": None,
                                                         "user_scope_id": ALICE_SCOPE, "vaf_username": "alice",
                                                         **entry}]})
    res = _history(_client({"user": ALICE}), f"telegram_{chat}")
    assert res.status_code == 403, res.text
    assert "plan" not in res.text and "Carol" not in res.text


def test_an_account_reads_its_own_chat_and_an_admin_reads_every_chat(world):
    _session(f"telegram_{ADMIN_TG}", ADMIN_SCOPE, "the admin's plan", "noted")
    _session(f"telegram_{ALICE_TG}", ALICE_SCOPE, "alice asks", "alice is answered")
    _session(f"telegram_{CONTACT_TG}", ADMIN_SCOPE, "hello from Carol", "hi Carol")
    who = {"user": ALICE}
    client = _client(who)
    mine = _history(client, f"telegram_{ALICE_TG}")
    assert mine.status_code == 200
    assert [m["content"] for m in mine.json()["messages"]] == ["alice asks", "alice is answered"]
    assert _history(client, "telegram_999").status_code == 403, "a chat with no file is nobody's to read"
    for admin in (ADMIN, SECOND_ADMIN, None):
        who["user"] = admin
        for chat, first in ((ADMIN_TG, "the admin's plan"), (ALICE_TG, "alice asks"), (CONTACT_TG, "hello from Carol")):
            res = _history(client, f"telegram_{chat}")
            assert res.status_code == 200 and res.json()["messages"][0]["content"] == first, (admin, chat)


# -- AC3: relay by code -----------------------------------------------------------------------

def test_a_relay_code_makes_the_sender_a_relay_contact_of_the_account_that_asked(world, monkeypatch):
    """MUTATION: `_pair_with_code` redeems the relay code under the owner channel (a whitelist
    entry, or None); drop `switch_on=True` (Alice's lane stays off); issue the relay code on
    "telegram" (the owner code is withdrawn)."""
    from vaf.api import telegram_bridge as tb
    from vaf.core import channel_pairing as cp
    _bot_running(monkeypatch)
    client = _client({"user": ALICE})

    owner = client.post("/api/telegram/pair").json()
    offer = client.post("/api/telegram/relay-pair")
    assert offer.status_code == 200, offer.text
    out = offer.json()
    assert out["link"] == f"https://t.me/vaf_bot?start={out['code']}" and out["command"] == f"/start {out['code']}"
    assert out["expires_in"] == cp.PAIRING_TTL_SECONDS and out["code"] != owner["code"]
    assert client.get("/api/telegram/relay-pair").json() == {"pending": True}
    assert client.get("/api/telegram/pair").json()["pending"] is True, "the owner code is still live"

    assert tb._pair_with_code("666", "dave", out["code"]) == RELAY_DONE
    assert world.relay() == [{"telegram_user_id": "666", "telegram_username": "dave", "user_scope_id": ALICE_SCOPE,
                              "vaf_username": "alice", "paired_by": "code"}]
    assert [e["telegram_user_id"] for e in world.on_disk()["telegram_config"]["whitelist"]] == [ADMIN_TG, ALICE_TG]
    assert world.on_disk()["connection_enabled_by_scope"][ALICE_SCOPE]["telegram"] is True
    assert [(k, f["username"], f["path"], f["detail"]) for k, f in world.events] == [
        ("channel_paired", "alice", "666", "relay 666")]
    assert client.get("/api/telegram/relay-pair").json() == {"pending": False}

    before = world.on_disk()
    assert tb._pair_with_code("667", "eve", out["code"]) is None, "a code is spent by its first use"
    assert world.on_disk() == before and len(world.events) == 1
    assert client.get("/api/telegram/pair").json()["pending"] is True


def test_an_owner_code_issued_after_a_relay_code_leaves_it_live(world, monkeypatch):
    _bot_running(monkeypatch)
    client = _client({"user": ALICE})
    relay = client.post("/api/telegram/relay-pair").json()
    client.post("/api/telegram/pair")
    assert client.get("/api/telegram/relay-pair").json() == {"pending": True}
    from vaf.api import telegram_bridge as tb
    assert tb._pair_with_code("666", None, relay["code"]) == RELAY_DONE


def test_no_relay_code_while_the_bot_is_not_running(world, monkeypatch):
    import vaf.api.telegram_bridge as tb
    _bot_running(monkeypatch)
    monkeypatch.setattr(tb, "is_bridge_running", lambda: False)
    res = _client({"user": ALICE}).post("/api/telegram/relay-pair")
    assert res.status_code == 409 and "not running" in res.json()["detail"]


# -- AC4: the writer refuses ------------------------------------------------------------------

@pytest.mark.parametrize("tid", [ADMIN_TG, ALICE_TG], ids=["the-admins-id", "alices-own-id"])
def test_a_paired_telegram_account_cannot_become_a_relay_contact(world, monkeypatch, tid):
    """P8. MUTATION: drop the whitelist check from `pair_telegram_relay` -> an entry is written."""
    from vaf.api import telegram_bridge as tb
    from vaf.core import channel_pairing as cp
    _bot_running(monkeypatch)
    code = _client({"user": ALICE}).post("/api/telegram/relay-pair").json()["code"]
    before = world.on_disk()
    assert tb._pair_with_code(tid, None, code) == RELAY_OWNER
    assert world.on_disk() == before and world.events == []
    assert cp.pair_telegram_relay(tid, None, user_scope_id=ADMIN_SCOPE, username="admin",
                                  may_take_over=True, paired_by="admin") == "owner"
    assert world.on_disk() == before


def test_a_code_does_not_move_another_accounts_relay_contact(world, monkeypatch):
    """MUTATION: drop the `may_take_over` check -> Bob's contact becomes Alice's."""
    from vaf.api import telegram_bridge as tb
    _bot_running(monkeypatch)
    world.seed(telegram_config={"relay_whitelist": [BOB_RELAY]})
    code = _client({"user": ALICE}).post("/api/telegram/relay-pair").json()["code"]
    assert "already a relay contact of another VAF account" in tb._pair_with_code("777", "x", code)
    assert world.relay() == [BOB_RELAY] and world.events == []


@pytest.mark.parametrize("tid", ["-100123", "12a", "1" * 21, "", "   ", "１２３", "12\n3"])
def test_only_a_persons_telegram_id_is_a_relay_contact(world, tid):
    """MUTATION: relax the id pattern to `-?[0-9]+` or `\\d{1,20}` or drop its length bound."""
    from vaf.core import channel_pairing as cp
    with pytest.raises(ValueError):
        cp.pair_telegram_relay(tid, None, user_scope_id=ALICE_SCOPE, username="alice",
                               may_take_over=False, paired_by="code")
    res = _client({"user": ADMIN}).post("/api/telegram/relay-whitelist-add", json={"telegram_user_id": tid})
    assert res.status_code == 400 and res.json()["detail"] == "A Telegram user ID is a number."
    assert world.relay() == []


def test_the_longest_telegram_id_is_accepted(world):
    from vaf.core import channel_pairing as cp
    assert cp.pair_telegram_relay("9" * 20, None, user_scope_id=ALICE_SCOPE, username="alice",
                                  may_take_over=False, paired_by="code") == "paired"


# -- AC5: an admin by id ----------------------------------------------------------------------

@pytest.mark.parametrize("admin", [ADMIN, SECOND_ADMIN, None], ids=["local-admin", "second-admin", "desktop"])
def test_an_admin_adds_and_moves_a_relay_contact_by_id(world, admin):
    """MUTATION: only the local admin's scope passes the route's admin check -> the second
    admin goes red."""
    me = admin or ADMIN
    world.seed(telegram_config={"relay_whitelist": [BOB_RELAY]})
    client = _client({"user": admin})
    res = client.post("/api/telegram/relay-whitelist-add", json={"telegram_user_id": "888", "telegram_username": "x"})
    assert res.status_code == 200 and res.json() == {"status": "ok", "relay_whitelist_count": 2}
    assert client.post("/api/telegram/relay-whitelist-add", json={"telegram_user_id": "777"}).status_code == 200
    owners = {e["telegram_user_id"]: (e["user_scope_id"], e["vaf_username"], e["paired_by"]) for e in world.relay()}
    assert owners == {"888": (me["user_scope_id"], me["username"], "admin"),
                      "777": (me["user_scope_id"], me["username"], "admin")}
    res = client.post("/api/telegram/relay-whitelist-add", json={"telegram_user_id": ALICE_TG})
    assert res.status_code == 409 and res.json()["detail"] == RELAY_OWNER
    assert set(owners) == {e["telegram_user_id"] for e in world.relay()}


def test_the_dashboard_says_who_may_add_by_id(world):
    who = {"user": ALICE}
    client = _client(who)
    assert client.get("/api/telegram/dashboard").json()["can_add_by_id"] is False
    for admin in (ADMIN, SECOND_ADMIN, None):
        who["user"] = admin
        assert client.get("/api/telegram/dashboard").json()["can_add_by_id"] is True


def test_a_writer_records_an_access_change_once(world):
    """MUTATION: record the event before the "moved" comparison -> the same contact added
    again records a second event."""
    from vaf.core import channel_pairing as cp
    kw = dict(user_scope_id=ALICE_SCOPE, username="alice", may_take_over=False, paired_by="code")
    assert cp.pair_telegram_relay("901", "a", **kw) == "paired"
    assert cp.pair_telegram_relay("901", "a", **kw) == "unchanged"
    assert cp.pair_telegram_relay("901", "renamed", **kw) == "paired", "a new display name is written"
    assert cp.pair_telegram_relay("901", None, user_scope_id=BOB_SCOPE, username="bob",
                                  may_take_over=True, paired_by="admin") == "paired"
    assert [(f["username"], f["detail"]) for _k, f in world.events] == [("alice", "relay 901"), ("bob", "relay 901")]


def test_a_legacy_entry_paired_again_by_code_records_its_proof(world):
    from vaf.core import channel_pairing as cp
    legacy = {k: v for k, v in BOB_RELAY.items() if k != "paired_by"}
    world.seed(telegram_config={"relay_whitelist": [legacy]})
    assert cp.pair_telegram_relay("777", None, user_scope_id=BOB_SCOPE, username="bob",
                                  may_take_over=False, paired_by="code") == "paired"
    assert world.relay() == [BOB_RELAY] and world.events == [], "no access moved"


# -- AC6: the strict reader -------------------------------------------------------------------

UNLINKED = [
    {"role": "user", "user_scope_id": "", "username": "dave"},
    {"role": "user", "user_scope_id": ALICE_SCOPE, "username": ""},
]
UNLINKED_ADMIN = [dict(u, role="admin") for u in UNLINKED]

STRICT_ROUTES = [
    ("post", "/api/telegram/relay-whitelist-remove", {"telegram_user_id": "777"}, UNLINKED),
    ("post", "/api/telegram/relay-pair", None, UNLINKED),
    ("get", "/api/telegram/relay-pair", None, UNLINKED),
    ("get", "/api/telegram/status", None, UNLINKED),
    ("get", "/api/telegram/dashboard", None, UNLINKED),
    ("get", f"/api/telegram/session/telegram_{ADMIN_TG}/history", None, UNLINKED),
    ("post", "/api/telegram/whitelist-add", {"telegram_user_id": "4242"}, UNLINKED_ADMIN),
    ("post", "/api/telegram/relay-whitelist-add", {"telegram_user_id": "888"}, UNLINKED_ADMIN),
    ("post", "/api/telegram/relay-whitelist-remove", {"telegram_user_id": "777"}, UNLINKED_ADMIN),
]


@pytest.mark.parametrize("method,path,body,users", STRICT_ROUTES, ids=[f"{m}-{p.rsplit('/', 1)[-1]}-{u[0]['role']}"
                                                                       for m, p, _b, u in STRICT_ROUTES])
def test_a_sign_in_without_an_account_is_refused_on_every_telegram_route(world, monkeypatch, method, path, body, users):
    """P6. MUTATION: any one of these routes back on a lenient reader (the removal of the
    admin's relay contact succeeds, or a read answers as the admin)."""
    from vaf.api.user_routes import UNLINKED_SIGN_IN
    _bot_running(monkeypatch)
    _session(f"telegram_{ADMIN_TG}", ADMIN_SCOPE, "the admin's plan")
    admins = dict(BOB_RELAY, user_scope_id=ADMIN_SCOPE, vaf_username="admin", paired_by="admin")
    world.seed(telegram_config={"relay_whitelist": [admins]})
    before = world.on_disk()
    for user in users:
        client = _client({"user": user})
        call = getattr(client, method)
        res = call(path, json=body) if body is not None else call(path)
        assert (res.status_code, res.json().get("detail")) == (400, UNLINKED_SIGN_IN), (user, res.text)
    assert world.on_disk() == before
    assert world.events == []


def test_the_desktop_without_a_sign_in_is_the_local_admin(world, monkeypatch):
    import vaf.api.telegram_routes as routes
    _bot_running(monkeypatch)
    client = _client({"user": None})
    assert client.get("/api/telegram/relay-pair").json() == {"pending": False}
    assert client.post("/api/telegram/relay-pair").status_code == 200
    assert client.get("/api/telegram/relay-pair").json() == {"pending": True}
    assert not hasattr(routes, "get_current_vaf_user"), "the lenient reader is gone"


# -- fuzz -------------------------------------------------------------------------------------

_ID = re.compile(r"[0-9]{1,20}")
_ACCOUNTS = [ADMIN, ALICE, BOB, {"username": "dave", "role": "user", "user_scope_id": "ab12cd34-0000-4000-8000-000000000004"}]
_IDS = [str(n) for n in (1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008)]


def _junk(rng):
    return rng.choice(["-" + rng.choice(_IDS), rng.choice(_IDS) + "x", "", " ", "\t", "abc",
                       "".join(rng.choice("0123456789") for _ in range(rng.randint(0, 25))),
                       " " + rng.choice(_IDS) + " "])


def _remove(caller, tid):
    import vaf.api.telegram_routes as routes
    from fastapi import HTTPException
    req = SimpleNamespace(state=SimpleNamespace(user=dict(caller)))
    try:
        asyncio.run(routes.relay_whitelist_remove(req, routes.WhitelistAddRequest(telegram_user_id=tid)))
    except HTTPException:
        pass


@pytest.fixture
def config_in_memory(world, monkeypatch):
    """The config store as a dict: the writer and the remove route read through `Config.load`
    and `Config.get` and write through `Config.save`, as on disk (a disk round trip per step
    cost about a millisecond, five seconds for the suite's draws)."""
    state = world.on_disk()
    copy = lambda: json.loads(json.dumps(state))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: copy()))
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: copy().get(key, default)))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, cfg: (state.clear(), state.update(json.loads(json.dumps(cfg))))))

    def seed_telegram(**fields):
        state["telegram_config"] = {**state.get("telegram_config", {}), **fields}

    def relay():
        return (state.get("telegram_config") or {}).get("relay_whitelist") or []

    return SimpleNamespace(seed_telegram=seed_telegram, relay=relay)


def test_relay_writes_match_the_model(config_in_memory):
    """Random operation sequences over four accounts (one admin) and eight ids, some of them
    owner pairings, against a reference model of the writer's and the remove route's rules.
    Invariants after every step: the stored list equals the model; an id at most once; no
    relay id is on the whitelist; a non-admin's operation leaves every other account's entry
    as it was; every entry carries its proof; every id is a person's Telegram id."""
    from vaf.core import channel_pairing as cp
    world = config_in_memory
    draws = int(os.environ.get("VAF_F425_FUZZ_DRAWS", "300"))
    rng = random.Random(425)
    for draw in range(draws):
        owners = {tid: rng.choice(_ACCOUNTS) for tid in _IDS if rng.random() < 0.25}
        whitelist = [{"telegram_user_id": tid, "user_scope_id": a["user_scope_id"], "vaf_username": a["username"]}
                     for tid, a in owners.items()]
        world.seed_telegram(whitelist=whitelist, relay_whitelist=[])
        model = {}
        relay = []
        for step in range(rng.randint(1, 40)):
            caller = rng.choice(_ACCOUNTS)
            is_admin = caller["role"] == "admin"
            op = rng.choice(["code", "admin", "remove", "junk"])
            tid = _junk(rng) if op == "junk" else rng.choice(_IDS)
            disk_before = {e["telegram_user_id"]: e for e in relay}
            name = rng.choice([None, "tg_name"])
            if op == "remove":
                _remove(caller, tid)
                key = tid.strip()
                if key in model and (is_admin or model[key][0] == caller["user_scope_id"]):
                    del model[key]
            else:
                by_admin = op == "admin" or (op == "junk" and is_admin)
                if by_admin and not is_admin:
                    caller = ADMIN
                kw = dict(user_scope_id=caller["user_scope_id"], username=caller["username"],
                          may_take_over=by_admin, paired_by="admin" if by_admin else "code")
                expect_error = not _ID.fullmatch(tid.strip())
                try:
                    result = cp.pair_telegram_relay(tid, name, **kw)
                    assert not expect_error, (draw, step, tid)
                except ValueError:
                    assert expect_error, (draw, step, tid)
                    result = None
                key = tid.strip()
                if result is not None:
                    prev = model.get(key)
                    if key in owners:
                        want = "owner"
                    elif prev is not None and prev[0] != caller["user_scope_id"] and not by_admin:
                        want = "taken"
                    else:
                        new = (caller["user_scope_id"], kw["paired_by"], name)
                        want = "unchanged" if prev == new else "paired"
                        model[key] = new
                    assert result == want, (draw, step, tid, result, want)
            relay = world.relay()
            ids = [e["telegram_user_id"] for e in relay]
            assert len(ids) == len(set(ids)), (draw, step)
            assert not set(ids) & set(owners), (draw, step)
            assert all(e.get("paired_by") in ("code", "admin") and _ID.fullmatch(e["telegram_user_id"]) for e in relay)
            assert {e["telegram_user_id"]: (e["user_scope_id"], e["paired_by"], e["telegram_username"])
                    for e in relay} == model, (draw, step, op)
            if not caller["role"] == "admin":
                after = {e["telegram_user_id"]: e for e in relay}
                others = lambda m: {k: v for k, v in m.items() if v["user_scope_id"] != caller["user_scope_id"]}
                assert others(after) == others(disk_before), (draw, step, op)
