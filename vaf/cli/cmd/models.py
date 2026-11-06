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
    # Try to parse number
    try:
        val = int(value)
    except ValueError:
        val = value
    
    Config.set(key, val)
    UI.success(f"Config '{key}' set to '{val}'")
