"""`vaf ww` -- Whare Wananga tool self-learning from the terminal.

Thin Typer wrapper around vaf.whare_wananga.cli (which holds the actual logic and is also runnable
standalone via `python -m vaf.whare_wananga.cli`)."""

from typing import List, Optional

import typer

from vaf.whare_wananga import cli as _ww

app = typer.Typer(help="Whare Wananga tool self-learning (train / inspect tool know-how)")


def _train(tools, all_, force, quick, verbose):
    argv = ["train"]
    if tools:
        argv += list(tools)
    if all_:
        argv.append("--all")
    if force:
        argv.append("--force")
    if quick:
        argv.append("--quick")
    if verbose:
        argv.append("--verbose")
    raise typer.Exit(_ww.main(argv))


@app.command()
def train(
    tools: Optional[List[str]] = typer.Argument(None, help="tool names (omit with --all)"),
    all: bool = typer.Option(False, "--all", help="train every tool in a queue"),
    force: bool = typer.Option(False, "--force", help="with --all, retrain already-learned tools too"),
    quick: bool = typer.Option(False, "--quick", help="small batches for a fast smoke test"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="print every probe"),
):
    """Train one or more tools (foreground, live)."""
    _train(tools, all, force, quick, verbose)


@app.command()
def retrain(
    tools: Optional[List[str]] = typer.Argument(None, help="tool names (omit with --all)"),
    all: bool = typer.Option(False, "--all"),
    force: bool = typer.Option(False, "--force"),
    quick: bool = typer.Option(False, "--quick"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    """Alias for train (a run is a fresh assessment)."""
    _train(tools, all, force, quick, verbose)


@app.command(name="list")
def list_(  # `list` shadows the builtin; the Typer command is still named "list"
):
    """List learned tools + state."""
    raise typer.Exit(_ww.main(["list"]))


@app.command()
def show(tool: str = typer.Argument(..., help="tool name")):
    """Show the three baskets for a tool."""
    raise typer.Exit(_ww.main(["show", tool]))


@app.command()
def delete(tool: str = typer.Argument(..., help="tool name")):
    """Delete a tool's stored knowledge."""
    raise typer.Exit(_ww.main(["delete", tool]))


@app.command()
def eager(
    action: str = typer.Argument("status", help="status | on | off | scan"),
    quick: bool = typer.Option(False, "--quick", help="with scan: small batches"),
    yes: bool = typer.Option(False, "--yes", help="with scan: train even if EAGER is disabled"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="with scan: print every probe"),
):
    """Opt-in proactive training of SAFE tools (status | on | off | scan)."""
    argv = ["eager", action]
    if quick:
        argv.append("--quick")
    if yes:
        argv.append("--yes")
    if verbose:
        argv.append("--verbose")
    raise typer.Exit(_ww.main(argv))


@app.command()
def teacher(action: str = typer.Argument("status", help="status | on | off")):
    """Opt-in offline co-learning with a stronger configured API (status | on | off)."""
    raise typer.Exit(_ww.main(["teacher", action]))
