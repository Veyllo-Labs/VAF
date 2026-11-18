# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A lane model of another provider than its lane's is reset in the stored config.

THE DEFECT. `subagent_model` and `browser_agent_model` ride the MAIN provider unless
their lane has its own. A switch of that provider left the old provider's model in
the config: every reader dropped it (`subagent_model_for`, the browser lane) and
warned once per process, and a sub-agent is a new process, so every sub-agent run
said "is not a DeepSeek model and is ignored" while DeepSeek was already selected.
Nothing ever removed the value.

THE RULE sits in the one write sink and the one read source: `Config.save` resets
such a value in the dict it writes, `Config.load` in the dict it returns and, under
the config lock, on disk. Every switching path (web, REST, CLI, terminal app,
setup) writes through `save`, so none needs a line of its own.
"""
import contextlib
import json
import os
import random
from types import SimpleNamespace

import pytest

import vaf.core.model_catalog as mc
import vaf.core.provider_registry as pr
from vaf.core.config import (
    LANE_MODEL_KEYS,
    MODEL_ID_KEYS,
    Config,
    foreign_lane_models,
    set_subagent_provider,
    subagent_model_for,
)

INCIDENT = {"provider": "deepseek", "subagent_provider": "", "subagent_model": "veyllo-chat"}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """A process that has reported and logged nothing yet; what would be printed or
    logged is recorded instead."""
    monkeypatch.setattr(pr, "_foreign_noted", set())
    monkeypatch.setattr(Config, "_lane_reset_logged", set())
    monkeypatch.setattr(Config, "_migration_logged", set())
    rec = SimpleNamespace(events=[], log=[])
    import vaf.cli.ui as ui_mod
    monkeypatch.setattr(ui_mod.UI, "event", staticmethod(lambda *a, **kw: rec.events.append(a)))
    monkeypatch.setattr("vaf.core.log_helper.append_domain_log",
                        lambda *a, **kw: rec.log.append(a))
    monkeypatch.setattr(mc, "_today", lambda: __import__("datetime").date(2027, 6, 1))
    return rec


@pytest.fixture
def stored(tmp_path, monkeypatch):
    """Write a synthetic config file and point Config at it; returns a reader of the
    raw file."""
    from vaf.core import migrations

    path = tmp_path / "config.json"
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "_filelock", None)

    def write(cfg: dict):
        path.write_text(json.dumps({"config_format_version": migrations.CONFIG_FORMAT_VERSION,
                                    **cfg}), encoding="utf-8")

    def raw() -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    return SimpleNamespace(write=write, raw=raw, path=path)


def _resets(log) -> list:
    return [a[1] for a in log if len(a) > 1 and "[LANE_MODEL_RESET]" in str(a[1])]


# ── AC1: the rule ────────────────────────────────────────────────────────────────

_GGUF = "unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q4_K_M.gguf"


@pytest.mark.parametrize("config, expected", [
    # The incident.
    ({"provider": "deepseek", "subagent_model": "veyllo-chat"},
     {"subagent_model": ("veyllo-chat", "deepseek")}),
    # The lane's own models stay.
    ({"provider": "deepseek", "subagent_model": "deepseek-v4-pro"}, {}),
    ({"provider": "deepseek", "subagent_model": "deepseek-flash",
      "browser_agent_model": "deepseek-flash"}, {}),
    # A sub-agent lane of its own decides, but only with its gate on.
    ({"provider": "deepseek", "subagent_provider": "openai",
      "subagent_use_separate_provider": True, "subagent_model": "gpt-5.6-sol"}, {}),
    ({"provider": "deepseek", "subagent_provider": "openai",
      "subagent_use_separate_provider": False, "subagent_model": "gpt-5.6-sol"},
     {"subagent_model": ("gpt-5.6-sol", "deepseek")}),
    # DeepSeek's routing alias is DeepSeek's only.
    ({"provider": "openai", "subagent_model": "deepseek-auto"},
     {"subagent_model": ("deepseek-auto", "openai")}),
    ({"provider": "deepseek", "subagent_model": "deepseek-auto"}, {}),
    # Local names fit the local lane and no API lane.
    ({"provider": "local", "subagent_model": "auto", "browser_agent_model": _GGUF}, {}),
    ({"provider": "deepseek", "subagent_model": "auto", "browser_agent_model": _GGUF},
     {"subagent_model": ("auto", "deepseek"), "browser_agent_model": (_GGUF, "deepseek")}),
    # An id nobody can place is the choice of whoever set it.
    ({"provider": "deepseek", "subagent_model": "my-gateway-model"}, {}),
    # The provider's own api_model vouches for a gateway's other-vendor name.
    ({"provider": "openai", "api_model_openai": "claude-sonnet-4-6",
      "subagent_model": "claude-sonnet-4-6"}, {}),
    # A browser lane with its own provider keeps that provider's model.
    ({"provider": "deepseek", "browser_agent_provider": "veyllo",
      "browser_agent_model": "veyllo-chat"}, {}),
    ({"provider": "deepseek", "browser_agent_provider": "inherit",
      "browser_agent_model": "veyllo-chat"},
     {"browser_agent_model": ("veyllo-chat", "deepseek")}),
    # The lanes read only with their own provider are never this rule's.
    ({"provider": "deepseek", "vision_model": "veyllo-chat", "voice_agent_model": "veyllo-chat",
      "failover_backup_model": "veyllo-chat", "thinking_model": "veyllo-chat"}, {}),
    # A missing provider is read as load reads it: local.
    ({"subagent_model": "gpt-5.6-sol"}, {"subagent_model": ("gpt-5.6-sol", "local")}),
])
def test_the_rule_table(config, expected):
    """MUTATIONS: read the sub-agent lane from the raw `subagent_provider` instead of
    `subagent_provider_override` (the gate-off row goes red); add `vision_model` to
    LANE_MODEL_KEYS (the never-returned row goes red)."""
    assert foreign_lane_models(config) == expected


def test_lane_model_keys_are_model_id_keys():
    """The rule's keys are model-id keys, so the retired-id migration covers them
    too and the schema documents them. MUTATION: rename a key in LANE_MODEL_KEYS."""
    assert LANE_MODEL_KEYS
    for key in LANE_MODEL_KEYS:
        assert key in MODEL_ID_KEYS
        assert Config.DEFAULTS[key] == ""


# ── AC2: a switch resets, a fitting value stays ──────────────────────────────────

def test_a_switch_resets_the_stale_lane_models(stored, monkeypatch, _quiet):
    """The save half alone: the load repair is stubbed, so only Config.save can
    have reset it. MUTATION: remove the reset_foreign_lane_models call in save."""
    monkeypatch.setattr(Config, "_repair_foreign_lane_models", classmethod(lambda cls, r: None))
    stored.write({"provider": "veyllo", "subagent_model": "veyllo-chat",
                  "browser_agent_model": "veyllo-chat"})
    Config.set("provider", "deepseek")
    raw = stored.raw()
    assert raw["provider"] == "deepseek"
    assert raw["subagent_model"] == "" and raw["browser_agent_model"] == ""
    assert len(_resets(_quiet.log)) == 2


def test_a_fitting_lane_model_survives_a_switch(stored):
    """A model of the new provider saved together with the switch (one Settings save)
    stays, and so does a model of a sub-agent lane with its own provider.
    MUTATION: reset the lane models unconditionally on a provider change."""
    stored.write({"provider": "veyllo", "subagent_model": "veyllo-chat"})
    Config.save({**Config.load(), "provider": "deepseek", "subagent_model": "deepseek-v4-pro"})
    assert stored.raw()["subagent_model"] == "deepseek-v4-pro"

    stored.write({"provider": "veyllo", "subagent_provider": "openai",
                  "subagent_use_separate_provider": True, "subagent_model": "gpt-5.6-sol",
                  "browser_agent_provider": "veyllo", "browser_agent_model": "veyllo-chat"})
    Config.set("provider", "deepseek")
    raw = stored.raw()
    assert raw["subagent_model"] == "gpt-5.6-sol"
    assert raw["browser_agent_model"] == "veyllo-chat"


# ── AC3: every switching path ────────────────────────────────────────────────────

_STALE = {"provider": "veyllo", "subagent_provider": "inherit",
          "subagent_use_separate_provider": False,
          "subagent_model": "veyllo-chat", "browser_agent_model": "veyllo-chat"}


def test_rest_save_resets_and_answers_the_reset(stored, monkeypatch):
    """The response is built from the dict save wrote, so Settings shows the reset
    value at once. MUTATION: reset a copy of the dict in save instead of the dict
    itself (the response body goes red)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from vaf.api import config_routes

    stored.write(_STALE)
    app = FastAPI()

    @app.middleware("http")
    async def _as_admin(request, call_next):
        request.state.user = {"username": "alice", "role": "admin", "user_scope_id": "ab12cd34"}
        return await call_next(request)

    app.include_router(config_routes.router)
    res = TestClient(app).patch("/api/config", json={"provider": "deepseek"})
    assert res.status_code == 200
    assert res.json()["subagent_model"] == ""
    assert res.json()["browser_agent_model"] == ""
    assert stored.raw()["subagent_model"] == "" and stored.raw()["browser_agent_model"] == ""


