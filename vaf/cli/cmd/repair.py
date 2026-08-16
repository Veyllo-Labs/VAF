# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf repair` - check the Docker services and put a broken one back.

The work is the framework's (`vaf/core/service_health.py`); this module is the
terminal's voice for it, and the renderer here is shared with the TUI so both
say the same thing about the same run.
"""
from typing import Any, Dict, List

import typer

from vaf.cli.ui import UI
from vaf.core.service_health import collect_service_status, repair_service_stack

_STATE_STYLE = {
    "ok": "green",
    "warn": "yellow",
    "error": "red",
    "absent": "dim",
    "unknown": "dim",
}


def format_status(status: Dict[str, Any]) -> List[str]:
    """The service table as plain lines, for a terminal or a TUI note."""
    lines: List[str] = []
    docker = status.get("docker") or {}
    if docker.get("available"):
        lines.append("Docker: reachable")
    else:
        detail = str(docker.get("detail") or "").strip()
        lines.append(f"Docker: NOT reachable ({docker.get('reason') or 'unknown'})"
                     + (f" - {detail}" if detail else ""))
    if not status.get("stack_root"):
        lines.append("Compose file: none found (this install manages no containers)")
    for svc in status.get("services", []):
        mark = "ok" if svc.get("state") == "ok" else str(svc.get("state") or "?")
        lines.append(f"  {str(svc.get('name') or ''):16} {mark:8} {svc.get('reason') or ''}")
    return lines


def print_status(status: Dict[str, Any]) -> None:
    docker = status.get("docker") or {}
    if docker.get("available"):
        UI.print("[bold]Docker:[/bold] [green]reachable[/green]")
    else:
        UI.print(f"[bold]Docker:[/bold] [red]not reachable[/red] "
                 f"({docker.get('reason') or 'unknown'})")
        if docker.get("detail"):
            UI.print(f"  {docker['detail']}", style="dim")
    if not status.get("stack_root"):
        UI.print("No compose file found: this install manages no containers.", style="dim")
    for svc in status.get("services", []):
        state = str(svc.get("state") or "unknown")
        style = _STATE_STYLE.get(state, "dim")
        UI.print(f"  {str(svc.get('name') or ''):16} [{style}]{state:8}[/{style}] "
                 f"{svc.get('reason') or ''}")


def format_step(step: Dict[str, Any]) -> str:
    """One finished repair step as plain text, for a terminal or a TUI note."""
    mark = "ok" if step.get("ok") else "failed"
    return f"[{mark}] {step.get('step')}: {step.get('message')}"


def print_step(step: Dict[str, Any]) -> None:
    ok = bool(step.get("ok"))
    mark = "[green]ok[/green]    " if ok else "[red]failed[/red]"
    UI.print(f"  {mark} {step.get('step')}: {step.get('message')}")


def cmd_repair(
    check: bool = typer.Option(False, "--check",
                               help="Report the service status and change nothing"),
    json_output: bool = typer.Option(False, "--json",
                                     help="Print the raw result as JSON"),
) -> None:
    """Check the Docker services and repair a broken one.

    Starts stopped containers, restarts unhealthy ones, and names what it cannot
    fix by itself (a port that disagrees with the configuration, a firewall in
    the way, a daemon that needs system privileges). Nothing is removed and no
    configuration is rewritten.
    """
    import json
    import sys

    def emit_json(payload: dict) -> None:
        """Straight to stdout, never through the console renderer.

        `UI.print` is a Rich console: on a pipe it falls back to 80 columns and
        word-wraps, which puts a raw newline inside a JSON string as soon as one
        `reason` sentence is long enough, and its markup parser eats any
        [bracketed] text in a value. Both make the documented `| jq` unusable.
        """
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        sys.stdout.flush()

    if check:
        status = collect_service_status()
        if json_output:
            emit_json(status)
            return
        print_status(status)
        broken = [s for s in status.get("services", [])
                  if s.get("required") and s.get("state") in ("error", "unknown")]
        if broken:
            UI.print("")
            UI.warning("Run `vaf repair` to try to fix this.")
            raise typer.Exit(1)
        return

    if not json_output:
        UI.print("Repairing the Docker services...")
    result = repair_service_stack(progress=None if json_output else print_step)
    if json_output:
        emit_json(result)
    else:
        UI.print("")
        print_status(result.get("status_after") or {})
        degraded = result.get("degraded") or []
        if result.get("ok") and not degraded:
            UI.success("The service stack is healthy.")
        elif result.get("ok"):
            UI.warning("The core stack is up, but these still need attention: "
                       + ", ".join(degraded))
        else:
            UI.warning("Some services still need attention; see the steps above.")
    if not result.get("ok"):
        raise typer.Exit(1)
