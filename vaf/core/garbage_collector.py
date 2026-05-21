"""
VAF Garbage Collector – periodic cleanup of temporary files, logs, and cache.

Runs as a daemon thread every gc_interval_hours (default 12).
- Log files: use dated names (basename_YYYY-MM-DD.log / .txt). GC deletes any such file
  whose date in the filename is older than gc_max_age_hours (default 48).
- Temp files: deletes by mtime (older than gc_max_age_hours).
- Cache dir: deletes by mtime. Thinking sessions: deleted by thinking_gc_hours.
- Thinking run logs (thinking_mode_logs/**/*.json): deleted by mtime (gc_max_age_hours).
Controlled via config: gc_enabled, gc_interval_hours, gc_max_age_hours, thinking_gc_hours.
"""

import gzip
import json
import logging
import re
import shutil
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

logger = logging.getLogger("vaf.gc")

# Pattern for dated log files: basename_YYYY-MM-DD.log or basename_YYYY-MM-DD.txt
# GC deletes files whose date in the name is older than gc_max_age_hours.
DATED_LOG_PATTERN = re.compile(r"^(.+)_(\d{4}-\d{2}-\d{2})\.(log|txt)$", re.IGNORECASE)

# Temp-file prefixes used by VAF (see tempfile calls across the codebase).
VAF_TEMP_PREFIXES = ("vaf_",)

# Default config values.
_DEFAULT_ENABLED = True
_DEFAULT_INTERVAL_HOURS = 12
_DEFAULT_MAX_AGE_HOURS = 48


