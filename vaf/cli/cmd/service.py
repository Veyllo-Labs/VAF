"""
vaf start / stop / restart / status

Desktop mode  → manages the VAF background process directly (PID file)
Server mode   → delegates to systemctl --user (systemd service)
"""

import os
import sys
import signal
import subprocess
from pathlib import Path

import typer

from vaf.cli.ui import UI

app = typer.Typer(hidden=True)  # commands registered directly on main app, not as subgroup

# ── helpers ──────────────────────────────────────────────────────────────────

def _pid_file() -> Path:
    return Path.home() / ".vaf" / "server.pid"

def _log_file() -> Path:
    return Path.home() / ".vaf" / "logs" / "vaf_run.log"

def _running_pid() -> int | None:
    """Return PID if VAF is running, else None (cleans up stale PID file)."""
    pf = _pid_file()
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)  # raises if dead
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        pf.unlink(missing_ok=True)
        return None

def _is_server_mode() -> bool:
    try:
        from vaf.core.config import Config
        return bool(Config.get("server_mode", False))
    except Exception:
        return False

def _systemctl(action: str):
    result = subprocess.run(["systemctl", "--user", action, "vaf"])
    raise typer.Exit(result.returncode)

# ── commands ──────────────────────────────────────────────────────────────────

def cmd_start():
    """Start VAF as a background service."""
    if _is_server_mode():
        _systemctl("start")
        return

    pid = _running_pid()
    if pid:
        UI.warning(f"VAF is already running (PID {pid})")
        raise typer.Exit(0)

    log = _log_file()
    log.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["VAF_NATIVE_WRAPPER"] = "1"

    with open(log, "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "vaf.main", "tray"],
            stdout=lf,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )

    _pid_file().write_text(str(proc.pid))
    UI.success(f"VAF started (PID {proc.pid})")
    UI.info(f"Log:  {log}")
    UI.info("Open: http://localhost:3000")


def cmd_stop():
    """Stop the running VAF background service."""
    if _is_server_mode():
        _systemctl("stop")
        return

    pid = _running_pid()
    if not pid:
        UI.warning("VAF is not running")
        return

    UI.info(f"Stopping VAF (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    # Wait up to 10 s for clean shutdown
    import time
    for _ in range(10):
        time.sleep(1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
    else:
        # Force-kill if still alive
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    _pid_file().unlink(missing_ok=True)
    UI.success("VAF stopped")


def cmd_restart():
    """Restart the VAF background service."""
    if _is_server_mode():
        _systemctl("restart")
        return

    cmd_stop()
    cmd_start()


def cmd_status():
    """Show VAF service status."""
    if _is_server_mode():
        _systemctl("status")
        return

    pid = _running_pid()
    if pid:
        UI.success(f"VAF is running (PID {pid})")
        UI.info("Web UI: http://localhost:3000")
    else:
        UI.warning("VAF is not running")
        UI.info("Start with: vaf start")
