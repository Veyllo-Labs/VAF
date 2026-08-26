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


# ══════════════════════════════════════════════════════════════════════════════
# reasoning_effort: the gpt-5.6 family refuses function tools without it
# ══════════════════════════════════════════════════════════════════════════════
# Measured against the live API, one variable at a time, with a function tool and
# no reasoning_effort in the body:
#   gpt-5.1 / 5.2 / 5.4 / 5.4-mini / 5.4-nano / 5.5   200
#   gpt-5.6-luna / gpt-5.6-terra / gpt-5.6-sol        400 ("Function tools with
#       reasoning_effort are not supported ... use /v1/responses or set
#       reasoning_effort to 'none'"), and 200 once "none" is sent
#   o1 / o3-mini / o4-mini    200 without it, 400 WITH it ("does not support 'none'")
#   gpt-4o                    200 without it, 400 WITH it ("Unrecognized request
#                             argument supplied: reasoning_effort")
# So the field can be neither omitted for everyone nor sent to every reasoning
# model. These tests pin both halves of that.

from vaf.core.api_backend import (  # noqa: E402
    openai_request_params,
    openai_tools_need_effort_none,
    note_openai_tools_effort_refusal,
)

_TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
_REFUSAL = ("Error code: 400 - {'error': {'message': \"Function tools with reasoning_effort "
            "are not supported for {model} in /v1/chat/completions. To use function tools, "
            "use /v1/responses or set reasoning_effort to 'none'.\"}}")


def _params(model, provider="openai", has_tools=True):
    return openai_request_params(provider, model, temperature=0.7,
                                 max_tokens=8192, has_tools=has_tools)


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"])
def test_gpt56_family_gets_effort_none_with_tools(model):
    assert _params(model)["reasoning_effort"] == "none"


@pytest.mark.parametrize("model", ["gpt-5.1", "gpt-5.2", "gpt-5.4", "gpt-5.4-mini",
                                   "gpt-5.5", "o1", "o3-mini", "o4-mini", "gpt-4o"])
def test_models_that_accept_tools_without_it_never_get_it(model):
    # The o-series and gpt-4o answer 400 when "none" is sent, so a blanket value
    # would break the models that work today.
    assert "reasoning_effort" not in _params(model)


def test_effort_none_is_a_tool_turn_rule_not_a_model_rule():
    # A tool-free call (vision description, summary, compaction) keeps the model's
    # server-side reasoning; only turns carrying tools give it up.
    assert "reasoning_effort" not in _params("gpt-5.6-luna", has_tools=False)


def test_openrouter_never_gets_effort_none():
    # OpenRouter normalizes the request itself and is excluded from every
    # OpenAI-direct parameter rule.
    assert "reasoning_effort" not in _params("openai/gpt-5.6-luna", provider="openrouter")


def test_gpt56_keeps_the_reasoning_token_key_and_drops_temperature():
    p = _params("gpt-5.6-luna")
    assert p["max_completion_tokens"] == 8192
    assert "max_tokens" not in p and "temperature" not in p


# ── The wiring, not just the stage: the field reaches the SDK request ─────────

def test_request_carries_effort_none_for_gpt56():
    kw = _drive(_provider(), "gpt-5.6-luna", tools=_TOOLS)
    assert kw["reasoning_effort"] == "none"
    assert kw["tools"] == _TOOLS
    assert "max_tokens" not in kw


def test_request_omits_effort_for_o_series():
    kw = _drive(_provider(), "o4-mini", tools=_TOOLS)
    assert "reasoning_effort" not in kw


# ── A family released later teaches itself, once, and cannot loop ─────────────

class _RefusingCompletions(_FakeCompletions):
    """400s on the first call the way the live API does, then succeeds."""

    def __init__(self, model):
        super().__init__()
        self.calls = []
        self._model = model

    def create(self, **kwargs):
        self.calls.append(kwargs)
        self.kwargs = kwargs
        if kwargs.get("reasoning_effort") != "none":
            raise RuntimeError(_REFUSAL.replace("{model}", self._model))
        return iter([])


def test_refusal_is_learned_and_the_retry_carries_the_field():
    model = "gpt-9.9-testonly"          # deliberately outside every seeded family
    assert not openai_tools_need_effort_none(model)
    p = _provider()
    p.client.chat.completions = _RefusingCompletions(model)
    list(p.chat_completion([{"role": "user", "content": "hi"}], 0.7, 8192, True, model, _TOOLS))
    calls = p.client.chat.completions.calls
    assert len(calls) == 2, "expected exactly one retry, not a loop"
    assert "reasoning_effort" not in calls[0]
    assert calls[1]["reasoning_effort"] == "none"
    assert openai_tools_need_effort_none(model)
    # A learned model IS a reasoning model: the retry must not carry max_tokens,
    # which every reasoning model measured so far rejects.
    assert "max_tokens" not in calls[1] and "temperature" not in calls[1]


def test_the_o_series_refusal_is_never_mistaken_for_this_one():
    # Same parameter, opposite meaning: "Unsupported value: 'reasoning_effort'
    # does not support 'none'". Recording it would break a working model.
    assert not note_openai_tools_effort_refusal(
        "o3-mini", "Unsupported value: 'reasoning_effort' does not support 'none'.")
    assert not openai_tools_need_effort_none("o3-mini")


def test_a_second_refusal_for_the_same_model_is_not_retried():
    model = "gpt-9.8-testonly"
    err = _REFUSAL.replace("{model}", model)
    assert note_openai_tools_effort_refusal(model, err) is True
    assert note_openai_tools_effort_refusal(model, err) is False


# ══════════════════════════════════════════════════════════════════════════════
# The coder builds its own body and must read the same rules
# ══════════════════════════════════════════════════════════════════════════════
# The coder talks raw HTTP instead of using the SDK, so it is the second place
# that decides the request shape. It used to hardcode `max_tokens` plus a
# `temperature`, which is a 400 on every gpt-5 model ("Unsupported parameter:
# 'max_tokens' ... Use 'max_completion_tokens'"), so an OpenAI coder run on that
# whole family could not start. A static guard because the request lives inside a
# multi-thousand-line run loop that no unit test can drive.

def test_coder_request_body_uses_the_shared_request_shape():
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "vaf" / "tools" / "coder.py"
    text = src.read_bytes().decode("utf-8")
    start = text.index("_req_body = {")
    block = text[start:start + 800]
    assert "openai_request_params" in block, (
        "the coder's request body must take its per-model parameters from "
        "api_backend.openai_request_params, not build them by hand"
    )
    assert '"max_tokens"' not in block and '"temperature"' not in block, (
        "the coder must not hardcode max_tokens/temperature: both are a 400 on "
        "the gpt-5 family"
    )
