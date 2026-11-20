# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every channel route that writes owner-relevant state says who may drive it, and a
non-admin driving it leaves every other account's entry as it was.

`Config.ROUTE_OWNED_SUBKEYS` names the messenger sub-keys only their routes and bridges write
(the pairings, the LID maps, the relay switch) and the lane that writes each. It says WHICH
lane writes, not WHICH CALLER may drive it, and the eleven route writers measured before this
guard each hand-rolled one of three rules; two got it wrong. `POST /api/whatsapp/lid-assign`
checked nothing, so a non-admin bound a WhatsApp LID to the admin's own number and became the
admin's owner lane; `POST /api/whatsapp/whitelist/remove` authorised by the first entry with
the number and then removed every one, the admin's included. A third, the WhatsApp dashboard,
wrote a guessed binding on a GET.

THE GUARD:
- discovery: every route function in `vaf/api/*_routes.py` that writes one of those sub-keys
  (a subscript store, `del`, `.pop` or `.setdefault` with the sub-key as a constant, an
  `.update` whose dict literal, keyword or `**{...}` names it, a dict literal naming it stored
  into a subscript, annotated or not), or that names a lane function from the table's values, directly or through a
  helper in its module;
- `RULES`, the declared rule per route. A writer missing from it, or a declared route that no
  longer writes, fails;
- a probe per declared route: a non-admin drives it against another account's entry, through
  the real router and its dependencies, and that entry must be unchanged.

The rules: `admin` (only an admin may write), `own` (a non-admin writes their own entries and
never another account's), `own-lane` (a non-admin's write lands in a map only their own lane
reads), `cache` (a value that names no account).

NAMED BOUNDARY of the discovery: it reads the `*_routes.py` modules only, and a sub-key only
where it is spelled as a constant. Routes registered elsewhere (`web_server.py`'s own routes,
the generic config PATCH in `config_routes.py`, whose route-owned sub-keys the save strip of
tests/test_messenger_subkeys_route_owned.py already holds), a key built at run time, a write
through a helper in another module that is not a lane named in the table, and a whole block
replaced by a value that was not a dict literal are invisible to it.

THE SECOND TABLE, `PROCESS_RULES`: every route that starts, stops or restarts a bridge's
processes (it names one of `BRIDGE_LIFECYCLE`, directly or through a helper in its module),
with its rule: `admin` (anybody else gets 403 and nothing is started or stopped) or
`own-process` (anybody else reaches only their own account's process, as their sign-in names
it). These routes write no route-owned sub-key, which is why they are not rows of `RULES`.
Its named boundary: a lifecycle function is seen only under its own name, and
`web_server.py`'s startup auto-start and reconnect worker are not routes.
"""
import ast
import json
import re
from pathlib import Path

import pytest

from vaf.core.config import Config

API = Path(__file__).resolve().parent.parent / "vaf" / "api"

RULES = {
    "app_link_routes:switch_relay": "admin",
    "telegram_routes:whitelist_add": "admin",
    "telegram_routes:start_pairing": "cache",
    "telegram_routes:get_telegram_dashboard": "cache",
    # The relay invite writes only the `bot_username` cache; the relay entry itself is written
    # by the bot when the person sends the code (`telegram_bridge._pair_with_code`).
    "telegram_routes:start_relay_pairing": "cache",
    # admin: an id typed in proves nothing about who holds it; every other account invites
    # the person by a one-time code they send to the bot (`relay-pair`), so the person chose
    # the account. Open to every account, the add made another account's allowed contact its
    # own relay contact (the bridge reads the relay list before the contact books).
    "telegram_routes:relay_whitelist_add": "admin",
    "telegram_routes:relay_whitelist_remove": "own",
    "whatsapp_routes:add_whitelist_entry": "own",
    "whatsapp_routes:remove_whitelist_entry": "own",
    "whatsapp_routes:assign_lid_to_number": "own-lane",
}
KNOWN_RULES = {"admin", "own", "own-lane", "cache"}

ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
ALICE = {"username": "alice", "role": "user", "user_scope_id": "ab12cd34-0000-4000-8000-000000000001"}
BOB_SCOPE = "ab12cd34-0000-4000-8000-000000000002"
ADMIN_PHONE = "+4917000000001"


# -- discovery --------------------------------------------------------------------------------

def _owned_sub_keys():
    return {sub for subs in Config.ROUTE_OWNED_SUBKEYS.values() for sub in subs}


def _lane_functions():
    """The function names the table's lane descriptions point at (`module.function`)."""
    names = set()
    for subs in Config.ROUTE_OWNED_SUBKEYS.values():
        for lane in subs.values():
            for dotted in re.findall(r"[A-Za-z_][\w.]*\.[A-Za-z_]\w*", lane):
                names.add(dotted.rsplit(".", 1)[1])
    return names


