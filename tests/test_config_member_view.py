# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An account without admin rights reads only its own connection data with its settings.

THE DEFECT, measured through `Config.config_for_user` and the real `GET /api/config`: the
non-admin view spread the stored WhatsApp block and replaced only its number list, so every
signed-in account received the map from WhatsApp IDs to phone numbers, the owner-control map
keyed by number and the latest chat events of every account with their scopes. The Slack
block, which has no view of its own, went out whole, and so did the local admin's scope id.
A number list that was not a list (a hand-edited `config.json`) failed every non-admin's
config read with a 500.

THE RULE: every messenger block is built from an allowlist, a registry channel without a
view of its own reads as the account's own switch, and `local_admin_scope_id` stays with the
admins. The drift guard below fails when a key reaches a non-admin as stored without a
reviewed reason: a structured key, a key that defaults to None, a key named like one
account's id, and a key the code reads or writes by name without a `DEFAULTS` row. Synthetic identities and an in-memory config only.
"""
import ast
import asyncio
import copy
import functools
import json
import random
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf.core.config as config_mod
from test_sensitive_config_keys_admin_only import _keys_the_code_reads
from vaf.core.channels import KNOWN_CHANNELS
from vaf.core.config import Config

ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000ad"
SECOND_ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000ae"
ALICE_SCOPE = "ab12cd34-0000-4000-8000-0000000000a1"
BOB_SCOPE = "ab12cd34-0000-4000-8000-0000000000b0"

ADMIN_PHONE = "+491110000001"
ALICE_PHONE = "+491110000002"
BOB_PHONE = "+491110000003"

ALICE = {"role": "user", "user_scope_id": ALICE_SCOPE, "username": "alice"}
LOCAL_ADMIN = {"role": "admin", "user_scope_id": ADMIN_SCOPE, "username": "admin"}
SECOND_ADMIN = {"role": "admin", "user_scope_id": SECOND_ADMIN_SCOPE, "username": "carla"}

WA_ADMIN = {"phone_number": ADMIN_PHONE, "user_scope_id": ADMIN_SCOPE, "vaf_username": "admin"}
WA_ALICE = {"phone_number": ALICE_PHONE, "user_scope_id": ALICE_SCOPE, "vaf_username": "alice"}
WA_BOB = {"phone_number": BOB_PHONE, "user_scope_id": BOB_SCOPE, "vaf_username": "bob"}

# The channels whose non-admin view is written out in `config_for_user`, with the fields
# each may carry. Every other registry channel reads as its own switch.
VIEW_FIELDS = {
    "whatsapp": {"enabled", "whitelist"},
    "telegram": {"enabled", "verified", "bot_username", "whitelist"},
    "discord": {"enabled", "verified", "configured", "chat_activity"},
    "veyllo_app": {"enabled", "relay_enabled"},
}
UNBUILT = [c for c in KNOWN_CHANNELS if c not in VIEW_FIELDS]


def _rich() -> dict:
    """Every block and every per-account map, holding Alice's data, Bob's and the admin's."""
    return {
        "local_admin_scope_id": ADMIN_SCOPE,
        "local_admin_username": "admin",
        "default_language": "en",
        "api_key_openai": "sk-test-not-a-real-key",
        "connection_enabled_by_scope": {
            ALICE_SCOPE: {"whatsapp": True, "telegram": True, "slack": True},
            BOB_SCOPE: {"whatsapp": True, "discord": True, "veyllo_app": True},
        },
        "whatsapp_config": {
            "enabled": True, "inbound_to_agent": True, "reply_window_hours": 72,
            "chat_sync_interval_sec": 600,
            "whitelist": [WA_ADMIN, WA_ALICE, WA_BOB],
            "lid_to_e164": {"111111111@lid": ADMIN_PHONE, "333333333@lid": BOB_PHONE},
            "owner_control": {ADMIN_PHONE: {"paused": True}, BOB_PHONE: {"paused": False}},
            "chat_activity": [
                {"ts": "2026-11-02T10:00:00", "chat_id": ADMIN_PHONE, "user_scope_id": ADMIN_SCOPE},
                {"ts": "2026-11-02T10:01:00", "chat_id": BOB_PHONE, "user_scope_id": BOB_SCOPE},
            ],
            "zz_planted_sub_key": "planted-whatsapp-value",
        },
        "telegram_config": {
            "enabled": True, "verified": True, "bot_username": "shared_vaf_bot",
            "whitelist": [
                {"telegram_user_id": 111222333, "user_scope_id": ADMIN_SCOPE, "vaf_username": "admin"},
                {"telegram_user_id": 333222111, "user_scope_id": BOB_SCOPE, "vaf_username": "bob"},
            ],
            "relay_whitelist": [{"telegram_user_id": 444555666, "label": "Carol",
                                 "user_scope_id": BOB_SCOPE}],
            "chat_activity": [{"ts": "2026-11-02T10:00:00", "chat_id": "111222333"}],
        },
        "discord_config": {
            "enabled": True, "verified": True, "admin_user_id": "555666777888",
            "admin_username": "admin_on_discord",
            "chat_activity": [{"ts": "2026-11-02T10:00:00", "channel_id": "999888777"}],
        },
        "veyllo_app_config": {
            "enabled": True, "relay_enabled": False, "relay_api_base": "https://relay.example.org",
            "relay_instance_id": "inst-ab12cd34", "device_idle_days": 90,
        },
        "slack_config": {"enabled": True, "verified": True, "user_scope_id": BOB_SCOPE,
                         "slack_user_id": "U0BOBSLACK"},
        "email_config": {"accounts": [{"email": "admin@example.org"}]},
        "email_config_by_scope": {
            ALICE_SCOPE: {"accounts": [{"email": "alice@example.org"}]},
            BOB_SCOPE: {"accounts": [{"email": "bob@example.org"}]},
        },
        "email_config_by_user": {"bob": {"accounts": [{"email": "bob-legacy@example.org"}]}},
        "github_config": {"accounts": [{"login": "admin-gh-login"}]},
        "github_config_by_user": {
            "alice": {"accounts": [{"login": "alice-gh-login", "allow_write": False}]},
            "bob": {"accounts": [{"login": "bob-gh-login", "allow_write": True}]},
        },
        "cloud_config": {"accounts": [{"email": "admin-cloud@example.org"}]},
        "cloud_config_by_user": {
            "alice": {"accounts": [{"email": "alice-cloud@example.org", "user_scope_id": ALICE_SCOPE}]},
            "bob": {"accounts": [{"email": "bob-cloud@example.org", "user_scope_id": BOB_SCOPE}]},
        },
    }


