# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The context-effort ladder: one primitive behind every settings surface.

`context_compress_tokens` is the price of ONE round-trip on a pay-per-token
provider (the whole history is resent and billed every time). The settings
surfaces offer it as fixed rungs from 8000 up to the model's real window; this
pins the rung math, the clamping, and the write permission - raising the budget
spends the instance's API money, so it is admin-only like every other spend knob.
"""
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

from vaf.core.config import Config
from vaf.core.context import (
    CONTEXT_EFFORT_MIN,
    context_effort_steps,
    resolve_context_effort,
    resolve_context_window,
)


def test_ladder_starts_at_floor_and_ends_at_the_real_window():
    steps = context_effort_steps(128_000)
    assert steps[0] == CONTEXT_EFFORT_MIN == 8000
    assert steps[-1] == 128_000
    # The shape the settings slider shows: seven rungs, six steps between them.
    assert steps == [8000, 16000, 30000, 45000, 64000, 90000, 128_000]
    assert len(steps) - 1 == 6
    assert steps == sorted(set(steps)), "rungs must be strictly increasing"


@pytest.mark.parametrize("window", [32_768, 65_536, 200_000, 1_048_576])
def test_ladder_adapts_to_any_window(window):
    steps = context_effort_steps(window)
    assert steps[0] == CONTEXT_EFFORT_MIN
    assert steps[-1] == window, "top rung is always the model's real window"
    assert all(s <= window for s in steps)
    assert steps == sorted(set(steps))


def test_ladder_degenerate_windows():
    """A window at or below the floor cannot offer a ladder - it offers itself."""
    assert context_effort_steps(8000) == [8000]
    assert context_effort_steps(4096) == [4096]
    assert context_effort_steps(0) == [CONTEXT_EFFORT_MIN]


def test_default_budget_is_a_rung_of_the_common_window():
    """45k must be selectable on the 128k window, not a value between two rungs."""
    assert Config.DEFAULTS["context_compress_tokens"] == 45000
    assert 45000 in context_effort_steps(128_000)


def test_window_resolves_per_provider_without_a_backend():
    assert resolve_context_window({"provider": "local", "n_ctx": 65_536}) == 65_536
    # Local floor: Config.load clamps n_ctx up to 32768, the resolver agrees.
    assert resolve_context_window({"provider": "local", "n_ctx": 4096}) == 32_768
    assert resolve_context_window({"provider": "openai", "api_model_openai": "gpt-4o"}) == 128_000
    assert resolve_context_window({"provider": "anthropic",
                                   "api_model_anthropic": "claude-haiku-4-5"}) == 200_000
    # Unknown model falls back to the safe default rather than raising.
    assert resolve_context_window({"provider": "deepseek", "api_model_deepseek": "who-knows"}) == 128_000


def test_effort_clamps_into_the_ladder_and_flags_local():
    api = resolve_context_effort({"provider": "openai", "api_model_openai": "gpt-4o",
                                  "context_compress_tokens": 45000})
    assert api["current"] == 45000 and api["max"] == 128_000 and api["applies"] is True

    # Above the window: never fires, so it is reported as the window.
    over = resolve_context_effort({"provider": "openai", "api_model_openai": "gpt-4o",
                                   "context_compress_tokens": 999_000})
    assert over["current"] == 128_000

    # Below the floor: would compress every turn.
    under = resolve_context_effort({"provider": "openai", "api_model_openai": "gpt-4o",
                                    "context_compress_tokens": 500})
    assert under["current"] == CONTEXT_EFFORT_MIN

    # 0 is the documented escape hatch and survives untouched.
    off = resolve_context_effort({"provider": "openai", "api_model_openai": "gpt-4o",
                                  "context_compress_tokens": 0})
    assert off["current"] == 0 and off["configured"] == 0

    # Local: the budget is ignored by the agent, and the UI is told so.
    local = resolve_context_effort({"provider": "local", "n_ctx": 32_768,
                                    "context_compress_tokens": 45000})
    assert local["applies"] is False and local["max"] == 32_768


def test_budget_is_admin_only():
    """Raising it spends the instance's API money, and there is only ONE config file."""
    assert Config.is_global_config_key("context_compress_tokens")
    assert "context_compress_tokens" not in Config.filter_for_non_admin(
        {"context_compress_tokens": 128_000, "theme": "dark"}
    )


def test_agent_limit_honours_the_same_floor():
    """The agent must not accept a budget the settings ladder cannot express."""
    from types import SimpleNamespace

    from vaf.core.agent import Agent

    a = Agent.__new__(Agent)
    a.api_backend = SimpleNamespace()
    a.config = SimpleNamespace(get=lambda key, default=None: 500 if key == "context_compress_tokens" else default)
    assert a._compression_limit(128_000) == CONTEXT_EFFORT_MIN

    a.config = SimpleNamespace(get=lambda key, default=None: 45000 if key == "context_compress_tokens" else default)
    assert a._compression_limit(128_000) == 45000
    # The window still wins when it is smaller than the budget.
    assert a._compression_limit(30_000) == 30_000
