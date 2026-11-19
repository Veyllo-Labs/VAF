# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every key that holds a secret, names an address, bounds everyone's run time or switches
a control that defends every account is admin-only to write.

THE DEFECT, measured with a non-admin token on the settings save (PATCH /api/config):
- `veyllo_base_url` was stored. Every Veyllo request (the vision lane among them) then
  went to the address the account chose, with the instance's Veyllo key as its Bearer
  token.
- `memory_db_url`, `memory_db_owner_url`, `memory_encryption_key`,
  `mail_store_encryption_key` and the KEK were stored: a secret was redacted from a
  non-admin's READ but not refused on their WRITE, so the next start would have moved
  every account's memory to a database of their choice.
- `config_format_version` = 1 was stored, and the next load re-ran the migrations over
  the admin's settings (a login token lifetime of 24 hours became 720).
- The run-time bounds (`api_timeout_*`, `browser_timeout_seconds`, ...) and the controls
  (`server_mode`, the upload scans, the security log retention) were stored as well, while
  USER_ISOLATION.md listed timeouts among the admin-only settings.
- `memory_db_owner_url`, the superuser DSN, was also handed to a non-admin on READ: it
  was not classified secret.

THE RULE: `is_global_config_key` is True for every `is_secret_config_key`, the address,
version, bound and control keys are entries (or the `api_timeout_` prefix), and the guards
below hold each CLASS, so the next key of a class is admin-only or the suite is red.
Synthetic identities and a scratch config only.
"""
import json
import re
from types import SimpleNamespace

import pytest

from vaf.core.config import Config

TENANT_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"

TIMEOUT_LIKE = ("chat_step_wall_clock_seconds",)
CONTROLS = ("server_mode", "upload_threat_scan_enabled", "upload_scan_advisory_enabled",
            "security_log_retention_days", "front_office_contact_reply_require_approval",
            "memory_db_echo", "auto_install_gpu", "update_include_prereleases",
            "config_format_version")
# Read with an inline default, outside DEFAULTS; named as entries so that declaring one in
# DEFAULTS later cannot open it.
UNDECLARED_CONTROLS = ("vaf_home", "task_stuck_guard_enabled", "workflows_enabled",
                       "force_server", "persist_server")


# ── the classes ───────────────────────────────────────────────────────────────

def test_every_secret_is_admin_only():
    """A value nobody but an admin may read is not one anybody may write. MUTATION: drop
    the `is_secret_config_key` clause in `is_global_config_key`."""
    secrets = [k for k in Config.DEFAULTS if Config.is_secret_config_key(k)]
    secrets += sorted(Config.SECRET_CONFIG_KEYS | Config.UI_MANAGED_SECRET_KEYS)
    # Names nobody has written yet, one per suffix: the rule is the classifier, not a list.
    secrets += [f"future{s}" for s in Config.SECRET_CONFIG_KEY_SUFFIXES]
    assert len(secrets) >= 20, secrets
    open_keys = sorted({k for k in secrets if not Config.is_global_config_key(k)})
    assert open_keys == [], f"secret but writable by any signed-in account: {open_keys}"
    assert Config.filter_for_non_admin({k: "x" for k in secrets}) == {}


def test_every_provider_endpoint_is_admin_only():
    """The base-URL keys a provider spec names decide where that provider's requests go,
    and each request carries the instance's key for it. MUTATION: drop the
    `veyllo_base_url` entry."""
    from vaf.core.provider_registry import PROVIDER_SPECS

    specs = PROVIDER_SPECS.values() if isinstance(PROVIDER_SPECS, dict) else PROVIDER_SPECS
    keys = {getattr(spec, field) for spec in specs
            for field in ("sdk_base_url_config_key", "coder_base_url_config_key",
                          "models_base_config_key")
            if getattr(spec, field, None)}
    assert {"veyllo_base_url", "local_api_url"} <= keys, keys
    open_keys = sorted(k for k in keys if not Config.is_global_config_key(k))
    assert open_keys == [], f"a provider endpoint writable by any signed-in account: {open_keys}"


def test_every_address_key_is_admin_only():
    """Every `*_url` key is an address something of the instance connects to. MUTATION:
    take `memory_db_owner_url` out of `SECRET_CONFIG_KEYS`."""
    keys = [k for k in Config.DEFAULTS if k.endswith("_url")]
    assert len(keys) >= 4, keys
    open_keys = sorted(k for k in keys if not Config.is_global_config_key(k))
    assert open_keys == [], f"an address writable by any signed-in account: {open_keys}"


def test_memory_database_addresses_are_secret():
    """Both DSNs may carry a password (the owner's that of the superuser role), so neither
    reaches a non-admin's read. MUTATION: take `memory_db_owner_url` out of
    `SECRET_CONFIG_KEYS` (the owner DSN went to every non-admin read)."""
    from vaf.core.db_credentials import _DSN_KEYS

    assert set(_DSN_KEYS) == {"memory_db_url", "memory_db_owner_url"}
    cfg = {**Config.DEFAULTS, **{k: f"postgresql://role:pw-{k}@db/vaf" for k in _DSN_KEYS}}
    shown = Config.config_for_user(cfg, TENANT_SCOPE, "user")
    for key in _DSN_KEYS:
        assert Config.is_secret_config_key(key), key
        assert "pw-" not in str(shown.get(key) or ""), f"{key} reaches a non-admin read"


def test_every_run_time_bound_is_admin_only():
    """USER_ISOLATION.md lists timeouts among the admin-only settings; every key named so
    is one, and the turn's wall clock with them. MUTATIONS: drop the `api_timeout_` prefix;
    drop the `browser_timeout_seconds` entry."""
    keys = [k for k in Config.DEFAULTS if "timeout" in k] + list(TIMEOUT_LIKE)
    assert len(keys) >= 9, keys
    open_keys = sorted(k for k in keys if not Config.is_global_config_key(k))
    assert open_keys == [], f"a run-time bound writable by any signed-in account: {open_keys}"


def test_controls_are_admin_only():
    """MUTATION: drop the `server_mode` entry (or any other of the list)."""
    open_keys = [k for k in CONTROLS if not Config.is_global_config_key(k)]
    assert open_keys == [], open_keys


# Names of a key whose value can switch off an identity, a policy, a guard or a check, or
# shorten what is kept for review: who a workflow runs as, a gate, a guard, an allowlist, a
# bypass, a trusted list, an approval, a scan, a retention, the collector's age.
_POLICY_NAME = re.compile(
    r"identity_injection|policy|_gate_|_gate$|guard|unrestricted|allowlist|allow_|bypass"
    r"|trusted|enforce|approval|threat_scan|scan_advisory|retention|rescan|^gc_")


def test_every_identity_and_policy_switch_is_admin_only():
    """A non-admin set `workflow_identity_injection` to `legacy` and every saved-template
    workflow ran as the machine owner. MUTATIONS: drop the `workflow_identity_injection`
    entry; drop the `gc_` prefix; drop `skills_rescan_interval_hours`."""
    keys = [k for k in Config.DEFAULTS if _POLICY_NAME.search(k)]
    assert {"workflow_identity_injection", "plan_gate_enabled", "gc_max_age_hours",
            "skills_rescan_interval_hours", "task_overwrite_guard_enabled"} <= set(keys)
    assert len(keys) >= 25, keys
    open_keys = sorted(k for k in keys if not Config.is_global_config_key(k))
    assert open_keys == [], f"a policy switch writable by any signed-in account: {open_keys}"


def test_every_flag_the_security_dashboard_reports_is_admin_only():
    """The dashboard calls them guardrails; one account switching one off switched it off
    for every account. Read from the reporter's source, so a flag added there is held too.
    MUTATION: drop the `ask_first_drain_gate_enabled` entry."""
    import inspect

    from vaf.api.security_routes import collect_guardrails_status

    keys = re.findall(r"Config\.get\(\"([a-z0-9_]+)\"", inspect.getsource(collect_guardrails_status))
    assert len(keys) >= 4, keys
    open_keys = sorted(k for k in keys if not Config.is_global_config_key(k))
    assert open_keys == [], f"a reported guardrail writable by any signed-in account: {open_keys}"


# ── the allowlist: a key outside DEFAULTS is admin-only ─────────────────────────

_VAF = __import__("pathlib").Path(__file__).resolve().parent.parent / "vaf"
# `Config.get("key"`, the module aliases of Config (`_CfgOv.get(`, `_CfgAuto.get(`, ...) and the
# agent's loaded copy (`self.config.get(`, `agent.config.get(`).
_READ = re.compile(r"\b(?:Config|_Cfg\w*|self\.config|agent\.config)\.get(?:_bool|_int|_float)?"
                   r"\(\s*[\"']([a-z][a-z0-9_]*)[\"']")


def _keys_the_code_reads() -> set:
    keys = set()
    for source in _VAF.rglob("*.py"):
        keys |= set(_READ.findall(source.read_bytes().decode("utf-8", "replace")))
    return keys


def test_every_key_the_code_reads_is_declared_or_admin_only():
    """A key read with an inline default is not in DEFAULTS, and a denylist let it through
    every non-admin save (`vaf_home` moved the TLS directory; the loop guards, the workflow
    switch and the research timeouts were stored too). MUTATION: drop the `not in
    cls.DEFAULTS` clause of `is_global_config_key`."""
    keys = _keys_the_code_reads()
    undeclared = sorted(k for k in keys if k not in Config.DEFAULTS)
    # A parser that silently matches nothing would make this pass on nothing.
    assert len(keys) >= 300 and len(undeclared) >= 40, (len(keys), len(undeclared))
    assert {"vaf_home", "task_stuck_guard_enabled", "research_overall_timeout_seconds",
            "workflows_enabled", "force_server"} <= keys
    open_keys = [k for k in undeclared if not Config.is_global_config_key(k)]
    assert open_keys == [], f"read outside DEFAULTS and writable by any account: {open_keys}"
    assert Config.is_global_config_key("a_key_nobody_has_written_yet")


def test_the_undeclared_controls_are_named_entries():
    """MUTATION: drop the `vaf_home` entry (or another of the five)."""
    missing = [k for k in UNDECLARED_CONTROLS if k not in Config.GLOBAL_CONFIG_KEYS]
    assert missing == [], missing


def test_one_table_of_route_owned_keys_and_one_strip():
    """Five owners (the profile, the engine, the certificate pair, the cloud, mail and GitHub
    lists) had a strip each, called one after another in both lanes. MUTATIONS: a lane that
    skips the strip (source); an owner constant that grows a key the table lacks."""
    import inspect

    from vaf.api import config_routes
    from vaf.core import local_engine, web_server
    from vaf.network import ssl_utils

    for key in (*local_engine.ENGINE_KEYS, *ssl_utils.CERTIFICATE_KEYS, "inference_profile",
                "cloud_config", "cloud_config_by_user", "email_config", "email_config_by_scope",
                "email_config_by_user", "github_config", "github_config_by_user"):
        assert Config.is_route_owned_key(key), key
    body = {"theme": "dark"}
    assert Config.without_route_owned_keys(body) is body
    patch_src = inspect.getsource(config_routes.patch_config)
    lane = inspect.getsource(web_server)
    lane = lane[lane.index('elif type == "save_config":'):]
    lane = lane[:lane.index("Config.split_connection_toggles")]
    for src in (patch_src, lane):
        assert "Config.without_route_owned_keys(" in src
        assert not re.search(r"without_(profile_key|engine_keys|certificate_keys|cloud_account_keys"
                             r"|mail_account_keys)\(", src)


# ── the save lanes, end to end ─────────────────────────────────────────────────

@pytest.fixture
def stored(tmp_path, monkeypatch):
    """A synthetic config file Config reads and writes; returns a reader of the raw file."""
    from vaf.core import migrations

    path = tmp_path / "config.json"
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "_filelock", None)
    monkeypatch.setattr(Config, "_migration_logged", set())

    def write(cfg: dict):
        path.write_text(json.dumps({"config_format_version": migrations.CONFIG_FORMAT_VERSION,
                                    **cfg}), encoding="utf-8")

    def raw() -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    return SimpleNamespace(write=write, raw=raw, path=path)


ADMIN_SET = {"veyllo_base_url": "https://api.veyllo.app/v1",
             "memory_db_url": "postgresql://app:pw-app@db/vaf",
             "memory_db_owner_url": "postgresql://owner:pw-owner@db/vaf",
             "local_network_jwt_expiry_hours": 24,
             "api_timeout_read": 600.0, "browser_timeout_seconds": 1800,
             "server_mode": False, "security_log_retention_days": 14,
             "upload_threat_scan_enabled": True, "workflow_identity_injection": "declared",
             "plan_gate_enabled": True, "gc_max_age_hours": 48,
             "skills_rescan_interval_hours": 5, "tts_auto_speak": False}
HOSTILE = {"veyllo_base_url": "http://192.0.2.10:8000/v1",
           "memory_db_url": "postgresql://x:x@192.0.2.10/vaf",
           "memory_db_owner_url": "postgresql://x:x@192.0.2.10/vaf",
           "config_format_version": 1,
           "api_timeout_read": 1.0, "browser_timeout_seconds": 1,
           "server_mode": True, "security_log_retention_days": 1,
           "upload_threat_scan_enabled": False, "workflow_identity_injection": "legacy",
           "plan_gate_enabled": False, "gc_max_age_hours": 1,
           "skills_rescan_interval_hours": 0,
           "vaf_home": "/home/user/VAF_Projects/ab12cd34", "task_stuck_guard_enabled": False,
           "workflows_enabled": False, "research_overall_timeout_seconds": 1,
           "a_key_nobody_has_written_yet": "x", "tts_auto_speak": True}
KEPT = [k for k in HOSTILE if k != "tts_auto_speak"]


def _patch(body: dict, user: dict) -> dict:
    import asyncio

    from vaf.api import config_routes
    request = SimpleNamespace(state=SimpleNamespace(user=user))
    return asyncio.run(config_routes.patch_config(dict(body), request, user))


def _ws_save(monkeypatch, *, role: str, scope: str, body: dict) -> dict:
    """One `save_config` over a real `/ws` connection with a signed token; the answer."""
    import threading

    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    from vaf.core.web_server import app

    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    token = jwt.encode({"sub": "7", "user_scope_id": scope, "username": "alice",
                        "role": role}, "s" * 32, algorithm="HS256")
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
    return box["msg"]


def _assert_kept(stored):
    from vaf.core import migrations

    raw = stored.raw()
    for key in KEPT:
        # A key the admin never set stays unset.
        expected = (migrations.CONFIG_FORMAT_VERSION if key == "config_format_version"
                    else ADMIN_SET.get(key))
        assert raw.get(key) == expected, (key, raw.get(key))
    # The TLS directory stays the admin's: the tray writes the server and CA keys there.
    from vaf.network.ssl_utils import _ssl_dir_path
    assert "VAF_Projects" not in str(_ssl_dir_path())
    assert raw["tts_auto_speak"] is True
    # The migrations did not run again over the admin's settings.
    assert Config.load()["local_network_jwt_expiry_hours"] == 24
    # Workflows still run as the account that started them.
    from vaf.workflows.engine import _identity_is_declared
    assert _identity_is_declared() is True


def test_non_admin_patch_keeps_the_sensitive_keys(stored):
    """MUTATIONS: drop the `veyllo_base_url` entry; drop the secret clause; drop the
    `config_format_version` entry."""
    stored.write(ADMIN_SET)
    answer = _patch(HOSTILE, {"role": "user", "user_scope_id": TENANT_SCOPE,
                              "username": "alice"})
    _assert_kept(stored)
    assert answer["veyllo_base_url"] == ADMIN_SET["veyllo_base_url"]
    assert "pw-" not in json.dumps(answer), "a DSN password reached the non-admin's answer"


def test_non_admin_websocket_save_keeps_the_sensitive_keys(stored, monkeypatch):
    """MUTATIONS: drop the `veyllo_base_url` entry; pass `is_admin=True` in the WebSocket
    lane."""
    from vaf.core.config import get_local_admin_scope_id

    assert TENANT_SCOPE != str(get_local_admin_scope_id())
    stored.write(ADMIN_SET)
    msg = _ws_save(monkeypatch, role="user", scope=TENANT_SCOPE, body=dict(HOSTILE))
    assert msg.get("status") == "success", msg
    _assert_kept(stored)


def test_admin_still_sets_the_endpoint_and_the_bounds(stored, monkeypatch):
    """MUTATION: drop the gated keys from every body in `split_connection_toggles`."""
    stored.write(ADMIN_SET)
    body = {"veyllo_base_url": "https://veyllo.example/v1", "api_timeout_read": 900.0,
            "browser_timeout_seconds": 600}
    _patch(body, {"role": "admin", "user_scope_id": ADMIN_SCOPE, "username": "admin"})
    raw = stored.raw()
    for key, value in body.items():
        assert raw[key] == value, key
    body = {"veyllo_base_url": "https://veyllo2.example/v1", "security_log_retention_days": 30}
    msg = _ws_save(monkeypatch, role="admin", scope=ADMIN_SCOPE, body=dict(body))
    assert msg.get("status") == "success", msg
    raw = stored.raw()
    for key, value in body.items():
        assert raw[key] == value, key
