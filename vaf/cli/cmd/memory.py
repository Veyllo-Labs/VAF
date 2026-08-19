# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf memory` - memory store maintenance.

`rekey` recovers from a rotated `memory_encryption_key`: rows written under
the previous key are decrypted with it and re-encrypted with the current one.
The old key is read from a config backup FILE and never printed or persisted
anywhere by this command.

`reembed` migrates the stored vectors to another embedding model: rows whose
embedding_model stamp differs from the target are decrypted, re-embedded and
re-stamped. The app start runs it automatically when config and store diverge;
the command exists for manual runs, dry runs and unreadable-row retries.

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


@app.command()
def reembed(
    target_model: str = typer.Option(
        None, "--target-model",
        help="Embedding model to migrate the store to (default: the managed target)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Count what would change; write nothing"),
    include_unreadable: bool = typer.Option(
        False, "--include-unreadable",
        help="Retry rows previously stamped unreadable (after a successful rekey)"),
    auto: bool = typer.Option(
        False, "--auto", hidden=True,
        help="Machine mode for the startup hook: quiet, status-file driven"),
    status_file: Path = typer.Option(
        None, "--status-file", hidden=True,
        help="Write progress snapshots to this JSON file"),
):
    """Re-embed memories/chunks whose vectors were written by another embedding model."""
    import asyncio

    from vaf.memory.reembed import (TARGET_EMBEDDING_MODEL, lock_file_path,
                                    reembed_store)

    target = target_model or TARGET_EMBEDDING_MODEL

    # One re-embed at a time, across processes (startup hook + manual CLI).
    from filelock import FileLock, Timeout
    lock_path = lock_file_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        if not auto:
            UI.info("A re-embed run is already active (see the app's progress "
                    "banner or logs/memory.log [REEMBED] lines).")
        raise typer.Exit(0)

    progress_cb = None
    if not auto and not dry_run:
        from vaf.cli.tui import TUI
        tui = TUI()

        def progress_cb(done: int, total: int) -> None:
            # An honest total exists here (row count), unlike sub-agent runs.
            tui.progress_bar(done, max(total, 1), label="Re-embedding memory store")

    try:
        report = asyncio.run(reembed_store(
            target, dry_run=dry_run, include_unreadable=include_unreadable,
            status_file=status_file, progress_cb=progress_cb))
    except RuntimeError as e:
        UI.error(str(e))
        raise typer.Exit(1)
    except Exception as e:
        UI.warning(f"Memory DB not reachable - start VAF (or the vaf-memory-db "
                   f"container) and retry. ({type(e).__name__})")
        raise typer.Exit(1)
    finally:
        lock.release()

    if not auto:
        for line in report.lines():
            UI.info(line)
    if report.pending_after and not dry_run:
        if not auto:
            UI.warning("Rows are still pending; the run can be repeated safely "
                       "(it resumes at the stamps).")
        raise typer.Exit(2)
    if not auto:
        UI.success("Re-embed dry-run complete." if dry_run else "Re-embed complete.")
