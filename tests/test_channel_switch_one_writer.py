# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An account's own channel switch has one writer, `Config.set_own_channel_switch`, the write
twin of the one reader, `messaging_connections.channel_enabled_for_scope`.

THE DEFECT, measured before this change: four hand-written merges stored into
`connection_enabled_by_scope` (the save step, both Telegram pairings and the Veyllo App
confirmation), and the WhatsApp session expiry wrote the local admin's switch by hand with
`Config.load()`/`Config.save()` outside `Config._locked()`. A fifth writer for the WhatsApp
switch route would have been one more copy to drift.

THE RULE: one function stores the switch, the AST guard below refuses a store anywhere else in
`vaf/`, and what the writer stores is what the reader reads back. The master gate of the
global WhatsApp switch is gone with its route checks and its worker probe.
"""
import ast
from pathlib import Path

import pytest

from vaf.core.config import Config
from vaf.core.messaging_connections import channel_enabled_for_scope
from vaf.core.platform import Platform

ROOT = Path(__file__).resolve().parents[1]
KEY = "connection_enabled_by_scope"
WRITER = "set_own_channel_switch"
# Builds the read view of one account (`out`, never the stored config): its own entry only.
READ_VIEW = ("config_for_user", "out")
MUTATORS = {"update", "setdefault", "pop", "popitem", "clear", "__setitem__", "__delitem__"}

ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
SECOND_ADMIN_SCOPE = "ab12cd34-0000-4000-8000-000000000002"
USER_SCOPE = "ab12cd34-0000-4000-8000-000000000001"


# -- AC4: one writer ------------------------------------------------------------------------------

def _mentions_key(node) -> bool:
    return any(isinstance(n, ast.Constant) and n.value == KEY for n in ast.walk(node))


def _root_name(node):
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def switch_stores(source: str):
    """(function, line) of every store into `connection_enabled_by_scope` outside the writer:
    a subscript store whose target names the key, a dict literal that carries it, a mutating
    call on it, and the same through a local name bound to anything that names the key (an
    alias such as `by_scope = config.get(KEY)`, mutated in place)."""
    found = []
    tree = ast.parse(source)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name == WRITER:
            continue
        aliases = set()
        grew = True
        while grew:
            grew = False
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and (_mentions_key(node.value) or
                                                     {_root_name(n) for n in ast.walk(node.value)} & aliases):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id not in aliases:
                            aliases.add(target.id)
                            grew = True

        def touches(target) -> bool:
            return _mentions_key(target) or _root_name(target) in aliases

        for node in ast.walk(fn):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
                targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
                for target in targets:
                    if not isinstance(target, ast.Subscript) or not touches(target):
                        continue
                    if (fn.name, _root_name(target)) == READ_VIEW:
                        continue
                    found.append((fn.name, node.lineno))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in MUTATORS and (touches(node.func.value) or any(
                        isinstance(a, ast.Dict) and _mentions_key(a) for a in node.args)):
                    found.append((fn.name, node.lineno))
            elif isinstance(node, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == KEY for k in node.keys):
                found.append((fn.name, node.lineno))
    return found


def test_one_writer_stores_the_switch():
    """MUTATION: put one pairing's hand merge back."""
    stores = {}
    writers = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if KEY not in source:
            continue
        rel = path.relative_to(ROOT).as_posix()
        found = switch_stores(source)
        if found:
            stores[rel] = found
        if f"def {WRITER}(" in source:
            writers.append(rel)
    assert stores == {}, "a store into the per-account switches outside Config.set_own_channel_switch"
    assert writers == ["vaf/core/config.py"]


def test_the_scanner_finds_every_shape_of_store():
    synthetic = (
        "def direct(config, scope):\n"
        "    config['connection_enabled_by_scope'] = {scope: {'telegram': True}}\n"
        "def alias(config, scope):\n"
        "    by_scope = config.get('connection_enabled_by_scope') or {}\n"
        "    mine = by_scope.get(scope) or {}\n"
        "    mine['telegram'] = True\n"
        "def setdefault(config, scope):\n"
        "    config.setdefault('connection_enabled_by_scope', {})[scope] = {}\n"
        "def literal(config):\n"
        "    return {**config, 'connection_enabled_by_scope': {}}\n"
        "def update(config):\n"
        "    config.update({'connection_enabled_by_scope': {}})\n"
        "def reader(config, scope):\n"
        "    by_scope = config.get('connection_enabled_by_scope') or {}\n"
        "    return by_scope.get(scope, {}).get('whatsapp', False)\n"
        "def set_own_channel_switch(config, scope, channel, on):\n"
        "    config['connection_enabled_by_scope'] = {scope: {channel: on}}\n"
        "def config_for_user(config, scope):\n"
        "    out = dict(config)\n"
        "    out['connection_enabled_by_scope'] = {}\n"
        "    return out\n"
    )
    assert sorted({name for name, _ in switch_stores(synthetic)}) == ["alias", "direct", "literal", "setdefault", "update"]


