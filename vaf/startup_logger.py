import os
import datetime
import sys


def _get_startup_log_path():
    """Path to startup_trace_YYYY-MM-DD.txt (dated for GC)."""
    from vaf.core.log_helper import get_dated_log_path
    return get_dated_log_path("startup_trace", "txt")


def log(component, message):
    """Writes a log entry to the central startup trace file. No-op when Debug Logs is off."""
    try:
        from vaf.core.log_helper import is_debug_logging_enabled
        if not is_debug_logging_enabled():
            return
        log_file = _get_startup_log_path()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        entry = f"[{timestamp}] [{component}] {message}\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(entry)
        print(f"DEBUG: {entry.strip()}")
    except Exception as e:
        print(f"!!! FAILED TO WRITE LOG: {e}")


def clear_log():
    """Clears the log file at startup. No-op when Debug Logs is off."""
    try:
        from vaf.core.log_helper import is_debug_logging_enabled
        if not is_debug_logging_enabled():
            return
        log_file = _get_startup_log_path()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"--- STARTUP TRACE STARTED AT {datetime.datetime.now()} ---\n")
            f.write(f"Python: {sys.executable}\n")
            f.write(f"Platform: {sys.platform}\n")
    except Exception:
        pass
