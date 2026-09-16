# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
vaf start / stop / restart / status

Desktop mode  -> manages the VAF process through two records: the service pid
                 file a launcher writes (`vaf start`, the tray dashboard, the
                 updater's relaunch) and the record the running VAF keeps about
                 itself (vaf/core/instance.py), which also says HOW it was
                 started, so a restart brings back the same kind of VAF
Server mode   -> delegates to systemctl --user (systemd service)

The launch primitive here, start_instance(), is also what the self-updater
uses to bring VAF back after a checkout swap: in the mode the stopped instance
ran in, so a windowed desktop app returns with its window and tray icon and a
headless service stays headless.
"""

import contextlib
import os
import platform
import signal
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import typer

from vaf.cli.ui import UI
from vaf.core import instance
from vaf.core.instance import Instance

app = typer.Typer(hidden=True)  # commands registered directly on main app, not as subgroup

# The tray's singleton listener (vaf/tray.py check_singleton). Owning this port
# is what makes a process THE service, whatever its command line looks like.
# The rule lives in vaf/core/instance.py; the name stays here for its readers.
TRAY_SINGLETON_PORT = instance.TRAY_SINGLETON_PORT

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
    """Return PID if VAF is running, else None (cleans up stale PID file).

    Two records answer. The service pid file is written by whoever LAUNCHED
    VAF detached (`vaf start`, the tray dashboard, the updater's relaunch).
    The launches that write none (the app shortcut, run_vaf.sh, a bare
    `vaf tray --no-top`) are covered by the record the running VAF writes for
    itself (vaf/core/instance.py), so status, the dashboard and the updater
    see a desktop-launched VAF too.
    """
    pf = _pid_file()
    if pf.exists():
        try:
            pid = int(pf.read_text().strip())
        except ValueError:
            pid = None
        if pid and _alive(pid):
            return pid
        # Gone, a zombie (exited, table entry not yet reaped: treating that as
        # "running" made stop signal into the void and report success while the
        # real VAF kept going), or unreadable: a stale record either way.
        pf.unlink(missing_ok=True)
    recorded = instance.read_record()
    return recorded.pid if recorded is not None else None


def _alive(pid: int) -> bool:
    """True when the pid exists and is not a zombie. Never raises.

    Not os.kill(pid, 0): on Windows that is not a probe but TerminateProcess
    with exit code 0, so a status check would have ended the very service it
    was asked about.
    """
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def _find_vaf_processes() -> list:
    """Running VAF processes, found by the singleton port they hold or by
    command line rather than by pid file.

    The pid file is only written by the launchers that start VAF detached.
    Every other way of starting - the app shortcut, run_vaf.sh, a bare tray -
    leaves none, so a pid-file lookup alone answers "not running" while VAF is
    plainly running. The rule lives in vaf/core/instance.py; this is its CLI
    name. Never raises.
    """
    return instance.locate_processes()

def _is_server_mode() -> bool:
    try:
        from vaf.core.config import Config
        return bool(Config.get("server_mode", False))
    except Exception:
        return False

def _systemctl(action: str):
    result = subprocess.run(["systemctl", "--user", action, "vaf"])
    raise typer.Exit(result.returncode)


def _psutil():
    import psutil
    return psutil


def _describe(inst: Instance) -> str:
    return f"PID {inst.pid}, {inst.mode}"


@contextlib.contextmanager
def _surviving_the_shutdown():
    """Ignore SIGTERM while VAF shuts down, and restore the disposition after.

    The tray's quit broadcasts `pkill -TERM -f "python.*vaf.main"` to sweep
    up its children, and that pattern matches the process doing the stopping
    as well: `vaf stop`, `vaf restart` and the self-updater are all
    `python -m vaf.main <verb>`. Measured with pgrep against a process started
    the way the updater is: it is on the list. Without this, the stop step
    would kill the updater in the middle of its own update, with VAF down and
    nothing left to start it again. POSIX only; there is no pkill on Windows,
    and the broadcast is skipped there.
    """
    if platform.system() == "Windows":
        yield
        return
    try:
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except (ValueError, OSError):        # not the main thread: nothing to shield
        yield
        return
    try:
        yield
    finally:
        try:
            signal.signal(signal.SIGTERM, previous)
        except (ValueError, OSError):
            pass


# ── the launch primitive ─────────────────────────────────────────────────────

def start_instance(mode: str = instance.MODE_HEADLESS, python: Optional[str] = None,
                   cwd: Optional[str] = None) -> int:
    """Start a detached VAF in `mode`, record its pid in the service pid file,
    and return the pid.

    headless is what `vaf start` documents: no window, no tray icon, the web UI
    in a browser. tray is the desktop app the way run_vaf.sh, the app shortcut
    and `vaf tray` start it: window plus tray icon. Both run the same entry
    point (`vaf.main tray --no-top`); the environment variable decides, as it
    does for the systemd unit and the native wrapper.

    The interpreter and working directory default to this process's. A relaunch
    passes the ones the previous instance recorded, so a pythonw.exe launch
    stays console-less and a checkout-relative start keeps its directory.

    Detached on every platform: a new session on POSIX, a detached process
    group on Windows (where a child left in the console's group dies with the
    console, which is also how the updater spawns itself). The instance records
    itself once up (vaf/core/instance.py); this only knows the pid it was given.
    """
    if mode not in instance.MODES:
        raise ValueError(f"cannot start VAF in mode {mode!r}")
    log = _log_file()
    log.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if mode == instance.MODE_HEADLESS:
        env[instance.HEADLESS_ENV] = "1"
    else:
        env.pop(instance.HEADLESS_ENV, None)

    argv = [python or sys.executable, "-m", "vaf.main", "tray", "--no-top"]
    kwargs = {"stdin": subprocess.DEVNULL, "env": env}
    if cwd and os.path.isdir(cwd):
        kwargs["cwd"] = cwd
    if platform.system() == "Windows":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if flags:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True

    with open(log, "a") as lf:
        proc = subprocess.Popen(argv, stdout=lf, stderr=subprocess.STDOUT, **kwargs)
    try:
        _pid_file().write_text(str(proc.pid))
    except Exception:
        pass
    return proc.pid


def relaunch(previous: Optional[Instance]) -> Optional[int]:
    """Start VAF again the way `previous` ran: its mode, interpreter and working
    directory. Returns the new pid.

    No previous instance means the documented default of `vaf start`, headless.
    A recorded interpreter that no longer exists, or none (an instance found
    in the process table), means this one. In server mode the lifecycle is
    systemd's, as for `vaf start`.
    """
    if _is_server_mode():
        _systemctl("start")             # raises typer.Exit with systemctl's code
        return None
    mode = instance.MODE_HEADLESS
    python = cwd = None
    if previous is not None:
        if previous.mode in instance.MODES:
            mode = previous.mode
        python = previous.python or None
        cwd = previous.cwd or None
    if python and not os.path.exists(python):
        python = None
    return start_instance(mode, python=python, cwd=cwd)


def stop_instance() -> Optional[Instance]:
    """Stop the running VAF and return what was stopped, or None when nothing
    ran. Terminates, waits up to 10 s, then kills.

    Targets, in order: the recorded instance; without a record, every process
    the finder returns (the singleton-port owner, or every `vaf.main tray` by
    argv); without either, the pid in the service pid file. A clean exit
    removes its records itself; a killed process cannot, so both records are
    dropped here for the pids that were stopped.
    """
    psutil = _psutil()
    running = instance.find_running()
    if running is not None and running.recorded:
        pids = [running.pid]
    else:
        pids = [int(p.pid) for p in _find_vaf_processes()]
        if not pids:
            pid = _running_pid()
            pids = [pid] if pid else []
        if running is None and pids:
            running = Instance(pid=pids[0], mode=instance.MODE_HEADLESS, recorded=False)
    if not pids:
        return None

    me = os.getpid()
    procs = []
    for pid in pids:
        if pid == me:
            continue
        try:
            procs.append(psutil.Process(pid))
        except psutil.Error:
            continue
    if not procs:
        _drop_records(pids)
        return None

    UI.info(f"Stopping VAF ({_describe(running)})...")
    with _surviving_the_shutdown():
        for proc in procs:
            try:
                proc.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=10)
        for proc in alive:
            try:
                proc.kill()
            except psutil.Error:
                pass
    _drop_records(pids)
    return running


def _drop_records(pids: List[int]) -> None:
    """Forget the records that name a pid that was just stopped: the
    instance's own, and the launcher's pid file only if it names one of them
    (another terminal may have started a newer service meanwhile, and deleting
    that record would make status lie and let the next start double-launch)."""
    for pid in pids:
        instance.forget(pid)
    try:
        pf = _pid_file()
        if pf.exists() and pf.read_text().strip() in {str(p) for p in pids}:
            pf.unlink(missing_ok=True)
    except Exception:
        pass


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

    pid = start_instance(instance.MODE_HEADLESS)
    UI.success(f"VAF started (PID {pid})")
    UI.info(f"Log:  {_log_file()}")
    UI.info("Open: http://localhost:3000")
    if watch:
        _open_dashboard()
    else:
        UI.info("Watch it live: vaf top")


def cmd_stop():
    """Stop the running VAF background service."""
    if _is_server_mode():
        with _surviving_the_shutdown():   # the unit's ExecStop runs the same quit
            _systemctl("stop")
        return

    if stop_instance() is None:
        UI.warning("VAF is not running")
        return
    UI.success("VAF stopped")


def cmd_restart():
    """Restart the VAF background service."""
    if _is_server_mode():
        _systemctl("restart")
        return

    previous = stop_instance()
    if previous is None:
        UI.warning("VAF was not running; starting it")
    pid = relaunch(previous)
    mode = previous.mode if previous is not None else instance.MODE_HEADLESS
    UI.success(f"VAF started (PID {pid}, {mode})")
    UI.info("Watch it live: vaf top")


def cmd_status():
    """Show VAF service status."""
    if _is_server_mode():
        _systemctl("status")
        return

    running = instance.find_running()
    if running is None:
        pid = _running_pid()
        if pid:
            running = Instance(pid=pid, mode=instance.MODE_HEADLESS, recorded=False)
    if running is not None:
        UI.success(f"VAF is running ({_describe(running)})")
        UI.info("Web UI: http://localhost:3000")
    else:
        UI.warning("VAF is not running")
        UI.info("Start with: vaf start")
