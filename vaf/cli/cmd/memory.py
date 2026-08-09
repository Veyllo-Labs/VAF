# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf memory` - memory store maintenance.

`rekey` recovers from a rotated `memory_encryption_key`: rows written under
the previous key are decrypted with it and re-encrypted with the current one.
The old key is read from a config backup FILE and never printed or persisted
anywhere by this command."""

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