# Every identifier in `_rich` that is not Alice's own.
FOREIGN = (
    ADMIN_SCOPE, BOB_SCOPE, ADMIN_PHONE, BOB_PHONE, "111111111@lid", "333333333@lid",
    "111222333", "333222111", "444555666", "Carol", "555666777888", "admin_on_discord",
    "999888777", "relay.example.org", "inst-ab12cd34", "U0BOBSLACK", "admin@example.org",
    "bob@example.org", "bob-legacy@example.org", "admin-gh-login", "bob-gh-login",
    "admin-cloud@example.org", "bob-cloud@example.org", "sk-test-not-a-real-key",
    "planted-whatsapp-value",
)
OWN = (ALICE_SCOPE, ALICE_PHONE, "alice@example.org", "alice-gh-login", "alice-cloud@example.org")


@pytest.fixture()
def state(monkeypatch):
    """Config on an in-memory dict, so no test run can touch a real config.json."""
    data = _rich()
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: copy.deepcopy(data)))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, cfg: (data.clear(), data.update(cfg))[0]))
    return data


def _request(user: dict):
    return SimpleNamespace(state=SimpleNamespace(user=dict(user)))


def _get(user: dict) -> dict:
    from vaf.api.config_routes import get_config
    return asyncio.run(get_config(_request(user)))


def _patch(body: dict, user: dict) -> dict:
    from vaf.api.config_routes import get_current_user_or_local_admin, patch_config
    request = _request(user)
    return asyncio.run(patch_config(body, request, get_current_user_or_local_admin(request)))


def _view(cfg: dict, user: dict = ALICE) -> dict:
    return Config.config_for_user(copy.deepcopy(cfg), user["user_scope_id"], user["role"],
                                  username=user["username"])


def _views(state) -> dict:
    """Alice's view through all three emits: the function, GET and the PATCH response."""
    return {
        "function": _view(state),
        "GET /api/config": _get(ALICE),
        "PATCH /api/config": _patch({"default_language": "de"}, ALICE),
    }


# ── WhatsApp ───────────────────────────────────────────────────────────────────────────

def test_a_non_admin_reads_only_their_switch_and_number_on_whatsapp(state):
    for emit, view in _views(state).items():
        assert view["whatsapp_config"] == {"enabled": True, "whitelist": [WA_ALICE]}, emit


