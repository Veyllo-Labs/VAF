# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Re-embed the memory store after an embedding-model change.

Two embedding models can produce same-dimensioned vectors that are mutually
meaningless, and the dimension check in database.py cannot see that. The
`embedding_model` stamp on every row can: this module re-embeds every row
whose stamp differs from the target model, stamps it, and only then is the
store consistent under the new model. Exposed as `vaf memory reembed` and
armed automatically at app start by `ensure_embedding_model_current()`.

Runs on the OWNER engine like the rekey job and for the same reason: the app
role is RLS-restricted, so a restricted run would "succeed" on a fraction of
the rows (see rekey.py). Decryption uses the current store key; rows the key
cannot open are stamped 'unreadable', left untouched, and excluded from the
pending count so they can never wedge the migration (a later successful
`vaf memory rekey` makes them eligible again via --include-unreadable).

The QUERY side stays on the OLD model until the whole store is re-embedded:
the automatic path only flips `memory_embedding_model` in the config after
the pending count reaches zero, so search never runs in a mixed vector
space. The worker is a separate process, which keeps the single-slot model
global honest (server: old model, worker: new model) and keeps bulk
embedding out of the serving process (memory-safety charter in rag.py).

Progress is written as atomic JSON snapshots to a status file so the server
process (banner), the CLI and the TUI can all read the same counters, and a
restarted server can re-attach to a running job.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# Where the managed migration is heading. The config DEFAULTS flip to this
# value only ships together with this module; the hook below keeps config and
# store consistent in both directions.
TARGET_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
LEGACY_EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# Stamp for rows whose ciphertext the current store key cannot open. Not a
# model name by design: it excludes the row from every pending count.
UNREADABLE_STAMP = "unreadable"

_BATCH_SIZE = 100

# Once-per-process latch for the startup hook (at_rest_migration pattern).
# Released again when the DB was unreachable so a later call retries.
_hook_done = False
_hook_lock = threading.Lock()


def status_file_path() -> Path:
    from vaf.core.config import Config
    return Path(Config.APP_DIR) / "maintenance" / "reembed_status.json"


def lock_file_path() -> Path:
    from vaf.core.config import Config
    return Path(Config.APP_DIR) / "maintenance" / "reembed.lock"


@dataclass
class ReembedCounts:
    reembedded: int = 0
    unreadable: int = 0   # ciphertext the current key cannot open - stamped, never overwritten


@dataclass
class ReembedReport:
    memories: ReembedCounts = field(default_factory=ReembedCounts)
    chunks: ReembedCounts = field(default_factory=ReembedCounts)
    pending_after: int = 0
    sweeps: int = 0
    caches_cleared: bool = False
    dry_run: bool = False
    target_model: str = TARGET_EMBEDDING_MODEL
    unreadable_ids: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        mode = " (dry-run: nothing written)" if self.dry_run else ""
        out = [
            f"Target model: {self.target_model}",
            f"Memories: {self.memories.reembedded} re-embedded, "
            f"{self.memories.unreadable} unreadable{mode}",
            f"Chunks:   {self.chunks.reembedded} re-embedded, "
            f"{self.chunks.unreadable} unreadable{mode}",
            f"Sweeps: {self.sweeps}, pending after: {self.pending_after}",
        ]
        if self.caches_cleared:
            out.append("Redis caches cleared (cached vectors/results belonged to the old model)")
        if self.unreadable_ids:
            out.append("Unreadable row ids (stamped, vectors left untouched): "
                       + ", ".join(self.unreadable_ids[:10])
                       + (" ..." if len(self.unreadable_ids) > 10 else ""))
        return out


