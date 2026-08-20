# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Security diagnostics commands, and the machine-wide known-bad hash list."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import typer

from vaf.cli.ui import UI
from vaf.core.security_misconfig import collect_security_findings

app = typer.Typer(help="Security diagnostics and hardening checks.")

_SEVERITY_ORDER: Dict[str, int] = {"high": 0, "medium": 1, "low": 2}
_SEVERITY_STYLE: Dict[str, str] = {"high": "red", "medium": "yellow", "low": "cyan"}


def _sort_findings(findings: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return sorted(findings, key=lambda f: (_SEVERITY_ORDER.get(str(f.get("severity", "")).lower(), 99), str(f.get("code", ""))))


@app.command("doctor")
def doctor() -> None:
    """Run security misconfiguration checks (safe output, no secrets)."""
    findings = _sort_findings(collect_security_findings())

    UI.panel("VAF Security Doctor", style="bold yellow")
    if not findings:
        UI.success("No security misconfiguration findings.")
        return

    counts = {"high": 0, "medium": 0, "low": 0}
    for item in findings:
        sev = str(item.get("severity", "low")).lower()
        if sev in counts:
            counts[sev] += 1

    UI.warning(
        f"Findings: {len(findings)} total "
        f"(high={counts['high']}, medium={counts['medium']}, low={counts['low']})."
    )
    for item in findings:
        sev = str(item.get("severity", "low")).lower()
        sev_label = sev.upper()
        style = _SEVERITY_STYLE.get(sev, "white")
        code = str(item.get("code", "unknown"))
        msg = str(item.get("message", ""))
        UI.print(f"[{style}]- [{sev_label}] {code}[/{style}] {msg}")


# ── the known-bad hash list ────────────────────────────────────────
#
# The dashboard is the usual way in. This group is the other one, for the machine
# owner at a terminal: a headless server with no browser open, or the moment right
# after an incident when the file to be listed is already sitting on this disk.
# Same store, same events - the CLI is not a second implementation.

threats = typer.Typer(help="The machine-wide list of known-bad file hashes.")
app.add_typer(threats, name="threats")


@threats.command("list")
def threats_list() -> None:
    """Show every listed digest."""
    from vaf.core.threat_db import list_threats, threat_db_path

    items = list_threats()
    UI.panel("Known-bad hash list", style="bold yellow")
    UI.print(f"[dim]{threat_db_path()}[/dim]")
    if not items:
        UI.success("Nothing is listed.")
        return
    UI.warning(f"{len(items)} listed digest(s).")
    for item in items:
        UI.print(f"[red]- {str(item.get('sha256', ''))[:12]}[/red] "
                 f"{item.get('kind', 'file')} {item.get('name', '')} "
                 f"[dim]{item.get('reason', '')} ({item.get('listed_at', '')})[/dim]")


@threats.command("check")
def threats_check(path: str = typer.Argument(..., help="File to look up.")) -> None:
    """Ask the list about a file, without changing anything."""
    from vaf.core.threat_db import check_file, digests_of_file

    target = Path(path).expanduser()
    if not target.is_file():
        UI.error(f"Not a file: {target}")
        raise typer.Exit(code=2)
    digests = digests_of_file(target)
    UI.print(f"sha256   {digests['sha256']}")
    UI.print(f"sha3-256 {digests['sha3_256']}")
    hit = check_file(target)
    if hit is None:
        UI.success("Not listed - this file would be accepted on every upload lane.")
        return
    UI.error(f"LISTED: {hit.get('reason') or 'listed as dangerous'} "
             f"(as {hit.get('name', '')}, {hit.get('listed_at', '')})")
    raise typer.Exit(code=1)


@threats.command("add")
def threats_add(path: str = typer.Argument(..., help="File to list as known-bad."),
                reason: str = typer.Option("listed from the terminal",
                                           help="Why this content is dangerous.")) -> None:
    """List a file's content as known-bad. Every upload lane refuses it from now on."""
    from vaf.core.threat_db import record_file_threat

    target = Path(path).expanduser()
    if not target.is_file():
        UI.error(f"Not a file: {target}")
        raise typer.Exit(code=2)
    record = record_file_threat(target, reason=reason, listed_by="cli")
    UI.success(f"Listed {record['sha256'][:12]} ({record['name']}).")
    UI.print("[dim]Every upload lane - chat, workspace, rooms, messengers, mail, "
             "cloud sync - now refuses this content.[/dim]")


@threats.command("remove")
def threats_remove(sha256: str = typer.Argument(..., help="The digest to delist.")) -> None:
    """Delist a digest, re-opening every upload lane to that content."""
    from vaf.core.threat_db import remove_threat

    if not remove_threat(sha256.strip().lower(), by="cli"):
        UI.error("That digest is not listed.")
        raise typer.Exit(code=1)
    UI.success(f"Delisted {sha256.strip().lower()[:12]}.")


__all__ = ["app", "doctor", "threats"]