def test_websocket_save_resets(stored, monkeypatch):
    """MUTATION: remove the reset call in save, with the load repair stubbed."""
    import threading

    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    from vaf.core.config import get_local_admin_scope_id
    from vaf.core.web_server import app

    monkeypatch.setattr(Config, "_repair_foreign_lane_models", classmethod(lambda cls, r: None))
    stored.write(_STALE)
    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    token = jwt.encode({"sub": "1", "user_scope_id": str(get_local_admin_scope_id()),
                        "username": "admin", "role": "admin"}, "s" * 32, algorithm="HS256")

    box = {}
    with TestClient(app, client=("127.0.0.1", 40000)).websocket_connect(f"/ws?token={token}") as ws:
        ws.send_text(json.dumps({"type": "save_config", "config": {"provider": "deepseek"}}))

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
    raw = stored.raw()
    assert raw["provider"] == "deepseek"
    assert raw["subagent_model"] == "" and raw["browser_agent_model"] == ""


def test_tui_provider_change_resets(stored, monkeypatch):
    """The terminal app's C shortcut and model overlay. MUTATION: remove the reset
    call in save, with the load repair stubbed."""
    import time

    import vaf.core.agent as agent_mod
    from vaf.cli.tui_app.agent_bridge import AgentBridge

    monkeypatch.setattr(Config, "_repair_foreign_lane_models", classmethod(lambda cls, r: None))
    stored.write(_STALE)
    monkeypatch.setattr(agent_mod, "reload_all_api_backends", lambda **kw: 1)
    monkeypatch.setattr(Config, "get_api_key", classmethod(lambda cls, p: "sk-test"))
    monkeypatch.setattr(AgentBridge, "_api_key_verifies", staticmethod(lambda p: True))
    events = SimpleNamespace(system_note=lambda t: None, event_note=lambda *a: None,
                             presence=lambda *a, **k: None, context=lambda *a: None)
    bridge = AgentBridge(
        SimpleNamespace(get_token_usage=lambda: (1, 2),
                        set_event_sink=lambda s: None, shutdown=lambda: None),
        SimpleNamespace(id="s"), None, events,
        web_interface_getter=lambda: SimpleNamespace(resolve_gate=lambda *a: True))
    try:
        bridge.apply_provider_change("deepseek")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and stored.raw().get("provider") != "deepseek":
            time.sleep(0.02)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (bridge._busy or not bridge._queue.empty()):
            time.sleep(0.02)
    finally:
        bridge.shutdown()
    raw = stored.raw()
    assert raw["provider"] == "deepseek"
    assert raw["subagent_model"] == "" and raw["browser_agent_model"] == ""