def _const_key(node):
    sl = node.slice if isinstance(node, ast.Subscript) else None
    return sl.value if isinstance(sl, ast.Constant) and isinstance(sl.value, str) else None


def _names_a_key(node, keys) -> bool:
    return isinstance(node, ast.Dict) and any(
        isinstance(k, ast.Constant) and k.value in keys for k in node.keys)


def _writes_directly(fn, keys, lanes) -> bool:
    for node in ast.walk(fn):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = node.targets
        if any(isinstance(t, ast.Subscript) and _const_key(t) in keys for t in targets):
            return True
        # A dict literal naming a sub-key, stored into a subscript (a whole block replaced);
        # an annotated assignment counts when it has a value, a bare annotation stores nothing.
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if (value is not None and _names_a_key(value, keys)
                and any(isinstance(t, ast.Subscript) for t in targets)):
            return True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("pop", "setdefault") and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value in keys):
            return True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"
                and (any(_names_a_key(a, keys) for a in node.args)
                     or any(kw.arg in keys if kw.arg is not None else _names_a_key(kw.value, keys)
                            for kw in node.keywords))):
            return True
        if isinstance(node, ast.Name) and node.id in lanes and fn.name != node.id:
            return True
        if isinstance(node, ast.Attribute) and node.attr in lanes:
            return True
    return False


def _is_route(fn) -> bool:
    for dec in fn.decorator_list:
        if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in ("get", "post", "put", "patch", "delete", "api_route")
                and isinstance(dec.func.value, ast.Name) and dec.func.value.id.endswith("router")):
            return True
    return False


def writer_routes(source: str, keys=None, lanes=None):
    """The route functions of one module that write a route-owned sub-key."""
    keys = _owned_sub_keys() if keys is None else keys
    lanes = _lane_functions() if lanes is None else lanes
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    writers = {name for name, fn in funcs.items() if _writes_directly(fn, keys, lanes)}
    grew = True
    while grew:
        grew = False
        for name, fn in funcs.items():
            if name in writers:
                continue
            if {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} & writers:
                writers.add(name)
                grew = True
    return sorted(name for name, fn in funcs.items() if name in writers and _is_route(fn))


def _discovered():
    out = set()
    for path in sorted(API.glob("*_routes.py")):
        for name in writer_routes(path.read_text(encoding="utf-8")):
            out.add(f"{path.stem}:{name}")
    return out


def test_every_writer_route_declares_its_rule():
    """MUTATION: drop `assign_lid_to_number` from RULES, or add a writer route anywhere."""
    found = _discovered()
    assert found - set(RULES) == set(), "a route writes owner-relevant channel state without a declared rule"
    assert set(RULES) - found == set(), "a declared route no longer writes; drop its row"
    assert set(RULES.values()) <= KNOWN_RULES


