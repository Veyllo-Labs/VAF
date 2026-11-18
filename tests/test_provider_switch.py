# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`provider_switch.switch_provider`: the one provider switch of the terminal routes.

The terminal app (through `AgentBridge.apply_provider_change`), `vaf settings` and `vaf
models config provider` each wrote `provider` by hand, with three different key checks
and three ideas of applying it. These pin the shared order: name, profile, key, write
under one lock hold, one forced reload. Lane models are reset by `Config.save`, never by
the switch itself.

MUTATIONS, each turns the named test red:
- drop `force=True` -> test_writes_two_keys_then_one_forced_reload;
- write outside `Config._locked()` -> test_written_under_one_lock_hold;
- add a `Config.set("subagent_model", "")` -> test_no_lane_key_is_written_by_the_switch;
- remove each refusal guard -> test_refusals_write_nothing[...];
- call `verify` without a typed key -> test_no_verify_without_typed_key;
- put the key into the `key_unverified` text -> test_key_never_in_text;
- a direct `Config.set("provider", ...)` in vaf/cli -> test_cli_writes_provider_only_through_the_switch;
- the settings menu writing by hand -> test_settings_menu_switches_through_core.
"""
import ast
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf.core.agent as agent_mod
from vaf.core import provider_switch
from vaf.core.config import Config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stored(tmp_path, monkeypatch):
    """A synthetic config file Config reads and writes; records the reloads."""
    from vaf.core import migrations

    path = tmp_path / "config.json"
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "_filelock", None)
    reloads = []
    monkeypatch.setattr(agent_mod, "reload_all_api_backends",
                        lambda **kw: reloads.append(kw) or 1)

    def write(cfg: dict):
        path.write_text(json.dumps({"config_format_version": migrations.CONFIG_FORMAT_VERSION,
                                    **cfg}), encoding="utf-8")

    def raw() -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    write({"provider": "local"})
    return SimpleNamespace(write=write, raw=raw, reloads=reloads)


@pytest.fixture
def keys(monkeypatch):
    """The key store as a dict: nothing reaches the encrypted store of this machine."""
    store = {}
    monkeypatch.setattr(Config, "get_api_key", classmethod(lambda cls, p: store.get(p, "")))
    monkeypatch.setattr(Config, "set_api_key",
                        classmethod(lambda cls, p, k: store.__setitem__(p, k)))
    return store


def _recording_set(monkeypatch):
    written = []
    real = Config.set.__func__

    def _set(cls, key, value):
        written.append(key)
        return real(cls, key, value)
    monkeypatch.setattr(Config, "set", classmethod(_set))
    return written


# ── AC3: the write and the reload ─────────────────────────────────────────────────

def test_writes_two_keys_then_one_forced_reload(stored, keys, monkeypatch):
    keys["anthropic"] = "sk-stored-key-value"
    written = _recording_set(monkeypatch)
    result = provider_switch.switch_provider("anthropic", "claude-sonnet-4-6")
    assert (result.status, result.code, result.changed) == ("done", "switched", 1)
    assert written == ["provider", "api_model_anthropic"]
    assert stored.raw()["provider"] == "anthropic"
    assert stored.raw()["api_model_anthropic"] == "claude-sonnet-4-6"
    assert stored.reloads == [{"force": True}], "force=True catches a moved key or model"


def test_no_live_agent_moved_is_stored(stored, keys, monkeypatch):
    keys["anthropic"] = "sk-stored-key-value"
    monkeypatch.setattr(agent_mod, "reload_all_api_backends", lambda **kw: 0)
    result = provider_switch.switch_provider("anthropic")
    assert (result.status, result.code, result.changed) == ("done", "stored", 0)


def test_written_under_one_lock_hold(stored, keys, monkeypatch):
    """Another process must not move the config between the reset read and the writes."""
    keys["anthropic"] = "sk-stored-key-value"
    state = {"depth": 0, "holds": 0, "in": []}

    @contextlib.contextmanager
    def _fake_lock():
        if state["depth"] == 0:
            state["holds"] += 1
        state["depth"] += 1
        try:
            yield
        finally:
            state["depth"] -= 1

    real_write = Config._write_config_file.__func__

    def _write(cls, data):
        state["in"].append(state["holds"] if state["depth"] else 0)
        return real_write(cls, data)

    monkeypatch.setattr(Config, "_locked", classmethod(lambda cls: _fake_lock()))
    monkeypatch.setattr(Config, "_write_config_file", classmethod(_write))
    start = state["holds"]
    provider_switch.switch_provider("anthropic", "claude-sonnet-4-6")
    assert len(state["in"]) == 2, state
    assert state["in"][0] and state["in"][0] == state["in"][1] == start + 1, \
        "the two writes were not made under one hold of the lock"


def test_no_lane_key_is_written_by_the_switch(stored, keys, monkeypatch):
    """The reset is Config.save's; the switch only reports it."""
    keys["deepseek"] = "sk-stored-key-value"
    # Fitting the old provider, so the load does not repair them first.
    stored.write({"provider": "veyllo", "subagent_model": "veyllo-chat",
                  "browser_agent_model": "veyllo-chat"})
    written = _recording_set(monkeypatch)
    result = provider_switch.switch_provider("deepseek", "deepseek-v4-pro")
    assert written == ["provider", "api_model_deepseek"]
    assert set(result.resets) == {"subagent_model", "browser_agent_model"}
    assert result.resets["subagent_model"] == ("veyllo-chat", "deepseek")
    raw = stored.raw()
    assert raw["subagent_model"] == "" and raw["browser_agent_model"] == "", \
        "Config.save resets them on the switch's write"


