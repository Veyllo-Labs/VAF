# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import typer
from rich.table import Table
from vaf.core.config import Config
from vaf.cli.ui import UI

app = typer.Typer()

@app.command("list")
def list_models():
    """List available and currently selected models."""
    current_model = Config.get("model")
    
    table = Table(title="Available Models")
    table.add_column("Name", style="cyan")
    table.add_column("Provider", style="magenta")
    table.add_column("Status", style="green")

    # Hardcoded list for now, could fetch from online or scan folder
    models = [
        {"name": "Veyllo/VQ-1_Instruct-q4_k_m", "provider": "local"},
        {"name": "Veyllo/VQ-1-Small-q4_k_m", "provider": "local"},
        {"name": "mistral-7b-instruct-v0.2.Q4_K_M", "provider": "local"},
    ]

    for model in models:
        status = "Active" if model["name"] == current_model else ""
        table.add_row(model["name"], model["provider"], status)

    UI.console.print(table)
    UI.print(f"\nCurrent context limit: [bold]{Config.get('n_ctx')}[/bold]")
    UI.print(f"GPU Layers: [bold]{Config.get('gpu_layers')}[/bold]")

@app.command("set")
def set_model(model_name: str):
    """Set the active model."""
    Config.set("model", model_name)
    UI.success(f"Active model set to: {model_name}")
    from vaf.core import local_engine
    if local_engine.is_vllm():
        # The GGUF setting is llama.cpp's; under vLLM it is stored and not run.
        UI.warning("The local engine is vLLM: this GGUF applies to llama.cpp only. Choose the "
                   "vLLM model with `vaf engine vllm --model owner/name`.")

@app.command("config")
def set_config(key: str, value: str):
    """Set arbitrary config value (e.g., gpu_layers)."""
    from vaf.core import local_engine
    if key.strip().lower() in local_engine.SWITCH_KEYS:
        UI.error(f"{key} changes through `vaf engine` (it installs, downloads the model and "
                 "checks that it fits): `vaf engine vllm --model owner/name` or "
                 "`vaf engine llama`.")
        raise typer.Exit(2)
    if key.strip().lower() == "deepseek_reasoning_effort":
        # Checked BEFORE the number parse below, and with the framework's own check, so the
        # CLI stores exactly what the backend will send. A value the API would refuse (422)
        # is refused here instead of "set" and then silently ignored.
        from vaf.core.api_backend import check_deepseek_reasoning_effort
        try:
            effort = check_deepseek_reasoning_effort(value)
        except ValueError as exc:
            UI.error(str(exc))
            raise typer.Exit(2)
        Config.set("deepseek_reasoning_effort", effort)
        UI.success("DeepSeek thinking effort set to "
                   + (f"'{effort}'" if effort else "DeepSeek's default (high)"))
        return
    if key.strip().lower() == "provider":
        _set_provider(value)
        return
    if key.strip().lower() == "subagent_provider":
        _set_subagent_provider(value)
        return
    # Try to parse number
    try:
        val = int(value)
    except ValueError:
        val = value

    # A lane model its lane's provider would drop is reset by Config.save, so storing
    # it would print "set" for a value that is not kept: refused instead. A provider
    # change that resets one names it. Checked and written under one hold of the
    # config lock (reentrant, so Config.set nests), so another process cannot move
    # the provider between the check and the write.
    from vaf.core.config import foreign_lane_models
    with Config._locked():
        current = Config.load()
        resets = foreign_lane_models({**current, key: val})
        if key in resets:
            UI.error(_refusal(key, val, resets[key][1], current))
            raise typer.Exit(2)
        Config.set(key, val)
    UI.success(f"Config '{key}' set to '{val}'")
    for reset_key, (old, lane) in resets.items():
        UI.warning(_reset_notice(reset_key, old, lane))


def _set_provider(value: str) -> None:
    """`provider` goes through the switch every terminal route shares: a provider without
    a key is refused (exit 2) and nothing is written, since the next start would boot into
    a backend that cannot be built."""
    from vaf.core.provider_switch import switch_provider
    result = switch_provider(value, verify=None)
    if result.status != "done":
        UI.error(result.text)
        raise typer.Exit(2)
    UI.success(f"Config 'provider' set to '{result.provider}'")
    for reset_key, (old, lane) in result.resets.items():
        UI.warning(_reset_notice(reset_key, old, lane))


