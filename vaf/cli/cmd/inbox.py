# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf inbox`: the person's conversations across every channel, from the terminal.

A headless install has the same question the inbox window answers, and the primitive in
`vaf/core/inbox.py` makes the answer a table. The command runs as the machine owner: the
CLI has no authentication, so there is no `--scope` (the rule of `vaf memory`), and the
group sits behind the same terminal door as `vaf session`, because it prints chats.

Read-only by design: the terminal prints, it does not read for the person. The seen mark is
written where the person reads (opening a conversation in a window) or where they say they
have read everything (the window's "mark all as read", `mark_all_seen`), and the done mark
has no button. A `vaf inbox read` would be one call to that same primitive, so the two
surfaces could not disagree; it is left out until a headless install asks for it, which is
the measurement that earns the command.
"""
import json
import sys
from typing import Optional

import typer

from vaf.cli.ui import UI

app = typer.Typer(help="Your conversations across every channel.")


@app.callback()
def _group():
    """Your conversations across every channel.

    Deliberate: without a callback, Typer collapses a one-command app into that
    command, and `vaf inbox list` would reject "list" as an extra argument."""


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
def list_conversations(
    channel: Optional[str] = typer.Option(None, "--channel", "-c",
                                          help="whatsapp, telegram, discord, mail or room (default: every channel)"),
    view: str = typer.Option("all", "--view", "-v", help="all, waits, unread or agent"),
    limit: int = typer.Option(50, "--limit", "-n", help="Rows to print"),
    groups: bool = typer.Option(True, "--groups/--no-groups", help="Include group chats and rooms"),
    done: bool = typer.Option(False, "--done", help="Include conversations the person answered last (or marked done through the API)"),
    bulk: bool = typer.Option(False, "--bulk", help="Include promotions, social, newsletters, notifications and junk mail"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Only conversations matching this text"),
    json_out: bool = typer.Option(False, "--json", help="One JSON object per line."),
) -> None:
    """Newest first: who wrote, when, unread, and whether somebody waits for you."""
    from vaf.core import inbox

    username, scope = _identity()
    result = inbox.list_conversations(
        username, scope, channels=[channel] if channel else None, view=view,
        include_groups=groups, include_done=done, include_bulk=bulk, query=query or "", limit=max(1, int(limit)))
    rows = result["rows"]
    if json_out:
        for row in rows:
            sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
        return
    counts = result["counts"]
    UI.console.print(f"[bold]{counts['all']} conversations[/bold]  {counts['waits']} wait for you  "
                     f"{counts['unread']} unread  {counts['agent']} answered by the agent"
                     + (f"  {counts['bulk_hidden']} bulk mail hidden (--bulk shows them)" if counts.get("bulk_hidden") else ""))
    if not rows:
        UI.console.print("  [dim]nothing here[/dim]")
        return
    from rich.table import Table
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    for col in ("When", "Channel", "Name", "Unread", "Waits", "Mode", "Preview"):
        table.add_column(col)
    for row in rows:
        waits = row["waits_reason"] if row["waits"] else ""
        preview = (row.get("preview") or "").replace("\n", " ")[:60]
        table.add_row(_when(row["last_ts"]), inbox.channel_label(row["channel"]), (row.get("name") or row["id"])[:40],
                      str(row["unread"] or ""), waits, row["mode"], preview)
    UI.console.print(table)
