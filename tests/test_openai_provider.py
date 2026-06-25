# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""OpenAIProvider compatibility tests.

Pins the per-model parameter gating: classic chat models (gpt-4o family) get
`max_tokens` + `temperature`, while reasoning models (o1/o3/o4 series, gpt-5) get
`max_completion_tokens` and NO `temperature` (those reject `max_tokens` and any
non-default temperature with HTTP 400). Faked client — no network, no API key.
"""
import pytest

from vaf.core.api_backend import OpenAIProvider


# ── Fake openai client (captures the kwargs passed to chat.completions.create) ─

class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return iter([])  # stream path: no chunks needed to inspect kwargs


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()


def _provider(name="openai"):
    p = OpenAIProvider(name, "dummy-key")
    p.client = _FakeClient()
    return p


def _drive(p, model, tools=None, temperature=0.7, max_tokens=8192):
    list(p.chat_completion([{"role": "user", "content": "hi"}],
                           temperature, max_tokens, True, model, tools))
    return p.client.chat.completions.kwargs


# ── Reasoning-model detection matrix ──────────────────────────────────────────

def test_is_reasoning_model_matrix():
    reasoning = ["o1", "o1-mini", "o1-preview", "o3", "o3-mini", "o4-mini",
                 "gpt-5", "gpt-5-mini", "openai/o3-mini"]
    classic = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo",
               "openai/gpt-4o"]
    assert all(OpenAIProvider._is_reasoning_model(m) for m in reasoning)
    assert not any(OpenAIProvider._is_reasoning_model(m) for m in classic)


# ── Classic chat models keep max_tokens + temperature ─────────────────────────

def test_classic_model_uses_max_tokens_and_temperature():
    kw = _drive(_provider(), "gpt-4o", temperature=0.5, max_tokens=4096)
    assert kw["max_tokens"] == 4096
    assert kw["temperature"] == 0.5
    assert "max_completion_tokens" not in kw


# ── Reasoning models switch to max_completion_tokens, drop temperature ────────

def test_reasoning_model_uses_max_completion_tokens_no_temperature():
    kw = _drive(_provider(), "o3-mini", temperature=0.7, max_tokens=8192)
    assert kw["max_completion_tokens"] == 8192
    assert "max_tokens" not in kw
    assert "temperature" not in kw


def test_gpt5_treated_as_reasoning():
    kw = _drive(_provider(), "gpt-5", temperature=0.7)
    assert "max_completion_tokens" in kw
    assert "max_tokens" not in kw
    assert "temperature" not in kw


def test_openrouter_does_not_gate_reasoning_models():
    # OpenRouter normalizes around max_tokens for every model — gating would lose the
    # token limit. A reasoning route via OpenRouter must still get max_tokens + temperature.
    kw = _drive(_provider("openrouter"), "openai/o3-mini", temperature=0.7, max_tokens=4096)
    assert kw["max_tokens"] == 4096
    assert kw["temperature"] == 0.7
    assert "max_completion_tokens" not in kw


# ── parallel_tool_calls gating ────────────────────────────────────────────────

def test_parallel_tool_calls_only_for_classic_models():
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    classic_kw = _drive(_provider(), "gpt-4o", tools=tools)
    assert classic_kw.get("parallel_tool_calls") is True
    assert classic_kw["tools"] == tools

    reasoning_kw = _drive(_provider(), "o4-mini", tools=tools)
    assert "parallel_tool_calls" not in reasoning_kw
    assert reasoning_kw["tools"] == tools


# ── stream kwarg is still passed to the OpenAI SDK (it accepts it) ─────────────

def test_stream_kwarg_present_for_openai():
    kw = _drive(_provider(), "gpt-4o")
    assert kw["stream"] is True
    assert kw["stream_options"] == {"include_usage": True}
