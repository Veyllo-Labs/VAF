# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Bring the files already on disk under the shield, and stop hoarding them.

Encrypting new writes protects nothing that is already lying around, and on a
real install that is the larger half: chat records going back to the first day,
one context archive per compression (thousands, because their cleanup only ran
on a clean shutdown), and every sub-agent payload ever written, because nothing
deleted those at all.

So this does two jobs, once per start:

1. **Migrate**: read each plaintext file through the tolerant reader and write
   it back encrypted. Idempotent, per file, and a failure on one file never
   stops the rest - a chat that cannot be re-written is left exactly as it was.
2. **Retain**: delete context archives past `context_archive_max_age_days` and
   sub-agent payloads whose task is long gone.

It also REPORTS, without touching, two files the audit found that no code
creates and no code deletes - a plain SQL dump of the auth tables and a config
backup still carrying a superseded memory key. Deleting a user's own files is
not this function's call; telling them is.

Wired into the web/tray startup AND the CLI start path on purpose. Wiring a
repair into one lane only is a mistake this repo has made before: a cleanup
that lived in the interactive CLI startup never ran for the web app, so the
records it was supposed to remove were permanent.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict

logger = logging.getLogger("vaf.core.at_rest_migration")

_done = False


def _migrate_file(path: Path) -> str:
    """Rewrite one plaintext file as ciphertext. Returns 'migrated'|'skipped'|'failed'."""
    from vaf.core import data_files

    try:
        raw = path.read_bytes()
    except OSError:
        return "failed"
    if not raw or data_files.is_encrypted(raw):
        return "skipped"
    try:
        data_files.write_bytes_atomic(path, raw, encrypt=True)
        return "migrated"
    except Exception as e:
        logger.warning("Could not encrypt %s: %s", path, e)
        return "failed"


def _migrate_tree(root: Path, patterns) -> Dict[str, int]:
    counts = {"migrated": 0, "skipped": 0, "failed": 0}
    if not root.is_dir():
        return counts
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file() or path.suffix == ".gz":
                continue
            counts[_migrate_file(path)] += 1
    return counts


def run_once(*, force: bool = False) -> Dict[str, object]:
    """Migrate and prune the file stores. Safe to call from every entry point."""
    global _done
    if _done and not force:
        return {"skipped": True}
    _done = True

    from vaf.core.config import Config
    from vaf.core.data_files import encryption_enabled
    from vaf.core.platform import Platform
    from vaf.core.secure_store import harden_dir

    report: Dict[str, object] = {}
    vaf_dir = Path(Platform.vaf_dir())

    trees = {
        "sessions": (vaf_dir / "sessions", ("*.json",)),
        "context_archive": (vaf_dir / "context_archive", ("context_*.json",)),
        "handoff_bundles": (vaf_dir / "handoff_bundles", ("*/*.json",)),
        "subagent_queue": (vaf_dir / "subagent_queue", ("*.json", "task_payloads/*.txt")),
        "main_context": (Path.cwd() / ".vaf" / "main", ("*.json", "*.md", "sessions/*/*")),
    }

    # Modes first: they apply whether or not encryption is on, and they are the
    # only protection a second local account ever sees.
    for name, (root, _patterns) in trees.items():
        if root.is_dir():
            harden_dir(root)

    if encryption_enabled():
        for name, (root, patterns) in trees.items():
            counts = _migrate_tree(root, patterns)
            if counts["migrated"] or counts["failed"]:
                report[name] = counts
                logger.info("at-rest migration %s: %s", name, counts)

    # Backfill complete and nothing plain left? Then close the tolerant read.
    # Only ever tightens, and only on evidence: a single failed file keeps the
    # store in the migrating state rather than locking a record out.
    if encryption_enabled() and Config.get("allow_plaintext_at_rest", True):
        failed = sum(int(c.get("failed", 0)) for c in report.values() if isinstance(c, dict))
        if not failed and not _any_plaintext_left(trees):
            Config.set("allow_plaintext_at_rest", False)
            report["enforced"] = True
            logger.info("Every store is ciphertext; plaintext reads are now refused.")

    report["archives_pruned"] = _prune_context_archives(vaf_dir)
    report["payloads_pruned"] = _prune_orphan_payloads(vaf_dir)

    # The plaintext rollback copy has done its job once no key is left in
    # config.json; keeping it would leave every key readable beside the data.
    try:
        from vaf.core.secure_store import drop_pre_migration_backup
        report["dropped_pre_keyring_backup"] = drop_pre_migration_backup()
    except Exception:
        pass

    leftovers = [
        str(p) for p in (
            vaf_dir / "config.json.bak-rls-cutover",
            vaf_dir / "vm-backups",
        ) if p.exists()
    ]
    if leftovers:
        report["review_these"] = leftovers
        logger.warning(
            "These hold readable copies of old data and nothing in VAF created or "
            "removes them - review and delete if you no longer need them: %s",
            ", ".join(leftovers),
        )
    return report


def _any_plaintext_left(trees) -> bool:
    from vaf.core import data_files

    for root, patterns in trees.values():
        if not root.is_dir():
            continue
        for pattern in patterns:
            for path in root.glob(pattern):
                if not path.is_file() or path.suffix == ".gz":
                    continue
                try:
                    if not data_files.is_encrypted(path.open("rb").read(len(data_files.FILE_MAGIC))):
                        return True
                except OSError:
                    return True  # cannot tell -> do not tighten
    return False


def _prune_context_archives(vaf_dir: Path) -> int:
    """Age-based sweep, because the existing cleanup needs a clean shutdown.

    `ContextManager.cleanup()` only unlinks what its OWN instance wrote and runs
    from `Agent.shutdown()`, so a crash, a tray kill or any of the other manager
    instances leaves its archives forever. Measured on a real install: 2467 files
    going back three months, each a fuller copy of a chat than the chat file.
    """
    from vaf.core.config import Config

    root = vaf_dir / "context_archive"
    if not root.is_dir():
        return 0
    try:
        days = int(Config.get("context_archive_max_age_days", 14) or 14)
    except (TypeError, ValueError):
        days = 14
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for path in root.glob("context_*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("Pruned %d context archive(s) older than %d days", removed, days)
    return removed


def _prune_orphan_payloads(vaf_dir: Path) -> int:
    """Drop sub-agent payloads whose task is finished and gone.

    `clear_all()` truncates the four queue files and never touched this directory,
    so the full instruction text of every task ever run accumulated indefinitely.
    Only files older than a day are considered, so a task in flight is never
    pulled out from under its runner.
    """
    root = vaf_dir / "subagent_queue" / "task_payloads"
    if not root.is_dir():
        return 0

    live: set = set()
    try:
        from vaf.core.subagent_ipc import get_ipc
        ipc = get_ipc()
        for task in list(ipc.get_pending_tasks()) + list(ipc.get_active_tasks()):
            tid = task.get("task_id") if isinstance(task, dict) else getattr(task, "task_id", None)
            if tid:
                live.add(str(tid))
    except Exception:
        return 0  # cannot tell what is live -> delete nothing

    cutoff = time.time() - 86400
    removed = 0
    for path in root.glob("*.txt"):
        try:
            if path.stem in live or path.stat().st_mtime > cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        logger.info("Pruned %d orphaned sub-agent payload file(s)", removed)
    return removed