def _app_for(user: dict):
    from fastapi import FastAPI

    from vaf.api.config_routes import router

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def _signed_in(request, call_next):
        request.state.user = dict(user)
        return await call_next(request)

    return app


@pytest.mark.parametrize("whitelist, expected", [
    (5, []),
    ("abc", []),
    ({"a": 1}, []),
    (True, []),
    (None, []),
    ([1, "x", None, WA_ALICE, WA_BOB], [WA_ALICE]),
])
def test_a_whitelist_that_is_not_a_list_reads_empty(state, whitelist, expected):
    from fastapi.testclient import TestClient

    state["whatsapp_config"]["whitelist"] = whitelist
    client = TestClient(_app_for(ALICE), raise_server_exceptions=False)
    response = client.get("/api/config")
    assert response.status_code == 200, response.text[:200]
    assert response.json()["whatsapp_config"]["whitelist"] == expected


# ── every other registry channel ───────────────────────────────────────────────────────

@pytest.mark.parametrize("channel", UNBUILT + ["signal"])
def test_every_registry_channel_without_a_view_reads_as_its_own_switch(state, monkeypatch, channel):
    if channel not in KNOWN_CHANNELS:
        # A channel added to the registry later.
        monkeypatch.setattr(config_mod, "_KNOWN_CHANNELS", KNOWN_CHANNELS + (channel,))
    key = f"{channel}_config"
    planted = {"enabled": True, "verified": True, "user_scope_id": BOB_SCOPE, "token_hint": "zz"}

    state[key] = planted
    state["connection_enabled_by_scope"][ALICE_SCOPE][channel] = True
    assert _view(state)[key] == {"enabled": True}
    state["connection_enabled_by_scope"][ALICE_SCOPE][channel] = False
    assert _view(state)[key] == {"enabled": False}

    for not_a_block in (None, [planted], "abc", 7):
        state[key] = not_a_block
        assert _view(state)[key] is None, not_a_block

    del state[key]
    assert key not in _view(state)


# ── what stays as it was ───────────────────────────────────────────────────────────────

def test_the_other_blocks_keep_their_view(state):
    view = _view(state)
    assert view["telegram_config"] == {"enabled": True, "verified": True,
                                       "bot_username": "shared_vaf_bot", "whitelist": []}
    assert view["discord_config"] == {"enabled": False, "verified": True, "configured": True,
                                      "chat_activity": []}
    assert view["veyllo_app_config"] == {"enabled": False, "relay_enabled": False}
    assert view["email_config_by_scope"] == {ALICE_SCOPE: {"accounts": [{"email": "alice@example.org"}]}}
    assert view["email_config"] is None and view["email_config_by_user"] == {}
    assert view["github_config"] is None
    assert view["github_config_by_user"] == {
        "alice": {"accounts": [{"login": "alice-gh-login", "allow_write": False}]}}
    assert view["cloud_config"] is None
    assert view["cloud_config_by_user"] == {
        "alice": {"accounts": [{"email": "alice-cloud@example.org", "user_scope_id": ALICE_SCOPE}]}}
    assert view["connection_enabled_by_scope"] == {
        ALICE_SCOPE: {"whatsapp": True, "telegram": True, "slack": True}}
    assert "api_key_openai" not in view


def test_the_local_admin_scope_is_not_sent_to_a_non_admin(state):
    state["vaf_home"] = "/home/user/.vaf"
    for emit, view in _views(state).items():
        assert "local_admin_scope_id" not in view, emit
        # A host path, which may carry the machine owner's home folder name.
        assert "vaf_home" not in view, emit
        # The Connections page compares it with the signed-in name to pick the admin's lists.
        assert view["local_admin_username"] == "admin", emit


def test_admin_views_are_unchanged(state):
    state["vaf_home"] = "/home/user/.vaf"
    stored = _rich()
    for admin in (LOCAL_ADMIN, SECOND_ADMIN):
        for view in (_view(state, admin), _get(admin)):
            wa = view["whatsapp_config"]
            for sub in ("whitelist", "lid_to_e164", "owner_control", "chat_activity",
                        "reply_window_hours", "inbound_to_agent", "zz_planted_sub_key"):
                assert wa[sub] == stored["whatsapp_config"][sub], (admin["username"], sub)
            assert {k: v for k, v in view["slack_config"].items() if k != "enabled"} == {
                k: v for k, v in stored["slack_config"].items() if k != "enabled"}
            assert view["telegram_config"]["relay_whitelist"] == stored["telegram_config"]["relay_whitelist"]
            assert view["local_admin_scope_id"] == ADMIN_SCOPE
            assert view["vaf_home"] == "/home/user/.vaf"
            assert view["email_config_by_scope"] == stored["email_config_by_scope"]
            assert view["api_key_openai"] == "", "a secret value reached an admin's browser"
    # The global switch is the local admin's; a second admin sees their own.
    assert _view(state, LOCAL_ADMIN)["whatsapp_config"]["enabled"] is True
    assert _view(state, SECOND_ADMIN)["whatsapp_config"]["enabled"] is False


