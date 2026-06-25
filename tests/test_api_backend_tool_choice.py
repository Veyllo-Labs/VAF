# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""APIBackendManager.chat_completion tool_choice gating tests.

Pins the UNIVERSAL DeepSeek tool_choice downgrade: DeepSeek (deepseek-v4-flash and
deepseek-v4-pro, both internally reasoning models) reject tool_choice="required" and
specific function-forcing dicts with HTTP 400; the manager downgrades any forcing form
to "auto" before calling the provider, while leaving "auto"/"none" and non-DeepSeek
providers untouched. Also pins that the downgrade runs AFTER the deepseek-auto model
resolution and the reasoner guard. Faked provider — no network, no API key.
"""
import pytest

from vaf.core.api_backend import APIBackendManager


# ── Fake provider (captures what chat_completion receives) ────────────────────

class _CaptureProvider:
    def __init__(self):
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_request_usage = {"input_tokens": 0, "output_tokens": 0}
        self.received = None

    def chat_completion(self, messages, temperature, max_tokens, stream, model, tools, tool_choice):
        self.received = {"model": model, "tools": tools, "tool_choice": tool_choice}
        return iter([])  # no chunks needed — we only inspect what was passed


def _mgr(provider_name="deepseek"):
    # Bypass __init__ (API-key/config/provider side effects); set only what chat_completion touches.
    m = APIBackendManager.__new__(APIBackendManager)
    m.provider_name = provider_name
    m.config = {}  # only .get("subagent_model", ...) is read on the deepseek-auto path
    m.provider = _CaptureProvider()
    m.session_usage = {"input_tokens": 0, "output_tokens": 0}
    m.last_request_usage = {"input_tokens": 0, "output_tokens": 0}
    return m


_TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {}}}]


def _drive(m, tool_choice, model="deepseek-v4-flash", tools=_TOOLS):
    list(m.chat_completion([{"role": "user", "content": "hi"}],
                           model=model, tools=tools, tool_choice=tool_choice))
    return m.provider.received


# ── DeepSeek: forcing forms get downgraded ────────────────────────────────────

def test_deepseek_required_downgraded_to_auto():
    m = _mgr("deepseek")
    assert _drive(m, "required")["tool_choice"] == "auto"


def test_deepseek_specific_function_dict_downgraded_to_auto():
    m = _mgr("deepseek")
    choice = {"type": "function", "function": {"name": "ask_user"}}
    assert _drive(m, choice)["tool_choice"] == "auto"


# ── DeepSeek: non-forcing forms untouched ─────────────────────────────────────

def test_deepseek_auto_untouched():
    m = _mgr("deepseek")
    assert _drive(m, "auto")["tool_choice"] == "auto"


def test_deepseek_none_untouched():
    m = _mgr("deepseek")
    assert _drive(m, "none")["tool_choice"] == "none"


# ── Ordering: the reasoner guard wins (runs first) ────────────────────────────

def test_deepseek_reasoner_guard_takes_precedence():
    m = _mgr("deepseek")
    received = _drive(m, "required", model="deepseek-reasoner")
    # reasoner guard strips tools + forces "none"; the downgrade block must not re-touch it
    assert received["tools"] is None
    assert received["tool_choice"] == "none"


# ── Other providers: never touched ────────────────────────────────────────────

def test_openai_required_untouched():
    m = _mgr("openai")
    assert _drive(m, "required", model="gpt-4o")["tool_choice"] == "required"


def test_openrouter_required_untouched():
    m = _mgr("openrouter")
    assert _drive(m, "required", model="deepseek/deepseek-chat")["tool_choice"] == "required"


# ── deepseek-auto resolution happens BEFORE the downgrade ─────────────────────

def test_deepseek_auto_resolves_to_pro_then_downgrades(monkeypatch):
    for var in ("VAF_IN_WORKFLOW_TERMINAL", "VAF_IN_AUTOMATION", "VAF_COMPACTION_IN_PROGRESS", "VAF_TOOL_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VAF_BACKGROUND_PRO", "1")
    m = _mgr("deepseek")
    received = _drive(m, "required", model="deepseek-auto")
    assert received["model"] == "deepseek-v4-pro"   # pro-context resolution
    assert received["tool_choice"] == "auto"          # then the downgrade


def test_deepseek_auto_resolves_to_flash_without_pro_context(monkeypatch):
    for var in ("VAF_IN_WORKFLOW_TERMINAL", "VAF_IN_AUTOMATION", "VAF_COMPACTION_IN_PROGRESS",
                "VAF_TOOL_MODEL", "VAF_BACKGROUND_PRO"):
        monkeypatch.delenv(var, raising=False)
    m = _mgr("deepseek")
    received = _drive(m, "auto", model="deepseek-auto")
    assert received["model"] == "deepseek-v4-flash"
