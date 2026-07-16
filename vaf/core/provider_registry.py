# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Single source of truth for the LLM provider set (docs/llm/PROVIDER_MODES.md).

Every place that used to keep its own copy of "which providers exist and what
are their endpoints" reads from here: the API-backend factory, the coder's
raw-HTTP endpoint map, live model discovery (sync and async copies), the
vision-capability predicate (previously three manually-synced copies that had
already diverged), and the CLI settings menus. The web Settings UI keeps its
TypeScript fallback copies for rendering, guarded against this module by
tests/test_provider_registry_sync.py.

Deliberately import-light (dataclasses + typing only at module level; Config
is imported lazily inside functions) so any module - including small tools -
can import it without pulling heavy dependency chains. Provider-specific
BEHAVIOR (DeepSeek reasoning_content, OpenAI reasoning-param gating, Veyllo
synthetic-id downgrade, Anthropic/Google message conversion) intentionally
stays as gated code branches per PROVIDER_MODES.md's additive-and-gated
principle: this registry is the WHO/WHERE, not the HOW.

Adding a provider: add the ProviderSpec here AND a row in
Config.PROVIDER_MODELS, then follow the checklist the sync test enforces
(coder endpoint reachable, web UI lists, PROVIDER_MODES.md catalog row).
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Provider class families the factory can build. New SDK-native providers
# need a new kind plus a factory branch; OpenAI-compatible ones are pure data.
KIND_OPENAI_COMPAT = "openai-compat"
KIND_ANTHROPIC_SDK = "anthropic-sdk"
KIND_GOOGLE_SDK = "google-sdk"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    kind: str
    label: str
    needs_api_key: bool = True
    # SDK chat lane (APIBackendManager factory). None = the SDK's own default.
    sdk_base_url: Optional[str] = None
    sdk_base_url_config_key: Optional[str] = None  # config override wins over sdk_base_url
    dummy_api_key: Optional[str] = None  # sent when needs_api_key is False (llama-server ignores it)
    # Coder raw-HTTP lane: OpenAI-COMPATIBLE endpoint (differs from the SDK
    # lane for anthropic/google, which expose separate compat endpoints).
    coder_base_url: Optional[str] = None
    coder_base_url_config_key: Optional[str] = None
    # Live model discovery lane. None = no remote listing (local models come from disk).
    models_url: Optional[str] = None
    models_base_config_key: Optional[str] = None  # build "<config base>/models" (veyllo)
    models_auth: str = "bearer"  # "bearer" | "x-api-key" | "query-key"
    # Vision
    vision_capable: bool = False
    vision_default_model: Optional[str] = None  # None + capable -> Config.get_default_model(name)


# Order matters: must match Config.PROVIDER_MODELS key order (CI-guarded),
# because UI dropdown fallbacks and default enumerations derive from it.
PROVIDER_SPECS: Dict[str, ProviderSpec] = {
    spec.name: spec
    for spec in (
        ProviderSpec(
            name="openai",
            kind=KIND_OPENAI_COMPAT,
            label="OpenAI",
            coder_base_url="https://api.openai.com/v1",
            models_url="https://api.openai.com/v1/models",
            vision_capable=True,
        ),
        ProviderSpec(
            name="anthropic",
            kind=KIND_ANTHROPIC_SDK,
            label="Anthropic (Claude)",
            coder_base_url="https://api.anthropic.com/v1",
            models_url="https://api.anthropic.com/v1/models",
            models_auth="x-api-key",
            vision_capable=True,
        ),
        ProviderSpec(
            name="deepseek",
            kind=KIND_OPENAI_COMPAT,
            label="DeepSeek",
            sdk_base_url="https://api.deepseek.com/v1",
            coder_base_url="https://api.deepseek.com/v1",
            # Discovery deliberately has NO /v1 (api.deepseek.com/models).
            models_url="https://api.deepseek.com/models",
            vision_capable=False,
        ),
        ProviderSpec(
            name="google",
            kind=KIND_GOOGLE_SDK,
            label="Google (Gemini)",
            coder_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            models_url="https://generativelanguage.googleapis.com/v1beta/models",
            models_auth="query-key",
            vision_capable=True,
        ),
        ProviderSpec(
            name="openrouter",
            kind=KIND_OPENAI_COMPAT,
            label="OpenRouter",
            sdk_base_url="https://openrouter.ai/api/v1",
            coder_base_url="https://openrouter.ai/api/v1",
            models_url="https://openrouter.ai/api/v1/models",
            vision_capable=True,
            vision_default_model="openai/gpt-4o",
        ),
        ProviderSpec(
            name="veyllo",
            kind=KIND_OPENAI_COMPAT,
            label="Veyllo",
            sdk_base_url="https://api.veyllo.app/v1",
            sdk_base_url_config_key="veyllo_base_url",
            coder_base_url="https://api.veyllo.app/v1",
            coder_base_url_config_key="veyllo_base_url",
            models_base_config_key="veyllo_base_url",
            vision_capable=True,
        ),
        ProviderSpec(
            name="local",
            kind=KIND_OPENAI_COMPAT,
            label="Local (GGUF)",
            needs_api_key=False,
            dummy_api_key="ollama",
            sdk_base_url_config_key="local_api_url",  # fallback: VAF's own llama-server
            vision_capable=True,  # honest capability decided by the mmproj probe below
        ),
    )
}