def test_the_scanner_reports_an_undeclared_writer():
    """The scanner's self-test: a helper writes a route-owned sub-key, a route calls it."""
    synthetic = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "def _stash(cfg):\n"
        "    cfg['whatsapp_config']['owner_control'] = {}\n"
        "def _relay(r):\n"
        "    return r.set_relay_enabled\n"
        "@router.post('/x')\n"
        "async def sneaky(request):\n"
        "    _stash({})\n"
        "@router.put('/y')\n"
        "async def also(request):\n"
        "    _relay(None)\n"
        "@router.post('/z')\n"
        "async def pops(request, wc):\n"
        "    wc.pop('lid_to_e164_by_scope', None)\n"
        "@router.post('/u')\n"
        "async def updates(request, wc):\n"
        "    wc.update({'lid_to_e164': {}})\n"
        "@router.post('/k')\n"
        "async def keywords(request, wc):\n"
        "    wc.update(owner_control={})\n"
        "@router.post('/b')\n"
        "async def block(request, cfg):\n"
        "    cfg['telegram_config'] = {'enabled': True, 'whitelist': []}\n"
        "@router.post('/s')\n"
        "async def splat(request, wc):\n"
        "    wc.update(**{'owner_control': {}})\n"
        "@router.post('/a')\n"
        "async def annotated(request, cfg):\n"
        "    cfg['telegram_config']: dict = {'whitelist': []}\n"
        "@router.post('/n')\n"
        "async def bare_annotation(request, cfg):\n"
        "    cfg['telegram_config']: dict\n"
        "@router.get('/w')\n"
        "async def harmless(request):\n"
        "    out = {'whitelist': []}\n"
        "    out.update(count=1)\n"
        "    return {'whitelist': []}\n"
    )
    assert writer_routes(synthetic) == ["also", "annotated", "block", "keywords", "pops", "sneaky", "splat",
                                        "updates"]


# -- the probes -------------------------------------------------------------------------------

@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"local_admin_scope_id": ADMIN_SCOPE, "local_admin_username": "admin"}),
                    encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)

    def seed(**blocks):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(blocks)
        path.write_text(json.dumps(data), encoding="utf-8")

    def on_disk():
        return json.loads(path.read_text(encoding="utf-8"))

    return type("F", (), {"seed": staticmethod(seed), "on_disk": staticmethod(on_disk)})


def _client(module):
    import importlib

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request, call_next):
        request.state.user = dict(ALICE)
        return await call_next(request)

    app.include_router(importlib.import_module(f"vaf.api.{module}").router)
    return TestClient(app)


TG_ADMIN = {"telegram_user_id": "4242", "user_scope_id": ADMIN_SCOPE, "vaf_username": "admin"}
TG_BOB_RELAY = {"telegram_user_id": "777", "telegram_username": None, "user_scope_id": BOB_SCOPE,
                "vaf_username": "bob"}
WA_ADMIN = {"phone_number": ADMIN_PHONE, "user_scope_id": ADMIN_SCOPE, "vaf_username": "admin"}
WA_ALICE_SAME = {"phone_number": ADMIN_PHONE, "user_scope_id": ALICE["user_scope_id"], "vaf_username": "alice"}


def _tg(cfg):
    return cfg.get("telegram_config") or {}


def _wa(cfg):
    return cfg.get("whatsapp_config") or {}