def _write_status(path: Optional[Path], payload: dict) -> None:
    """Atomic snapshot write (tmp + os.replace, the config-writer pattern):
    a reader never sees a torn file, and the file survives the writer."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".reembed_status.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, str(path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug(f"reembed status write skipped: {e}")


def read_status() -> Optional[dict]:
    """Read the latest status snapshot, or None if there is none/unparsable."""
    try:
        raw = status_file_path().read_bytes()
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def count_pending(db, target_model: str) -> dict:
    """Rows still carrying a vector from another model. 'unreadable' rows are
    excluded on purpose (see module docstring). Also reports any stamp that is
    neither legacy, target nor unreadable, so a custom store is detectable."""
    from sqlalchemy import text
    out = {"memories": 0, "chunks": 0, "foreign_stamps": []}
    for table in ("memories", "chunks"):
        out[table] = (await db.execute(text(
            f"SELECT count(*) FROM {table} "
            "WHERE embedding IS NOT NULL "
            "AND embedding_model IS DISTINCT FROM :t "
            "AND embedding_model IS DISTINCT FROM :unreadable"
        ), {"t": target_model, "unreadable": UNREADABLE_STAMP})).scalar() or 0
        rows = (await db.execute(text(
            f"SELECT DISTINCT embedding_model FROM {table} "
            "WHERE embedding IS NOT NULL AND embedding_model IS NOT NULL"
        ))).scalars().all()
        for stamp in rows:
            if stamp not in (LEGACY_EMBEDDING_MODEL, target_model, UNREADABLE_STAMP) \
                    and stamp not in out["foreign_stamps"]:
                out["foreign_stamps"].append(stamp)
    return out


async def reembed_store(
    target_model: str = TARGET_EMBEDDING_MODEL,
    *,
    dry_run: bool = False,
    include_unreadable: bool = False,
    status_file: Optional[Path] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> ReembedReport:
    """Re-embed every row whose stamp differs from ``target_model``.

    Keyset-paginated (never whole tables in RAM), per-batch commits, and a
    sweep loop: rows written DURING the run by the still-old serving process
    carry the old stamp and are picked up by the next sweep, until a sweep
    finds nothing new. Idempotent - a killed run resumes at the stamps.
    """
    from sqlalchemy import text
    from vaf.memory.crypto import decrypt_field, get_crypto
    from vaf.memory.database import get_owner_db
    from vaf.memory.embeddings import (EmbeddingService, get_memory_usage_mb)
    from vaf.memory.rag import _memory_summary_text
    from vaf.core.log_helper import append_domain_log

    crypto = get_crypto()  # raises early on unreadable config / corrupt key
    svc = EmbeddingService(model_name=target_model)  # pinned override
    dim = svc.get_dimension()
    report = ReembedReport(dry_run=dry_run, target_model=target_model)

    unreadable_filter = "" if include_unreadable else \
        "AND embedding_model IS DISTINCT FROM :unreadable "
    params_base = {"t": target_model}
    if not include_unreadable:
        params_base["unreadable"] = UNREADABLE_STAMP

    def _embed_passage(text_value: str) -> Optional[str]:
        vec = svc.embed_sync(text_value, prefix="passage")
        if not vec or len(vec) != dim:
            return None
        # pgvector text-input format for the CAST(:e AS vector) below.
        return "[" + ",".join(repr(float(x)) for x in vec) + "]"

    def _meta_tags(meta) -> list:
        # Raw text() selects hand JSONB back as a string on asyncpg.
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                return []
        return (meta or {}).get("tags", []) if isinstance(meta, dict) else []

    async with get_owner_db() as db:
        # Reuse the rekey probe: a restricted role sees a fraction of the
        # store and would report a hollow success.
        from vaf.memory.rekey import _assert_owner_visibility
        await _assert_owner_visibility(db)

        pending = await count_pending(db, target_model)
        total = pending["memories"] + pending["chunks"]
        done = 0
        commit_pending = 0

        def _snapshot(phase: str, active: bool = True, error: Optional[str] = None):
            _write_status(status_file, {
                "kind": "memory_reembed",
                "active": active,
                "done": done,
                "total": total,
                "phase": phase,
                "unreadable": report.memories.unreadable + report.chunks.unreadable,
                "target_model": target_model,
                "error": error,
            })
            if progress_cb:
                try:
                    progress_cb(done, total)
                except Exception:
                    pass

        _snapshot("starting")

        made_progress = True
        while made_progress:
            report.sweeps += 1
            made_progress = False

            # ── memories: summary vector from decrypted content head + tags ──
            last_id = None
            while True:
                q = ("SELECT id, encrypted_content, nonce, meta FROM memories "
                     "WHERE embedding IS NOT NULL "
                     "AND embedding_model IS DISTINCT FROM :t "
                     + unreadable_filter
                     + ("AND id > :last " if last_id is not None else "")
                     + "ORDER BY id LIMIT :lim")
                params = dict(params_base, lim=_BATCH_SIZE)
                if last_id is not None:
                    params["last"] = last_id
                rows = (await db.execute(text(q), params)).all()
                if not rows:
                    break
                for mid, content_ct, nonce, meta in rows:
                    last_id = mid
                    try:
                        content = crypto.decrypt(bytes(content_ct), bytes(nonce))
                    except Exception:
                        report.memories.unreadable += 1
                        report.unreadable_ids.append(f"memory:{mid}")
                        if not dry_run:
                            await db.execute(text(
                                "UPDATE memories SET embedding_model = :s WHERE id = :i"),
                                {"s": UNREADABLE_STAMP, "i": mid})
                        done += 1
                        continue
                    if dry_run:
                        report.memories.reembedded += 1  # readable, would re-embed
                        done += 1
                        continue
                    vec = _embed_passage(_memory_summary_text(content, _meta_tags(meta)))
                    if vec is None:
                        # Transient embed failure: count, do NOT stamp - the row
                        # stays pending, so completion (and the config flip)
                        # cannot be claimed past it.
                        report.memories.unreadable += 1
                        report.unreadable_ids.append(f"memory:{mid}")
                        done += 1
                        continue
                    await db.execute(text(
                        "UPDATE memories SET embedding = CAST(:e AS vector), "
                        "embedding_model = :s WHERE id = :i"),
                        {"e": vec, "s": target_model, "i": mid})
                    commit_pending += 1
                    report.memories.reembedded += 1
                    made_progress = True
                    done += 1
                    if commit_pending >= _BATCH_SIZE:
                        await db.commit()
                        commit_pending = 0
                _snapshot("memories")
                append_domain_log("memory", f"[REEMBED] memories done={done}/{total} "
                                            f"rss={get_memory_usage_mb():.0f}MB")

            # ── chunks: vector from the decrypted chunk text ─────────────────
            last_id = None
            while True:
                q = ("SELECT id, text FROM chunks "
                     "WHERE embedding IS NOT NULL "
                     "AND embedding_model IS DISTINCT FROM :t "
                     + unreadable_filter
                     + ("AND id > :last " if last_id is not None else "")
                     + "ORDER BY id LIMIT :lim")
                params = dict(params_base, lim=_BATCH_SIZE)
                if last_id is not None:
                    params["last"] = last_id
                rows = (await db.execute(text(q), params)).all()
                if not rows:
                    break
                for cid, value in rows:
                    last_id = cid
                    plain = decrypt_field(value)
                    if plain == "[Decryption failed]" or not (plain or "").strip():
                        report.chunks.unreadable += 1
                        report.unreadable_ids.append(f"chunk:{cid}")
                        if not dry_run:
                            await db.execute(text(
                                "UPDATE chunks SET embedding_model = :s WHERE id = :i"),
                                {"s": UNREADABLE_STAMP, "i": cid})
                        done += 1
                        continue
                    if dry_run:
                        report.chunks.reembedded += 1  # readable, would re-embed
                        done += 1
                        continue
                    vec = _embed_passage(plain)
                    if vec is None:
                        report.chunks.unreadable += 1
                        report.unreadable_ids.append(f"chunk:{cid}")
                        done += 1
                        continue
                    await db.execute(text(
                        "UPDATE chunks SET embedding = CAST(:e AS vector), "
                        "embedding_model = :s WHERE id = :i"),
                        {"e": vec, "s": target_model, "i": cid})
                    commit_pending += 1
                    report.chunks.reembedded += 1
                    made_progress = True
                    done += 1
                    if commit_pending >= _BATCH_SIZE:
                        await db.commit()
                        commit_pending = 0
                _snapshot("chunks")
                append_domain_log("memory", f"[REEMBED] chunks done={done}/{total} "
                                            f"rss={get_memory_usage_mb():.0f}MB")

            if commit_pending:
                await db.commit()
                commit_pending = 0
            if dry_run:
                break  # one sweep is the answer; nothing was written anyway

            # Rows ingested during the sweep carry the old stamp - recount.
            pending = await count_pending(db, target_model)
            still = pending["memories"] + pending["chunks"]
            if still and made_progress:
                total = done + still
            else:
                report.pending_after = still
                break

        report.pending_after = pending["memories"] + pending["chunks"] if dry_run \
            else report.pending_after

    # Old-model vectors and result snippets sit in Redis under old identities;
    # after a real, complete run they are garbage - clear like rekey does.
    if not dry_run and report.pending_after == 0 \
            and (report.memories.reembedded or report.chunks.reembedded):
        try:
            from vaf.memory.cache import get_cache
            report.caches_cleared = bool(await get_cache().clear_all())
        except Exception as e:
            logger.debug(f"Cache clear skipped: {e}")

    _write_status(status_file, {
        "kind": "memory_reembed",
        "active": False,
        "done": done if not dry_run else 0,
        "total": total if not dry_run else 0,
        "phase": "done",
        "unreadable": report.memories.unreadable + report.chunks.unreadable,
        "target_model": target_model,
        "error": None,
    })
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Startup hook: keep the configured model and the stored vectors consistent.
# Wired into ALL FOUR start lanes (web server startup event, modern CLI,
# classic CLI, TUI bridge) - a hook wired into one lane only is how a cleanup
# once ran for the terminal and never for the web app.
# ─────────────────────────────────────────────────────────────────────────────

def _count_pending_standalone() -> Optional[dict]:
    """Pending counts on a fresh owner connection. None = DB unreachable."""
    async def _inner():
        from vaf.memory.database import get_owner_db
        async with get_owner_db() as db:
            from vaf.memory.rekey import _assert_owner_visibility
            await _assert_owner_visibility(db)
            return await count_pending(db, TARGET_EMBEDDING_MODEL)
    try:
        return asyncio.run(_inner())
    except Exception as e:
        logger.warning(f"reembed hook: store not reachable/probed: {e}")
        return None


def _flip_to_target() -> None:
    """The store is fully on the target model: move the serving process over.
    Atomic config write; live EmbeddingService instances follow via their
    config-backed model_name property, the model global via reset."""
    from vaf.core.config import Config
    from vaf.core.log_helper import append_domain_log
    from vaf.memory.embeddings import reset_embedding_service
    Config.set("memory_embedding_model", TARGET_EMBEDDING_MODEL)
    reset_embedding_service()
    append_domain_log("memory", f"[REEMBED] serving model flipped to {TARGET_EMBEDDING_MODEL}")
    logger.info(f"Memory embedding model is now {TARGET_EMBEDDING_MODEL}")


def _spawn_worker() -> Optional[subprocess.Popen]:
    """Start the re-embed worker as a separate process. The worker holds the
    new model; the serving process keeps the old one until the flip (single-
    slot model global stays honest in both). Double-starts are prevented by
    the worker's own file lock."""
    # Same spawn vector as the service wrapper (service.py): -m vaf.main.
    cmd = [sys.executable, "-m", "vaf.main", "memory", "reembed", "--auto",
           "--target-model", TARGET_EMBEDDING_MODEL,
           "--status-file", str(status_file_path())]
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.error(f"reembed hook: worker spawn failed: {e}")
        return None


