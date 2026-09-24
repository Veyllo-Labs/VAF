# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf outbox`: what the agent prepared and nobody has sent yet, from the terminal.

A send the agent makes on the person's own web chat turn is parked for them
(`vaf/core/outbound_hold.py`). A headless install has the same two questions the card
answers: what is waiting, and does it go out or not. The command runs as the machine owner,
so there is no `--scope` (the rule of `vaf memory` and `vaf inbox`), and the group sits
behind the same terminal door, because it prints messages.

Unlike `vaf inbox`, this group DOES write: `send`, `discard` and `edit` are the whole point
of a draft, and a person on a headless box has no other way to reach one. All three are the
functions the card's route calls (`outbound_hold.send_draft` / `discard_draft` /
`revise_draft`), so the two surfaces cannot disagree about what a click does.

NAMED BOUNDARY: a send from here does not wake the chat the draft came from. The wake turn is
queued on the task queue of the process that runs the chats, and this command is a process of
its own with a queue nobody drains. The agent hears about the send at that chat's next turn
instead (`outbound_hold.decision_notes`), the same way it hears about a discard.
"""
import json
import sys
from typing import Optional

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
    from rich.markup import escape
    from rich.table import Table
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    for col in ("When", "Kind", "Id", "Channel", "To", "Preview"):
        table.add_column(col)
    for row in rows:
        # Everything that came from a message is DATA and is escaped before it meets Rich:
        # the body was written by the model, the reason by a bridge, the recipient by whoever
        # typed it, and any of them may contain "[bold]" or "[/red]" - the first would style
        # the table, the second raises inside it. Only the two status tags are markup.
        preview = escape((row.get("preview") or "").replace("\n", " ")[:60])
        # A draft whose last attempt failed is still waiting for the person, and the reason is
        # the whole point of showing it: without the line they would press send again blind.
        state = str(row.get("state") or "")
        if state == "failed":
            preview = f"[red]not sent:[/red] {escape((row.get('error') or '').strip()[:40])} | {preview}"
        elif state == "ambiguous":
            preview = f"[yellow]may already have been sent:[/yellow] {preview}"
        # Every address and every file, because approving is approving what leaves: a Bcc or
        # a document the table did not show would go out unseen.
        files = [str(a) for a in (row.get("attachments") or []) if str(a)]
        if files:
            preview = f"{preview} [dim]files:[/dim] {escape(', '.join(files)[:60])}"
        to = escape((row.get("recipient") or "")[:40])
        for label in ("cc", "bcc"):
            if str(row.get(label) or "").strip():
                to = f"{to} [dim]{label}:[/dim] {escape(str(row.get(label)).strip()[:40])}"
        table.add_row(_when(row["created_ts"]), escape(str(row["kind"])), str(row["id"]),
                      escape(str(row["channel"])), to, preview)
    UI.console.print(table)
    UI.console.print("[dim]vaf outbox send <kind> <id>   vaf outbox discard <kind> <id>   "
                     "vaf outbox edit <kind> <id> --text ...[/dim]")


def _kind_or_exit(kind: str) -> str:
    if kind not in ("mail", "call"):
        UI.console.print("[red]Unknown kind.[/red] Use 'mail' or 'call', as the list prints it.")
        raise typer.Exit(1)
    return kind


@app.command("send")
def send_entry(
    kind: str = typer.Argument(..., help="mail or call, as the list prints it"),
    entry_id: int = typer.Argument(..., help="The id the list prints"),
) -> None:
    """Send one waiting draft now. A failure leaves it waiting, with the reason."""
    from rich.markup import escape

    from vaf.core.outbound_hold import send_draft
    username, scope = _identity()
    # The same acts the card performs, through the one function both call: for a mail that is
    # release AND drain (a terminal that only released printed "Sent." over a mail still
    # waiting), for a call the claimed re-dispatch through its own tool.
    outcome = send_draft(_kind_or_exit(kind), int(entry_id), username=username,
                         user_scope_id=scope, user_role="admin", wake=False)
    state, error = str(outcome.get("state") or ""), escape(str(outcome.get("error") or ""))
    if outcome.get("ok"):
        if state == "pending":
            UI.console.print("[green]Released.[/green] The next outbox run delivers it.")
        else:
            UI.console.print("[green]Sent.[/green]")
        return
    if error in ("not waiting", "no mail account"):
        UI.console.print("[red]No draft with that id is waiting.[/red]")
    elif state == "ambiguous":
        # Handed to the server and never confirmed: nobody may send it again, only drop it.
        UI.console.print(f"[yellow]May already have been sent.[/yellow] {error}")
    else:
        UI.console.print(f"[red]Not sent[/red] (state: {state or 'unknown'}), the draft stays. {error}".rstrip())
    raise typer.Exit(1)


@app.command("discard")
def discard_entry(
    kind: str = typer.Argument(..., help="mail or call, as the list prints it"),
    entry_id: int = typer.Argument(..., help="The id the list prints"),
) -> None:
    """Drop one waiting draft. Nothing was on the wire, so nothing is recalled."""
    from vaf.core.outbound_hold import discard_draft
    username, scope = _identity()
    if not discard_draft(_kind_or_exit(kind), int(entry_id), username=username,
                         user_scope_id=scope):
        UI.console.print("[red]No draft with that id is waiting.[/red]")
        raise typer.Exit(1)
    UI.console.print("[green]Dropped.[/green]")


@app.command("edit")
def edit_entry(
    kind: str = typer.Argument(..., help="mail or call, as the list prints it"),
    entry_id: int = typer.Argument(..., help="The id the list prints"),
    text: Optional[str] = typer.Option(None, "--text", help="The new text of the message"),
    subject: Optional[str] = typer.Option(None, "--subject", help="A mail's new subject"),
) -> None:
    """Change a waiting draft's words before it is sent. The recipients stay as they are."""
    from vaf.core.outbound_hold import revise_draft
    if text is None and subject is None:
        UI.console.print("[red]Nothing to change.[/red] Pass --text, --subject or both.")
        raise typer.Exit(1)
    username, scope = _identity()
    result = revise_draft(_kind_or_exit(kind), int(entry_id), username=username,
                          user_scope_id=scope, body=text, subject=subject)
    if result.get("ok"):
        UI.console.print("[green]Changed.[/green] vaf outbox send sends it.")
        return
    if result.get("error") == "empty":
        UI.console.print("[red]The text is empty.[/red] Discard the draft instead.")
    else:
        UI.console.print("[red]No draft with that id is waiting.[/red]")
    raise typer.Exit(1)