# ── the sweep ──────────────────────────────────────────────────────────────────────────

def test_no_other_accounts_data_reaches_a_non_admin(state):
    for emit, view in _views(state).items():
        text = json.dumps(view)
        leaked = [f for f in FOREIGN if f in text]
        assert not leaked, f"{emit} carries another account's data: {leaked}"
        missing = [o for o in OWN if o not in text]
        assert not missing, f"{emit} lost the account's own data: {missing}"


# Keys that reach a non-admin as stored: installation settings, one value for the whole
# installation, keyed by no account and naming none. Exact names, never a prefix: a prefix
# would admit the next key under it unreviewed.
_MODEL = "how the installation's model lane runs, one value for every account"
_ATTACHMENTS = "the attachment index's tuning, one value for every account"
_RESEARCH = "a run-time bound of the research lane, one value for every account"
_SERVER = "the local model server of this machine"
_GUARD = "an agent-loop guard read for every account's turns"
_THINKING = "the thinking-mode loop, one for the installation"
REVIEWED_INSTALLATION_SETTINGS = {
    "channel_ingress_policy": "which channel doors the Front Office opens; the Connections page renders it",
    "failover_triggers": "the error classes that switch to the fallback provider",
    "local_network_allowed_networks": "the networks the local-network listener admits",
    "sandbox_env_registry_hosts": "the image registries a sandbox environment may pull from",
    "voice_awareness_topics": "the topics the voice agent listens for",
    "trusted_sources_custom": "the web search's added trusted sites, one list for every account",
    "trusted_sources_disabled": "the web search's switched-off trusted sites, one list for every account",
    "email_agent_trusted_sender_domains": "mail domains the agent trusts, one list for every account",
    "anthropic_prompt_cache": _MODEL,
    "anthropic_thinking": _MODEL,
    "google_thinking": _MODEL,
    "deepseek_reasoning_effort": _MODEL,
    "temperature_auto": _MODEL,
    "model_name": _MODEL,
    "thinking_model": _MODEL,
    "attachment_rag_hybrid_enabled": _ATTACHMENTS,
    "attachment_rag_hybrid_lexical_k": _ATTACHMENTS,
    "attachment_rag_hybrid_lexical_scan_limit": _ATTACHMENTS,
    "attachment_rag_hybrid_rrf_k": _ATTACHMENTS,
    "attachment_rag_hybrid_vector_k": _ATTACHMENTS,
    "attachment_rag_op_timeout_sec": _ATTACHMENTS,
    "attachment_rag_safe_mode": _ATTACHMENTS,
    "attachment_rag_sync_timeout_sec": _ATTACHMENTS,
    "attachment_rag_vector_coalesce_debounce_ms": _ATTACHMENTS,
    "attachment_rag_vector_coalesce_enabled": _ATTACHMENTS,
    "attachment_rag_vector_max_index_ops_per_window": _ATTACHMENTS,
    "attachment_rag_vector_max_search_ops_per_window": _ATTACHMENTS,
    "attachment_rag_vector_rate_window_sec": _ATTACHMENTS,
    "attachment_rag_vector_search_rate_window_sec": _ATTACHMENTS,
    "attachment_rag_vector_unload_model_after_op": _ATTACHMENTS,
    "research_overall_timeout_seconds": _RESEARCH,
    "research_section_idle_timeout_seconds": _RESEARCH,
    "research_section_llm_timeout_seconds": _RESEARCH,
    "research_section_timeout_seconds": _RESEARCH,
    "research_web_search_timeout_seconds": _RESEARCH,
    "subagent_heartbeat_timeout_seconds": _RESEARCH,
    "force_server": _SERVER,
    "persist_server": _SERVER,
    "server_ready_timeout": _SERVER,
    "task_stuck_guard_enabled": _GUARD,
    "task_stuck_autodone_turns": _GUARD,
    "task_stuck_nudge_turns": _GUARD,
    "thinking_recent_request_runs": _THINKING,
    "thinking_startup_grace_seconds": _THINKING,
    "channel_history_window_messages": "how many channel messages a turn reads back, for every account",
    "desktop_window_height": "the desktop shell window of this machine",
    "desktop_window_width": "the desktop shell window of this machine",
    "github_oauth_client_id": "the GitHub app's public client id, one for the installation",
    "language": "the installation's fallback language",
    "memory_ingest_profile_enabled": "a timing switch of the memory store, one for every account",
    "speech_language": "this machine's speech input language",
    "speech_mic_index": "this machine's microphone",
    "update_include_prereleases": "which releases the installation updates to",
    "workflows_enabled": "the workflow engine switch, one for the installation",
}
# A name of this shape is keyed by account, so it can never be an installation setting.
PER_ACCOUNT_NAME = re.compile(r"(_by_(scope|user|username|account)$)|(^(email|github|cloud)_config)")
# A scalar of this shape names one account.
ACCOUNT_ID_NAME = re.compile(r"_(scope|user)_id$")
SENTINEL = "zz-another-account-sentinel"

