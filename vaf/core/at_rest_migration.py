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
(`repair_context_copies_once`): the saved context state that one chat carried
into another before the session loader cleared it on a switch. Once per
installation, recorded by `context_copy_sweep_done` in the config, the same place
this sweep records its other lasting decision (`allow_plaintext_at_rest`). The
headless runner calls it too, before its first task, so no turn saves a chat
while the repair rewrites it.

Wired into the web/tray startup AND the CLI start path on purpose. Wiring a
repair into one lane only is a mistake this repo has made before: a cleanup
that lived in the interactive CLI startup never ran for the web app, so the
records it was supposed to remove were permanent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

logger = logging.getLogger("vaf.core.at_rest_migration")

_done = False

# The one-time repair's done flag. A config key beside `allow_plaintext_at_rest`
# rather than a step in vaf/core/migrations.py: those run inside Config.load(), in
# every process and long before the keyring can open a chat, and must stay pure
# transforms of the config dict.
CONTEXT_COPY_SWEEP_DONE = "context_copy_sweep_done"

# On-disk format tag of the repair's journal (`_journal_path`). The reader also takes
# a journal without one. Pinned as a literal in tests/test_persisted_format_tags.py.
CONTEXT_REPAIR_FORMAT = "ctxrepair-1-141182"

_repair_lock = threading.Lock()
_repair_ran = False


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

    counts = repair_context_copies_once()
    if counts is not None:
        report["context_copies"] = counts

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


def _state_digest(data) -> Optional[str]:
    """`_context_state_key` as a short digest: what groups chats, and what the journal
    records instead of the summaries themselves."""
    key = _context_state_key(data)
    return hashlib.sha256(key.encode("utf-8")).hexdigest() if key is not None else None


def repair_context_copies_once() -> Optional[Dict[str, int]]:
    """Run the one-time repair of the chats (`_drop_shared_context_state`) when it is due.

    Once per process, and serialized. The web startup (in every lifespan, so the second
    server of the TLS mode waits as well), the headless runner before its first task and
    every CLI start path call it; whichever comes first runs the pass and the others wait
    for it. That is what keeps a turn's save off a chat between the pass's read and its
    write: `SessionManager.save` takes no lock the pass could hold, so the pass has to be
    over before anything that saves chats starts. Returns the counts, or None when
    nothing ran.

    `context_copy_sweep_done` is set only after a pass that left nothing open: no
    failed rewrite (a group left unfinished in the journal is one) and no chat it could
    not read (a key that is not in the ring at this start would otherwise end the repair
    with every copy still in place). Otherwise the next start runs it again.

    NAMED BOUNDARY: a second VAF process that saves a chat while this one's pass runs
    (a terminal app started in the seconds after the server, on an installation that has
    not finished the pass) is not held off; nothing serializes saves across processes.
    """
    global _repair_ran
    with _repair_lock:
        if _repair_ran:
            return None
        _repair_ran = True
        try:
            from vaf.core import data_stores
            from vaf.core.config import Config

            if Config.get_bool(CONTEXT_COPY_SWEEP_DONE, False):
                return None
            counts = _drop_shared_context_state(data_stores.sealed_trees().get("sessions"))
        except Exception as e:
            logger.warning("Context state repair skipped: %s", e)
            return None
        if counts["dropped"]:
            logger.info("Context state repair: dropped the saved context state of %d chat(s) "
                        "that shared it with another chat.", counts["dropped"])
        if counts["failed"]:
            logger.warning("Context state repair incomplete, %d chat(s) could not be rewritten; "
                           "it will run again at the next start.", counts["failed"])
        if counts["unreadable"]:
            logger.warning("Context state repair incomplete, %d chat(s) could not be read; "
                           "it will run again at the next start.", counts["unreadable"])
        if not (counts["failed"] or counts["unreadable"]):
            try:
                Config.set(CONTEXT_COPY_SWEEP_DONE, True)
            except Exception as e:
                logger.warning("Could not record the context state repair as done: %s", e)
        return counts