class GarbageCollector:
    """Singleton garbage collector that periodically removes stale temp files."""

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._timer: threading.Timer | None = None
        self._running = False

    @classmethod
    def get_instance(cls) -> "GarbageCollector":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def start(self):
        """Start the periodic GC timer. Safe to call multiple times."""
        if not self._is_enabled():
            logger.info("[GC] Garbage collector disabled in config")
            return
        if self._running:
            return
        self._running = True
        # Run first collection immediately, then schedule recurring.
        threading.Thread(target=self._initial_run, daemon=True).start()
        logger.info(
            "[GC] Started (interval=%dh, max_age=%dh)",
            self._interval_hours(),
            self._max_age_hours(),
        )

    def stop(self):
        """Cancel the timer."""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def run_now(self) -> Dict[str, int]:
        """Run a collection cycle immediately. Returns stats dict."""
        return self._collect()

    # ------------------------------------------------------------------ #
    #  Scheduling                                                         #
    # ------------------------------------------------------------------ #

    def _initial_run(self):
        try:
            stats = self._collect()
            logger.info("[GC] Initial collection: %s", stats)
        except Exception as exc:
            logger.error("[GC] Initial collection failed: %s", exc)
        self._schedule_next()

    def _schedule_next(self):
        if not self._running:
            return
        interval_sec = self._interval_hours() * 3600
        self._timer = threading.Timer(interval_sec, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self):
        try:
            stats = self._collect()
            logger.info("[GC] Collection complete: %s", stats)
            
            # Audit log notification
            try:
                from vaf.core.user_notifications import append_notification
                from vaf.core.config import get_local_admin_scope_id
                
                deleted = stats.get("deleted", 0)
                freed = stats.get("freed_bytes", 0) / (1024 * 1024)
                sessions = stats.get("thinking_sessions_deleted", 0)
                
                if deleted > 0 or sessions > 0:
                    append_notification(
                        user_scope_id=str(get_local_admin_scope_id()),
                        kind="system",
                        title="System cleanup finished",
                        status="success",
                        summary=f"Deleted {deleted} files and {sessions} old thinking sessions. Freed {freed:.1f} MB."
                    )
            except Exception:
                pass
                
        except Exception as exc:
            logger.error("[GC] Collection failed: %s", exc)
        self._schedule_next()

    # ------------------------------------------------------------------ #
    #  Collection logic                                                   #
    # ------------------------------------------------------------------ #

    def _collect(self) -> Dict[str, int]:
        cutoff = datetime.now() - timedelta(hours=self._max_age_hours())
        stats: Dict[str, int] = {"deleted": 0, "freed_bytes": 0, "errors": 0, "thinking_sessions_deleted": 0}

        self._clean_log_files(cutoff, stats)
        self._clean_temp_files(cutoff, stats)
        self._clean_cache_dir(cutoff, stats)
        self._clean_old_thinking_sessions(stats)
        self._clean_old_thinking_run_logs(cutoff, stats)

        return stats

    # -- Old thinking-mode sessions (by age) -----------------------------

    def _clean_old_thinking_sessions(self, stats: Dict[str, int]) -> None:
        """Delete thinking-mode sessions older than thinking_gc_hours."""
        try:
            from vaf.core.session import SessionManager
            from vaf.core.config import Config
        except Exception:
            return
        hours = 12
        try:
            hours = int(Config.get("thinking_gc_hours", 12) or 12)
        except (TypeError, ValueError):
            pass
        cutoff = datetime.now() - timedelta(hours=hours)
        sm = SessionManager()
        for filepath in list(sm.storage_dir.glob("*.json")) + list(sm.storage_dir.glob("*.json.gz")):
            sid = filepath.name.split(".")[0]
            try:
                if filepath.suffix == ".gz":
                    with gzip.open(filepath, "rt", encoding="utf-8") as f:
                        data = json.load(f)
                else:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                if (data.get("metadata") or {}).get("source") != "thinking":
                    continue
                updated = data.get("updated_at") or data.get("created_at") or ""
                if not updated:
                    continue
                try:
                    # Parse ISO datetime (use first 19 chars for naive YYYY-MM-DDTHH:MM:SS)
                    updated_dt = datetime.fromisoformat(updated[:19])
                except Exception:
                    continue
                if updated_dt < cutoff:
                    sm.delete(sid)
                    stats["thinking_sessions_deleted"] = stats.get("thinking_sessions_deleted", 0) + 1
                    logger.debug("[GC] Deleted old thinking session %s", sid)
            except Exception as exc:
                logger.debug("[GC] Skip session %s: %s", sid, exc)

    # -- Thinking-mode run log JSONs (Platform.vaf_dir/thinking_mode_logs/**/*.json) --

    def _clean_old_thinking_run_logs(self, cutoff: datetime, stats: Dict[str, int]) -> None:
        """Delete thinking-mode run log JSONs older than gc_max_age_hours (by mtime)."""
        try:
            from vaf.core.platform import Platform
            log_dir = Platform.vaf_dir() / "thinking_mode_logs"
        except Exception:
            return
        if not log_dir.exists():
            return
        try:
            for entry in log_dir.rglob("*.json"):
                if entry.is_file():
                    self._delete_if_old(entry, cutoff, stats)
        except PermissionError:
            stats["errors"] += 1

    # -- Log files (dated names: basename_YYYY-MM-DD.log / .txt) -------------

    def _clean_log_files(self, cutoff: datetime, stats: Dict[str, int]):
        try:
            from vaf.core.log_helper import get_app_log_dir
            log_dir = get_app_log_dir()
        except Exception:
            return
        if not log_dir.exists():
            return
        try:
            for entry in log_dir.iterdir():
                if not entry.is_file():
                    continue
                m = DATED_LOG_PATTERN.match(entry.name)
                if m:
                    self._delete_log_if_date_old(entry, cutoff, stats)
        except PermissionError:
            stats["errors"] += 1

    @staticmethod
    def _delete_log_if_date_old(filepath: Path, cutoff: datetime, stats: Dict[str, int]):
        """Delete a dated log file if the date in its name is before cutoff."""
        try:
            if not filepath.is_file():
                return
            m = DATED_LOG_PATTERN.match(filepath.name)
            if not m:
                return
            date_str = m.group(2)
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date >= cutoff:
                return
            size = filepath.stat().st_size
            filepath.unlink()
            stats["deleted"] += 1
            stats["freed_bytes"] += size
            logger.debug("[GC] Deleted dated log %s (%d bytes)", filepath, size)
        except Exception as exc:
            logger.debug("[GC] Failed to delete %s: %s", filepath, exc)
            stats["errors"] += 1

    # -- System temp files -----------------------------------------------

    def _clean_temp_files(self, cutoff: datetime, stats: Dict[str, int]):
        temp_dir = Path(tempfile.gettempdir())
        if not temp_dir.exists():
            return
        try:
            for entry in temp_dir.iterdir():
                if not any(entry.name.startswith(p) for p in VAF_TEMP_PREFIXES):
                    continue
                if entry.is_file():
                    self._delete_if_old(entry, cutoff, stats)
                elif entry.is_dir():
                    self._delete_dir_if_old(entry, cutoff, stats)
        except PermissionError:
            stats["errors"] += 1

    # -- Cache directory --------------------------------------------------

    def _clean_cache_dir(self, cutoff: datetime, stats: Dict[str, int]):
        try:
            from vaf.core.platform import Platform
            cache_dir = Platform.cache_dir()
        except Exception:
            return
        if not cache_dir.exists():
            return
        try:
            for entry in cache_dir.rglob("*"):
                if entry.is_file():
                    self._delete_if_old(entry, cutoff, stats)
        except PermissionError:
            stats["errors"] += 1

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _delete_if_old(filepath: Path, cutoff: datetime, stats: Dict[str, int]):
        try:
            if not filepath.is_file():
                return
            mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
            if mtime < cutoff:
                size = filepath.stat().st_size
                filepath.unlink()
                stats["deleted"] += 1
                stats["freed_bytes"] += size
                logger.debug("[GC] Deleted %s (%d bytes)", filepath, size)
        except Exception as exc:
            logger.debug("[GC] Failed to delete %s: %s", filepath, exc)
            stats["errors"] += 1

    @staticmethod
    def _delete_dir_if_old(dirpath: Path, cutoff: datetime, stats: Dict[str, int]):
        try:
            if not dirpath.is_dir():
                return
            mtime = datetime.fromtimestamp(dirpath.stat().st_mtime)
            if mtime < cutoff:
                size = sum(f.stat().st_size for f in dirpath.rglob("*") if f.is_file())
                shutil.rmtree(dirpath, ignore_errors=True)
                stats["deleted"] += 1
                stats["freed_bytes"] += size
                logger.debug("[GC] Deleted dir %s (%d bytes)", dirpath, size)
        except Exception as exc:
            logger.debug("[GC] Failed to delete dir %s: %s", dirpath, exc)
            stats["errors"] += 1

    # ------------------------------------------------------------------ #
    #  Config helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_enabled() -> bool:
        try:
            from vaf.core.config import Config
            return bool(Config.get("gc_enabled", _DEFAULT_ENABLED))
        except Exception:
            return _DEFAULT_ENABLED

    @staticmethod
    def _interval_hours() -> int:
        try:
            from vaf.core.config import Config
            return int(Config.get("gc_interval_hours", _DEFAULT_INTERVAL_HOURS))
        except Exception:
            return _DEFAULT_INTERVAL_HOURS

    @staticmethod
    def _max_age_hours() -> int:
        try:
            from vaf.core.config import Config
            return int(Config.get("gc_max_age_hours", _DEFAULT_MAX_AGE_HOURS))
        except Exception:
            return _DEFAULT_MAX_AGE_HOURS
