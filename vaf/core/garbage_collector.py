"""
VAF Garbage Collector – periodic cleanup of temporary files, logs, and cache.

Runs as a daemon thread every gc_interval_hours (default 12).
Deletes files older than gc_max_age_hours (default 48).
Controlled via config keys: gc_enabled, gc_interval_hours, gc_max_age_hours.
"""

import logging
import shutil
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

logger = logging.getLogger("vaf.gc")

# Log filenames safe to delete (allowlist – no wildcards).
KNOWN_LOG_FILES = frozenset({
    "backend.log",
    "webui.log",
    "web_debug.log",
    "server.log",
    "server_cmd.log",
    "queue.log",
    "memory.log",
    "workflow_debug.log",
    "telegram_reply.log",
    "rag.log",
    "prompt.log",
    "headless.log",
    "startup_trace.txt",
    "platform_subprocess.log",
    "tray_debug.log",
    "tray_startup.txt",
    "callback_debug.txt",
})

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
        except Exception as exc:
            logger.error("[GC] Collection failed: %s", exc)
        self._schedule_next()

    # ------------------------------------------------------------------ #
    #  Collection logic                                                   #
    # ------------------------------------------------------------------ #

    def _collect(self) -> Dict[str, int]:
        cutoff = datetime.now() - timedelta(hours=self._max_age_hours())
        stats: Dict[str, int] = {"deleted": 0, "freed_bytes": 0, "errors": 0}

        self._clean_log_files(cutoff, stats)
        self._clean_temp_files(cutoff, stats)
        self._clean_cache_dir(cutoff, stats)

        return stats

    # -- Log files -------------------------------------------------------

    def _clean_log_files(self, cutoff: datetime, stats: Dict[str, int]):
        try:
            from vaf.core.log_helper import get_app_log_dir
            log_dir = get_app_log_dir()
        except Exception:
            return
        if not log_dir.exists():
            return
        for filename in KNOWN_LOG_FILES:
            self._delete_if_old(log_dir / filename, cutoff, stats)

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