_VAF_ROOT = Path(__file__).resolve().parent.parent / "vaf"
# The class and its module aliases, as the read scan of the admin-only guard spells them.
_CONFIG_NAME = re.compile(r"^(Config|_Cfg\w*)$")


def _is_config(node) -> bool:
    return isinstance(node, ast.Name) and bool(_CONFIG_NAME.match(node.id))


def _loads_config(node) -> bool:
    """`Config.load()`, also wrapped (`dict(Config.load())`, `copy.deepcopy(Config.load())`)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "load" and _is_config(func.value):
        return True
    return any(_loads_config(arg) for arg in node.args)


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_nodes(scope):
    """The nodes of one scope, without descending into the functions defined in it."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _written_in(scope, inherited: frozenset, keys: set) -> None:
    nodes = list(_own_nodes(scope))
    loaded = inherited | {target.id for node in nodes
                          if isinstance(node, ast.Assign) and _loads_config(node.value)
                          for target in node.targets if isinstance(target, ast.Name)}
    for node in nodes:
        if isinstance(node, _SCOPES):
            # A closure writes through the name its enclosing function loaded.
            _written_in(node, frozenset(loaded), keys)
            continue
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set" and _is_config(node.func.value) and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AugAssign) else [])
        for target in targets:
            if not (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)):
                continue
            owner = target.value
            if ((isinstance(owner, ast.Name) and owner.id in loaded)
                    or (isinstance(owner, ast.Attribute) and owner.attr == "config"
                        and isinstance(owner.value, ast.Name) and owner.value.id in ("self", "agent"))):
                keys.add(target.slice.value)


@functools.lru_cache(maxsize=None)
def _keys_the_code_writes(root: Path) -> frozenset:
    """Literal keys written through `Config.set("key", ...)`, through `name["key"] = ...` where
    `name` was bound to a loaded config in that function or one enclosing it, and through the
    agent's loaded copy (`self.config["key"]`, `agent.config["key"]`)."""
    keys: set = set()
    for source in root.rglob("*.py"):
        text = source.read_bytes()
        # Only a module that names the class, an alias or the agent's copy can write one.
        if b"Config" not in text and b"_Cfg" not in text and b".config[" not in text:
            continue
        _written_in(ast.parse(text), frozenset(), keys)
    return frozenset(keys)


def _undeclared_keys() -> set:
    """Keys the code reads or writes by name that have no `DEFAULTS` row."""
    return (_keys_the_code_reads() | _keys_the_code_writes(_VAF_ROOT)) - set(Config.DEFAULTS)


def _swept_keys() -> set:
    named = re.compile(r"(_config$)|(_by_(scope|user|username|account)$)")
    keys = {k for k, v in Config.DEFAULTS.items() if v is None or isinstance(v, (dict, list))}
    keys |= {k for k in list(Config.DEFAULTS) + list(Config.ROUTE_OWNED_KEYS) if named.search(k)}
    keys |= {f"{c}_config" for c in config_mod._KNOWN_CHANNELS}
    keys |= _undeclared_keys()
    return keys


def _planted(key: str):
    if isinstance(Config.DEFAULTS.get(key), list):
        return [SENTINEL, {"user_scope_id": BOB_SCOPE, "value": SENTINEL}]
    return {"zz_planted": SENTINEL, BOB_SCOPE: {"value": SENTINEL},
            "bob": {"accounts": [{"login": SENTINEL, "user_scope_id": BOB_SCOPE}]},
            "whitelist": [{"user_scope_id": BOB_SCOPE, "phone_number": SENTINEL}]}