def _drop_shared_context_state(tree) -> Dict[str, int]:
    """Drop the saved context state that one chat carried into another.

    Until the session loader cleared the agent's context manager on a switch, a chat with
    nothing saved (a contact's first message, a new web chat, another account's new chat
    on the same worker) took over the previous chat's summary, decisions, goal, files and
    errors, and the runner's next save stored them as that chat's own. Every later load
    restored them, and they went to the model with every message of that chat.

    A copy cannot be told from its origin except by the identical text, so EVERY chat
    whose saved context state is identical to another chat's (`_context_state_key`)
    loses its `context` entry, the origin included. The origin loses only its LLM
    summary: its full transcript is replayed and compressed structurally on its next
    load, and the next compression in a turn writes a new summary. Nothing else in the
    file changes: not the transcript, not the other `runtime_state` keys (sidebar
    documents, editor selections, the other providers), not `updated_at`, and not the
    file's modification time, which orders the chat list.

    All or nothing per group of identical chats, a crash included. Before the first
    write of a group its chats go into a journal (`_journal_path`, encrypted like the
    stores); the journal is removed when the group is done. A start that finds one,
    after a crash or a write that failed, finishes that group before anything else: it
    drops the entry from every listed chat that still holds the same state. Without
    that, the chats already rewritten would leave the rest looking unique for good.

    Chats are encrypted at rest and stay so: each file is read through the session read
    seam's decrypt and written back through `data_files.write_bytes_atomic`, as
    ciphertext when it was ciphertext, whatever `file_encryption_enabled` says for new
    writes. A file that cannot be read (a key the ring does not hold, a plaintext file in
    an enforced store) is counted and never written. The rewrite reads the file again
    under the session's append lock, which holds off the background appends, and drops
    the entry only while the file still holds the shared state.

    NAMED BOUNDARY: legacy `.json.gz` chats are not read; the sealed globs leave them
    out, and the loader prefers the `.json` a later save writes beside one. A copy is
    found only while it is identical: an origin that later saved a different state
    leaves its copy alone.
    """
    counts = {"dropped": 0, "changed": 0, "gone": 0, "unreadable": 0, "failed": 0}
    if not tree:
        return counts
    root, patterns = Path(tree[0]), tree[1]
    if not (root / "sessions").is_dir():
        return counts

    # A group a start before this one left unfinished goes first.
    try:
        journal = _read_journal()
    except Exception as e:
        logger.warning("The context state repair journal could not be read: %s", e)
        counts["unreadable"] += 1
        return counts
    if journal is not None:
        state, chats = journal
        if not _rewrite_group(root, chats, state, counts):
            return counts
        _clear_journal()

    from vaf.core.session import SessionManager

    reader = SessionManager(storage_dir=str(root / "sessions"))
    groups: Dict[str, List[Path]] = {}
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path.suffix == ".gz":
                continue
            try:
                state = _state_digest(reader._read_session_file(path))
            except Exception:
                counts["unreadable"] += 1
                continue
            if state is not None:
                groups.setdefault(state, []).append(path)

    for state, paths in groups.items():
        if len(paths) < 2:
            continue
        chats = [path.relative_to(root).as_posix() for path in paths]
        if not _write_journal(state, chats):
            counts["failed"] += len(chats)
            break
        if not _rewrite_group(root, chats, state, counts):
            break  # the journal stays: the next start finishes this group first
        _clear_journal()
    return counts


def _rewrite_group(root: Path, chats: List[str], state: str, counts: Dict[str, int]) -> bool:
    """Drop the entry from every chat of one group; True when none is left open."""
    done = True
    for rel in chats:
        result = _drop_context_entry(root / rel, state)
        counts[result] += 1
        if result in ("failed", "unreadable"):
            done = False
    return done


def _drop_context_entry(path: Path, state: str) -> str:
    """Remove the `context` provider entry from one chat file that still holds `state`.

    Returns 'dropped', 'changed' (the file no longer holds that state), 'gone' (the chat
    was deleted), 'unreadable' or 'failed' (the write did not happen; the file is as it
    was)."""
    from vaf.core import data_files
    from vaf.core.session import _append_lock

    with _append_lock(path.name[: -len(".json")]):
        try:
            raw = path.read_bytes()
            stat = path.stat()
        except FileNotFoundError:
            return "gone"
        except OSError:
            return "unreadable"
        try:
            data = json.loads(data_files.decrypt_bytes(raw).decode("utf-8"))
        except Exception:
            return "unreadable"
        if _state_digest(data) != state:
            return "changed"
        data["runtime_state"]["providers"].pop("context", None)
        # The serialization SessionManager.save writes, lone surrogates replaced alike.
        payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8", errors="replace")
        try:
            # Ciphertext stays ciphertext: with file_encryption_enabled off, new writes are
            # plaintext, but a repair must not turn a chat nobody touched into one.
            data_files.write_bytes_atomic(
                path, payload, encrypt=True if data_files.is_encrypted(raw) else None)
        except Exception as e:
            logger.warning("Could not drop the copied context state from %s: %s", path.name, e)
            return "failed"
        _keep_mtime(path, stat)
    return "dropped"


def _keep_mtime(path: Path, stat) -> None:
    """The chat list is ordered by the file's modification time, and a repair is no news."""
    try:
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    except OSError:
        pass


def _journal_path() -> Path:
    """Where an unfinished group is recorded: a runtime file of the maintenance folder,
    never backed up (vaf/core/data_stores.py)."""
    from vaf.core.config import Config
    return Path(Config.APP_DIR) / "maintenance" / "context_repair.json"


def _write_journal(state: str, chats: List[str]) -> bool:
    from vaf.core import data_files
    try:
        data_files.write_json_atomic(
            _journal_path(), {"format": CONTEXT_REPAIR_FORMAT, "state": state, "chats": chats})
        return True
    except Exception as e:
        logger.warning("Could not write the context state repair journal: %s", e)
        return False


def _read_journal():
    """`(state, chats)` of an unfinished group, or None when there is none. Raises when a
    journal is there and cannot be used: a group must never be guessed at."""
    from vaf.core import data_files

    path = _journal_path()
    if not path.exists():
        return None
    data = json.loads(data_files.read_bytes(path).decode("utf-8"))
    if not isinstance(data, dict) or data.get("format", CONTEXT_REPAIR_FORMAT) != CONTEXT_REPAIR_FORMAT:
        raise ValueError("not a journal this version writes")
    state, chats = data.get("state"), data.get("chats")
    if not isinstance(state, str) or not isinstance(chats, list):
        raise ValueError("the journal is incomplete")
    for rel in chats:
        # Only chat files of the sessions store, never a path out of it.
        parts = PurePosixPath(str(rel)).parts
        if not parts or parts[0] != "sessions" or ".." in parts or not str(rel).endswith(".json"):
            raise ValueError(f"the journal names a file outside the chats: {rel!r}")
    return state, [str(rel) for rel in chats]


def _clear_journal() -> None:
    try:
        _journal_path().unlink()
    except FileNotFoundError:
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