def _cli(*args):
    from typer.testing import CliRunner
    from vaf.cli.cmd import models
    return CliRunner().invoke(models.app, ["config", *args])


def _shown(events, style: str) -> str:
    return " ".join(str(a[1]) for a in events if a and a[0] == style)


def test_cli_setter_names_the_reset(stored, _quiet, monkeypatch):
    """MUTATION: drop the warning loop in `vaf models config`."""
    monkeypatch.setattr(Config, "get_api_key", classmethod(lambda cls, p: "sk-test"))
    stored.write(_STALE)
    result = _cli("provider", "deepseek")
    assert result.exit_code == 0, result.output
    shown = _shown(_quiet.events, "Warning")
    for key in ("subagent_model", "browser_agent_model"):
        assert (f"{key} 'veyllo-chat' is not one of DeepSeek's models and was reset; DeepSeek's "
                f"own model (deepseek-flash) is used now. Choose another with: vaf models "
                f"config {key} <model>") in shown
    assert stored.raw()["subagent_model"] == ""


def test_subagent_provider_change_resets_its_model(stored):
    """`vaf settings` and the terminal app write the sub-agent provider through
    set_subagent_provider; the tool model of the old lane goes with it.
    MUTATION: hard-wire the sub-agent lane to the main provider."""
    stored.write({"provider": "deepseek", "subagent_model": "deepseek-v4-pro"})
    set_subagent_provider("openai")
    raw = stored.raw()
    assert raw["subagent_use_separate_provider"] is True
    assert raw["subagent_model"] == ""


