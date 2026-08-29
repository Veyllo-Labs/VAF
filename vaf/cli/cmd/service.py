# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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

# The tray's singleton listener (vaf/tray.py check_singleton). Owning this port
# is what makes a process THE service, whatever its command line looks like.
TRAY_SINGLETON_PORT = 8002

# ── helpers ──────────────────────────────────────────────────────────────────

def _pid_file() -> Path:
    # Deliberate: NOT server.pid. That name belongs to the llama backend
    # (backend.py pid_file), whose orphan cleanup KILLS any pid found there
    # when llama's 8080 health does not answer - a tray pid written into it
    # made the freshly spawned tray clean ITSELF up as an "orphaned server"
    # (live incident, twice: the vaf tray dashboard child died after one line).
    return Path.home() / ".vaf" / "service.pid"

def _log_file() -> Path:
    return Path.home() / ".vaf" / "logs" / "vaf_run.log"

def _running_pid() -> int | None:
    """Return PID if VAF is running, else None (cleans up stale PID file)."""
    pf = _pid_file()
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)  # raises if the pid is gone entirely
        if _is_zombie(pid):
            # A zombie has exited; only its table entry survives until the parent
            # reaps it, and kill(pid, 0) succeeds for it. Treating that as "running"
            # made stop send signals into the void and then report success, while
            # the real VAF kept going - and with it a frontend serving a stale build.
            pf.unlink(missing_ok=True)
            return None
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        pf.unlink(missing_ok=True)
        return None


def _is_zombie(pid: int) -> bool:
    """True when the pid exists only as an unreaped exit status. Never raises."""
    try:
        import psutil
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except Exception:
        return False


def _find_vaf_processes() -> list:
    """Running VAF processes, found by command line rather than by pid file.

    The pid file is only written by `vaf server start`. Every other way of
    starting - the tray, run_vaf.sh, the app bundle - leaves none, so a pid-file
    lookup alone answers "not running" while VAF is plainly running. Never raises.
    """
    try:
        import psutil
    except Exception:
        return []

    # The tray holds a singleton listener (tray.py check_singleton), so the
    # process owning that port IS the service - an identity no command line can
    # fake. Preferred over scanning argv, which cannot tell the service from a
    # dashboard wrapper watching it (both run "-m vaf.main tray").
    me = os.getpid()
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if (conn.status == psutil.CONN_LISTEN and conn.laddr
                    and conn.laddr.port == TRAY_SINGLETON_PORT and conn.pid
                    and conn.pid != me):
                return [psutil.Process(conn.pid)]
    except Exception:
        pass

    found = []
    try:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                if proc.info["pid"] == me:
                    continue
                # Match on exact argv ELEMENTS, not the joined string: a shell
                # whose -c payload merely QUOTES "vaf.main tray" (a supervisor
                # line, a grep, a script wrapper) must never count as VAF -
                # stop would kill it and the dashboard would "attach" to it.
                # Deliberately tray-only: `vaf run` is somebody's interactive
                # session, not the background service, and stop must not end it.
                parts = list(proc.info["cmdline"] or [])
                if "vaf.main" in parts and "tray" in parts:
                    found.append(proc)
            except Exception:
                continue
    except Exception:
        pass
    return found

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

def _open_dashboard():
    """Hand the terminal over to the live dashboard (vaf top)."""
    from vaf.cli.cmd.top import cmd_top
    # Called directly, so pass real values - typer's Option defaults only
    # materialize when the function is invoked as a CLI command.
    cmd_top(interval=2.0, once=False, logs=True)


def cmd_start(
    watch: bool = typer.Option(None, "--watch/--no-watch",
                               help="Open the live dashboard (vaf top) after starting "
                                    "(default: on in an interactive terminal)"),
):
    """Start VAF as a background service."""
    if watch is None:
        # Unset flag: a person at a terminal gets the dashboard, scripts and
        # pipes stay headless.
        watch = os.isatty(1)
    elif not isinstance(watch, bool):
        # Direct callers (cmd_restart, the updater) bypass typer, so the
        # parameter arrives as typer's truthy OptionInfo default - which would
        # silently turn every restart into a dashboard takeover.
        watch = False
    if _is_server_mode():
        if watch:
            result = subprocess.run(["systemctl", "--user", "start", "vaf"])
            if result.returncode != 0:
                raise typer.Exit(result.returncode)
            _open_dashboard()
            return
        _systemctl("start")
        return

    pid = _running_pid()
    if pid:
        UI.warning(f"VAF is already running (PID {pid})")
        if watch:
            _open_dashboard()
            return
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
    if watch:
        _open_dashboard()
    else:
        UI.info("Watch it live: vaf top")


def cmd_stop():
    """Stop the running VAF background service."""
    if _is_server_mode():
        _systemctl("stop")
        return

    pid = _running_pid()
    if not pid:
        # No pid file does NOT mean nothing is running: only `vaf server start`
        # writes one. Look for the processes themselves before giving up, so the
        # command tells the truth for a tray-started VAF too.
        procs = _find_vaf_processes()
        if not procs:
            UI.warning("VAF is not running")
            return
        UI.info(f"Stopping VAF ({len(procs)} process(es), no pid file)...")
        for proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        import psutil
        gone, alive = psutil.wait_procs(procs, timeout=10)
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                pass
        UI.success("VAF stopped")
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