# route -> (seed blocks, method, path, body, the status the route answers, the other accounts' entries).
# The status is pinned so a probe whose URL went stale (404, 405) fails instead of passing on
# an untouched config.
PROBES = {
    "app_link_routes:switch_relay": (
        {"veyllo_app_config": {"enabled": True, "relay_enabled": True}},
        "put", "/api/app-link/relay", {"enabled": False},
        403,
        lambda c: (c.get("veyllo_app_config") or {}).get("relay_enabled")),
    "telegram_routes:whitelist_add": (
        {"telegram_config": {"verified": True, "whitelist": [TG_ADMIN]}},
        "post", "/api/telegram/whitelist-add", {"telegram_user_id": "4242"},
        403,
        lambda c: _tg(c).get("whitelist")),
    "telegram_routes:start_pairing": (
        {"telegram_config": {"bot_username": "shared_vaf_bot", "whitelist": [TG_ADMIN]}},
        "post", "/api/telegram/pair", None,
        409,
        lambda c: (_tg(c).get("bot_username"), _tg(c).get("whitelist"))),
    "telegram_routes:get_telegram_dashboard": (
        {"telegram_config": {"bot_username": "shared_vaf_bot", "whitelist": [TG_ADMIN]}},
        "get", "/api/telegram/dashboard", None,
        200,
        lambda c: (_tg(c).get("bot_username"), _tg(c).get("whitelist"))),
    "telegram_routes:start_relay_pairing": (
        {"telegram_config": {"bot_username": "shared_vaf_bot", "whitelist": [TG_ADMIN]}},
        "post", "/api/telegram/relay-pair", None,
        409,
        lambda c: (_tg(c).get("bot_username"), _tg(c).get("whitelist"))),
    "telegram_routes:relay_whitelist_add": (
        {"telegram_config": {"relay_whitelist": [TG_BOB_RELAY]}},
        "post", "/api/telegram/relay-whitelist-add", {"telegram_user_id": "777"},
        403,
        lambda c: _tg(c).get("relay_whitelist")),
    "telegram_routes:relay_whitelist_remove": (
        {"telegram_config": {"relay_whitelist": [TG_BOB_RELAY]}},
        "post", "/api/telegram/relay-whitelist-remove", {"telegram_user_id": "777"},
        200,
        lambda c: _tg(c).get("relay_whitelist")),
    "whatsapp_routes:add_whitelist_entry": (
        {"whatsapp_config": {"enabled": True, "whitelist": [WA_ADMIN]}},
        "post", "/api/whatsapp/whitelist/add", {"phone_number": ADMIN_PHONE, "user_scope_id": ADMIN_SCOPE,
                                                "vaf_username": "admin"},
        # A non-admin naming another account, by name or by scope, is refused outright.
        403,
        lambda c: [e for e in _wa(c).get("whitelist") or [] if e.get("user_scope_id") != ALICE["user_scope_id"]]),
    "whatsapp_routes:remove_whitelist_entry": (
        {"whatsapp_config": {"enabled": True, "whitelist": [WA_ALICE_SAME, WA_ADMIN]}},
        "post", "/api/whatsapp/whitelist/remove", {"phone_number": ADMIN_PHONE},
        200,
        lambda c: [e for e in _wa(c).get("whitelist") or [] if e.get("user_scope_id") != ALICE["user_scope_id"]]),
    "whatsapp_routes:assign_lid_to_number": (
        {"whatsapp_config": {"enabled": True, "lid_to_e164": {"111111111111111@lid": "+491700000077"},
                             "lid_to_e164_by_scope": {BOB_SCOPE: {"222222222222222@lid": "+491700000088"}}}},
        "post", "/api/whatsapp/lid-assign", {"lid_jid": "111111111111111@lid", "phone_number": ADMIN_PHONE},
        200,
        lambda c: (_wa(c).get("lid_to_e164"), (_wa(c).get("lid_to_e164_by_scope") or {}).get(BOB_SCOPE))),
}


def test_every_declared_route_has_a_probe():
    assert set(PROBES) == set(RULES)


@pytest.mark.parametrize("route", sorted(RULES))
def test_a_non_admin_leaves_every_other_accounts_entry_alone(config_file, monkeypatch, route):
    """MUTATION: delete the non-admin check in `relay_whitelist_remove` (Bob's relay contact
    goes), or the non-admin lane in `assign_lid_to_number` (the shared map changes)."""
    monkeypatch.setattr("vaf.core.whatsapp_auth.get_linked_phone", lambda username: None)
    seed, method, path, body, expected, others = PROBES[route]
    config_file.seed(**json.loads(json.dumps(seed)))
    before = json.dumps(others(config_file.on_disk()), sort_keys=True)
    assert before not in ("null", "[]", "[null, null]"), "the probe has nothing of another account to touch"
    client = _client(route.split(":", 1)[0])
    call = getattr(client, method)
    res = call(path, json=body) if body is not None else call(path)
    assert res.status_code == expected, (res.status_code, res.text)
    assert json.dumps(others(config_file.on_disk()), sort_keys=True) == before


