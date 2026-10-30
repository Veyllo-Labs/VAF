# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf providers`: which provider and model does each job use.

    vaf providers            a table: job, provider, model, and how it was decided
    vaf providers --json     the rows of POST /api/config/model-roles, as JSON

Every row comes from `vaf/core/model_roles.py`, the resolver the lanes themselves ask,
so the table shows what runs and not what a setting says. Read-only. Not `vaf roles`:
next to `vaf users` that would read as account roles.
"""
import json

import typer

from vaf.cli.ui import UI

_JOB = {
    "chat": "Chat",
    "vision": "Images",
    "ocr": "Text recognition",
    "browser_agent": "Browser agent",
    "subagents": "Sub-agents",
    "thinking": "Background thinking",
    "voice_call": "Voice call",
    "speech_to_text": "Speech to text",
    "text_to_speech": "Text to speech",
    "failover": "Failover",
}

_HOW = {
    "main": "main",
    "follows_main": "follows main",
    "set": "set",
    "unavailable": "not available",
    "off": "off",
    "local_engine": "local engine",
    "via_vision": "uses the vision model",
}


def _how(row: dict) -> str:
    from vaf.core.model_roles import REASON_TEXT
    how = _HOW.get(row["source"], row["source"])
    for code in [row["reason"], *row.get("also_reasons", [])]:
        if code:
            how += ". " + REASON_TEXT.get(code, code)
    return how


def providers_cmd(as_json: bool = typer.Option(False, "--json", help="Print the rows as JSON")) -> None:
    """Which provider and model does each job use."""
    from vaf.core.model_roles import resolve_roles

    rows = [a.as_dict() for a in resolve_roles()]
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    from rich.table import Table

    table = Table(title="Who does what", show_lines=False)
    table.add_column("Job")
    table.add_column("Provider")
    table.add_column("Model", overflow="fold")
    table.add_column("How", overflow="fold")
    for row in rows:
        provider = row["provider"] or "-"
        if row["also"]:
            provider += ", then " + ", ".join(row["also"])
        table.add_row(_JOB.get(row["role"], row["role"]), provider, row["model"] or "-", _how(row))
    UI.console.print(table)