def _surviving_keys(keys=None) -> set:
    survivors = set()
    for key in (_swept_keys() if keys is None else keys):
        cfg = {"local_admin_scope_id": ADMIN_SCOPE, key: _planted(key)}
        if SENTINEL in json.dumps(_view(cfg)):
            survivors.add(key)
    return survivors


def test_the_key_scans_find_the_keys_they_must():
    """A scan that silently matches nothing would make the guard below pass on nothing."""
    written = _keys_the_code_writes(_VAF_ROOT)
    assert len(written) >= 40, len(written)
    assert {"trusted_sources_custom", "local_admin_scope_id", "whatsapp_config",
            "deepseek_reasoning_effort"} <= written
    assert {"trusted_sources_custom", "trusted_sources_disabled", "vaf_home"} <= _undeclared_keys()


def test_every_structured_key_is_projected_or_reviewed():
    survivors = _surviving_keys()
    assert survivors - set(REVIEWED_INSTALLATION_SETTINGS) == set(), (
        "these keys reach an account without admin rights as stored: project them in "
        "Config.config_for_user, or add them to REVIEWED_INSTALLATION_SETTINGS with the "
        "reason they hold no account's data")
    assert not [k for k in REVIEWED_INSTALLATION_SETTINGS if PER_ACCOUNT_NAME.search(k)]
    assert all(reason.strip() for reason in REVIEWED_INSTALLATION_SETTINGS.values())
    # A reviewed key that no longer exists or no longer passes is a stale reason.
    assert set(REVIEWED_INSTALLATION_SETTINGS) <= survivors


def test_every_account_id_setting_is_projected_or_reviewed():
    """A scalar setting named like one account's id (`local_admin_scope_id`)."""
    keys = {k for k, v in Config.DEFAULTS.items()
            if ACCOUNT_ID_NAME.search(k) and not isinstance(v, (dict, list))}
    passing = {k for k in keys
               if SENTINEL in json.dumps(_view({"local_admin_scope_id": ADMIN_SCOPE, k: SENTINEL}))}
    assert passing - set(REVIEWED_INSTALLATION_SETTINGS) == set()


def test_the_structured_key_guard_flags_a_new_key(monkeypatch):
    monkeypatch.setattr(Config, "DEFAULTS", {**Config.DEFAULTS, "zz_by_scope": {},
                                             "zz_new_block": {"a": 1}, "zz_none_block": None})
    flagged = _surviving_keys() - set(REVIEWED_INSTALLATION_SETTINGS)
    assert {"zz_by_scope", "zz_new_block", "zz_none_block"} <= flagged


def test_the_guard_flags_a_key_without_a_defaults_row(monkeypatch, tmp_path):
    """Read or written by name in code, never declared: both scans pick it up."""
    import test_sensitive_config_keys_admin_only as reads_guard

    (tmp_path / "zz_module.py").write_text(
        "from vaf.core.config import Config\n"
        "def remember(numbers):\n"
        "    cfg = dict(Config.load())\n"
        "    cfg['zz_owner_numbers'] = numbers\n"
        "    Config.save(cfg)\n"
        "    Config.set('zz_set_key', numbers)\n"
        "    return Config.get('zz_read_key')\n"
        "def outer():\n"
        "    cfg = Config.load()\n"
        "    def inner():\n"
        "        cfg['zz_closure_key'] = 1\n"
        "    return inner\n", encoding="utf-8")
    monkeypatch.setattr(reads_guard, "_VAF", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "_VAF_ROOT", tmp_path)
    keys = {"zz_owner_numbers", "zz_set_key", "zz_read_key", "zz_closure_key"}
    assert keys <= _undeclared_keys()
    assert keys <= _surviving_keys(_swept_keys()) - set(REVIEWED_INSTALLATION_SETTINGS)


def test_the_id_guard_flags_a_new_account_id(monkeypatch):
    monkeypatch.setattr(Config, "DEFAULTS", {**Config.DEFAULTS, "zz_owner_user_id": ""})
    with pytest.raises(AssertionError):
        test_every_account_id_setting_is_projected_or_reviewed()