# -- the second table: who may drive a bridge's processes ---------------------------------------
#
# `RULES` above declares the writers of route-owned config sub-keys. Starting and stopping a
# bridge writes none of them, so its routes are declared here instead, with their own
# discovery: every route function in `vaf/api/*_routes.py` that names one of the bridge
# lifecycle functions below (a call, an attribute such as `importlib...stop_bridge()`, or a
# function handed to `asyncio.to_thread`), directly or through a helper in its module.
# Measured before this table: the WhatsApp start, stop and restart took no `Request` and read
# no account, so an ordinary account's Stop and Restart ended every account's process.
#
# The rules: `admin` (an account that is not an admin gets 403 and no lifecycle function is
# called), `own-process` (such an account reaches no whole-bridge function, and every account
# function it reaches names its own name and scope).
#
# NAMED BOUNDARY of this discovery: a lifecycle function is seen only under one of the names
# below, spelled as a constant; `web_server.py`'s startup auto-start and its reconnect worker
# drive the WhatsApp bridge too, and they are not routes.

BRIDGE_LIFECYCLE = {
    "start_bridge", "stop_bridge", "restart_bridge", "restart_bridge_result", "start_missing_accounts",
    "start_account", "stop_account", "restart_account",
}
WHOLE_BRIDGE = {"start_bridge", "stop_bridge", "restart_bridge", "restart_bridge_result", "start_missing_accounts"}

PROCESS_RULES = {
    "config_routes:disconnect_channel": "admin",
    "discord_routes:start_discord_bridge": "admin",
    "discord_routes:stop_discord_bridge": "admin",
    "telegram_routes:start_telegram_bridge": "admin",
    "telegram_routes:stop_telegram_bridge": "admin",
    "whatsapp_routes:start_whatsapp_bridge": "own-process",
    "whatsapp_routes:stop_whatsapp_bridge": "own-process",
    "whatsapp_routes:restart_whatsapp_bridge": "own-process",
}
PROCESS_RULE_KINDS = {"admin", "own-process"}


def _names_a_lifecycle_function(fn, names) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names:
            return True
        if isinstance(node, ast.Attribute) and node.attr in names:
            return True
    return False


def process_routes(source: str, names=None):
    """The route functions of one module that drive a bridge's processes."""
    names = BRIDGE_LIFECYCLE if names is None else names
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    drivers = {name for name, fn in funcs.items() if _names_a_lifecycle_function(fn, names)}
    grew = True
    while grew:
        grew = False
        for name, fn in funcs.items():
            if name in drivers:
                continue
            if {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} & drivers:
                drivers.add(name)
                grew = True
    return sorted(name for name, fn in funcs.items() if name in drivers and _is_route(fn))


def _discovered_process_routes():
    out = set()
    for path in sorted(API.glob("*_routes.py")):
        for name in process_routes(path.read_text(encoding="utf-8")):
            out.add(f"{path.stem}:{name}")
    return out


def test_every_process_route_declares_its_rule():
    """MUTATION: drop a row; or add a `stop_bridge()` call to any new route."""
    found = _discovered_process_routes()
    assert found - set(PROCESS_RULES) == set(), "a route drives a bridge without a declared rule"
    assert set(PROCESS_RULES) - found == set(), "a declared route no longer drives a bridge; drop its row"
    assert set(PROCESS_RULES.values()) <= PROCESS_RULE_KINDS


