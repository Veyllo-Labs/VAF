import typer
import os
from typing import Optional
from vaf.cli.cmd.bridge_discord import run_bridge as start_discord

app = typer.Typer(help="Bridge VAF to external platforms (Discord, Slack, etc.)")

@app.command()
def discord(
    token: Optional[str] = typer.Option(None, help="Discord Bot Token (or set DISCORD_TOKEN env)"),
    gateway: str = typer.Option("ws://127.0.0.1:8000", help="URL of the running VAF Gateway")
):
    """Starts the Discord Bridge."""
    bot_token = token or os.getenv("DISCORD_TOKEN")
    if not bot_token:
        typer.echo("Error: No Discord token provided. Use --token or set DISCORD_TOKEN.", err=True)
        raise typer.Exit(1)
        
    typer.echo(f"Starting Discord Bridge to {gateway}...")
    start_discord(bot_token, gateway)

if __name__ == "__main__":
    app()
