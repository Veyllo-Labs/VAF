# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""APIBackendManager.chat_completion tool_choice gating tests.

Pins the UNIVERSAL DeepSeek tool_choice downgrade: DeepSeek (deepseek-flash and
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


def _drive(m, tool_choice, model="deepseek-flash", tools=_TOOLS):
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
    assert received["model"] == "deepseek-flash"


def test_deepseek_auto_in_an_automation_resolves_to_the_catalog_pro_target(monkeypatch):
    """The alias targets come from the model catalog. MUTATION: put a
    "deepseek-v4-flash" literal back into either branch of the backend and the
    flash case above or this one goes red."""
    from vaf.core.model_catalog import alias_target

    for var in ("VAF_IN_WORKFLOW_TERMINAL", "VAF_COMPACTION_IN_PROGRESS",
                "VAF_TOOL_MODEL", "VAF_BACKGROUND_PRO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VAF_IN_AUTOMATION", "1")
    received = _drive(_mgr("deepseek"), "auto", model="deepseek-auto")
    assert received["model"] == alias_target("deepseek", "deepseek-auto", pro=True) == "deepseek-v4-pro"
    monkeypatch.delenv("VAF_IN_AUTOMATION")
    received = _drive(_mgr("deepseek"), "auto", model="deepseek-auto")
    assert received["model"] == alias_target("deepseek", "deepseek-auto", pro=False) == "deepseek-flash"


def test_coder_resolves_deepseek_auto_through_the_same_catalog_targets():
    """The coder keeps its own, narrower condition (a workflow run only), but the
    targets are the catalog's. MUTATION: restore the "deepseek-v4-flash" literal
    in coder.py's resolver."""
    from vaf.tools.coder import _coder_deepseek_auto_model

    assert _coder_deepseek_auto_model(False, "") == "deepseek-flash"
    assert _coder_deepseek_auto_model(True, "") == "deepseek-v4-pro"
    # The caller hands over config.subagent_model_for("deepseek"): None for the
    # alias itself or a model of another provider.
    assert _coder_deepseek_auto_model(True, None) == "deepseek-v4-pro"
    assert _coder_deepseek_auto_model(True, "deepseek-flash") == "deepseek-flash"
    # Outside a workflow the configured sub-agent model is not the coder's to take.
    assert _coder_deepseek_auto_model(False, "deepseek-v4-pro") == "deepseek-flash"


# ── A model of another provider never reaches this one ────────────────────────

_PRO_ENV = ("VAF_IN_WORKFLOW_TERMINAL", "VAF_IN_AUTOMATION", "VAF_COMPACTION_IN_PROGRESS",
            "VAF_TOOL_MODEL", "VAF_BACKGROUND_PRO")


@pytest.fixture
def _quiet_notes(monkeypatch):
    """A process that has reported nothing yet; what it would print is recorded."""
    import vaf.core.provider_registry as pr
    import vaf.cli.ui as ui_mod
    monkeypatch.setattr(pr, "_foreign_noted", set())
    events = []
    monkeypatch.setattr(ui_mod.UI, "event", staticmethod(lambda *a, **kw: events.append(a)))
    return events


def test_the_pro_context_never_sends_a_tool_model_of_another_provider(monkeypatch, _quiet_notes):
    """The incident: main provider switched from Veyllo to DeepSeek, the tool /
    workflow model still `veyllo-chat`, a thinking run set VAF_BACKGROUND_PRO,
    and DeepSeek answered 400 "you passed veyllo-chat" on every call.
    MUTATION: read `self.config.get("subagent_model")` raw again in the
    deepseek-auto block."""
    from vaf.core.config import Config

    for var in _PRO_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VAF_BACKGROUND_PRO", "1")
    live = {"provider": "deepseek", "subagent_model": "veyllo-chat"}
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: dict(live)))
    m = _mgr("deepseek")
    assert _drive(m, "auto", model="deepseek-auto")["model"] == "deepseek-v4-pro"
    # A DeepSeek tool model is still honoured.
    live["subagent_model"] = "deepseek-flash"
    assert _drive(_mgr("deepseek"), "auto", model="deepseek-auto")["model"] == "deepseek-flash"


