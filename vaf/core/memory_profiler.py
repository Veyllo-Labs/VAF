"""
VAF Memory Profiler - Tracks memory usage and identifies leaks.
Runs as a background thread and logs memory snapshots.
"""
import threading
import time
import gc
import sys
import os
import tracemalloc
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from vaf.core.log_helper import append_domain_log, get_app_log_dir

# Profiling interval in seconds
PROFILE_INTERVAL = 30
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MemoryProfiler:
    """Background memory profiler that tracks and logs memory usage."""

    _instance: Optional["MemoryProfiler"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._snapshots: List[Dict] = []
        self._start_memory: float = 0.0
        get_app_log_dir().mkdir(parents=True, exist_ok=True)

    @classmethod
    def get_instance(cls) -> "MemoryProfiler":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def start(self):
        """Start the background profiler thread."""
        if self._running:
            return

        # Skip profiler entirely if debug logs disabled (performance boost)
        from vaf.core.log_helper import is_debug_logging_enabled
        if not is_debug_logging_enabled():
            return

        self._running = True
        # DISABLED: tracemalloc causes massive memory leaks (100MB+ per minute)
        # It tracks every allocation and the tracking data itself leaks
        # if not tracemalloc.is_tracing():
        #     tracemalloc.start(3)
        self._start_memory = self._get_memory_mb()
        self._thread = threading.Thread(target=self._profile_loop, daemon=True, name="MemoryProfiler")
        self._thread.start()
        self._log(f"Memory Profiler started (tracemalloc DISABLED). Initial memory: {self._start_memory:.0f}MB")

    def stop(self):
        """Stop the profiler."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        self._log("Memory Profiler stopped.")

    def _profile_loop(self):
        """Main profiling loop."""
        while self._running:
            try:
                self._take_snapshot()
            except Exception as e:
                self._log(f"Profiler error: {e}")
            time.sleep(PROFILE_INTERVAL)

    def _format_allocation_site(self, stat) -> str:
        """Format a tracemalloc Statistic as 'path:line' (project-relative when possible)."""
        if not stat.traceback:
            return "?"
        frame = stat.traceback[0]
        try:
            path = Path(frame.filename).relative_to(PROJECT_ROOT)
        except ValueError:
            path = Path(frame.filename)
        # Note: tracemalloc Frame objects only have filename and lineno, not name
        return f"{path}:{frame.lineno}"

    def _take_snapshot(self):
        """Take a memory snapshot and log it."""
        memory_mb = self._get_memory_mb()
        delta = memory_mb - self._start_memory

        # Log warning if memory is growing fast
        warning = ""
        if memory_mb > 8000:
            warning = " ⚠️ CRITICAL"
        elif memory_mb > 4000:
            warning = " ⚠️ HIGH"
        elif delta > 500:
            warning = " ⚠️ GROWING"

        if tracemalloc.is_tracing():
            t_snapshot = tracemalloc.take_snapshot()
            top_stats = t_snapshot.statistics("lineno")
            top_allocations: List[str] = []
            for stat in top_stats[:10]:
                size_mib = stat.size / 1024 / 1024
                site = self._format_allocation_site(stat)
                top_allocations.append(f"{size_mib:.1f} MiB - {site}")

            snapshot = {
                "timestamp": datetime.now().isoformat(),
                "memory_mb": memory_mb,
                "delta_mb": delta,
                "objects": {},
                "top_allocations": top_allocations,
            }
            self._snapshots.append(snapshot)

            if len(self._snapshots) > 100:
                self._snapshots = self._snapshots[-100:]

            self._log(f"Memory: {memory_mb:.0f}MB (Δ{delta:+.0f}MB){warning}")
            self._log("[Top 10 Memory Allocations]")
            for line in top_allocations:
                self._log(f"  {line}")
        else:
            gc.collect()
            obj_counts = self._get_object_counts()
            snapshot = {
                "timestamp": datetime.now().isoformat(),
                "memory_mb": memory_mb,
                "delta_mb": delta,
                "objects": obj_counts,
            }
            self._snapshots.append(snapshot)

            if len(self._snapshots) > 100:
                self._snapshots = self._snapshots[-100:]

            top_objects = sorted(obj_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            obj_str = ", ".join([f"{k}:{v}" for k, v in top_objects])
            self._log(f"Memory: {memory_mb:.0f}MB (Δ{delta:+.0f}MB){warning} | Top: {obj_str}")

        # Auto-cleanup if memory is high
        if memory_mb > 4000:
            self._emergency_cleanup()

    def _get_memory_mb(self) -> float:
        """Get current process memory in MB."""
        try:
            import psutil
            return psutil.Process().memory_info().rss / (1024 * 1024)
        except ImportError:
            return 0.0

    def _get_object_counts(self) -> Dict[str, int]:
        """Get counts of objects by type (leak suspects)."""
        counts: Dict[str, int] = {}

        # Count specific types that are common leak suspects
        suspects = [
            "dict", "list", "tuple", "str", "bytes",
            "function", "method", "frame", "cell",
            "Message", "Session", "Chunk", "Memory",
        ]

        for obj in gc.get_objects():
            type_name = type(obj).__name__
            if type_name in suspects:
                counts[type_name] = counts.get(type_name, 0) + 1

        return counts

    def _emergency_cleanup(self):
        """Emergency memory cleanup when usage is high."""
        self._log("🚨 Emergency cleanup triggered!")

        before = self._get_memory_mb()

        # Clear embedding cache (but KEEP the ONNX model loaded!)
        # Unloading ONNX model causes it to be reloaded, wasting time and fragmenting memory
        try:
            from vaf.memory.embeddings import get_embedding_service
            svc = get_embedding_service()
            if svc:
                svc.clear_cache()  # Only clear the text→embedding cache, not the model
            self._log("  - Cleared embedding cache (kept model)")
        except Exception as e:
            self._log(f"  - Embedding cleanup failed: {e}")

        # Unload Whisper STT model (can be 1–20GB+; native allocs not freed by gc alone)
        try:
            from vaf.core.web_server import unload_whisper_model, _whisper_model
            if _whisper_model is not None:
                unload_whisper_model()
                self._log("  - Unloaded Whisper model")
            else:
                self._log("  - Whisper model not loaded (skipped)")
        except Exception as e:
            self._log(f"  - Whisper unload failed: {e}")

        # Release TTS resources (Piper subprocess, pyttsx3 engine; can reduce memory under load)
        try:
            from vaf.core.speech import get_speech_manager
            get_speech_manager().release_tts_resources()
            self._log("  - Released TTS resources")
        except Exception as e:
            self._log(f"  - TTS release failed: {e}")

        # Force multiple GC passes
        for i in range(3):
            collected = gc.collect()
            self._log(f"  - GC pass {i+1}: collected {collected} objects")

        # REMOVED: torch import causes 1GB+ RAM explosion!
        # CUDA cache clearing not needed when CUDA_VISIBLE_DEVICES="" is set

        after = self._get_memory_mb()
        self._log(f"  - Cleanup result: {before:.0f}MB → {after:.0f}MB (freed {before-after:.0f}MB)")

    def _log(self, message: str):
        """Write one line to memory.log with [PROFILER] prefix."""
        append_domain_log("memory", f"[PROFILER] {message}")

    def get_report(self) -> str:
        """Generate a summary report."""
        if not self._snapshots:
            return "No snapshots available."

        current = self._snapshots[-1]
        lines = [
            "=== Memory Profiler Report ===",
            f"Current: {current['memory_mb']:.0f}MB",
            f"Growth since start: {current['delta_mb']:+.0f}MB",
            f"Snapshots: {len(self._snapshots)}",
            "",
        ]

        if current.get("top_allocations"):
            lines.append("Top allocations:")
            for line in current["top_allocations"]:
                lines.append(f"  {line}")
        else:
            lines.append("Top object types:")
            for name, count in sorted(current["objects"].items(), key=lambda x: x[1], reverse=True)[:10]:
                lines.append(f"  {name}: {count:,}")

        return "\n".join(lines)


def start_profiler():
    """Start the global memory profiler."""
    MemoryProfiler.get_instance().start()


def stop_profiler():
    """Stop the global memory profiler."""
    MemoryProfiler.get_instance().stop()


def get_memory_report() -> str:
    """Get current memory report."""
    return MemoryProfiler.get_instance().get_report()


# Track object growth between calls
_last_counts: Dict[str, int] = {}


def log_object_growth():
    """Log which object types are growing (call periodically)."""
    global _last_counts
    gc.collect()

    current_counts: Dict[str, int] = {}
    for obj in gc.get_objects():
        type_name = type(obj).__name__
        current_counts[type_name] = current_counts.get(type_name, 0) + 1

    if _last_counts:
        growth = []
        for name, count in current_counts.items():
            prev = _last_counts.get(name, 0)
            delta = count - prev
            if delta > 100:  # Only report significant growth
                growth.append((name, delta, count))

        if growth:
            growth.sort(key=lambda x: x[1], reverse=True)
            append_domain_log("memory", "[PROFILER] Object growth:")
            for name, delta, total in growth[:10]:
                append_domain_log("memory", f"[PROFILER]   {name}: +{delta} (total: {total})")

    _last_counts = current_counts
