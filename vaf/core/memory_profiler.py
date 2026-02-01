"""
VAF Memory Profiler - Tracks memory usage and identifies leaks.
Runs as a background thread and logs memory snapshots.
"""
import threading
import time
import gc
import sys
import os
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Profiling interval in seconds
PROFILE_INTERVAL = 30
LOG_DIR = Path(__file__).resolve().parents[2] / "logs"


class MemoryProfiler:
    """Background memory profiler that tracks and logs memory usage."""

    _instance: Optional["MemoryProfiler"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._snapshots: List[Dict] = []
        self._start_memory: float = 0.0
        LOG_DIR.mkdir(parents=True, exist_ok=True)

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

        self._running = True
        self._start_memory = self._get_memory_mb()
        self._thread = threading.Thread(target=self._profile_loop, daemon=True, name="MemoryProfiler")
        self._thread.start()
        self._log(f"Memory Profiler started. Initial memory: {self._start_memory:.0f}MB")

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

    def _take_snapshot(self):
        """Take a memory snapshot and log it."""
        memory_mb = self._get_memory_mb()
        delta = memory_mb - self._start_memory

        # Get object counts for common leak suspects
        gc.collect()  # Collect garbage first for accurate counts
        obj_counts = self._get_object_counts()

        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "memory_mb": memory_mb,
            "delta_mb": delta,
            "objects": obj_counts,
        }
        self._snapshots.append(snapshot)

        # Keep only last 100 snapshots
        if len(self._snapshots) > 100:
            self._snapshots = self._snapshots[-100:]

        # Log warning if memory is growing fast
        warning = ""
        if memory_mb > 8000:
            warning = " ⚠️ CRITICAL"
        elif memory_mb > 4000:
            warning = " ⚠️ HIGH"
        elif delta > 500:
            warning = " ⚠️ GROWING"

        # Format top object types
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

        # Clear embedding cache
        try:
            from vaf.memory.embeddings import cleanup_embedding_memory
            cleanup_embedding_memory()
            self._log("  - Cleared embedding cache")
        except Exception as e:
            self._log(f"  - Embedding cleanup failed: {e}")

        # Force multiple GC passes
        for i in range(3):
            collected = gc.collect()
            self._log(f"  - GC pass {i+1}: collected {collected} objects")

        # Clear CUDA if available
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                self._log("  - Cleared CUDA cache")
        except ImportError:
            pass

        after = self._get_memory_mb()
        self._log(f"  - Cleanup result: {before:.0f}MB → {after:.0f}MB (freed {before-after:.0f}MB)")

    def _log(self, message: str):
        """Write to log file."""
        try:
            log_file = LOG_DIR / "memory_profiler.log"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat()} {message}\n")
        except Exception:
            pass

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
            "Top object types:",
        ]

        for name, count in sorted(current['objects'].items(), key=lambda x: x[1], reverse=True)[:10]:
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
            log_file = LOG_DIR / "memory_profiler.log"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat()} Object growth:\n")
                for name, delta, total in growth[:10]:
                    f.write(f"  {name}: +{delta} (total: {total})\n")

    _last_counts = current_counts