def test_the_pro_context_reads_the_tool_model_live(monkeypatch, _quiet_notes):
    """A long-lived product manager holds the config of its creation; a tool
    model changed in Settings since applies at once, like api_model_deepseek
    one line above it. MUTATION: pass `self.config` to subagent_model_for."""
    from vaf.core.config import Config

    for var in _PRO_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VAF_BACKGROUND_PRO", "1")
    monkeypatch.setattr(Config, "load", classmethod(
        lambda cls: {"provider": "deepseek", "subagent_model": "deepseek-flash"}))
    m = _mgr("deepseek")
    m.config = {"provider": "deepseek", "subagent_model": ""}   # the creation snapshot
    assert _drive(m, "auto", model="deepseek-auto")["model"] == "deepseek-flash"


def test_a_placed_model_costs_no_config_read(monkeypatch, _quiet_notes):
    """Before the fit check an explicit model read no config; one the catalog
    places on the provider still reads none. MUTATION: load the config eagerly
    in `_chat_single`."""
    from vaf.core.config import Config

    import sys

    for var in _PRO_ENV:
        monkeypatch.delenv(var, raising=False)
    loads = []

    def _load(cls):
        # Only the reads the model lookup makes: from _chat_single itself or from its
        # read-on-first-use wrapper. Other lanes of the call (failover, accounting)
        # have their own config reads, which this check does not add.
        caller = sys._getframe(1).f_code
        if caller.co_filename.endswith("api_backend.py") and caller.co_name in ("_chat_single", "get"):
            loads.append(1)
        return {}

    monkeypatch.setattr(Config, "load", classmethod(_load))

    def _product_mgr():
        m = _mgr("openai")
        m.config = {"failover_level": "off"}   # the snapshot a product manager holds
        return m

    assert _drive(_product_mgr(), "auto", model="gpt-5.6-sol")["model"] == "gpt-5.6-sol"
    assert loads == []
    # A foreign one is looked up (the warning's log line reads the config too).
    _drive(_product_mgr(), "auto", model="veyllo-chat")
    assert loads


def test_an_explicit_model_of_another_provider_falls_back_to_the_configured_one(monkeypatch, _quiet_notes):
    """MUTATION: delete the fit check in `_chat_single`."""
    from vaf.core.config import Config

    for var in _PRO_ENV:
        monkeypatch.delenv(var, raising=False)
    live = {"api_model_openai": "gpt-5.6-luna"}
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: dict(live)))
    assert _drive(_mgr("openai"), "auto", model="veyllo-chat")["model"] == "gpt-5.6-luna"
    assert _drive(_mgr("openai"), "auto", model="veyllo-chat")["model"] == "gpt-5.6-luna"
    assert len(_quiet_notes) == 1                        # said once, not per call
    assert "veyllo-chat" in _quiet_notes[0][1]
    # Nothing configured: the catalog default.
    live.clear()
    assert _drive(_mgr("openai"), "auto", model="veyllo-chat")["model"] == \
        Config.PROVIDER_MODELS["openai"]["default"]
    # A fitting explicit model passes as it was.
    assert _drive(_mgr("openai"), "auto", model="gpt-5.6-sol")["model"] == "gpt-5.6-sol"


def test_local_names_fall_back_silently_as_before(monkeypatch, _quiet_notes):
    from vaf.core.config import Config

    for var in _PRO_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {"api_model_openai": "gpt-5.6-luna"}))
    for local in ("Veyllo/VQ-1_Instruct-q4_k_m", "auto", "Qwen3.5-4B-UD-Q8_K_XL.gguf"):
        assert _drive(_mgr("openai"), "auto", model=local)["model"] == "gpt-5.6-luna", local
    assert _quiet_notes == []


def test_an_embedded_manager_falls_back_to_its_own_config(monkeypatch, _quiet_notes):
    """Embedded: the programmatic config, never the machine's file."""
    from vaf.core.config import Config

    for var in _PRO_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {"api_model_openai": "machine-model"}))
    m = _mgr("openai")
    m._embedded = True
    m.config = {"api_model_openai": "gpt-5.6-terra"}
    assert _drive(m, "auto", model="claude-sonnet-4-6")["model"] == "gpt-5.6-terra"
