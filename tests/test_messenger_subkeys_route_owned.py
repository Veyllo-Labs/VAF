# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The pairings and the runtime lists inside the messenger blocks change only through their
routes and bridges, never through a settings save.

THE DEFECT, measured on both save lanes (PATCH /api/config and the real WebSocket
`save_config`), for the local admin and for a second admin: the Settings page sends its copy
of each whole messenger block, and the merge replaces a block whole. A page loaded before a
route removed a Telegram owner pairing, a relay contact, a WhatsApp number or a LID mapping,
or before the Veyllo relay was switched off, wrote the old value back on its next save: all
ten route-written sub-keys of the run came back. After a Telegram disconnect, a stale page's
save brought the block back with both whitelists.

THE RULE: `Config.ROUTE_OWNED_SUBKEYS` names those sub-keys, and the one strip both lanes run
(`Config.without_route_owned_keys(..., stored=...)`) puts each back to its stored value, or
leaves it out when the store does not hold it. The settings-owned fields of the same blocks
still save. Synthetic identities and a scratch config only.
"""
import copy
import inspect
import json
import os
import random
import threading
import time
from types import SimpleNamespace

import pytest

from vaf.core.config import Config

LOCAL_ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
SECOND_ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000bb"
TENANT_SCOPE = "ab12cd34-0000-4000-8000-000000000001"

LOCAL_ADMIN = {"role": "admin", "user_scope_id": LOCAL_ADMIN_SCOPE, "username": "admin"}
SECOND_ADMIN = {"role": "admin", "user_scope_id": SECOND_ADMIN_SCOPE, "username": "bob"}
NON_ADMIN = {"role": "user", "user_scope_id": TENANT_SCOPE, "username": "alice"}

# The table, written out here so the test does not follow a change of the constant.
GUARDED = {
    "telegram_config": ("whitelist", "relay_whitelist", "bot_username", "chat_activity"),
    "whatsapp_config": ("whitelist", "lid_to_e164", "owner_control", "chat_activity"),
    "discord_config": ("chat_activity",),
    "veyllo_app_config": ("relay_enabled", "relay_instance_id"),
}
PAIRS = [(block, sub) for block, subs in GUARDED.items() for sub in subs]

TG_OWNER = {"telegram_user_id": 123456789, "telegram_username": "alice_tg",
            "user_scope_id": LOCAL_ADMIN_SCOPE, "vaf_username": "admin"}
TG_REMOVED = {"telegram_user_id": 987654321, "telegram_username": "mallory_tg",
              "user_scope_id": TENANT_SCOPE, "vaf_username": "alice"}
WA_OWNER = {"phone_number": "+49123456789", "user_scope_id": LOCAL_ADMIN_SCOPE,
            "vaf_username": "admin"}
WA_REMOVED = {"phone_number": "+49987654321", "user_scope_id": TENANT_SCOPE,
              "vaf_username": "alice"}

# What the routes and the bridges hold after the page below was loaded.
STORED = {
    "local_admin_scope_id": LOCAL_ADMIN_SCOPE,
    "default_language": "en",
    "telegram_config": {
        "enabled": False, "verified": False, "bot_username": "vaf_new_bot",
        "whitelist": [TG_OWNER],
        "relay_whitelist": [{"telegram_user_id": 222222222, "label": "Carol"}],
        "chat_activity": [{"ts": "2026-11-02T10:00:00", "chat_id": "123456789", "text": "new"}],
    },
    "whatsapp_config": {
        "enabled": False, "inbound_to_agent": True, "reply_window_hours": 72,
        "chat_sync_interval_sec": 600,
        "whitelist": [WA_OWNER],
        "lid_to_e164": {"111111111@lid": "+49123456789"},
        "owner_control": {"+49123456789": {"paused": True}},
        "chat_activity": [{"ts": "2026-11-02T10:00:00", "chat_id": "+49123456789"}],
    },
    "discord_config": {
        "enabled": False, "verified": True, "admin_user_id": "555555555",
        "chat_activity": [{"ts": "2026-11-02T10:00:00", "channel_id": "777"}],
    },
    "veyllo_app_config": {
        "enabled": True, "relay_enabled": False, "relay_api_base": "https://veyllo.app",
        "relay_instance_id": "", "device_idle_days": 90,
    },
}

# The page as loaded before: every guarded sub-key holds its old value, and the
# settings-owned fields carry the changes this save is for.
PAGE = {
    "default_language": "de",
    "telegram_config": {
        "enabled": True, "verified": True, "bot_username": "vaf_old_bot",
        "whitelist": [TG_OWNER, TG_REMOVED],
        "relay_whitelist": [{"telegram_user_id": 222222222, "label": "Carol"},
                            {"telegram_user_id": 333333333, "label": "Dave"}],
        "chat_activity": [{"ts": "2026-11-01T09:00:00", "chat_id": "123456789", "text": "old"}],
    },
    "whatsapp_config": {
        "enabled": True, "inbound_to_agent": False, "reply_window_hours": 24,
        "chat_sync_interval_sec": 600,
        "whitelist": [WA_OWNER, WA_REMOVED],
        "lid_to_e164": {"111111111@lid": "+49123456789", "222222222@lid": "+49987654321"},
        "owner_control": {"+49123456789": {"paused": False}},
        "chat_activity": [{"ts": "2026-11-01T09:00:00", "chat_id": "+49123456789"}],
    },
    "discord_config": {
        "enabled": True, "verified": True, "admin_user_id": "666666666",
        "chat_activity": [{"ts": "2026-11-01T09:00:00", "channel_id": "777"}],
    },
    "veyllo_app_config": {
        "enabled": True, "relay_enabled": True, "relay_api_base": "https://relay.example.org",
        "relay_instance_id": "inst-old", "device_idle_days": 30,
    },
}


def test_the_fixture_differs_in_every_guarded_pair():
    """Otherwise a case below could pass without the strip doing anything."""
    for block, sub in PAIRS:
        assert PAGE[block][sub] != STORED[block][sub], (block, sub)


@pytest.fixture
def stored(tmp_path, monkeypatch):
    """A synthetic config file Config reads and writes; returns a reader of the raw file."""
    from vaf.core import migrations, security_events

    path = tmp_path / "config.json"
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "_filelock", None)
    # The Discord admin change of the save is recorded as a security event; not here.
    monkeypatch.setattr(security_events, "log_security_event", lambda *a, **k: None)

    def write(cfg: dict):
        path.write_text(json.dumps({"config_format_version": migrations.CONFIG_FORMAT_VERSION,
                                    **cfg}), encoding="utf-8")

    def raw() -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    write(copy.deepcopy(STORED))
    return SimpleNamespace(write=write, raw=raw, path=path)


def _patch(body: dict, user: dict) -> dict:
    import asyncio

    from vaf.api import config_routes
    request = SimpleNamespace(state=SimpleNamespace(user=user))
    return asyncio.run(config_routes.patch_config(copy.deepcopy(body), request, user))


def _ws_save(monkeypatch, user: dict, body: dict) -> dict:
    """One `save_config` over a real `/ws` connection with a signed token; the answer."""
    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    from vaf.core.web_server import app

    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    token = jwt.encode({"sub": "7", "user_scope_id": user["user_scope_id"],
                        "username": user["username"], "role": user["role"]},
                       "s" * 32, algorithm="HS256")
    box = {}
    with TestClient(app, client=("127.0.0.1", 40000)).websocket_connect(f"/ws?token={token}") as ws:
        ws.send_text(json.dumps({"type": "save_config", "config": body}))

        def pump():
            try:
                for _ in range(100):
                    msg = ws.receive_json()
                    if msg.get("type") == "config_saved":
                        box["msg"] = msg
                        return
            except Exception as e:  # noqa: BLE001 - reported by the assert below
                box["error"] = e

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        reader.join(20)
    assert "msg" in box, f"no config_saved ({box.get('error')!r})"
    assert box["msg"].get("status") == "success", box["msg"]
    return box["msg"]


def _save(lane: str, monkeypatch, user: dict, body: dict) -> None:
    if lane == "patch":
        _patch(body, user)
    else:
        _ws_save(monkeypatch, user, body)


def _canon(value) -> str:
    """Equal values of equal types (``true`` is not ``1`` here)."""
    return json.dumps(value, sort_keys=True)


def test_the_table_holds_the_measured_sub_keys():
    """MUTATION: drop `owner_control` from the table."""
    table = Config.ROUTE_OWNED_SUBKEYS
    assert {(b, s) for b, subs in table.items() for s in subs} == set(PAIRS)
    for block, subs in table.items():
        for sub, lane in subs.items():
            assert isinstance(lane, str) and lane.strip(), (block, sub)


def test_the_strip_needs_the_stored_config():
    """A forgotten `stored` must fail loudly: empty would wipe the sub-keys, skipping would
    keep the defect. MUTATION: default `stored=None`."""
    with pytest.raises(TypeError):
        Config.without_route_owned_keys({"theme": "dark"})


def test_both_lanes_pass_the_config_they_loaded():
    """By source, before the shared step. MUTATION: either lane back to the one-argument
    call, or the call moved behind `split_connection_toggles`."""
    from vaf.api import config_routes
    from vaf.core import web_server

    patch_src = inspect.getsource(config_routes.patch_config)
    patch_src = patch_src[:patch_src.index("Config.split_connection_toggles(")]
    assert "body = Config.without_route_owned_keys(body, stored=current)" in patch_src
    assert "current = Config.load()" in patch_src
    source = inspect.getsource(web_server)
    lane = source[source.index('elif type == "save_config":'):]
    lane = lane[:lane.index("Config.split_connection_toggles")]
    assert "new_config = Config.without_route_owned_keys(new_config, stored=existing)" in lane
    assert "existing = Config.load()" in lane


@pytest.mark.parametrize("lane", ["patch", "ws"])
@pytest.mark.parametrize("user", [LOCAL_ADMIN, SECOND_ADMIN, NON_ADMIN],
                         ids=["local_admin", "second_admin", "non_admin"])
def test_a_stale_page_does_not_bring_back_a_route_written_sub_key(stored, monkeypatch, lane,
                                                                  user):
    """MUTATIONS: the nested loop out of the strip (every admin case red); the WebSocket lane
    passing `stored={}` (ws cases red, the sub-keys are wiped); the PATCH lane passing
    `stored={}` (patch cases red); one pair out of the table (red, naming it); the strip
    dropping or restoring the whole block (the settings-owned fields below go red)."""
    _save(lane, monkeypatch, user, PAGE)
    raw = stored.raw()
    for block, sub in PAIRS:
        assert _canon(raw[block].get(sub)) == _canon(STORED[block][sub]), (block, sub)
    assert raw["default_language"] == "de"

    if user is NON_ADMIN:
        # Unchanged: a non-admin's save never carried a messenger block. Only their own
        # switches are recorded.
        for block in GUARDED:
            assert _canon(raw[block]) == _canon(STORED[block]), block
        assert set(raw["connection_enabled_by_scope"]) == {TENANT_SCOPE}
        assert raw["connection_enabled_by_scope"][TENANT_SCOPE]["telegram"] is True
        return

    # The settings-owned fields of the same blocks still save.
    tg, wa, dc, va = (raw["telegram_config"], raw["whatsapp_config"], raw["discord_config"],
                      raw["veyllo_app_config"])
    assert tg["verified"] is True and tg["enabled"] is True
    assert wa["inbound_to_agent"] is False and wa["reply_window_hours"] == 24
    assert dc["admin_user_id"] == "666666666" and dc["enabled"] is True
    assert va["relay_api_base"] == "https://relay.example.org"
    assert va["device_idle_days"] == 30
    if user is LOCAL_ADMIN:
        assert wa["enabled"] is True
    else:
        # A second admin's WhatsApp switch is their own (split_connection_toggles).
        assert wa["enabled"] is False
        assert raw["connection_enabled_by_scope"][SECOND_ADMIN_SCOPE]["whatsapp"] is True


@pytest.mark.parametrize("lane", ["patch", "ws"])
def test_a_dashboard_mirror_entry_does_not_replace_the_stored_entry(stored, monkeypatch, lane):
    """The dashboards mirror a route write into the page with a reduced entry (no
    `user_scope_id`); the next Save used to store it over the route's. MUTATION: the nested
    loop out of the strip."""
    body = copy.deepcopy(PAGE)
    body["whatsapp_config"]["whitelist"] = [{"phone_number": "+49123456789",
                                             "vaf_username": None}]
    body["telegram_config"]["whitelist"] = [{"telegram_user_id": 123456789,
                                             "telegram_username": "alice_tg"}]
    _save(lane, monkeypatch, LOCAL_ADMIN, body)
    raw = stored.raw()
    assert raw["whatsapp_config"]["whitelist"] == [WA_OWNER]
    assert raw["telegram_config"]["whitelist"] == [TG_OWNER]
    assert raw["whatsapp_config"]["whitelist"][0]["user_scope_id"] == LOCAL_ADMIN_SCOPE


@pytest.mark.parametrize("lane", ["patch", "ws"])
@pytest.mark.parametrize("case", ["telegram_disconnected", "relay_never_claimed",
                                  "whatsapp_never_set_up"])
def test_a_sub_key_the_store_does_not_hold_is_not_created(stored, monkeypatch, lane, case):
    """A disconnected or never-set-up block gets none of its runtime lists from a save.
    MUTATION: keep the sent value when the held block lacks the sub-key."""
    cfg = copy.deepcopy(STORED)
    if case == "telegram_disconnected":
        cfg["telegram_config"] = None
        block = "telegram_config"
    elif case == "relay_never_claimed":
        del cfg["veyllo_app_config"]["relay_instance_id"]
        block = "veyllo_app_config"
    else:
        del cfg["whatsapp_config"]
        block = "whatsapp_config"
    stored.write(cfg)
    _save(lane, monkeypatch, LOCAL_ADMIN, PAGE)
    raw = stored.raw()
    saved = raw[block]
    assert isinstance(saved, dict), saved
    held = cfg.get(block) if isinstance(cfg.get(block), dict) else {}
    for sub in GUARDED[block]:
        if sub in held:
            assert _canon(saved[sub]) == _canon(held[sub]), sub
        else:
            assert sub not in saved, sub
    assert raw["default_language"] == "de"


def test_the_strip_keeps_its_invariants():
    """MUTATIONS: change `payload[block]` in place (the snapshots go red); return a copy
    every time (the identity checks go red)."""
    payload = copy.deepcopy(PAGE)
    payload["email_config"] = {"accounts": []}
    payload["whatsapp_config"]["Whitelist"] = ["variant"]
    payload["whatsapp_config"]["whitelist "] = ["variant 2"]
    held = copy.deepcopy(STORED)
    p_snap, h_snap = _canon(payload), _canon(held)
    out = Config.without_route_owned_keys(payload, stored=held)
    assert _canon(payload) == p_snap and _canon(held) == h_snap
    assert "email_config" not in out
    assert out["whatsapp_config"]["Whitelist"] == ["variant"]
    assert out["whatsapp_config"]["whitelist "] == ["variant 2"]
    assert out["whatsapp_config"]["whitelist"] == STORED["whatsapp_config"]["whitelist"]
    assert out["whatsapp_config"]["whitelist"] is not held["whatsapp_config"]["whitelist"]

    unchanged = {"theme": "dark", "telegram_config": copy.deepcopy(STORED["telegram_config"])}
    assert Config.without_route_owned_keys(unchanged, stored=held) is unchanged
    for value in (None, [], "", 0, False, "text"):
        body = {"telegram_config": value, "theme": "dark"}
        assert Config.without_route_owned_keys(body, stored=held) is body

    # Equal by `==` is not the stored value: a held 1 stays 1 under a sent True. MUTATION:
    # compare the restored block with `==`.
    body = {"veyllo_app_config": {"relay_enabled": True}}
    out = Config.without_route_owned_keys(body, stored={"veyllo_app_config": {"relay_enabled": 1}})
    assert _canon(out["veyllo_app_config"]["relay_enabled"]) == "1"


# --- Fuzz ---------------------------------------------------------------------------------

_SETTINGS_SUBKEYS = ("enabled", "verified", "inbound_to_agent", "relay_api_base",
                     "admin_user_id", "device_idle_days")
_VARIANTS = (
    lambda s: s.capitalize(),
    lambda s: s.upper(),
    lambda s: s + " ",
    lambda s: " " + s,
    lambda s: s.replace("i", "\u0456", 1),  # Cyrillic i
    lambda s: chr(ord(s[0]) + 0xFEE0) + s[1:],  # fullwidth first letter
    lambda s: s[:2] + "\u200d" + s[2:],  # zero-width joiner
)
_NON_DICTS = ([], "", 0, False, "text", 1.5, ["whitelist"])


def _value(rng: random.Random, depth: int = 0):
    kind = rng.randrange(8 if depth < 4 else 5)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice((True, False))
    if kind == 2:
        return rng.choice((0, 1, -3, 123456789))
    if kind == 3:
        return rng.choice((0.0, 1.0, 2.5))
    if kind == 4:
        return rng.choice(("", "a", "+49123456789", "inst-1"))
    if kind in (5, 6):
        return [_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {rng.choice(("a", "b", "user_scope_id", "1")): _value(rng, depth + 1)
            for _ in range(rng.randrange(4))}


def _block(rng: random.Random, name: str):
    kind = rng.randrange(6)
    if kind == 0:
        return "absent"
    if kind == 1:
        return None
    if kind == 2:
        return rng.choice(_NON_DICTS)
    block = {}
    for sub in GUARDED[name]:
        if rng.random() < 0.6:
            block[sub] = _value(rng)
    for sub in rng.sample(_SETTINGS_SUBKEYS, rng.randrange(3)):
        block[sub] = _value(rng)
    for _ in range(rng.randrange(3)):
        block[rng.choice(_VARIANTS)(rng.choice(GUARDED[name]))] = _value(rng)
    for _ in range(rng.randrange(2)):
        block[rng.choice(("extra", "x", "note"))] = _value(rng)
    return block


def _config(rng: random.Random) -> dict:
    cfg = {}
    for name in GUARDED:
        drawn = _block(rng, name)
        if drawn != "absent":
            cfg[name] = drawn
    if rng.random() < 0.3:
        cfg["email_config"] = _value(rng)
    if rng.random() < 0.3:
        cfg["vllm_gpu_memory_utilization"] = _value(rng)
    for key in rng.sample(("theme", "default_language", "tts_auto_speak"), rng.randrange(3)):
        cfg[key] = _value(rng)
    return cfg


def _check_one(payload: dict, stored_cfg: dict) -> None:
    """The oracle, written from the rule rather than from the code."""
    p_snap, s_snap = _canon(payload), _canon(stored_cfg)
    out = Config.without_route_owned_keys(payload, stored=stored_cfg)
    changed = False
    for key in payload:
        if key in Config.ROUTE_OWNED_KEYS or key.startswith("vllm_"):
            assert key not in out, key
            changed = True
        elif key in GUARDED and isinstance(payload[key], dict):
            sent = payload[key]
            held = stored_cfg.get(key) if isinstance(stored_cfg.get(key), dict) else {}
            want = {k: v for k, v in sent.items() if k not in GUARDED[key]}
            want.update({s: held[s] for s in GUARDED[key] if s in held})
            assert _canon(out[key]) == _canon(want), (key, sent, held)
            changed = changed or _canon(want) != _canon(sent)
        elif key in GUARDED:
            assert out[key] is payload[key], key
        else:
            assert _canon(out[key]) == _canon(payload[key]), key
    assert set(out) == {k for k in payload
                        if not (k in Config.ROUTE_OWNED_KEYS or k.startswith("vllm_"))}
    assert _canon(payload) == p_snap and _canon(stored_cfg) == s_snap
    assert (out is payload) == (not changed)

    merged = Config.merge_preserving_nonempty_sensitive(stored_cfg, out)
    for block, subs in GUARDED.items():
        if block not in payload or not (payload[block] is None or isinstance(payload[block], dict)):
            continue
        held = stored_cfg.get(block) if isinstance(stored_cfg.get(block), dict) else {}
        got = merged.get(block) if isinstance(merged.get(block), dict) else {}
        for sub in subs:
            assert (sub in got) == (sub in held), (block, sub)
            if sub in held:
                assert _canon(got[sub]) == _canon(held[sub]), (block, sub)


def test_the_strip_fuzz():
    """Seeded; `VAF_ROUTE_OWNED_FUZZ_DRAWS` raises the count outside the suite."""
    draws = int(os.environ.get("VAF_ROUTE_OWNED_FUZZ_DRAWS", "400"))
    rng = random.Random(412)
    for _ in range(draws):
        _check_one(_config(rng), _config(rng))


def test_a_large_whitelist_strips_in_linear_time():
    """One shallow copy of the block and one deep copy per held sub-key."""
    big = [{"phone_number": f"+49{i:09d}", "user_scope_id": TENANT_SCOPE} for i in range(5000)]
    payload = {"whatsapp_config": {"enabled": True, "whitelist": big + [WA_REMOVED]}}
    held = {"whatsapp_config": {"enabled": False, "whitelist": copy.deepcopy(big)}}
    started = time.perf_counter()
    out = Config.without_route_owned_keys(payload, stored=held)
    assert time.perf_counter() - started < 1.0
    assert out["whatsapp_config"]["whitelist"] == big