# ── AC4: the load repair ─────────────────────────────────────────────────────────

def test_load_repairs_and_writes_back(stored, _quiet):
    """MUTATION: drop the write-back (the raw file goes red)."""
    stored.write({**INCIDENT, "browser_agent_model": "veyllo-chat", "temperature": 0.3})
    loaded = Config.load()
    assert loaded["subagent_model"] == "" and loaded["browser_agent_model"] == ""
    raw = stored.raw()
    assert raw["subagent_model"] == "" and raw["browser_agent_model"] == ""
    assert raw["temperature"] == 0.3
    assert "api_model_deepseek" not in raw, "a sparse file stays sparse"
    assert _resets(_quiet.log) == [
        "[LANE_MODEL_RESET] subagent_model: 'veyllo-chat' is not one of DeepSeek's models, the "
        "provider this lane runs on; reset to empty (DeepSeek's own model)",
        "[LANE_MODEL_RESET] browser_agent_model: 'veyllo-chat' is not one of DeepSeek's models, "
        "the provider this lane runs on; reset to empty (DeepSeek's own model)",
    ]


def test_the_once_set_keeps_a_digest_and_the_log_a_clipped_value(stored, _quiet):
    """`browser_agent_model` is writable by any signed-in user: neither the set that
    keeps the log to one line nor the line may hold a value of their chosen size.
    MUTATIONS: key the set on the value; log the value unclipped."""
    for i in range(3):
        stored.write({"provider": "deepseek", "browser_agent_model": f"gpt-{i}-" + "x" * 100_000})
        Config.load()
    held = sum(len(str(part)) for entry in Config._lane_reset_logged for part in entry)
    assert len(Config._lane_reset_logged) == 3 and held < 1_000
    lines = _resets(_quiet.log)
    assert len(lines) == 3 and all(len(line) < 500 for line in lines)
    assert "chars left out" in lines[0]


def test_the_default_config_builds_no_copy():
    """Config.load asks the rule on every call; with both lane keys empty it must
    not copy DEFAULTS. MUTATION: drop the early return."""
    class _NoIteration(dict):
        def __iter__(self):
            raise AssertionError("copied")

        def keys(self):
            raise AssertionError("copied")

    assert foreign_lane_models(_NoIteration({"provider": "deepseek", "subagent_model": " "})) == {}


