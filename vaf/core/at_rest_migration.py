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

And it runs ONE one-time repair of the chats themselves
(`_drop_shared_context_state`): the saved context state that one chat carried
into another before the session loader cleared it on a switch. Once per
installation, recorded by `context_copy_sweep_done` in the config, the same place
this sweep records its other lasting decision (`allow_plaintext_at_rest`).

Wired into the web/tray startup AND the CLI start path on purpose. Wiring a
repair into one lane only is a mistake this repo has made before: a cleanup
that lived in the interactive CLI startup never ran for the web app, so the
records it was supposed to remove were permanent.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("vaf.core.at_rest_migration")

_done = False

# The one-time repair's done-marker. A config key beside `allow_plaintext_at_rest`
# rather than a step in vaf/core/migrations.py: those run inside Config.load(), in
# every process and long before the keyring can open a chat, and must stay pure
# transforms of the config dict.
CONTEXT_COPY_SWEEP_DONE = "context_copy_sweep_done"


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

    from vaf.core import data_stores
    from vaf.core.config import Config
    from vaf.core.data_files import encryption_enabled
    from vaf.core.platform import Platform
    from vaf.core.secure_store import harden_dir, harden_path

    report: Dict[str, object] = {}
    vaf_dir = Path(Platform.vaf_dir())

    # The stores and their files come from the one list (vaf/core/data_stores.py, `sealed`),
    # which the key probe in data_files reads as well, the working memory of this launch
    # (`main_context`, under the launch directory) included.
    sealed = data_stores.sealed_trees()
    trees = data_stores.sealed_trees(with_launch_dir=True)

    # Modes first: they apply whether or not encryption is on, and they are the
    # only protection a second local account ever sees.
    entries = [root / name for name, (root, _patterns) in sealed.items()]
    for directory in entries + [trees["main_context"][0]]:
        if directory.is_dir():
            harden_dir(directory)

    # And the stores that are NOT sealed, for a reason worth writing down: their writers
    # do not read through the encrypted seam (automations, reminders and the planner use
    # plain json.load/json.dump), so encrypting them would lock their own loaders out.
    # Modes are the half that can be applied today, and they are not cosmetic here: an
    # automation's prompt is user-authored natural language, the same content class as a
    # chat message, and it sat world-readable in a world-listable directory while the
    # sessions beside it were owner-only ciphertext. Bringing them under encryption means
    # converting those writers to data_files first; that is its own change, and until it
    # happens this is the honest half. Which stores: `harden` in the same list.
    for name, root in data_stores.hardened_dirs().items():
        if root.is_dir():
            harden_dir(root)
            report.setdefault("hardened", []).append(name)
            for child in root.rglob("*"):
                try:
                    if child.is_dir():
                        harden_dir(child)
                    elif child.is_file():
                        harden_path(child)
                except OSError:
                    continue

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

    # Once per installation, and marked done only when no rewrite failed, so a chat that
    # could not be written gets the pass again at the next start.
    try:
        if not Config.get_bool(CONTEXT_COPY_SWEEP_DONE, False):
            counts = _drop_shared_context_state(sealed.get("sessions"))
            report["context_copies"] = counts
            if counts["dropped"]:
                logger.info("Dropped the saved context state of %d chat(s) that held the same "
                            "state as another chat: %s", counts["dropped"], counts)
            if not counts["failed"]:
                Config.set(CONTEXT_COPY_SWEEP_DONE, True)
    except Exception as e:
        logger.warning("Context state repair skipped: %s", e)

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


def _context_state_key(data) -> Optional[str]:
    """A chat's saved context state as one comparable string; None when it has none.

    The `context` provider serializes the agent's context manager: its intent and its
    state, plus the manager's limits. Compared are the intent and the state without their
    `last_updated` stamps. Left out are `captured_at` (when it was saved) and the limits
    (`max_tokens`, `trigger_threshold`), which belong to the agent and its model, not to a
    chat: a copy saved after a model change carries other limits and the same content.
    An empty state is None - every chat that never compressed carries the same empty
    state, and it holds nothing of anyone.
    """
    runtime = data.get("runtime_state") if isinstance(data, dict) else None
    providers = runtime.get("providers") if isinstance(runtime, dict) else None
    entry = providers.get("context") if isinstance(providers, dict) else None
    state = entry.get("state") if isinstance(entry, dict) else None
    if not isinstance(state, dict):
        return None
    content = {}
    for part in ("intent", "state"):
        fields = state.get(part)
        if isinstance(fields, dict):
            content[part] = {k: v for k, v in fields.items() if k != "last_updated"}
    if not any(value for fields in content.values() for value in fields.values()):
        return None
    return json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)


