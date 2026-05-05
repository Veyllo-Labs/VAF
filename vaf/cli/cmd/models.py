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

@app.command("config")
def set_config(key: str, value: str):
    """Set arbitrary config value (e.g., gpu_layers)."""
    # Try to parse number
    try:
        val = int(value)
    except ValueError:
        val = value
    
    Config.set(key, val)
    UI.success(f"Config '{key}' set to '{val}'")