def test_a_line_break_in_the_value_logs_one_line(stored, _quiet):
    """A hand-edited value cannot forge a second log line. MUTATION: log the value
    without repr."""
    stored.write({"provider": "deepseek", "subagent_model": "gpt-5.6-sol\n[FAKE] injected"})
    Config.load()
    lines = _resets(_quiet.log)
    assert len(lines) == 1 and "\n" not in lines[0]


def test_the_repair_write_back_holds_the_lock(stored, monkeypatch):
    """config.json is written by several processes. MUTATION: write outside the lock."""
    state = {"held": False, "written_under_lock": None}

    @contextlib.contextmanager
    def _fake_lock():
        state["held"] = True
        try:
            yield
        finally:
            state["held"] = False

    real_write = Config._write_config_file.__func__

    def _write(cls, data):
        state["written_under_lock"] = state["held"]
        return real_write(cls, data)

    stored.write(INCIDENT)
    monkeypatch.setattr(Config, "_locked", classmethod(lambda cls: _fake_lock()))
    monkeypatch.setattr(Config, "_write_config_file", classmethod(_write))
    Config.load()
    assert state["written_under_lock"] is True
    assert stored.raw()["subagent_model"] == ""


def test_the_repair_decides_again_on_disk(stored, monkeypatch):
    """Another process turns on a Veyllo sub-agent lane between the first read and
    the lock: the value fits that lane and must not be wiped.
    MUTATION: write the first read's decision."""
    @contextlib.contextmanager
    def _lock_after_a_concurrent_save():
        raw = stored.raw()
        raw["subagent_provider"] = "veyllo"
        raw["subagent_use_separate_provider"] = True
        stored.path.write_text(json.dumps(raw), encoding="utf-8")
        yield

    stored.write(INCIDENT)
    monkeypatch.setattr(Config, "_locked", classmethod(lambda cls: _lock_after_a_concurrent_save()))
    loaded = Config.load()
    assert loaded["subagent_model"] == ""                 # this read's decision
    raw = stored.raw()
    assert raw["subagent_use_separate_provider"] is True
    assert raw["subagent_model"] == "veyllo-chat"         # kept for the Veyllo lane


def test_an_unwritable_repair_logs_once(stored, monkeypatch, _quiet):
    """Config.load runs on every Config.get; a repair that cannot be written back
    repeats each time and must not fill the log. MUTATION: drop the once-set."""
    def _fail(cls, data):
        raise OSError("read-only")

    stored.write(INCIDENT)
    monkeypatch.setattr(Config, "_write_config_file", classmethod(_fail))
    for _ in range(3):
        assert Config.load()["subagent_model"] == ""
    assert len(_resets(_quiet.log)) == 1
    assert stored.raw()["subagent_model"] == "veyllo-chat"


def test_a_failing_repair_does_not_turn_the_load_into_defaults(stored, monkeypatch):
    """Config.load answers DEFAULTS on any exception, which would drop every saved
    setting for the call. MUTATION: remove the try around the repair in load."""
    def _boom(cls, result):
        raise RuntimeError("broken repair")

    monkeypatch.setattr(Config, "_repair_foreign_lane_models", classmethod(_boom))
    stored.write({**INCIDENT, "temperature": 0.3})
    loaded = Config.load()
    assert loaded["provider"] == "deepseek"
    assert loaded["temperature"] == 0.3


def test_a_retired_id_migrates_before_the_repair(stored):
    """A retired name of the lane's own provider moves to its successor and is
    kept, not reset. The catalog lists a retired name as its provider's, so the rule
    never resets one, whichever of the two repairs runs first.
    MUTATION: treat the catalog's retired names as foreign."""
    assert foreign_lane_models({"provider": "deepseek", "subagent_model": "deepseek-chat"}) == {}
    stored.write({"provider": "deepseek", "subagent_model": "deepseek-chat"})
    assert Config.load()["subagent_model"] == "deepseek-flash"
    assert stored.raw()["subagent_model"] == "deepseek-flash"


