# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`reload_local_model`: the weight swap that keeps the conversation.

The classic lanes swap the local GGUF by discarding the whole agent
(`shutdown(); _make_cli_agent(); load_model(); init_chat()`), which destroys
the running conversation - five call sites hand-roll that. This primitive is
the engine's own answer: re-resolve the file from the live config, make the
ONE llama server hold it (`ensure_local_model`, model-aware), recompute the
identity the tool-call parser gates on, rebuild the system prompt and
re-attach the history tail.

The refusals matter as much as the swap: a `VAF_MODEL_OVERRIDE` pin (sub-agent
processes are pinned to their model), a non-local provider (no weights to
serve) and library mode (weights live in-process) must all return False
without touching anything.
"""
import threading
from types import SimpleNamespace

import pytest

import vaf.core.agent as agent_mod
import vaf.core.backend as backend_mod
from vaf.core.agent import Agent


def _dummy(monkeypatch, *, config_model="qwen-test.gguf", swap_ok=True,
           provider="local", llm=None, session_id="green123456"):
    """A minimal stand-in driven through the UNBOUND method, with the two
    module seams (Config.load, ensure_local_model) recorded."""
    calls = {"swap": [], "bind": [], "init": 0}

    monkeypatch.delenv("VAF_MODEL_OVERRIDE", raising=False)
    monkeypatch.setattr(agent_mod.Config, "load",
                        classmethod(lambda cls: {"model": config_model}))
    monkeypatch.setattr(backend_mod, "ensure_local_model",
                        lambda path, reason="", **kw:
                        calls["swap"].append(path) or swap_ok)

    d = SimpleNamespace(
        provider=provider,
        llm=llm,
        _backend_swap_lock=threading.Lock(),
        use_server=False,
        _tokenizer_instance="stale",
        filename="gemma-4-old.gguf",
        model_path="/models/gemma-4-old.gguf",
        current_session_id=session_id,
        history=[{"role": "system", "content": "OLD"},
                 {"role": "user", "content": "hallo"},
                 {"role": "assistant", "content": "hi"}],
    )

    def _resolve():
        d.filename = config_model
        d.model_path = f"/models/{config_model}"

    def _init_chat():
        calls["init"] += 1
        d.history = [{"role": "system", "content": "NEW"}]

    d.ensure_model_exists = _resolve
    d.init_chat = _init_chat
    d._bind_session_persistence = lambda sid: calls["bind"].append(sid)
    d._apply_local_model_identity = lambda: Agent._apply_local_model_identity(d)
    return d, calls


def test_the_swap_keeps_the_conversation(monkeypatch):
    d, calls = _dummy(monkeypatch)
    assert Agent.reload_local_model(d) is True
    assert calls["swap"] == ["/models/qwen-test.gguf"]
    assert d.history[0] == {"role": "system", "content": "NEW"}
    assert d.history[1:] == [{"role": "user", "content": "hallo"},
                             {"role": "assistant", "content": "hi"}], (
        "the running conversation was wiped - the init_chat trap")
    assert d.use_server is True
    assert d._tokenizer_instance is None
    assert calls["bind"] == ["green123456"], (
        "session persistence was not re-pointed after the prompt rebuild")


def test_the_parser_identity_follows_the_weights(monkeypatch):
    """A Gemma -> Qwen swap that kept model_mode == 'gemma4' would keep the
    wrong tool-call parser active."""
    d, _ = _dummy(monkeypatch)
    d.is_gemma_local = True
    d.model_mode = "gemma4"
    Agent.reload_local_model(d)
    assert d.model_display_name == "Qwen"
    assert d.is_gemma_local is False
    assert d.model_mode is None


def test_a_failed_server_swap_leaves_the_chat_alone(monkeypatch):
    d, calls = _dummy(monkeypatch, swap_ok=False)
    assert Agent.reload_local_model(d) is False
    assert calls["init"] == 0
    assert d.history[0]["content"] == "OLD"


def test_an_env_pinned_process_refuses(monkeypatch):
    d, calls = _dummy(monkeypatch)
    monkeypatch.setenv("VAF_MODEL_OVERRIDE", "pinned.gguf")
    assert Agent.reload_local_model(d) is False
    assert calls["swap"] == []


def test_a_cloud_provider_refuses(monkeypatch):
    d, calls = _dummy(monkeypatch, provider="veyllo")
    assert Agent.reload_local_model(d) is False
    assert calls["swap"] == []


def test_library_mode_refuses(monkeypatch):
    d, calls = _dummy(monkeypatch, llm=object())
    assert Agent.reload_local_model(d) is False
    assert calls["swap"] == []


def test_an_embedded_agent_with_overrides_refuses(monkeypatch):
    """config_overrides means the CALLER chose the model - a reload that
    re-reads the on-disk config would move the agent off that choice, the
    same hole reload_api_backend guards against."""
    d, calls = _dummy(monkeypatch)
    d._config_overrides = {"model": "caller-chosen.gguf"}
    assert Agent.reload_local_model(d) is False
    assert calls["swap"] == []


@pytest.mark.parametrize("fname,display,gemma,mode", [
    ("gemma-4-E4B-Q8.gguf", "Gemma", True, "gemma4"),
    ("gemma-3n-E2B.gguf", "Gemma", True, "gemma3n"),
    ("qwen3.5-2b.gguf", "Qwen", False, None),
    ("Meta-Llama-3.gguf", "Llama", False, None),   # matching is case-insensitive
    ("somethingelse.gguf", "VQ-1", False, None),
])
def test_the_identity_helper_matches_like_the_constructor(fname, display, gemma, mode):
    d = SimpleNamespace(filename=fname)
    Agent._apply_local_model_identity(d)
    assert (d.model_display_name, d.is_gemma_local, d.model_mode) == (display, gemma, mode)
