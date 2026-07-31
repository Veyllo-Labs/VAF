# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Embedding config passthrough: Agent(config={...}) must reach the API backend.

Pins the fix for the critical embedding bug where APIBackendManager re-read the
api_key / api_model from disk (~/.vaf/config.json), ignoring the config passed
programmatically via Agent(config={...}). Faked provider — no network, no real key.

Covers:
  T1  a RAW override api_key reaches api_backend.api_key UNDECODED, with no disk key.
  T2  an embedded api_model is honoured when chat_completion is called with model=None.
  T3  the product (non-embedded) path still reads the model fresh from disk each call.
  T4  no disk key and no override still raises ValueError (behaviour unchanged).
"""
import pytest

from vaf.core.api_backend import APIBackendManager
from vaf.core.config import Config


# ── Fake provider (captures what chat_completion receives) ────────────────────

class _CaptureProvider:
    def __init__(self):
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_request_usage = {"input_tokens": 0, "output_tokens": 0}
        self.received = None

    def chat_completion(self, messages, temperature, max_tokens, stream, model, tools, tool_choice):
        self.received = {"model": model, "tools": tools, "tool_choice": tool_choice}
        return iter([])  # no chunks — we only inspect what was passed


@pytest.fixture
def fake_provider(monkeypatch):
    """Stub _create_provider so no real client/network is built (m.provider is a capture provider)."""
    monkeypatch.setattr(APIBackendManager, "_create_provider", lambda self: _CaptureProvider())


def _drive(m):
    list(m.chat_completion([{"role": "user", "content": "hi"}], model=None, stream=False))
    return m.provider.received


# ── T1: RAW override key reaches the backend undecoded, no disk key ────────────

def test_raw_override_key_reaches_backend_undecoded(monkeypatch, fake_provider):
    """The caller's key wins and is used verbatim.

    The key now travels as the override DICT rather than a pre-extracted value - one
    caller-supplied source instead of two, and the same dict the failover chain and model
    discovery consult. The shape below is deliberate: valid base64 alphabet and length, so a
    decoder in the path would corrupt it silently rather than fail loudly.
    """
    monkeypatch.setattr("vaf.core.api_keys.resolve_api_key",
                        lambda provider, caller=None: (caller or {}).get(f"api_key_{provider}", ""))
    raw = "skABCDEFGH123456"
    m = APIBackendManager("openai", config={**Config.DEFAULTS, "api_key_openai": raw},
                          caller_config={"api_key_openai": raw})
    assert m.api_key == raw       # used as-is: never decoded, never read from disk
    assert m._embedded is True


# ── T2: embedded api_model honoured when chat_completion gets model=None ───────

def test_embedded_model_resolves_from_config(fake_provider):
    cfg = {**Config.DEFAULTS, "api_key_openai": "sk-x", "api_model_openai": "gpt-4o-mini"}
    m = APIBackendManager("openai", config=cfg, caller_config={"api_key_openai": "sk-x"})
    assert _drive(m)["model"] == "gpt-4o-mini"


# ── T3: product path still reads the model fresh from disk each call ───────────

def test_product_path_reads_model_live_from_disk(monkeypatch, fake_provider):
    disk = {"api_model_openai": "DISK-MODEL-1"}
    monkeypatch.setattr(Config, "load", lambda: dict(disk))
    monkeypatch.setattr("vaf.core.api_keys.resolve_api_key", lambda provider, caller=None: "sk-x")
    m = APIBackendManager("openai")  # non-embedded (config=None)
    assert m._embedded is False
    assert _drive(m)["model"] == "DISK-MODEL-1"
    # mutate disk -> a fresh Config.load() inside chat_completion must pick it up (no caching)
    disk["api_model_openai"] = "DISK-MODEL-2"
    assert _drive(m)["model"] == "DISK-MODEL-2"


# ── T4: no disk key and no override still raises (unchanged) ───────────────────

def test_missing_key_still_raises(monkeypatch):
    """Nothing configured anywhere is still "" and still refuses - the normal state. It is
    the case a corrupt store used to be indistinguishable from."""
    monkeypatch.setattr(Config, "load", lambda: dict(Config.DEFAULTS))
    monkeypatch.setattr("vaf.core.api_keys.resolve_api_key", lambda provider, caller=None: "")
    with pytest.raises(ValueError, match="API key missing"):
        APIBackendManager("openai")
