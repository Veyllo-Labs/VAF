# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf outbox`: what the agent prepared and nobody has sent yet, from the terminal.

A send the agent makes on the person's own web chat turn is parked for them
(`vaf/core/outbound_hold.py`). A headless install has the same two questions the card
answers: what is waiting, and does it go out or not. The command runs as the machine owner,
so there is no `--scope` (the rule of `vaf memory` and `vaf inbox`), and the group sits
behind the same terminal door, because it prints messages.

Unlike `vaf inbox`, this group DOES write: `send` and `discard` are the whole point of a
draft, and a person on a headless box has no other way to reach one. Both go through the
same functions the route calls, so the two surfaces cannot disagree about what a click does.
"""
import json
import sys

import typer

from vaf.cli.ui import UI

app = typer.Typer(help="Messages your agent prepared and has not sent.")


@app.callback()
def _group():
    """Messages your agent prepared and has not sent.

    Deliberate: without a callback, Typer collapses a one-command app into that command."""


def _identity():
    from vaf.core.identity_binding import resolve_owner_identity
    ident = resolve_owner_identity()
    return ident.username, ident.scope


def _when(ts: float) -> str:
    if not ts:
        return ""
    from datetime import datetime
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


@app.command("list")
def list_pending(
    limit: int = typer.Option(50, "--limit", "-n", help="Rows to print"),
    json_out: bool = typer.Option(False, "--json", help="One JSON object per line."),
) -> None:
    """Newest first: what it is, who it would reach, and the id to send or discard it."""
    from vaf.core.outbound_hold import pending

    username, scope = _identity()
    rows = pending(username, scope, limit=max(1, int(limit)))
    if json_out:
        for row in rows:
            sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
        return
    UI.console.print(f"[bold]{len(rows)} waiting to be sent[/bold]")
    if not rows:
        UI.console.print("  [dim]nothing waiting[/dim]")
        return
    from rich.table import Table
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    for col in ("When", "Kind", "Id", "Channel", "To", "Preview"):
        table.add_column(col)
    for row in rows:
        preview = (row.get("preview") or "").replace("\n", " ")[:60]
        # A draft whose last attempt failed is still waiting for the person, and the reason is
        # the whole point of showing it: without the line they would press send again blind.
        if str(row.get("state") or "") == "failed":
            preview = f"[red]not sent:[/red] {(row.get('error') or '').strip()[:40]} | {preview}"
        table.add_row(_when(row["created_ts"]), row["kind"], str(row["id"]), row["channel"],
                      (row.get("recipient") or "")[:40], preview)
    UI.console.print(table)
    UI.console.print("[dim]vaf outbox send <kind> <id>   vaf outbox discard <kind> <id>[/dim]")


@app.command("send")
def send_entry(
    kind: str = typer.Argument(..., help="mail or call, as the list prints it"),
    entry_id: int = typer.Argument(..., help="The id the list prints"),
) -> None:
    """Send one waiting draft now. A failure leaves it waiting, with the reason."""
    username, scope = _identity()
    if kind == "call":
        from vaf.core.outbound_hold import approve_call
        result = approve_call(int(entry_id), username=username, user_scope_id=scope,
                              user_role="admin")
        if result.get("ok"):
            UI.console.print(f"[green]Sent.[/green] {result.get('result', '')}")
            return
        UI.console.print(f"[red]Not sent:[/red] {result.get('result', '')}")
        raise typer.Exit(1)
    if kind == "mail":
        # The same two acts the card performs (release AND drain), through the one function
        # both call: a terminal that only released printed "Sent." over a mail still waiting.
        from vaf.mail.service import release_held_draft
        outcome = release_held_draft(scope, username, int(entry_id))
        if outcome.get("error") == "not waiting":
            UI.console.print("[red]No draft with that id is waiting.[/red]")
            raise typer.Exit(1)
        if outcome.get("ok"):
            UI.console.print("[green]Sent.[/green]")
            return
        UI.console.print(f"[yellow]Released, not delivered yet[/yellow] "
                         f"(state: {outcome.get('state') or 'unknown'}). "
                         f"{outcome.get('error') or 'The next outbox run takes it.'}")
        return
    UI.console.print("[red]Unknown kind.[/red] Use 'mail' or 'call', as the list prints it.")
    raise typer.Exit(1)


@app.command("discard")
def discard_entry(
    kind: str = typer.Argument(..., help="mail or call, as the list prints it"),
    entry_id: int = typer.Argument(..., help="The id the list prints"),
) -> None:
    """Drop one waiting draft. Nothing was on the wire, so nothing is recalled."""
    username, scope = _identity()
    if kind == "call":
        from vaf.core.outbound_hold import discard_call
        ok = discard_call(int(entry_id), username=username, user_scope_id=scope)
    elif kind == "mail":
        from vaf.mail.service import MailService
        ok = MailService(scope).discard_draft(int(entry_id))
    else:
        UI.console.print("[red]Unknown kind.[/red] Use 'mail' or 'call', as the list prints it.")
        raise typer.Exit(1)
    if not ok:
        UI.console.print("[red]No draft with that id is waiting.[/red]")
        raise typer.Exit(1)
    UI.console.print("[green]Dropped.[/green]")