def _set_subagent_provider(value: str) -> None:
    """`subagent_provider` names the choice and a second key gates it; written alone the
    name does nothing. Both go through ``set_subagent_provider``, after
    the checks the settings menus make."""
    from vaf.core.api_keys import ApiKeyUnavailable
    from vaf.core.config import SUBAGENT_PROVIDER_KEYS, foreign_lane_models, \
        set_subagent_provider, subagent_provider_override
    from vaf.core.provider_registry import api_provider_names, provider_label
    name = value.strip().lower()
    if name not in ("", "inherit", "local") and name not in api_provider_names():
        UI.error(f"Unknown provider '{value.strip()}': use inherit, local or one of "
                 f"{', '.join(api_provider_names())}.")
        raise typer.Exit(2)
    if name == "local":
        # The same refusal as `provider local`: under the api profile no local model runs,
        # and switching sub-agents onto one would only fail at their start.
        from vaf.core import inference_profile
        if not inference_profile.local_inference_allowed():
            UI.error(inference_profile.refusal("a local model"))
            raise typer.Exit(2)
    if name not in ("", "inherit", "local"):
        try:
            key = Config.get_api_key(name)
        except ApiKeyUnavailable as exc:
            UI.error(str(exc))
            raise typer.Exit(2)
        if not key:
            UI.error(f"No API key configured for {name}. Cannot switch sub-agents to this "
                     "provider; add the key in vaf settings first.")
            raise typer.Exit(2)
    name_key, gate_key = SUBAGENT_PROVIDER_KEYS
    with Config._locked():
        chosen = name or "inherit"
        resets = foreign_lane_models({**Config.load(), name_key: chosen,
                                      gate_key: chosen != "inherit"})
        set_subagent_provider(chosen)
    lane = subagent_provider_override()
    main = str(Config.get("provider", "local") or "local")
    UI.success("Sub-agents run on " + (provider_label(lane) if lane
                                        else f"the main provider ({provider_label(main)})"))
    for reset_key, (old, lane_name) in resets.items():
        UI.warning(_reset_notice(reset_key, old, lane_name))


# The job each lane model belongs to in `model_roles`, for the model now in use.
_LANE_ROLES = {"subagent_model": "subagents", "browser_agent_model": "browser_agent"}
_SHOWN_CHARS = 200


def _shown(value) -> str:
    from vaf.core.tool_dispatch import clip_middle
    return clip_middle(str(value), _SHOWN_CHARS, marker="... ({left_out} chars left out) ...")


def _refusal(key: str, val, lane: str, current: dict) -> str:
    """Why ``val`` is not stored for ``key``, and what can be stored instead."""
    from vaf.core.config import subagent_provider_override
    from vaf.core.provider_registry import provider_label
    label = provider_label(lane)
    example = Config.get_default_model(lane)
    text = (f"{key} '{_shown(val)}' is not one of {label}'s models, the provider this lane "
            f"runs on. Use one of them"
            + (f" (for example {example})" if example else "")
            + f", or leave it empty for {label}'s own model: vaf models config {key} \"\"")
    named = str(current.get("subagent_provider") or "").strip()
    if (key == "subagent_model" and named.lower() not in ("", "inherit")
            and subagent_provider_override(current) is None):
        text += (f". The sub-agent provider '{named}' is not switched on, so sub-agents run on "
                 f"{label}: choose it in vaf settings, or switch it on with: vaf models config "
                 f"subagent_provider {named}")
    return text


def _reset_notice(key: str, old, lane: str) -> str:
    """What a provider change reset, what is used now, and how to choose another."""
    from vaf.core.model_roles import resolve_role
    from vaf.core.provider_registry import provider_label
    label = provider_label(lane)
    try:
        now = resolve_role(_LANE_ROLES[key], warn=False, explain=False, probe_local=False).model
    except Exception:
        now = None
    used = f"{label}'s own model ({now})" if now else f"{label}'s own model"
    return (f"{key} '{_shown(old)}' is not one of {label}'s models and was reset; {used} is "
            f"used now. Choose another with: vaf models config {key} <model>")