def test_local_ignores_a_model(stored, keys, monkeypatch):
    stored.write({"provider": "anthropic"})
    written = _recording_set(monkeypatch)
    result = provider_switch.switch_provider("LOCAL ", "some-model")
    assert (result.status, result.provider, result.model) == ("done", "local", "")
    assert written == ["provider"]


# ── AC4: the refusals ─────────────────────────────────────────────────────────────

def _unreadable(cls, provider):
    from vaf.core.api_keys import ApiKeyUnavailable
    raise ApiKeyUnavailable(f"The stored API key for '{provider}' could not be read (test).")


@pytest.mark.parametrize("cause, provider, code", [
    ("unknown", "acme", "unknown_provider"),
    ("profile", "local", "profile_api"),
    ("no_key", "anthropic", "no_key"),
    ("unreadable", "anthropic", "key_unavailable"),
])
def test_refusals_write_nothing(stored, keys, monkeypatch, cause, provider, code):
    stored.write({"provider": "openai"})
    if cause == "profile":
        stored.write({"provider": "openai", "inference_profile": "api"})
    if cause == "unreadable":
        monkeypatch.setattr(Config, "get_api_key", classmethod(_unreadable))
    before = stored.raw()
    result = provider_switch.switch_provider(provider, "m")
    assert (result.status, result.code) == ("refused", code)
    assert stored.raw() == before, "a refused switch wrote the config"
    assert stored.reloads == []
    if cause == "profile":
        assert 'profile "api"' in result.text
    if cause == "unknown":
        assert "acme" in result.text and "anthropic" in result.text


def test_unverified_key_is_kept_provider_unchanged(stored, keys):
    result = provider_switch.switch_provider("anthropic", new_key="sk-typed-key-value",
                                             verify=lambda p: False)
    assert (result.status, result.code) == ("refused", "key_unverified")
    assert keys == {"anthropic": "sk-typed-key-value"}, "the key was discarded"
    assert stored.raw()["provider"] == "local"
    assert "did not verify" in result.text


def test_no_verify_without_typed_key(stored, keys):
    keys["anthropic"] = "sk-stored-key-value"
    asked = []
    result = provider_switch.switch_provider("anthropic", verify=asked.append)
    assert result.status == "done" and asked == [], "a stored key was re-tested"


