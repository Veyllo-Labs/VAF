# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Rule-2 CI guard: every copy of the provider set stays in sync with the registry.

vaf/core/provider_registry.py is the single source of truth (WHO/WHERE). This
test fails when any known copy drifts:
  - Config.PROVIDER_MODELS (names AND order),
  - the coder endpoint map and the model-discovery lane (by construction),
  - the web Settings UI's TypeScript fallbacks (PROVIDER_META,
    FALLBACK_PROVIDER_MODELS, dynamicProviders) - parsed as text, which is
    crude but exactly what drift detection needs,
  - the vision predicate consumers (agent.py / browser_agent.py must delegate
    to the registry instead of keeping private copies; two of the historic
    three copies did not know veyllo).

Adding a provider: see the module docstring in provider_registry.py.
"""
import re
from pathlib import Path

from vaf.core.config import Config
from vaf.core.provider_registry import (
    PROVIDER_SPECS,
    api_provider_names,
    coder_endpoints,
    model_supports_vision,
    models_discovery,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

VALID_MODELS_AUTH = {"bearer", "x-api-key", "query-key"}


def test_registry_matches_config_provider_models_order():
    # Same names, same ORDER: UI dropdown fallbacks and default enumerations
    # derive from this order on both sides.
    registry_order = [n for n in PROVIDER_SPECS if n != "local"]
    assert registry_order == list(Config.PROVIDER_MODELS), (
        "PROVIDER_SPECS (minus local) and Config.PROVIDER_MODELS must list the "
        "same providers in the same order"
    )


def test_every_api_provider_is_fully_wired():
    endpoints = coder_endpoints()
    for name in api_provider_names():
        spec = PROVIDER_SPECS[name]
        assert spec.needs_api_key is True, f"{name}: API providers require a key"

        # Coder raw-HTTP lane: present for every API provider, sane base URL.
        assert name in endpoints, f"{name}: missing from coder_endpoints()"
        base = endpoints[name]["base"]
        assert base.startswith("https://"), f"{name}: coder base not https ({base!r})"
        assert not base.endswith("/"), f"{name}: coder base has a trailing slash ({base!r})"

        # Live model-discovery lane: a (url, auth) tuple with a known auth kind.
        discovery = models_discovery(name)
        assert discovery is not None, f"{name}: models_discovery() returned None"
        url, auth = discovery
        assert url.startswith("https://"), f"{name}: discovery url not https ({url!r})"
        assert auth in VALID_MODELS_AUTH, f"{name}: unknown models auth {auth!r}"


def _settings_modal_text() -> str:
    return (REPO_ROOT / "web" / "components" / "SettingsModal.tsx").read_text(
        encoding="utf-8"
    )


def _extract_block(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    assert start != -1, f"SettingsModal.tsx: {start_marker!r} not found"
    end = text.find(end_marker, start)
    assert end != -1, f"SettingsModal.tsx: no {end_marker!r} after {start_marker!r}"
    return text[start:end]


def test_web_settings_provider_meta_covers_registry():
    block = _extract_block(_settings_modal_text(), "const PROVIDER_META", "];")
    for name in api_provider_names():
        assert f"id: '{name}'" in block, (
            f"web PROVIDER_META is missing provider {name!r} (SettingsModal.tsx)"
        )


def test_web_settings_fallback_models_cover_registry():
    block = _extract_block(
        _settings_modal_text(), "const FALLBACK_PROVIDER_MODELS", "};"
    )
    for name in api_provider_names():
        assert re.search(rf"\b{re.escape(name)}\s*:", block), (
            f"web FALLBACK_PROVIDER_MODELS is missing provider {name!r} "
            "(SettingsModal.tsx)"
        )


def test_web_settings_dynamic_providers_cover_registry():
    match = re.search(
        r"const dynamicProviders\s*=\s*\[([^\]]*)\]", _settings_modal_text()
    )
    assert match, "SettingsModal.tsx: dynamicProviders array not found"
    listed = set(re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)))
    for name in api_provider_names():
        assert name in listed, (
            f"web dynamicProviders is missing provider {name!r} (SettingsModal.tsx)"
        )


def test_vision_consumers_delegate_to_registry():
    # agent.py and browser_agent.py used to carry private vision-capability
    # copies that drifted (neither knew veyllo). They must delegate now.
    for rel in ("vaf/core/agent.py", "vaf/tools/browser_agent.py"):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "provider_registry" in source, (
            f"{rel} does not reference provider_registry - private vision copy back?"
        )


def test_vision_predicate_knows_veyllo_and_deepseek():
    assert model_supports_vision("veyllo", "veyllo-chat") is True
    assert model_supports_vision("deepseek", "deepseek-v4-pro") is False
    assert model_supports_vision("deepseek", "anything-at-all") is False


def test_workflow_engine_scope_injection_covers_python_sandbox():
    """Rule-2 guard: the workflow engine keeps its OWN copy of the per-tool
    scope injection (narrower than the agent dispatcher by design). The
    sandbox's per-user container workdir depends on user_scope_id reaching
    the tool from BOTH dispatchers - the agent one is covered by the facade
    guard; this pins the engine copy."""
    from pathlib import Path
    import re

    src = (Path(__file__).resolve().parents[1] / "vaf" / "workflows" / "engine.py").read_text()
    match = re.search(
        r"def _inject_user_scope.*?(?=\n                # Retry logic)", src, re.DOTALL
    )
    assert match, "engine _inject_user_scope block not found - update this guard"
    block = match.group(0)
    for tool in ("memory_save", "python_sandbox", "schedule_reminder", "send_to_user"):
        assert f'"{tool}"' in block, (
            f"workflow engine _inject_user_scope no longer covers {tool} - "
            "sync with the agent dispatcher (vaf/core/agent.py execute_tool)"
        )
