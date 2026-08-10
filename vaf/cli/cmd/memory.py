# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf memory` - memory store maintenance.

`rekey` recovers from a rotated `memory_encryption_key`: rows written under
the previous key are decrypted with it and re-encrypted with the current one.
The old key is read from a config backup FILE and never printed or persisted
anywhere by this command.

`cross-chat` is a dry run of the Cross Chat Hint lane: it prints exactly what a
turn with that question would put into the prompt, and why. Unit tests cannot
tell you whether the lane finds the chat you actually meant; this can.

Neither command takes a scope. The CLI has no authentication - the local user is
the machine owner - so both run under the owner's identity, and a `--scope`
option would be a purpose-built reader of another tenant's chat text."""

import json
from pathlib import Path

import typer

from vaf.cli.ui import UI

app = typer.Typer(help="Memory store maintenance")


@app.callback()
def _group():
    """Memory store maintenance.

    Deliberate: without a callback, Typer collapses a one-command app into
    that command, and `vaf memory rekey` would reject "rekey" as an extra
    argument."""


@app.command("cross-chat")
def cross_chat(
    query: str = typer.Option(..., "--query", "-q", help="The question a turn would ask"),
    k: int = typer.Option(0, "--limit", "-n", help="Override cross_chat_hint_k (0 = use the config)"),
):
    """Show which of your other chats would be hinted for QUERY, and print the block."""
    from vaf.core.cross_chat import format_hints, hints_for_turn, query_terms
    from vaf.core.identity_binding import resolve_owner_identity

    owner = resolve_owner_identity()
    terms = query_terms(query)
    if not terms:
        UI.warning("No searchable terms in that question (all stopwords or too short).")
        raise typer.Exit(0)
    UI.info(f"Terms: {', '.join(terms)}")

    if k > 0:
        from vaf.core.config import Config
        from vaf.core.cross_chat import find_hints
        hints = find_hints(
            query,
            user_scope_id=owner.scope,
            username=owner.username,
            k=k,
            min_terms=max(1, int(Config.get("cross_chat_hint_min_terms", 2) or 1)),
            max_age_days=max(1, int(Config.get("cross_chat_hint_max_age_days", 30) or 30)),
        )
    else:
        hints = hints_for_turn(query, user_scope_id=owner.scope, username=owner.username)

    if not hints:
        UI.info("No hints - nothing would be added to the prompt.")
        return
    for hint in hints:
        UI.info(f"{hint.session_id}  score={hint.score}  terms={', '.join(hint.terms)}")
    UI.info("")
    UI.info("Block as the model would see it:")
    UI.info(format_hints(hints))


@app.command()
def rekey(
    old_key_file: Path = typer.Option(
        ..., "--old-key-file",
        help="Config backup (json) that still carries the previous memory_encryption_key"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Count what would change; write nothing"),
):
    """Re-encrypt memories/chunks from the key in OLD-KEY-FILE to the current key."""
    try:
        raw = json.loads(Path(old_key_file).read_text(encoding="utf-8"))
    except FileNotFoundError:
        UI.error(f"Backup file not found: {old_key_file}")
        raise typer.Exit(1)
    except Exception as e:
        UI.error(f"Backup file is not readable json: {type(e).__name__}")
        raise typer.Exit(1)
    old_key = str(raw.get("memory_encryption_key") or "")
    if not old_key:
        UI.error("The backup file carries no memory_encryption_key.")
        raise typer.Exit(1)

    import asyncio

    from vaf.memory.rekey import rekey_store

    try:
        report = asyncio.run(rekey_store(old_key, dry_run=dry_run))
    except RuntimeError as e:
        UI.error(str(e))
        raise typer.Exit(1)
    except Exception as e:
        # The memory DB is a separate Docker service and may simply be down.
        UI.warning(f"Memory DB not reachable - start VAF (or the vaf-memory-db "
                   f"container) and retry. ({type(e).__name__})")
        raise typer.Exit(1)

    for line in report.lines():
        UI.info(line)
    if report.memories.failed or report.chunks.failed:
        UI.warning("Some rows were unreadable with BOTH keys and were left "
                   "untouched - a second backup with another key may exist.")
        raise typer.Exit(2)
    UI.success("Rekey dry-run complete." if dry_run else "Rekey complete.")