def test_inherit_rides_the_main_provider_for_the_browser_lane():
    """`inherit` follows the main provider for every lane; the browser lane read it as
    a provider name, so the rule and the runtime disagreed. MUTATION: drop the
    `inherit` branch in model_roles.browser_lane."""
    from vaf.core.model_roles import browser_lane, resolve_role

    cfg = {"provider": "deepseek", "browser_agent_provider": "Inherit",
           "browser_agent_model": "deepseek-flash"}
    assert browser_lane(cfg) == ("", "deepseek-flash")
    row = resolve_role("browser_agent", config=cfg, warn=False, explain=False, probe_local=False)
    assert (row.provider, row.model) == ("deepseek", "deepseek-flash")
    assert foreign_lane_models(cfg) == {}


def test_the_foreign_model_warning_reads_for_every_provider(_quiet):
    """No "a OpenRouter model"; and a process that read the setting before the reset
    is told to restart. MUTATION: restore the article or drop the restart hint."""
    pr.note_foreign_model("openrouter", "veyllo-chat", "subagent_model")
    text = str(_quiet.events[-1][1])
    assert "is not one of OpenRouter's models and is ignored" in text
    assert "restart VAF" in text


# ── AC5: the incident is silent after the repair ─────────────────────────────────

def test_the_incident_config_is_silent_after_load(stored, _quiet):
    """The headline: the warning every sub-agent printed. MUTATION: remove the load
    repair (subagent_model_for reports the foreign model again)."""
    stored.write(INCIDENT)
    Config.load()
    assert subagent_model_for() is None
    assert _quiet.events == []
    assert not any("[FOREIGN_MODEL]" in str(a) for a in _quiet.log)


# ── AC6: the CLI refuses a foreign lane model ────────────────────────────────────

def test_cli_refuses_a_foreign_lane_model(stored, _quiet):
    """MUTATION: drop the refusal branch in `vaf models config`."""
    stored.write({"provider": "deepseek", "subagent_model": "deepseek-v4-pro"})
    before = stored.raw()
    result = _cli("subagent_model", "veyllo-chat")
    assert result.exit_code == 2
    shown = _shown(_quiet.events, "Error")
    assert "subagent_model 'veyllo-chat' is not one of DeepSeek's models" in shown
    assert "(for example deepseek-flash)" in shown
    assert 'leave it empty for DeepSeek\'s own model: vaf models config subagent_model ""' in shown
    assert "not switched on" not in shown
    assert stored.raw() == before


def test_cli_explains_a_sub_agent_provider_that_is_not_switched_on(stored, _quiet):
    """The generic setter writes `subagent_provider` without its gate, so the lane
    still runs on the main provider; the refusal says why. MUTATION: drop the hint."""
    stored.write({"provider": "deepseek", "subagent_provider": "openai",
                  "subagent_use_separate_provider": False})
    result = _cli("subagent_model", "gpt-5.6-sol")
    assert result.exit_code == 2
    shown = _shown(_quiet.events, "Error")
    assert "The sub-agent provider 'openai' is not switched on, so sub-agents run on DeepSeek" in shown
    assert "vaf settings" in shown
    stored.write({"provider": "deepseek", "subagent_provider": "openai",
                  "subagent_use_separate_provider": True})
    assert _cli("subagent_model", "gpt-5.6-sol").exit_code == 0
    assert stored.raw()["subagent_model"] == "gpt-5.6-sol"


