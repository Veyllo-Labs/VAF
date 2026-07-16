# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Pins the provider factory's observable behavior, byte for byte.

Written BEFORE the registry refactor against the if/elif factory, so the
data-driven replacement must reproduce exactly this: provider class, base
URL, key handling, and the two error messages. Uses the embedded lane
(config= + api_key=) so nothing reads ~/.vaf/config.json.
"""
import pytest

from vaf.core.api_backend import (
    AnthropicProvider,
    APIBackendManager,
    GoogleProvider,
    OpenAIProvider,
)

FAKE_KEY = "test-key-not-real"


def _mgr(provider, config=None, api_key=FAKE_KEY):
    return APIBackendManager(provider, config=config or {}, api_key=api_key)


@pytest.mark.parametrize(
    "provider,cls,base_url",
    [
        ("openai", OpenAIProvider, None),
        ("deepseek", OpenAIProvider, "https://api.deepseek.com/v1"),
        ("openrouter", OpenAIProvider, "https://openrouter.ai/api/v1"),
        ("veyllo", OpenAIProvider, "https://api.veyllo.app/v1"),
    ],
)
def test_openai_compat_providers(provider, cls, base_url):
    mgr = _mgr(provider)
    assert type(mgr.provider) is cls
    assert mgr.provider.provider_name == provider
    assert mgr.api_key == FAKE_KEY
    client_base = str(mgr.provider.client.base_url).rstrip("/")
    if base_url is None:
        assert "api.openai.com" in client_base
    else:
        assert client_base == base_url


def test_sdk_native_providers():
    assert type(_mgr("anthropic").provider) is AnthropicProvider
    assert type(_mgr("google").provider) is GoogleProvider


def test_local_needs_no_key_and_honors_local_api_url():
    mgr = APIBackendManager("local", config={}, api_key="")
    assert type(mgr.provider) is OpenAIProvider
    assert mgr.provider.provider_name == "local"
    # default: VAF's own llama-server, never Ollama's 11434
    assert ":8080" in str(mgr.provider.client.base_url)

    mgr2 = APIBackendManager(
        "local", config={"local_api_url": "http://127.0.0.1:11434/v1"}, api_key=""
    )
    assert "11434" in str(mgr2.provider.client.base_url)


def test_veyllo_base_url_is_config_overridable():
    mgr = _mgr("veyllo", config={"veyllo_base_url": "https://staging.veyllo.app/v1"})
    assert "staging.veyllo.app" in str(mgr.provider.client.base_url)


def test_error_messages_are_stable():
    with pytest.raises(ValueError, match=r"^Unsupported provider: nope$"):
        _mgr("nope")
    with pytest.raises(ValueError, match=r"^API key missing for openai$"):
        APIBackendManager("openai", config={}, api_key="")
