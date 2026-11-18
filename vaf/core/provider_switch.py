# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Switch the installation's main provider (``local`` or an API provider), once.

``switch_provider`` is the one implementation behind the terminal app (its model
picker and settings, through ``AgentBridge.apply_provider_change``), `vaf settings`
and `vaf models config provider`. Each of them used to write ``provider`` and
``api_model_<p>`` by hand with its own key check and its own idea of applying it:
the terminal app refused a keyless provider and reloaded, `vaf settings` refused and
did not reload, `vaf models config` wrote any name without asking.

The order, and why:

1. The name is checked against the provider registry (``local`` or
   ``provider_registry.api_provider_names``); ``local`` is refused under the
   ``api`` inference profile with the profile's own sentence.
2. An API provider needs a key. A typed key is stored first, then checked with one
   real request (``verify``, ``key_verifies`` by default). A key that fails is KEPT,
   because the request can fail on the network as easily as on the key, and the
   provider stays where it was. Without a typed key nothing is verified: re-testing
   a stored key would spend a request on every switch.
3. ``provider`` and the model are written through ``Config.set`` (so the change
   observers fire) under one hold of the config lock, together with the read that
   names the lane models the new provider resets. The reset itself is
   ``Config.save``'s rule, applied on every write path; this module never writes a
   lane model.
4. ``reload_all_api_backends(force=True)`` moves every live agent of this process.
   ``force``: a key or model may have changed while the provider name stayed.

The key never reaches a result text.

Named boundaries, each deliberate:

- **Not on the facade.** The provider here is the installation's, like
  ``local_engine.apply`` and ``inference_profile.apply``: an embedder's agent with its
  own config is skipped by ``reload_all_api_backends``, and docs/EMBEDDING.md tells an
  embedder to build a new agent instead. No embedder has needed this switch.
- **The web Settings save stays on its own lane** (``RELOAD_CONFIG`` and
  ``on_config_changed``): it saves the whole page, keys from the same payload
  included, and answers ``requires_refresh``. Moving it here would change that
  contract.
- **`vaf setup` keeps its own write** (``_store_veyllo_key``): it writes the key,
  ``provider`` and ``vision_provider`` exactly as the web wizard does, runs before any
  agent exists, and holds the key it writes.
- **The engine and then the provider** ("from an API to vLLM") has one caller, the
  terminal app (``AgentBridge.apply_engine(..., make_local=True)``); a second caller
  would earn a ``switch_to_local(engine)`` here.
- **The prompt-toolkit and classic terminal lanes** still rebuild their agent after
  their settings menu; their writes go through this function, the rebuild is theirs.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

LOCAL = "local"

# What a refusal is, by code: the result's text says it in English.
REFUSALS = ("unknown_provider", "profile_api", "no_key", "key_unavailable", "key_unverified")


@dataclass(frozen=True)
class ProviderSwitch:
    """What ``switch_provider`` did.

    ``status`` is ``done`` or ``refused``. ``code``: ``switched`` (a live agent of this
    process moved) or ``stored`` (written, no live agent moved) when done, else one of
    ``REFUSALS``. ``changed`` counts the agents that moved; ``resets`` names the lane
    models ``Config.save`` reset with the switch (key -> (old value, lane provider)).
    """

    status: str
    code: str
    text: str
    provider: str
    model: str = ""
    changed: int = 0
    resets: Dict[str, Tuple[Any, str]] = field(default_factory=dict)


def key_verifies(provider: str) -> bool:
    """One real request against ``provider`` with its stored key; False on any error.

    It performs a chat completion, so a UI calls it off its own thread.
    """
    try:
        from vaf.core.api_backend import APIBackendManager
        return bool(APIBackendManager.test_connection(provider))
    except Exception:
        return False


def _label(provider: str) -> str:
    try:
        from vaf.core.provider_registry import provider_label
        return provider_label(provider)
    except Exception:
        return provider


def _refused(code: str, text: str, provider: str) -> ProviderSwitch:
    return ProviderSwitch("refused", code, text, provider)


def _reload_live_agents() -> int:
    """Move every live agent of this process. An agent exists only once vaf.core.agent
    is imported, so a one-shot command that never loaded it has none to move and does
    not pay for the import."""
    agent_mod = sys.modules.get("vaf.core.agent")
    if agent_mod is None:
        return 0
    return int(agent_mod.reload_all_api_backends(force=True) or 0)


def switch_provider(provider: str, model: str = "", *, new_key: str = "",
                    verify: Optional[Callable[[str], bool]] = key_verifies) -> ProviderSwitch:
    """Make ``provider`` the main provider (and ``model`` its API model), then move the
    live agents of this process onto it. See the module docstring for the order.

    ``new_key`` is a key the person just typed: stored, then verified with ``verify``
    (None skips the check). ``model`` is ignored for ``local``, whose model is the
    engine's (``model`` / ``vllm_model``).
    """
    from vaf.core import inference_profile
    from vaf.core.config import Config

    name = str(provider or "").strip().lower()
    chosen = str(model or "").strip()
    typed = str(new_key or "").strip()

    if name == LOCAL:
        if not inference_profile.local_inference_allowed():
            return _refused("profile_api", inference_profile.refusal("the local model"), name)
        chosen = ""
    else:
        from vaf.core.provider_registry import api_provider_names
        names = api_provider_names()
        if name not in names:
            return _refused(
                "unknown_provider",
                f"Unknown provider '{str(provider or '').strip()}': choose local or one of "
                f"{', '.join(names)}.", name)
        label = _label(name)
        if typed:
            Config.set_api_key(name, typed)
        from vaf.core.api_keys import ApiKeyUnavailable
        try:
            stored = Config.get_api_key(name)
        except ApiKeyUnavailable as exc:
            return _refused("key_unavailable",
                            f"{exc} The provider was not changed.", name)
        if not stored:
            return _refused("no_key",
                            f"There is no API key for {label}, so the provider was not "
                            f"changed. Add the key first.", name)
        if typed and verify is not None and not verify(name):
            return _refused("key_unverified",
                            f"The API key for {label} was saved but did not verify, so the "
                            f"provider was not changed.", name)

    updates = {"provider": name}
    if chosen:
        updates[f"api_model_{name}"] = chosen
    with Config._locked():
        # Read at call time from the module, the reader Config.save applies.
        from vaf.core.config import foreign_lane_models
        resets = foreign_lane_models({**Config.load(), **updates})
        for key, value in updates.items():
            Config.set(key, value)

    changed = _reload_live_agents()
    shown = _label(name) + (f" ({chosen})" if chosen else "")
    if changed:
        return ProviderSwitch("done", "switched", f"Switched to {shown}.", name, chosen,
                              changed, resets)
    return ProviderSwitch("done", "stored", f"The provider is set to {shown}.", name, chosen,
                          0, resets)