@pytest.fixture
def table_config(monkeypatch):
    state = {"local_admin_scope_id": ADMIN_SCOPE}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    return state


ROLES = {"local_admin": (ADMIN_SCOPE, True), "second_admin": (SECOND_ADMIN_SCOPE, True), "user": (USER_SCOPE, False)}


@pytest.mark.parametrize("on", [True, False])
@pytest.mark.parametrize("channel", ["whatsapp", "telegram", "veyllo_app"])
@pytest.mark.parametrize("role", sorted(ROLES))
def test_the_writer_mirrors_the_reader(table_config, role, channel, on):
    """What the writer stores, the reader reads back, the shared bot's own rule aside: on
    Telegram and the Veyllo App nothing is on while the bot is off, and an admin rides the bot
    whatever their own entry says. MUTATION: swap the writer's "each" branch to the per-scope
    map."""
    scope, admin = ROLES[role]
    state = table_config
    state[f"{channel}_config"] = {"enabled": True, "other": 1}
    # The opposite first (a change or not, as the block stands), so the next write is one.
    Config.set_own_channel_switch(state, scope, channel, not on)
    assert Config.set_own_channel_switch(state, scope, channel, on) is True
    assert Config.set_own_channel_switch(state, scope, channel, on) is False, "the same value again changes nothing"
    if channel == "whatsapp":
        expected = on
    elif role == "local_admin":
        expected = True             # the bot's own switch: the local admin rides it
    else:
        expected = on or admin
    assert channel_enabled_for_scope(channel, scope, admin=admin) is expected
    if channel == "whatsapp" and role == "local_admin":
        assert state["whatsapp_config"] == {"enabled": on, "other": 1}
        assert ADMIN_SCOPE not in (state.get(KEY) or {})
    else:
        assert state[KEY][scope] == {channel: on}
        assert state[f"{channel}_config"] == {"enabled": True, "other": 1}, "the global block is not theirs"

    # The bot off: nobody's shared lane is on, whatever the writer stored.
    if channel != "whatsapp":
        state[f"{channel}_config"] = {"enabled": False}
        assert channel_enabled_for_scope(channel, scope, admin=admin) is False


def test_the_writer_keeps_the_other_switches_and_refuses_no_account(table_config):
    state = table_config
    state[KEY] = {USER_SCOPE: {"telegram": True}, SECOND_ADMIN_SCOPE: {"whatsapp": True}}
    assert Config.set_own_channel_switch(state, f" {USER_SCOPE} ", "WhatsApp", True) is True
    assert state[KEY] == {USER_SCOPE: {"telegram": True, "whatsapp": True}, SECOND_ADMIN_SCOPE: {"whatsapp": True}}
    # The global block is created when it is absent.
    assert Config.set_own_channel_switch(state, ADMIN_SCOPE, "whatsapp", False) is True
    assert state["whatsapp_config"] == {"enabled": False}
    for scope, channel in (("", "whatsapp"), (None, "whatsapp"), ("  ", "telegram"), (USER_SCOPE, "signal")):
        with pytest.raises(ValueError):
            Config.set_own_channel_switch(state, scope, channel, True)


# -- the pairings write what they wrote --------------------------------------------------------