def test_cli_checks_and_writes_under_one_hold_of_the_lock(stored, monkeypatch):
    """Another process must not move the provider between the check and the write.
    MUTATION: check before taking the lock."""
    import vaf.core.config as config_mod

    state = {"depth": 0, "holds": 0, "checked_in": None, "written_in": None}

    @contextlib.contextmanager
    def _fake_lock():
        if state["depth"] == 0:
            state["holds"] += 1
        state["depth"] += 1
        try:
            yield
        finally:
            state["depth"] -= 1

    real_check = config_mod.foreign_lane_models

    def _check(cfg):
        if state["checked_in"] is None:
            state["checked_in"] = state["holds"] if state["depth"] else 0
        return real_check(cfg)

    real_write = Config._write_config_file.__func__

    def _write(cls, data):
        state["written_in"] = state["holds"] if state["depth"] else 0
        return real_write(cls, data)

    stored.write({"provider": "deepseek"})
    monkeypatch.setattr(Config, "_locked", classmethod(lambda cls: _fake_lock()))
    monkeypatch.setattr(config_mod, "foreign_lane_models", _check)
    monkeypatch.setattr(Config, "_write_config_file", classmethod(_write))
    assert _cli("subagent_model", "deepseek-v4-pro").exit_code == 0
    assert state["checked_in"] and state["checked_in"] == state["written_in"]


def test_cli_stores_a_fitting_lane_model(stored, _quiet):
    stored.write({"provider": "deepseek"})
    result = _cli("subagent_model", "deepseek-v4-pro")
    assert result.exit_code == 0, result.output
    assert stored.raw()["subagent_model"] == "deepseek-v4-pro"
    assert _shown(_quiet.events, "Warning") == ""


# ── Fuzz: the rule as a classifier over config values ────────────────────────────

_NAMES = list(pr.PROVIDER_SPECS)
_EXCLUDED = ("vision_model", "voice_agent_model", "failover_backup_model", "thinking_model")


def _model_pool() -> list:
    ids = list(mc.all_model_ids())
    for cat in mc.CATALOG.values():
        ids += list(cat.aliases) + list(cat.migrations)
    ids += ["auto", _GGUF, "Veyllo/VQ-1_Instruct-q4_k_m", "my-gateway-model",
            "gpt-7-nova", "deepseek-v5", "vendor/some-model", ""]
    return ids


def _draw(rng: random.Random, pool: list) -> dict:
    def model():
        m = rng.choice(pool)
        return rng.choice([m, m, f" {m} ", m.upper()]) if m else m

    def lane_name():
        return rng.choice(_NAMES + ["", "inherit"])

    cfg = {
        "provider": rng.choice(_NAMES + [""]),
        "subagent_provider": lane_name(),
        "subagent_use_separate_provider": rng.choice([True, False]),
        "browser_agent_provider": lane_name(),
        "subagent_model": model(),
        "browser_agent_model": model(),
    }
    for key in _EXCLUDED:
        if rng.random() < 0.5:
            cfg[key] = model()
    if rng.random() < 0.3:
        cfg[f"api_model_{rng.choice(pr.api_provider_names())}"] = model()
    return cfg


def _check_draw(cfg: dict) -> list:
    """The draw's oracle violations, as text."""
    from vaf.core.model_roles import resolve_role

    # (a) stable after the reset, (b) idempotent, (c) every key not reset is unchanged,
    # (d) a reset sub-agent or browser model is one the runtime drops, (e) the excluded
    # keys are never reset, (f) a kept sub-agent model is one the runtime uses.
    bad = []
    original = {**Config.DEFAULTS, **cfg}
    work = dict(cfg)
    resets = Config.reset_foreign_lane_models(work)
    if foreign_lane_models(work):                                   # (a)
        bad.append("not stable after the reset")
    again = dict(work)
    if Config.reset_foreign_lane_models(again) or again != work:    # (b)
        bad.append("not idempotent")
    for key in set(cfg) | set(work):                                # (c), (e)
        if key not in resets and cfg.get(key) != work.get(key):
            bad.append(f"touched {key}")
        if key in _EXCLUDED and key in resets:
            bad.append(f"reset {key}")
    used = subagent_model_for(config=original)
    if "subagent_model" in resets and used is not None:                      # (d)
        bad.append("reset a sub-agent model the runtime would use")
    kept = str(cfg.get("subagent_model") or "").strip()
    if ("subagent_model" not in resets and kept and kept.lower() != "deepseek-auto"
            and used != kept):                                                # (f)
        bad.append("kept a sub-agent model the runtime drops")
    lane = str(original.get("browser_agent_provider") or "").strip()
    lane = "" if lane.lower() == "inherit" else lane
    main = str(original.get("provider") or "local").strip()
    if "browser_agent_model" in resets and (lane or main) not in ("", "local"):
        browser = resolve_role("browser_agent", config=original, warn=False,
                               explain=False, probe_local=False).model
        if str(browser).strip() == str(cfg["browser_agent_model"]).strip():      # (d)
            bad.append("reset a browser model the runtime would use")
    return bad