def test_verify_none_skips(stored, keys):
    result = provider_switch.switch_provider("anthropic", new_key="sk-typed-key-value",
                                             verify=None)
    assert result.status == "done" and stored.raw()["provider"] == "anthropic"


def test_key_never_in_text(stored, keys):
    secret = "sk-typed-key-value-1234"
    for verifies in (False, True):
        result = provider_switch.switch_provider("anthropic", new_key=secret,
                                                 verify=lambda p, v=verifies: v)
        assert secret not in result.text and secret not in repr(result)


# ── AC11: no other provider writer under vaf/cli ──────────────────────────────────

# Deliberate: `vaf setup` writes the key, provider and vision_provider exactly as the web
# wizard does, before any agent exists (named in vaf/core/provider_switch.py).
_ALLOWED = {"vaf/cli/cmd/setup.py"}


def _provider_writes(tree: ast.AST) -> list:
    """Lines of `Config.set("provider", ...)` / `Config.set("api_model_...")` /
    `Config.set(f"api_model_...")`."""
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "Config" and node.args):
            continue
        first = node.args[0]
        text = ""
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            text = first.value
        elif isinstance(first, ast.JoinedStr) and first.values and isinstance(
                first.values[0], ast.Constant):
            text = str(first.values[0].value)
        if text == "provider" or text.startswith("api_model_"):
            found.append(node.lineno)
    return found


def test_cli_writes_provider_only_through_the_switch():
    offenders = []
    for path in sorted((ROOT / "vaf" / "cli").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in _ALLOWED:
            continue
        lines = _provider_writes(ast.parse(path.read_text(encoding="utf-8")))
        offenders += [f"{rel}:{n}" for n in lines]
    assert offenders == [], f"write the provider through provider_switch: {offenders}"
    # The scan finds what it looks for: the allowed file still holds one.
    assert _provider_writes(ast.parse((ROOT / "vaf/cli/cmd/setup.py").read_text(
        encoding="utf-8")))


def test_settings_menu_switches_through_core(monkeypatch):
    """`vaf settings` > AI Provider, every branch that moves the provider or its model."""
    from vaf.cli.cmd import settings

    calls = []
    monkeypatch.setattr(provider_switch, "switch_provider",
                        lambda p, m="", *, new_key="", verify=None: calls.append(
                            (p, m, new_key)) or provider_switch.ProviderSwitch(
                                "done", "stored", "ok", p, m))
    monkeypatch.setattr(settings.UI, "clear", lambda: None)
    monkeypatch.setattr(settings.UI, "panel", lambda *a, **k: None)
    monkeypatch.setattr(settings.UI.console, "input", lambda *a, **k: "")
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: {
        "provider": "anthropic", "auto_start_local_server": True}.get(k, d)))
    monkeypatch.setattr(Config, "set", classmethod(lambda cls, k, v: None))
    monkeypatch.setattr(Config, "get_api_key", classmethod(lambda cls, p: ""))
    monkeypatch.setattr(settings, "_select_api_model", lambda p: None)
    answers = []
    monkeypatch.setattr(settings.inquirer, "prompt", lambda q: answers.pop(0))
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda *a: "sk-typed-key-value")

    answers[:] = [{"provider": "local"}, None]
    settings.api_provider_menu()
    answers[:] = [{"provider": "openai"}]
    settings.api_provider_menu()
    assert calls == [("local", "", ""), ("openai", "", "sk-typed-key-value")]

    from vaf.core.api_backend import APIBackendManager
    monkeypatch.setattr(APIBackendManager, "get_available_models",
                        staticmethod(lambda p: ["claude-a", "claude-b"]))
    answers[:] = [{"model": "claude-b"}]
    settings.select_api_model_menu()
    assert calls[-1] == ("anthropic", "claude-b", "")