# The route-owned messenger sub-keys a non-admin sees, and how.
SUB_KEY_EXCEPTIONS = {
    ("telegram_config", "bot_username"),  # the shared bot's name, the same for everyone
    ("veyllo_app_config", "relay_enabled"),  # coerced to a bool
    ("whatsapp_config", "whitelist"),  # the account's own entries only
}


@pytest.mark.parametrize("block, sub", sorted(
    (block, sub) for block, subs in Config.ROUTE_OWNED_SUBKEYS.items() for sub in subs))
def test_no_route_owned_messenger_sub_key_reaches_a_non_admin(block, sub):
    planted = [{"user_scope_id": BOB_SCOPE, "phone_number": SENTINEL, "chat_id": SENTINEL},
               {"user_scope_id": ALICE_SCOPE, "phone_number": ALICE_PHONE}]
    cfg = {"local_admin_scope_id": ADMIN_SCOPE, block: {sub: planted}}
    if (block, sub) == ("telegram_config", "bot_username"):
        # Copied as stored: the one bot every account pairs with, named by its own `getMe`.
        cfg[block][sub] = "shared_vaf_bot"
        assert _view(cfg)[block] == {"enabled": False, "verified": False,
                                     "bot_username": "shared_vaf_bot", "whitelist": []}
        return
    view = _view(cfg)
    assert SENTINEL not in json.dumps(view), f"{block}.{sub} reached a non-admin"
    if (block, sub) == ("veyllo_app_config", "relay_enabled"):
        assert view[block][sub] is True
    elif (block, sub) == ("whatsapp_config", "whitelist"):
        assert view[block][sub] == [planted[1]]
    else:
        assert (block, sub) not in SUB_KEY_EXCEPTIONS
        assert sub not in (view.get(block) or {}) or view[block][sub] == []


# ── fuzz ───────────────────────────────────────────────────────────────────────────────

FOREIGN_MARK = "zzforeignzz"
EXTRA_CHANNEL = "signal"
SUB_KEYS = ("enabled", "verified", "bot_username", "whitelist", "relay_whitelist", "chat_activity",
            "lid_to_e164", "owner_control", "admin_user_id", "admin_username", "relay_instance_id",
            "relay_api_base", "relay_enabled", "configured", "inbound_to_agent",
            "reply_window_hours", "user_scope_id")
# Fields a view copies as stored: the fuzz gives them values that name nobody.
COPIED = {("telegram", "verified"), ("telegram", "bot_username"), ("discord", "verified")}


def _foreign(rng) -> object:
    return rng.choice([
        f"+49{FOREIGN_MARK}{rng.randrange(1000)}",
        {f"{FOREIGN_MARK}@lid": f"+49{FOREIGN_MARK}"},
        [{"user_scope_id": f"{FOREIGN_MARK}-scope", "chat_id": FOREIGN_MARK}],
        rng.randrange(10 ** 9),
    ])


def _block(rng, channel):
    shape = rng.randrange(7)
    if shape == 0:
        return None
    if shape == 1:
        return {}
    if shape == 2:
        return [FOREIGN_MARK]
    if shape == 3:
        return FOREIGN_MARK
    if shape == 4:
        return rng.randrange(100)
    block = {}
    for sub in rng.sample(SUB_KEYS, rng.randrange(len(SUB_KEYS))):
        if (channel, sub) in COPIED:
            block[sub] = rng.choice([True, False, "shared_vaf_bot", None])
        else:
            block[sub] = _foreign(rng)
    for i in range(rng.randrange(3)):
        block[f"zz_{FOREIGN_MARK}_{i}"] = _foreign(rng)
    if channel == "whatsapp":
        block["whitelist"] = _whitelist(rng)
    return block


def _whitelist(rng):
    if rng.random() < 0.2:
        return rng.choice([5, FOREIGN_MARK, {FOREIGN_MARK: 1}, True, None])
    pool = [
        lambda: {"user_scope_id": ALICE_SCOPE, "phone_number": ALICE_PHONE},
        lambda: {"user_scope_id": f"{FOREIGN_MARK}-bob", "phone_number": f"+49{FOREIGN_MARK}"},
        lambda: {"user_scope_id": None, "phone_number": FOREIGN_MARK},
        lambda: {"user_scope_id": 12345, "phone_number": FOREIGN_MARK},
        lambda: {"phone_number": FOREIGN_MARK},
        lambda: FOREIGN_MARK,
        lambda: 7,
    ]
    return [rng.choice(pool)() for _ in range(rng.randrange(6))]


