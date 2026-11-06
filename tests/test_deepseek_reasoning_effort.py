# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""DeepSeek's thinking effort (`deepseek_reasoning_effort`).

DeepSeek's thinking-mode guide documents `reasoning_effort` low, high and max for the
OpenAI format; thinking is on by default at high. Measured against the live API
(2026-11-06, one function tool, deepseek-flash and deepseek-v4-pro): each of the
three answers 200, and an unknown value answers 422 on every call. So the effort is
sent for provider deepseek only, only as one of the three values, and an empty key
leaves the request exactly as it was before the key existed.

Both lanes that build a DeepSeek body are covered: the SDK lane through
APIBackendManager._chat_single, and the coder's raw HTTP lane (static guard, the
request lives inside a run loop no unit test can drive). Faked clients, no network.
"""
import logging
import re
from pathlib import Path

import pytest

from vaf.core import api_backend
from vaf.core.api_backend import (
    APIBackendManager,
    DEEPSEEK_REASONING_EFFORTS,
    OpenAIProvider,
    deepseek_reasoning_effort,
    openai_request_params,
)
from vaf.core.config import Config

_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {}}}]


def _params(provider, model="deepseek-flash", effort=None, has_tools=True):
    return openai_request_params(provider, model, temperature=0.7, max_tokens=8192,
                                 has_tools=has_tools, reasoning_effort=effort)


# ── The builder ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_deepseek_gets_the_chosen_effort(model, effort):
    assert _params("deepseek", model, effort)["reasoning_effort"] == effort


@pytest.mark.parametrize("effort", [None, ""])
def test_no_effort_leaves_the_body_as_it_was(effort):
    before = openai_request_params("deepseek", "deepseek-flash", temperature=0.7,
                                   max_tokens=8192, has_tools=True)
    assert "reasoning_effort" not in before
    assert _params("deepseek", effort=effort) == before


@pytest.mark.parametrize("effort", ["bogus", "ultra", "none", "minimal", "xhigh", "medium"])
def test_a_value_outside_the_three_never_reaches_the_body(effort):
    # The API answers an unknown value with a 422 on every call; the aliases it does
    # accept are deliberately not offered, and `none` is not how thinking is switched off.
    assert "reasoning_effort" not in _params("deepseek", effort=effort)


def test_the_builder_normalises_like_the_resolver():
    assert _params("deepseek", effort=" MAX ")["reasoning_effort"] == "max"


@pytest.mark.parametrize("provider,model", [
    ("openai", "gpt-4o"),
    ("veyllo", "veyllo-chat"),
    ("openrouter", "deepseek/deepseek-v4-pro"),
    ("local", "llama3"),
])
def test_other_providers_ignore_the_effort(provider, model):
    without = openai_request_params(provider, model, temperature=0.7, max_tokens=8192,
                                    has_tools=True)
    assert _params(provider, model, "max") == without


def test_openai_keeps_its_own_none_effort():
    # The direct-OpenAI "none" for gpt-5.6 + tools is untouched by a DeepSeek effort.
    assert _params("openai", "gpt-5.6-terra", "max")["reasoning_effort"] == "none"


# ── The resolver ──────────────────────────────────────────────────────────────

def test_resolver_reads_the_three_values_and_empty():
    for value in DEEPSEEK_REASONING_EFFORTS:
        assert deepseek_reasoning_effort({"deepseek_reasoning_effort": value.upper()}) == value
    assert deepseek_reasoning_effort({}) is None
    assert deepseek_reasoning_effort({"deepseek_reasoning_effort": ""}) is None
    assert deepseek_reasoning_effort(None) is None


def test_resolver_warns_once_for_an_invalid_value(caplog, monkeypatch):
    monkeypatch.setattr(api_backend, "_deepseek_effort_warned", set())
    with caplog.at_level(logging.WARNING, logger="vaf.api_backend"):
        for _ in range(3):
            assert deepseek_reasoning_effort({"deepseek_reasoning_effort": "ultra"}) is None
    hits = [r for r in caplog.records if "deepseek_reasoning_effort" in r.getMessage()]
    assert len(hits) == 1


# ── The SDK lane: APIBackendManager._chat_single -> OpenAIProvider ────────────

class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return iter([])


class _FakeClient:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions()


def _manager(provider_name, config, model):
    m = APIBackendManager.__new__(APIBackendManager)
    m.provider_name = provider_name
    m.caller_config = None
    m._embedded = True
    m.config = config
    m.provider = OpenAIProvider(provider_name, "dummy-key")
    m.provider.client = _FakeClient()
    m.session_usage = {"input_tokens": 0, "output_tokens": 0}
    m.last_request_usage = {"input_tokens": 0, "output_tokens": 0}
    list(m._chat_single([{"role": "user", "content": "hi"}], model=model, tools=_TOOLS))
    return m.provider.client.chat.completions.kwargs


def test_an_embedded_deepseek_manager_sends_its_configs_effort():
    sent = _manager("deepseek", {"deepseek_reasoning_effort": "max"}, "deepseek-flash")
    assert sent["reasoning_effort"] == "max"
    assert sent["model"] == "deepseek-flash"


def test_a_deepseek_manager_without_the_key_sends_no_effort():
    sent = _manager("deepseek", {}, "deepseek-v4-pro")
    assert "reasoning_effort" not in sent


def test_another_provider_does_not_read_the_key():
    class _Watch(dict):
        def get(self, key, default=None):
            assert key != "deepseek_reasoning_effort", "a non-DeepSeek call read the key"
            return super().get(key, default)

    sent = _manager("openai", _Watch({"deepseek_reasoning_effort": "max"}), "gpt-4o")
    assert "reasoning_effort" not in sent


def test_product_mode_reads_the_fresh_machine_config(monkeypatch):
    monkeypatch.setattr(Config, "load", classmethod(
        lambda cls, *a, **k: {"deepseek_reasoning_effort": "low"}))
    m = APIBackendManager.__new__(APIBackendManager)
    m.provider_name = "deepseek"
    m.caller_config = None
    m._embedded = False
    m.config = {}
    m.provider = OpenAIProvider("deepseek", "dummy-key")
    m.provider.client = _FakeClient()
    m.session_usage = {"input_tokens": 0, "output_tokens": 0}
    m.last_request_usage = {"input_tokens": 0, "output_tokens": 0}
    list(m._chat_single([{"role": "user", "content": "hi"}], model="deepseek-flash",
                        tools=_TOOLS))
    assert m.provider.client.chat.completions.kwargs["reasoning_effort"] == "low"


# ── The coder's raw HTTP lane (static guard) ──────────────────────────────────

def test_both_coder_request_sites_pass_the_deepseek_effort():
    text = (_ROOT / "vaf" / "tools" / "coder.py").read_bytes().decode("utf-8")
    calls = [m.start() for m in re.finditer(r"openai_request_params\(", text)]
    sites = [text[i:i + 400] for i in calls]
    assert len(sites) == 2, f"expected the two coder request sites, found {len(sites)}"
    for block in sites:
        assert "reasoning_effort=_deepseek_effort" in block[:block.index(")") + 120], block
    assert "_deepseek_effort = deepseek_reasoning_effort(Config)" in text


# ── Who may write it ──────────────────────────────────────────────────────────

def test_the_key_is_admin_only():
    # Thinking tokens are billed: like anthropic_thinking, instance spend.
    assert Config.is_global_config_key("deepseek_reasoning_effort")


# ── The Settings control covers every job that can reach DeepSeek ─────────────

def test_settings_lane_list_covers_every_chat_provider_role():
    from vaf.core.model_roles import ROLE_CONFIG_KEYS
    ts = (_ROOT / "web" / "lib" / "deepseekEffort.ts").read_bytes().decode("utf-8")
    block = ts[ts.index("DEEPSEEK_LANE_KEYS = ["):]
    block = block[:block.index("]")]
    listed = set(re.findall(r"'([a-z_]+)'", block))
    # Every role key that names a chat-model provider. The speech keys name the STT/TTS
    # engine providers, which never call DeepSeek's chat API; the sub-agent gate is a
    # switch, not a provider name (deepseekInUse reads it for subagent_provider).
    roles = {k for k in ROLE_CONFIG_KEYS
             if (k == "provider" or k.endswith("_provider")) and not k.startswith("speech_")
             and k != "subagent_use_separate_provider"}
    assert roles <= listed, f"Settings would hide the effort for: {sorted(roles - listed)}"
    modal = (_ROOT / "web" / "components" / "SettingsModal.tsx").read_bytes().decode("utf-8")
    assert "deepseekInUse(localConfig, providerHasKey('deepseek'))" in modal


# ── The one check every writer shares ─────────────────────────────────────────

from vaf.core.api_backend import check_deepseek_reasoning_effort  # noqa: E402


@pytest.mark.parametrize("raw,stored", [
    ("", ""), ("   ", ""), (None, ""), ("low", "low"), (" High ", "high"), ("MAX", "max"),
])
def test_check_returns_what_may_be_stored(raw, stored):
    assert check_deepseek_reasoning_effort(raw) == stored


@pytest.mark.parametrize("raw", ["bogus", "ultra", "none", "0", 0, "medium"])
def test_check_refuses_with_the_accepted_values_named(raw):
    with pytest.raises(ValueError) as exc:
        check_deepseek_reasoning_effort(raw)
    text = str(exc.value)
    assert f"'{raw}'" in text and "low, high or max" in text and "empty value" in text


@pytest.mark.parametrize("raw", ["", "low", "HIGH", " max", "bogus", "ultra", "0", "none"])
def test_the_resolver_and_the_check_accept_the_same_set(raw, monkeypatch):
    monkeypatch.setattr(api_backend, "_deepseek_effort_warned", set())
    try:
        expected = check_deepseek_reasoning_effort(raw) or None
    except ValueError:
        expected = None
    assert deepseek_reasoning_effort({"deepseek_reasoning_effort": raw}) == expected


def _cli(monkeypatch, *args):
    from typer.testing import CliRunner
    from vaf.cli.cmd import models
    written = {}
    monkeypatch.setattr(Config, "set", classmethod(lambda cls, k, v: written.__setitem__(k, v)))
    result = CliRunner().invoke(models.app, ["config", *args])
    return result, written


def test_the_cli_refuses_a_value_the_api_would_refuse(monkeypatch):
    result, written = _cli(monkeypatch, "deepseek_reasoning_effort", "bogus")
    assert result.exit_code == 2
    shown = " ".join(result.output.split())
    assert "low, high or max" in shown and "empty value" in shown
    assert written == {}, "a refused value must not be stored"


@pytest.mark.parametrize("raw,stored", [("MAX", "max"), ("", ""), ("low", "low")])
def test_the_cli_stores_the_checked_value(monkeypatch, raw, stored):
    result, written = _cli(monkeypatch, "deepseek_reasoning_effort", raw)
    assert result.exit_code == 0, result.output
    assert written == {"deepseek_reasoning_effort": stored}


def test_the_cli_refuses_digits_before_the_number_parse(monkeypatch):
    # The generic setter turns "0" into int 0; checked first, it is refused, not stored.
    result, written = _cli(monkeypatch, "deepseek_reasoning_effort", "0")
    assert result.exit_code == 2 and written == {}


def test_a_value_with_a_line_break_logs_one_line(caplog, monkeypatch):
    # The message quotes the value with repr, so a newline in a hand-edited value cannot
    # forge a second log line.
    monkeypatch.setattr(api_backend, "_deepseek_effort_warned", set())
    with caplog.at_level(logging.WARNING, logger="vaf.api_backend"):
        deepseek_reasoning_effort({"deepseek_reasoning_effort": "x\n[deepseek] FORGED line"})
    hits = [r.getMessage() for r in caplog.records if "deepseek_reasoning_effort" in r.getMessage()]
    assert len(hits) == 1 and "\n" not in hits[0], hits


@pytest.mark.parametrize("raw", ["[/bold]", "[red]x[/red]"])
def test_the_cli_refuses_markup_like_any_other_bad_value(monkeypatch, raw):
    # Rich renders markup in UI.error: "[/bold]" raised a MarkupError (exit 1, traceback),
    # and "[red]x[/red]" was drawn red instead of shown as typed.
    result, written = _cli(monkeypatch, "deepseek_reasoning_effort", raw)
    assert result.exit_code == 2, (result.exit_code, result.exception)
    shown = " ".join(result.output.split())  # Rich wraps at the terminal width
    assert raw in shown and "low, high or max" in shown
    assert written == {}


def test_set_model_shows_a_markup_like_name_as_typed(monkeypatch):
    from typer.testing import CliRunner
    from vaf.cli.cmd import models
    written = {}
    monkeypatch.setattr(Config, "set", classmethod(lambda cls, k, v: written.__setitem__(k, v)))
    result = CliRunner().invoke(models.app, ["set", "[/bold]"])
    assert result.exit_code == 0, (result.exit_code, result.exception)
    assert "[/bold]" in result.output and written == {"model": "[/bold]"}


def test_the_generic_setter_shows_markup_like_values_as_typed(monkeypatch):
    result, written = _cli(monkeypatch, "some_key", "[/bold]")
    assert result.exit_code == 0, (result.exit_code, result.exception)
    assert "[/bold]" in result.output and written == {"some_key": "[/bold]"}


# ── The web helpers, run for real in node ─────────────────────────────────────

_TS = _ROOT / "web" / "node_modules" / "typescript"


def _run_ts(expr_cases):
    import json
    import shutil
    import subprocess
    if shutil.which("node") is None or not _TS.is_dir():
        pytest.skip("node or web/node_modules/typescript is not installed")
    script = (
        "const ts = require(process.argv[1]);"
        "const fs = require('fs');"
        "const src = fs.readFileSync(process.argv[2], 'utf8');"
        "const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS,"
        " target: ts.ScriptTarget.ES2019}}).outputText;"
        "const m = {exports: {}}; new Function('module', 'exports', js)(m, m.exports);"
        "const cases = JSON.parse(process.argv[3]);"
        "process.stdout.write(JSON.stringify(cases.map(([fn, args]) => m.exports[fn](...args))));"
    )
    out = subprocess.run(["node", "-e", script, str(_TS), str(_ROOT / "web" / "lib" / "deepseekEffort.ts"),
                          json.dumps(expr_cases)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_deepseek_in_use_covers_a_key_and_every_lane():
    cases = [
        ["deepseekInUse", [{}, True]],                                   # a key alone
        ["deepseekInUse", [{"provider": "openai"}, False]],              # nothing
        ["deepseekInUse", [{"thinking_provider": "DeepSeek"}, False]],
        ["deepseekInUse", [{"failover_backup_provider": "deepseek"}, False]],
        ["deepseekInUse", [{"voice_agent_provider": "deepseek"}, False]],
        ["deepseekInUse", [{"subagent_provider": "deepseek"}, False]],   # gate off: inert
        ["deepseekInUse", [{"subagent_provider": "deepseek",
                            "subagent_use_separate_provider": True}, False]],
    ]
    assert _run_ts(cases) == [True, False, True, True, True, False, True]


def test_the_select_shows_a_stale_value_as_ignored():
    cases = [
        ["effortSelectValue", [""]],
        ["effortSelectValue", [None]],
        ["effortSelectValue", [" MAX "]],
        ["effortSelectValue", ["bogus"]],
    ]
    assert _run_ts(cases) == [
        {"value": "", "invalid": False},
        {"value": "", "invalid": False},
        {"value": "max", "invalid": False},
        {"value": "bogus", "invalid": True},
    ]


def test_the_ai_tab_loads_the_stored_key_state_and_shows_the_ignored_hint():
    modal = (_ROOT / "web" / "components" / "SettingsModal.tsx").read_bytes().decode("utf-8")
    # Opened directly on the AI tab, storedKeys stayed null and a stored DeepSeek key read as
    # none, hiding the select. One fetch, the existing one, on both tabs.
    assert "(activeTab !== 'general' && activeTab !== 'ai')" in modal
    assert modal.count("fetch('/api/config/api-keys'") == 1
    assert "effortSelectValue(localConfig.deepseek_reasoning_effort)" in modal
    assert re.search(r"\{effort\.invalid && \(\s*<p[^>]*>\s*\{tAi\('deepseekEffortInvalid'", modal)
