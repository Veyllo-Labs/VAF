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
  (a subscript store, `del`, `.pop` or `.setdefault` with the sub-key as a constant), or that
  names a lane function from the table's values, directly or through a helper in its module;
- `RULES`, the declared rule per route. A writer missing from it, or a declared route that no
  longer writes, fails;
- a probe per declared route: a non-admin drives it against another account's entry, through
  the real router and its dependencies, and that entry must be unchanged.

The rules: `admin` (only an admin may write), `own` (a non-admin writes their own entries and
never another account's), `own-lane` (a non-admin's write lands in a map only their own lane
reads), `cache` (a value that names no account).
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
    "telegram_routes:relay_whitelist_add": "own",
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
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("pop", "setdefault") and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value in keys):
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
        "@router.get('/w')\n"
        "async def harmless(request):\n"
        "    return {'whitelist': []}\n"
    )
    assert writer_routes(synthetic) == ["also", "pops", "sneaky"]


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


# route -> (seed blocks, method, path, body, the other accounts' entries)
PROBES = {
    "app_link_routes:switch_relay": (
        {"veyllo_app_config": {"enabled": True, "relay_enabled": True}},
        "put", "/api/app-link/relay", {"enabled": False},
        lambda c: (c.get("veyllo_app_config") or {}).get("relay_enabled")),
    "telegram_routes:whitelist_add": (
        {"telegram_config": {"verified": True, "whitelist": [TG_ADMIN]}},
        "post", "/api/telegram/whitelist-add", {"telegram_user_id": "4242"},
        lambda c: _tg(c).get("whitelist")),
    "telegram_routes:start_pairing": (
        {"telegram_config": {"bot_username": "shared_vaf_bot", "whitelist": [TG_ADMIN]}},
        "post", "/api/telegram/pair", None,
        lambda c: (_tg(c).get("bot_username"), _tg(c).get("whitelist"))),
    "telegram_routes:get_telegram_dashboard": (
        {"telegram_config": {"bot_username": "shared_vaf_bot", "whitelist": [TG_ADMIN]}},
        "get", "/api/telegram/dashboard", None,
        lambda c: (_tg(c).get("bot_username"), _tg(c).get("whitelist"))),
    "telegram_routes:relay_whitelist_add": (
        {"telegram_config": {"relay_whitelist": [TG_BOB_RELAY]}},
        "post", "/api/telegram/relay-whitelist-add", {"telegram_user_id": "777"},
        lambda c: [e for e in _tg(c).get("relay_whitelist") or [] if e.get("user_scope_id") != ALICE["user_scope_id"]]),
    "telegram_routes:relay_whitelist_remove": (
        {"telegram_config": {"relay_whitelist": [TG_BOB_RELAY]}},
        "post", "/api/telegram/relay-whitelist-remove", {"telegram_user_id": "777"},
        lambda c: _tg(c).get("relay_whitelist")),
    "whatsapp_routes:add_whitelist_entry": (
        {"whatsapp_config": {"enabled": True, "whitelist": [WA_ADMIN]}},
        "post", "/api/whatsapp/whitelist/add", {"phone_number": ADMIN_PHONE, "user_scope_id": ADMIN_SCOPE,
                                                "vaf_username": "admin"},
        lambda c: [e for e in _wa(c).get("whitelist") or [] if e.get("user_scope_id") != ALICE["user_scope_id"]]),
    "whatsapp_routes:remove_whitelist_entry": (
        {"whatsapp_config": {"enabled": True, "whitelist": [WA_ALICE_SAME, WA_ADMIN]}},
        "post", "/api/whatsapp/whitelist/remove", {"phone_number": ADMIN_PHONE},
        lambda c: [e for e in _wa(c).get("whitelist") or [] if e.get("user_scope_id") != ALICE["user_scope_id"]]),
    "whatsapp_routes:assign_lid_to_number": (
        {"whatsapp_config": {"enabled": True, "lid_to_e164": {"111111111111111@lid": "+491700000077"},
                             "lid_to_e164_by_scope": {BOB_SCOPE: {"222222222222222@lid": "+491700000088"}}}},
        "post", "/api/whatsapp/lid-assign", {"lid_jid": "111111111111111@lid", "phone_number": ADMIN_PHONE},
        lambda c: (_wa(c).get("lid_to_e164"), (_wa(c).get("lid_to_e164_by_scope") or {}).get(BOB_SCOPE))),
}


def test_every_declared_route_has_a_probe():
    assert set(PROBES) == set(RULES)


@pytest.mark.parametrize("route", sorted(RULES))
def test_a_non_admin_leaves_every_other_accounts_entry_alone(config_file, monkeypatch, route):
    """MUTATION: delete the non-admin check in `relay_whitelist_remove` (Bob's relay contact
    goes), or the non-admin lane in `assign_lid_to_number` (the shared map changes)."""
    monkeypatch.setattr("vaf.core.whatsapp_auth.get_linked_phone", lambda username: None)
    seed, method, path, body, others = PROBES[route]
    config_file.seed(**json.loads(json.dumps(seed)))
    before = json.dumps(others(config_file.on_disk()), sort_keys=True)
    assert before not in ("null", "[]", "[null, null]"), "the probe has nothing of another account to touch"
    client = _client(route.split(":", 1)[0])
    call = getattr(client, method)
    res = call(path, json=body) if body is not None else call(path)
    assert res.status_code < 500, res.text
    assert json.dumps(others(config_file.on_disk()), sort_keys=True) == before