def _draw(rng) -> tuple:
    cfg = {"local_admin_scope_id": f"{FOREIGN_MARK}-admin-scope"}
    for channel in KNOWN_CHANNELS + (EXTRA_CHANNEL,):
        if rng.random() < 0.85:
            cfg[f"{channel}_config"] = _block(rng, channel)
    toggles = rng.choice([
        {ALICE_SCOPE: {c: rng.random() < 0.5 for c in KNOWN_CHANNELS + (EXTRA_CHANNEL,)},
         f"{FOREIGN_MARK}-bob": {"whatsapp": True}},
        {ALICE_SCOPE: FOREIGN_MARK, f"{FOREIGN_MARK}-bob": {"whatsapp": True}},
        [FOREIGN_MARK],
        None,
    ])
    if toggles is not None:
        cfg["connection_enabled_by_scope"] = toggles
    cfg["email_config_by_scope"] = {ALICE_SCOPE: {"accounts": [{"email": "alice@example.org"}]},
                                    f"{FOREIGN_MARK}-bob": {"accounts": [{"email": FOREIGN_MARK}]}}
    cfg["email_config"] = {"accounts": [{"email": FOREIGN_MARK}]}
    cfg["github_config_by_user"] = {"alice": {"accounts": [{"login": "alice-gh-login"}]},
                                    "bob": {"accounts": [{"login": FOREIGN_MARK}]}}
    cfg["cloud_config_by_user"] = {
        "alice": {"accounts": [{"email": "alice-cloud", "user_scope_id": ALICE_SCOPE},
                               {"email": FOREIGN_MARK, "user_scope_id": f"{FOREIGN_MARK}-bob"}]},
        "bob": {"accounts": [{"email": FOREIGN_MARK, "user_scope_id": f"{FOREIGN_MARK}-bob"}]}}
    scope = ALICE_SCOPE if rng.random() < 0.9 else None
    return cfg, scope


def _check(cfg: dict, scope) -> None:
    before = json.dumps(cfg, sort_keys=True)
    view = Config.config_for_user(cfg, scope, "user", username="alice")
    assert json.dumps(cfg, sort_keys=True) == before, "the stored config was changed"
    text = json.dumps(view, sort_keys=True)
    assert FOREIGN_MARK not in text, "another account's data reached the view"
    assert text == json.dumps(Config.config_for_user(cfg, scope, "user", username="alice"),
                              sort_keys=True), "not deterministic"
    toggles = cfg.get("connection_enabled_by_scope")
    own = toggles.get(scope) if isinstance(toggles, dict) and scope else None
    own = own if isinstance(own, dict) else {}
    for channel in KNOWN_CHANNELS + (EXTRA_CHANNEL,):
        key = f"{channel}_config"
        block = view.get(key)
        if isinstance(block, dict):
            assert set(block) <= VIEW_FIELDS.get(channel, {"enabled"}), (key, sorted(block))
        if channel not in VIEW_FIELDS:
            if key not in cfg:
                assert key not in view
            elif isinstance(cfg[key], dict):
                assert block == {"enabled": bool(own.get(channel, False))}
            else:
                assert block is None
    stored = cfg.get("whatsapp_config") or {}
    if isinstance(stored, dict):
        wl = stored.get("whitelist")
        expected = ([e for e in wl if isinstance(e, dict) and str(e.get("user_scope_id")) == scope]
                    if isinstance(wl, list) and scope else [])
        assert view["whatsapp_config"]["whitelist"] == expected
    admin = Config.config_for_user(cfg, None, "admin")
    assert admin.get("whatsapp_config") == cfg.get("whatsapp_config")


def test_member_view_fuzz(monkeypatch):
    monkeypatch.setattr(config_mod, "_KNOWN_CHANNELS", KNOWN_CHANNELS + (EXTRA_CHANNEL,))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {"local_admin_scope_id": ADMIN_SCOPE}))
    rng = random.Random(420)
    for draw in range(300):
        cfg, scope = _draw(rng)
        try:
            _check(cfg, scope)
        except Exception as e:
            raise AssertionError(f"draw {draw} failed: {e!r}") from e


def test_the_view_is_linear_in_the_whitelist(state):
    state["whatsapp_config"]["whitelist"] = [dict(WA_BOB) for _ in range(100_000)] + [WA_ALICE]
    start = time.perf_counter()
    view = Config.config_for_user(state, ALICE_SCOPE, "user", username="alice")
    assert time.perf_counter() - start < 0.5
    assert view["whatsapp_config"]["whitelist"] == [WA_ALICE]