VEYLLO_DEFAULT_BASE = "https://api.veyllo.app/v1"


def get_spec(provider: str) -> Optional[ProviderSpec]:
    return PROVIDER_SPECS.get((provider or "").lower())


def api_provider_names() -> List[str]:
    """The API providers, in Config.PROVIDER_MODELS order (local excluded)."""
    return [n for n in PROVIDER_SPECS if n != "local"]


def resolve_sdk_base_url(provider: str, config: Optional[dict] = None) -> Optional[str]:
    """Base URL for the SDK chat lane. None = use the SDK default."""
    spec = get_spec(provider)
    if spec is None:
        return None
    cfg = config or {}
    if spec.sdk_base_url_config_key:
        override = cfg.get(spec.sdk_base_url_config_key, "")
        if override:
            return override
        if spec.name == "local":
            # VAF's own llama-server (port 8080, Docker/env-aware) - NOT
            # Ollama's 11434, which is nothing in a stock install.
            from vaf.core.config import Config

            return Config.get_llama_server_url("/v1")
    return spec.sdk_base_url


def coder_endpoints(config: Optional[dict] = None) -> Dict[str, Dict[str, str]]:
    """The coder's OpenAI-compatible endpoint map: {provider: {base, model}}.

    Covers every API provider by construction (the historic Veyllo gap - a
    provider added centrally but missing here - cannot recur).
    """
    from vaf.core.config import Config

    cfg = config or {}
    out: Dict[str, Dict[str, str]] = {}
    for name in api_provider_names():
        spec = PROVIDER_SPECS[name]
        base = spec.coder_base_url or ""
        if spec.coder_base_url_config_key:
            base = cfg.get(spec.coder_base_url_config_key) or Config.get(
                spec.coder_base_url_config_key, ""
            ) or base
        out[name] = {"base": base.rstrip("/"), "model": Config.get_default_model(name)}
    return out


def models_discovery(provider: str, config: Optional[dict] = None) -> Optional[Tuple[str, str]]:
    """(url, auth_kind) for live model listing, or None when unsupported.

    Exact-key lookup on purpose (no case normalization): the web handler's
    provider string is client-controlled, and the pre-registry code returned
    an empty list for non-canonical spellings without firing any request -
    that behavior is preserved.
    """
    spec = PROVIDER_SPECS.get(provider)
    if spec is None or spec.name == "local":
        return None
    if spec.models_base_config_key:
        from vaf.core.config import Config

        cfg = config or {}
        base = cfg.get(spec.models_base_config_key) or Config.get(
            spec.models_base_config_key, ""
        ) or VEYLLO_DEFAULT_BASE
        return (base.rstrip("/") + "/models", spec.models_auth)
    if spec.models_url:
        return (spec.models_url, spec.models_auth)
    return None


def model_supports_vision(provider: str, model: str, probe_local: bool = True) -> bool:
    """Whether (provider, model) accepts image input.

    THE shared implementation - previously three manually-synced copies
    (vision_infer.py / agent.py / browser_agent.py) that had diverged
    (two of them did not know veyllo). Model-substring rules are capability
    facts, not behavior, so they live with the registry.
    """
    provider = (provider or "").lower()
    model = (model or "").lower()
    spec = get_spec(provider)
    if spec is not None and not spec.vision_capable:
        return False
    if provider in ("anthropic", "google", "veyllo"):
        return True
    if provider == "openai":
        return any(k in model for k in ("gpt-4o", "gpt-4-turbo", "gpt-4-vision", "o1", "o3"))
    if provider == "openrouter":
        return any(
            k in model for k in ("gpt-4o", "claude-3", "gemini", "vision", "vl", "llava", "pixtral")
        )
    if provider == "local":
        # Honest capability: with vision_provider=local the llama server is
        # launched with the mmproj projector and reports image support on
        # /v1/models. Probe defensively; an unreachable server counts as
        # capable so the normal lazy-load/error path stays intact.
        # probe_local=False skips the HTTP probe entirely (returns True):
        # the agent/browser hot paths run this check on every LLM round trip
        # and historically treated local as capable without a network call.
        if not probe_local:
            return True
        try:
            import requests as _rq

            r = _rq.get("http://127.0.0.1:8080/v1/models", timeout=2)
            if r.status_code == 200:
                return "multimodal" in r.text.lower() or "image" in r.text.lower()
        except Exception:
            pass
        return True
    return True  # unknown: let the model decide


def default_vision_model(provider: str) -> Optional[str]:
    """Safe default vision model for an explicit vision provider without a model."""
    spec = get_spec(provider)
    if spec is None or not spec.vision_capable or spec.name == "local":
        return None
    if spec.vision_default_model:
        return spec.vision_default_model
    from vaf.core.config import Config

    return Config.get_default_model(spec.name)