def test_the_process_scanner_reports_an_undeclared_route():
    """The scanner's self-test: a direct call, an attribute call, a function handed over, a
    helper; a bare import and a non-route stay out."""
    synthetic = (
        "import asyncio, importlib\n"
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "def _halt():\n"
        "    from x import stop_bridge\n"
        "    stop_bridge()\n"
        "def not_a_route():\n"
        "    start_bridge()\n"
        "@router.post('/a')\n"
        "async def direct(request):\n"
        "    start_bridge(only='x')\n"
        "@router.post('/b')\n"
        "async def attribute(request):\n"
        "    importlib.import_module('vaf.api.x_bridge').restart_bridge()\n"
        "@router.post('/c')\n"
        "async def handed_over(request):\n"
        "    await asyncio.to_thread(stop_account, 'alice')\n"
        "@router.post('/d')\n"
        "async def through_a_helper(request):\n"
        "    _halt()\n"
        "@router.post('/e')\n"
        "async def only_imports(request):\n"
        "    from x import start_missing_accounts as s\n"
        "    return {}\n"
        "@router.get('/f')\n"
        "async def harmless(request):\n"
        "    return {'start_bridge': 1}\n"
    )
    assert process_routes(synthetic) == ["attribute", "direct", "handed_over", "through_a_helper"]


# route -> (method, path, the status an account that is not an admin gets). Pinned, so a probe
# whose URL went stale (404, 405) fails instead of passing on nothing called.
PROCESS_PROBES = {
    "config_routes:disconnect_channel": ("delete", "/api/config/channels/whatsapp/credentials", 403),
    "discord_routes:start_discord_bridge": ("post", "/api/discord/start", 403),
    "discord_routes:stop_discord_bridge": ("post", "/api/discord/stop", 403),
    "telegram_routes:start_telegram_bridge": ("post", "/api/telegram/start", 403),
    "telegram_routes:stop_telegram_bridge": ("post", "/api/telegram/stop", 403),
    "whatsapp_routes:start_whatsapp_bridge": ("post", "/api/whatsapp/start", 200),
    "whatsapp_routes:stop_whatsapp_bridge": ("post", "/api/whatsapp/stop", 200),
    "whatsapp_routes:restart_whatsapp_bridge": ("post", "/api/whatsapp/restart", 200),
}


def test_every_declared_process_route_has_a_probe():
    assert set(PROCESS_PROBES) == set(PROCESS_RULES)
    assert all(PROCESS_PROBES[r][2] == (403 if kind == "admin" else 200) for r, kind in PROCESS_RULES.items())


@pytest.mark.parametrize("route", sorted(PROCESS_RULES))
def test_a_non_admin_drives_only_their_own_process(config_file, monkeypatch, route):
    """Every lifecycle function of every bridge is a recorder (nothing starts or stops for
    real). MUTATION: remove `Depends(require_admin)` from `stop_telegram_bridge`; make a
    WhatsApp route call `stop_bridge()` for everybody."""
    import importlib

    config_file.seed(whatsapp_config={"enabled": True},
                     connection_enabled_by_scope={ALICE["user_scope_id"]: {"whatsapp": True}})
    calls = []
    answers = {"start_account": (True, "started"), "restart_account": (True, "restarted"),
               "stop_account": True, "start_bridge": True, "restart_bridge": True,
               "restart_bridge_result": (True, "", []),
               "start_missing_accounts": [], "stop_bridge": None}
    for channel in ("whatsapp", "telegram", "discord"):
        module = importlib.import_module(f"vaf.api.{channel}_bridge")
        for name in BRIDGE_LIFECYCLE:
            monkeypatch.setattr(module, name,
                                lambda *a, _n=name, _c=channel, **k: calls.append((_c, _n) + a) or answers[_n],
                                raising=False)
        monkeypatch.setattr(module, "is_bridge_running", lambda: True, raising=False)
    method, path, expected = PROCESS_PROBES[route]
    res = getattr(_client(route.split(":", 1)[0]), method)(path)
    assert res.status_code == expected, (res.status_code, res.text)
    if PROCESS_RULES[route] == "admin":
        assert calls == []
        return
    assert calls, "the probe reached no lifecycle function"
    assert not [c for c in calls if c[1] in WHOLE_BRIDGE], calls
    own = {"start_account": ("alice", ALICE["user_scope_id"]), "restart_account": ("alice", ALICE["user_scope_id"]),
           "stop_account": ("alice", ALICE["user_scope_id"])}
    assert all(c[0] == "whatsapp" and c[2:] == own[c[1]] for c in calls), calls