def _drop_shared_context_state(tree) -> Dict[str, int]:
    """Drop the saved context state that one chat carried into another. Runs once.

    Until the session loader cleared the agent's context manager on a switch, a chat with
    nothing saved (a contact's first message, a new web chat, another account's new chat
    on the same worker) took over the previous chat's summary, decisions, goal, files and
    errors, and the runner's next save stored them as that chat's own. Every later load
    restored them, and they went to the model with every message of that chat.

    A copy cannot be told from its origin except by the identical text, so EVERY chat
    whose saved context state is identical to another chat's (`_context_state_key`)
    loses its `context` entry, the origin included. The origin loses only its LLM
    summary: its full transcript is replayed and compressed structurally on its next
    load. Nothing else in the file changes: not the transcript, not the other
    `runtime_state` keys (sidebar documents, editor selections, the other providers),
    not `updated_at`, and not the file's modification time, which orders the chat list.

    Chats are encrypted at rest, and stay so: each file is read through the session read
    seam and written back through `data_files.write_bytes_atomic`, as a save would. A
    file that cannot be read (a key the ring does not hold, a plaintext file in an
    enforced store) is counted and never written. The rewrite reads the file again under
    the session's append lock and drops the entry only while the file still holds the
    shared state, so a turn saved since the scan is not replaced by an older copy. And
    it is all or nothing per shared state: when one chat cannot be written, the chats
    of the same state already rewritten are put back byte for byte, and the pass is not
    marked done, so the next start finds the whole group again.

    NAMED BOUNDARY: legacy `.json.gz` chats are not read; the sealed globs leave them
    out, and the loader prefers the `.json` a later save writes beside one. A turn that
    loaded a copy-carrying chat before this pass and saves it afterwards writes the copy
    back; the window is the first seconds after a start. And a copy is found only while
    it is identical: an origin that later saved a different state leaves its copy alone.
    """
    counts = {"dropped": 0, "changed": 0, "unreadable": 0, "failed": 0, "put_back": 0}
    if not tree:
        return counts
    root, patterns = tree
    sessions_dir = Path(root) / "sessions"
    if not sessions_dir.is_dir():
        return counts

    from vaf.core.session import SessionManager

    reader = SessionManager(storage_dir=str(sessions_dir))
    groups: Dict[str, List[Path]] = {}
    for pattern in patterns:
        for path in sorted(Path(root).glob(pattern)):
            if not path.is_file() or path.suffix == ".gz":
                continue
            try:
                key = _context_state_key(reader._read_session_file(path))
            except Exception:
                counts["unreadable"] += 1
                continue
            if key is not None:
                groups.setdefault(key, []).append(path)

    for key, paths in groups.items():
        if len(paths) < 2:
            continue
        # All or nothing per shared state: once one chat has lost the entry, a chat that
        # kept it because its write failed would look unique at the next start and never
        # be found again. So a failure puts the files already rewritten back as they were.
        rewritten = []
        failed = False
        for path in paths:
            result, before = _drop_context_entry(path, key)
            if result == "dropped":
                rewritten.append((path, before))
            elif result == "failed":
                counts["failed"] += 1
                failed = True
                break
            else:
                counts[result] += 1
        if failed:
            for path, before in rewritten:
                _put_back(path, before)
            counts["put_back"] += len(rewritten)
        else:
            counts["dropped"] += len(rewritten)
    return counts


def _drop_context_entry(path: Path, key: str):
    """Remove the `context` provider entry from one chat file that still holds `key`.

    Returns `(result, before)`: result is 'dropped', 'changed' (the file no longer holds
    that state), 'unreadable' or 'failed' (the write did not happen; the file is as it
    was), and `before` the file's bytes and stat as they were read, for `_put_back`."""
    from vaf.core import data_files
    from vaf.core.session import _append_lock

    with _append_lock(path.name[: -len(".json")]):
        try:
            before = (path.read_bytes(), path.stat())
            # The session read seam's own decrypt (SessionManager._read_session_file), on
            # the very bytes that `_put_back` would restore.
            data = json.loads(data_files.decrypt_bytes(before[0]).decode("utf-8"))
        except Exception:
            return "unreadable", None
        if _context_state_key(data) != key:
            return "changed", None
        data["runtime_state"]["providers"].pop("context", None)
        # The serialization SessionManager.save writes, lone surrogates replaced alike.
        payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8", errors="replace")
        try:
            data_files.write_bytes_atomic(path, payload)
        except Exception as e:
            logger.warning("Could not drop the copied context state from %s: %s", path.name, e)
            return "failed", None
        _keep_mtime(path, before[1])
    return "dropped", before


def _put_back(path: Path, before) -> None:
    """Write a chat file's earlier bytes back unchanged (already ciphertext, if they were)."""
    from vaf.core import data_files
    from vaf.core.session import _append_lock

    raw, stat = before
    with _append_lock(path.name[: -len(".json")]):
        try:
            data_files.write_bytes_atomic(path, raw, encrypt=False)
        except Exception as e:
            logger.warning("Could not put %s back after a failed repair: %s", path.name, e)
            return
        _keep_mtime(path, stat)


def _keep_mtime(path: Path, stat) -> None:
    """The chat list is ordered by the file's modification time, and a repair is no news."""
    try:
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    except OSError:
        pass


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