@pytest.fixture
def config_file(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(Config, "CONFIG_FILE", app / "config.json")
    monkeypatch.setattr(Config, "APP_DIR", app)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr("vaf.core.security_events.log_security_event", lambda *a, **k: None)
    Config.save({"local_admin_scope_id": ADMIN_SCOPE, "telegram_config": {"enabled": True},
                 KEY: {USER_SCOPE: {"whatsapp": True}}})
    return app


def test_the_pairings_write_what_they_wrote(config_file):
    """The Telegram owner and relay pairings and the Veyllo App confirmation turn on exactly
    the account's own switch on their channel, in the per-account map (both channels are a
    shared bot, the local admin's included), and keep every other entry."""
    from vaf.core.app_link import pairing as app_pairing
    from vaf.core.channel_pairing import pair_telegram_account, pair_telegram_relay

    assert pair_telegram_account("4242", "alice_tg", user_scope_id=USER_SCOPE, username="alice",
                                 may_take_over=False, switch_on=True) == "paired"
    stored = Config.load()[KEY]
    assert stored[USER_SCOPE] == {"whatsapp": True, "telegram": True}
    assert Config.load()["telegram_config"]["enabled"] is True

    # Nothing to change: no save at all.
    before = Config.CONFIG_FILE.stat().st_mtime_ns
    assert pair_telegram_account("4242", "alice_tg", user_scope_id=USER_SCOPE, username="alice",
                                 may_take_over=False, switch_on=True) == "unchanged"
    assert Config.CONFIG_FILE.stat().st_mtime_ns == before

    assert pair_telegram_relay("4343", None, user_scope_id=SECOND_ADMIN_SCOPE, username="bob",
                               may_take_over=False, paired_by="code", switch_on=True) == "paired"
    assert Config.load()[KEY][SECOND_ADMIN_SCOPE] == {"telegram": True}

    # The local admin's Telegram pairing: the per-account map, as before (the bot's switch is
    # the global one and stays as it is).
    assert pair_telegram_account("4444", None, user_scope_id=ADMIN_SCOPE, username="admin",
                                 may_take_over=False, switch_on=True) == "paired"
    assert Config.load()[KEY][ADMIN_SCOPE] == {"telegram": True}

    app_pairing._switch_lane_on(USER_SCOPE)
    assert Config.load()[KEY] == {USER_SCOPE: {"whatsapp": True, "telegram": True, "veyllo_app": True},
                                  SECOND_ADMIN_SCOPE: {"telegram": True}, ADMIN_SCOPE: {"telegram": True}}
    assert Config.load().get("whatsapp_config") is None, "no pairing writes the WhatsApp switch"


def test_the_save_step_writes_through_the_writer(table_config, monkeypatch):
    """`split_connection_toggles` records a non-admin's and a second admin's own switch through
    the writer and keeps the local admin's global one. MUTATION: drop the writer call."""
    calls = []
    real = Config.set_own_channel_switch.__func__
    monkeypatch.setattr(Config, "set_own_channel_switch",
                        classmethod(lambda cls, *a: calls.append(a[1:]) or real(cls, *a)))
    existing = {"whatsapp_config": {"enabled": True}, "telegram_config": {"enabled": True}}
    Config.split_connection_toggles(existing, {"whatsapp_config": {"enabled": False}}, USER_SCOPE, is_admin=False)
    Config.split_connection_toggles(existing, {"whatsapp_config": {"enabled": True}}, SECOND_ADMIN_SCOPE, is_admin=True)
    assert calls == [(USER_SCOPE, "whatsapp", False), (SECOND_ADMIN_SCOPE, "whatsapp", True)]
    assert existing[KEY] == {USER_SCOPE: {"whatsapp": False}, SECOND_ADMIN_SCOPE: {"whatsapp": True}}
    assert existing["whatsapp_config"] == {"enabled": True}


# -- AC13: the master gate is gone -----------------------------------------------------------------

def test_the_master_gate_is_gone():
    """MUTATION: restore `_global_switch_on`."""
    hits = []
    for path in sorted((ROOT / "vaf").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in ("_global_switch_on", "SWITCHED_OFF_FOR_ACCOUNT", "SWITCHED_OFF_FOR_ADMIN", "any_process_connected"):
            if needle in text:
                hits.append((path.relative_to(ROOT).as_posix(), needle))
    assert hits == []
    from vaf.api import whatsapp_bridge
    doc = " ".join((whatsapp_bridge.__doc__ or "").split())
    assert "own switch AND the master gate of every start" not in doc
    assert "NAMED BOUNDARY: the global" not in doc
    # The docs a stranger reads say it no more either.
    stale = ("is still the master switch of every start", "a second admin's Connections switch still stops",
             "and the master switch: while it is off", "bridge's master switch")
    said = [(path.relative_to(ROOT).as_posix(), phrase) for path in sorted((ROOT / "docs").rglob("*.md"))
            for phrase in stale if phrase in " ".join(path.read_text(encoding="utf-8").split())]
    assert said == []
