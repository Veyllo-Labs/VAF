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