def test_the_rule_holds_for_random_configs():
    """Seeded property test. VAF_FUZZ_DRAWS raises the draw count outside the suite."""
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "300"))
    rng = random.Random(389)
    pool = _model_pool()
    failures = []
    for i in range(draws):
        cfg = _draw(rng, pool)
        bad = _check_draw(cfg)
        if bad:
            failures.append((i, cfg, bad))
    assert failures == [], failures[:5]


# ── `vaf models config provider` and `subagent_provider` go through the switches ───

def test_cli_provider_without_key_is_refused(stored, _quiet, monkeypatch):
    """Every other route refuses a keyless provider; the generic write stored it and the
    next start booted into a backend that cannot be built. MUTATION: revert `provider` to
    the generic write - red."""
    monkeypatch.setattr(Config, "get_api_key", classmethod(lambda cls, p: ""))
    stored.write({"provider": "local"})
    before = stored.raw()
    result = _cli("provider", "deepseek")
    assert result.exit_code == 2, result.output
    assert stored.raw() == before
    assert "There is no API key for DeepSeek" in _shown(_quiet.events, "Error")
    assert _cli("provider", "acme").exit_code == 2
    assert stored.raw() == before


def test_cli_subagent_provider_writes_the_pair(stored, _quiet, monkeypatch):
    """The name alone is inert; the command switches it on. MUTATION: revert
    `subagent_provider` to the generic write (the gate stays False) - red."""
    monkeypatch.setattr(Config, "get_api_key", classmethod(lambda cls, p: "sk-test"))
    stored.write({"provider": "deepseek"})
    result = _cli("subagent_provider", "openai")
    assert result.exit_code == 0, result.output
    raw = stored.raw()
    assert raw["subagent_provider"] == "openai" and raw["subagent_use_separate_provider"] is True
    assert "Sub-agents run on OpenAI" in _shown(_quiet.events, "Success")
    assert _cli("subagent_provider", "inherit").exit_code == 0
    assert stored.raw()["subagent_use_separate_provider"] is False


def test_cli_subagent_provider_refuses_keyless_and_unknown(stored, _quiet, monkeypatch):
    monkeypatch.setattr(Config, "get_api_key", classmethod(lambda cls, p: ""))
    stored.write({"provider": "deepseek"})
    before = stored.raw()
    assert _cli("subagent_provider", "openai").exit_code == 2
    assert "No API key configured for openai" in _shown(_quiet.events, "Error")
    assert _cli("subagent_provider", "acme").exit_code == 2
    assert stored.raw() == before
    assert _cli("subagent_provider", "local").exit_code == 0, "local needs no key"


def test_cli_subagent_provider_local_is_refused_under_the_api_profile(stored, _quiet):
    """The same refusal `provider local` gives there. MUTATION: drop the check - red."""
    stored.write({"provider": "deepseek", "inference_profile": "api"})
    before = stored.raw()
    result = _cli("subagent_provider", "local")
    assert result.exit_code == 2
    assert 'profile "api"' in _shown(_quiet.events, "Error")
    assert stored.raw() == before