def _hook_worker() -> None:
    global _hook_done
    from vaf.core.config import Config

    config_model = Config.get("memory_embedding_model", LEGACY_EMBEDDING_MODEL)
    if config_model not in (TARGET_EMBEDDING_MODEL, LEGACY_EMBEDDING_MODEL):
        logger.info(f"reembed hook: custom embedding model '{config_model}' - "
                    "managed migration does not apply")
        return

    pending = _count_pending_standalone()
    if pending is None:
        # Unreachable DB must not consume the once-per-process latch
        # (database.py migration-latch incident) - retry on the next call.
        with _hook_lock:
            _hook_done = False
        return
    if pending["foreign_stamps"]:
        logger.warning(f"reembed hook: store carries vectors from unmanaged model(s) "
                       f"{pending['foreign_stamps']} - not touching it")
        return

    open_rows = pending["memories"] + pending["chunks"]
    if open_rows == 0:
        # Fresh install / already migrated: converge the config.
        if config_model != TARGET_EMBEDDING_MODEL:
            _flip_to_target()
        return

    if config_model == TARGET_EMBEDDING_MODEL:
        # DEFAULTS flipped ahead of this store (sparse config without the
        # key): PIN the old model before anything serves a query, then
        # migrate. Queries must never run in a mixed vector space.
        logger.warning("reembed hook: config says target model but the store is "
                       "not migrated - pinning the legacy model until it is")
        Config.set("memory_embedding_model", LEGACY_EMBEDDING_MODEL)
        from vaf.memory.embeddings import reset_embedding_service
        reset_embedding_service()

    logger.info(f"reembed hook: {open_rows} rows to re-embed towards "
                f"{TARGET_EMBEDDING_MODEL}; starting background worker")
    from vaf.core.maintenance_state import MAINTENANCE

    def _mirror_status() -> None:
        s = read_status() or {}
        MAINTENANCE.update(
            kind="memory_reembed", active=bool(s.get("active")),
            done=int(s.get("done") or 0), total=int(s.get("total") or 0),
            phase=str(s.get("phase") or ""), error=str(s.get("error") or ""))

    MAINTENANCE.update(kind="memory_reembed", active=True, done=0,
                       total=open_rows, phase="starting")
    for attempt in range(3):
        proc = _spawn_worker()
        if proc is None:
            MAINTENANCE.update(kind="memory_reembed", active=False,
                               phase="error", error="worker spawn failed")
            return
        while True:
            try:
                proc.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                _mirror_status()
                continue
        if (read_status() or {}).get("active"):
            # Not our child doing the work (a previous boot's worker holds
            # the lock): follow the status file until it settles.
            import time
            while (read_status() or {}).get("active"):
                _mirror_status()
                time.sleep(5)
        _mirror_status()
        pending = _count_pending_standalone()
        if pending is None:
            return
        if pending["memories"] + pending["chunks"] == 0:
            _flip_to_target()
            MAINTENANCE.update(kind="memory_reembed", active=False, phase="done",
                               done=open_rows, total=open_rows)
            return
        logger.warning(f"reembed hook: worker finished with "
                       f"{pending['memories'] + pending['chunks']} rows still pending "
                       f"(attempt {attempt + 1}/3)")
    logger.error("reembed hook: migration did not complete after 3 attempts; "
                 "serving stays on the legacy model (run 'vaf memory reembed' "
                 "manually and check logs/memory.log [REEMBED] lines)")
    MAINTENANCE.update(kind="memory_reembed", active=False, phase="error",
                       error="migration incomplete after 3 attempts")


def ensure_embedding_model_current() -> None:
    """Once per process, on every start lane: reconcile the configured
    embedding model with the stored vectors, spawning the background re-embed
    worker when they diverge. Returns immediately; all waiting happens on a
    monitor thread. The thread only counts rows, spawns a process and writes
    config - no embedding and no ingest runs on it, so the memory-safety
    charter's main-loop rule (rag.py) is not in play."""
    global _hook_done
    with _hook_lock:
        if _hook_done:
            return
        _hook_done = True
    threading.Thread(target=_hook_worker, daemon=True,
                     name="reembed-startup-hook").start()
