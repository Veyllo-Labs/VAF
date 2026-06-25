# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Single-source-of-truth for per-provider models (Config.PROVIDER_MODELS).

Pins that every consumer derives from the one definition: config DEFAULTS, the
api_backend default/fallback lookups, and the /api/provider-models endpoint all
agree with Config.PROVIDER_MODELS — so a model change in one place propagates.
"""
from vaf.core.config import Config, PROVIDER_MODELS
from vaf.core.api_backend import APIBackendManager


API_PROVIDERS = ["openai", "anthropic", "google", "deepseek", "openrouter"]


def test_every_provider_has_default_and_nonempty_fallback():
    for p in API_PROVIDERS:
        info = PROVIDER_MODELS[p]
        assert info["default"], f"{p} missing default"
        assert info["fallback"], f"{p} missing fallback list"
        # the default should be offered in the fallback list (so it's selectable offline)
        assert info["default"] in info["fallback"], f"{p} default not in its fallback list"


def test_config_defaults_track_source():
    for p in API_PROVIDERS:
        assert Config.DEFAULTS[f"api_model_{p}"] == PROVIDER_MODELS[p]["default"]


def test_helpers():
    assert Config.get_default_model("google") == PROVIDER_MODELS["google"]["default"]
    assert Config.get_fallback_models("anthropic") == PROVIDER_MODELS["anthropic"]["fallback"]
    # unknown / local -> empty (local models come from disk, not this list)
    assert Config.get_default_model("local") == ""
    assert Config.get_fallback_models("nope") == []


def test_api_backend_available_models_track_source():
    for p in API_PROVIDERS:
        assert APIBackendManager.get_available_models(p) == PROVIDER_MODELS[p]["fallback"]
    # local keeps its own on-disk-style list
    assert APIBackendManager.get_available_models("local") == ["llama3", "mistral", "codellama"]


def test_no_retired_models_in_source():
    flat = " ".join(
        info["default"] + " " + " ".join(info["fallback"])
        for info in PROVIDER_MODELS.values()
    )
    for retired in ("claude-3-5-sonnet-20241022", "claude-3-opus-20240229",
                    "gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash"):
        assert retired not in flat, f"retired model {retired} leaked into PROVIDER_MODELS"


def test_provider_models_endpoint_returns_source():
    import asyncio
    from vaf.api.config_routes import get_provider_models
    result = asyncio.run(get_provider_models())
    assert result == Config.PROVIDER_MODELS
    assert result["google"]["default"] == "gemini-2.5-flash"
