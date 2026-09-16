# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The running VAF instance: which process it is, and how it was started.

One VAF process serves the desktop window, the tray icon, the web UI and the
agent loop. Tooling outside that process - `vaf stop`, `vaf status`,
`vaf restart`, `vaf top` and the self-updater, whether typed in a terminal or
started from the web UI - needs two facts about it: which process to stop,
and how to start it again so it comes back the way it was.

WHICH process is answered by locate_processes(): the owner of the tray's
singleton port (an identity no command line can fake), or failing that every
`vaf.main tray` process matched on exact argv elements. This is the one
implementation of that rule; the CLI's finder reads through it.

HOW it was started is answered by the record the instance writes for itself
in `<vaf dir>/instance.json` once its singleton check passes: pid, mode,
interpreter and working directory. A launcher-written pid file cannot carry
that, and it only exists for the launches that write one (`vaf start`, the
tray dashboard); the app shortcut, run_vaf.sh and a bare `vaf tray --no-top`
write none, so the restart after an update used to have nothing to go on and
could only bring VAF back headless.

Modes:
  tray      `vaf tray`, run_vaf.sh, the app shortcut: desktop window + tray icon
  headless  `vaf start`, the systemd unit, the native wrapper: no window, no icon

A record can outlive its process (a crash, `kill -9`, a reboot that reuses the
pid), so readers trust it only while the pid is alive AND runs a tray entry
point; anything else is removed as stale. An instance started by a version
that kept no record is still found through locate_processes(), with its mode
read from the process environment where the platform allows it.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

RECORD_NAME = "instance.json"

MODE_TRAY = "tray"
MODE_HEADLESS = "headless"
MODES = (MODE_TRAY, MODE_HEADLESS)

# The env var that turns `vaf.main tray` into the headless entry point
# (vaf/main.py); the same one the systemd unit and `vaf start` set.
HEADLESS_ENV = "VAF_NATIVE_WRAPPER"

# The tray's singleton listener (vaf/tray.py check_singleton). Owning this port
# is what makes a process THE service, whatever its command line looks like.
TRAY_SINGLETON_PORT = 8002


@dataclass(frozen=True)
class Instance:
    pid: int
    mode: str
    python: str = ""
    cwd: str = ""
    started_at: str = ""
    # False when the instance was found in the process table rather than read
    # from its own record (started by a version that kept none).
    recorded: bool = True


def record_path() -> Path:
    from vaf.core.platform import get_vaf_dir
    return get_vaf_dir() / RECORD_NAME


# ── the instance's own side ──────────────────────────────────────────────────

def register(mode: str) -> Optional[Path]:
    """Record this process as the running instance. Returns the record's path,
    or None when it could not be written (a read-only home must not stop VAF
    from starting). A wrong mode is a programming error and does raise."""
    if mode not in MODES:
        raise ValueError(f"unknown instance mode {mode!r}; expected one of {MODES}")
    data = {
        "pid": os.getpid(),
        "mode": mode,
        "python": sys.executable or "",
        "cwd": _safe_cwd(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path = record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)   # atomic: a reader never sees a half-written record
        return path
    except Exception:
        return None


def unregister() -> None:
    """Remove the record if it is this process's own. Never raises, and never
    removes another instance's record (a refused second instance must not
    erase the first one's)."""
    forget(os.getpid())


def forget(pid: int) -> None:
    """Remove the record when it names `pid`. The instance calls this for
    itself on a clean exit; the tooling that killed an instance calls it for
    the pid it killed, since a killed process removes nothing. Never raises."""
    try:
        path = record_path()
        raw = _load_raw(path)
        if raw is not None and int(raw.get("pid", -1)) == int(pid):
            path.unlink(missing_ok=True)
    except Exception:
        pass


# ── the tooling side ─────────────────────────────────────────────────────────

def read_record() -> Optional[Instance]:
    """The recorded instance while its process is alive and runs a tray entry
    point. A record whose process is gone, or whose pid now belongs to
    something else, is removed and None is returned. Never raises."""
    try:
        path = record_path()
        if not path.exists():
            return None
        raw = _load_raw(path)
        if raw is None:                 # unreadable or not a record: stale
            path.unlink(missing_ok=True)
            return None
        try:
            pid = int(raw.get("pid"))
            mode = str(raw.get("mode") or "")
        except Exception:
            pid, mode = -1, ""
        if pid <= 0 or mode not in MODES or not _is_live_vaf_pid(pid):
            path.unlink(missing_ok=True)
            return None
        return Instance(
            pid=pid, mode=mode,
            python=str(raw.get("python") or ""),
            cwd=str(raw.get("cwd") or ""),
            started_at=str(raw.get("started_at") or ""),
            recorded=True,
        )
    except Exception:
        return None


def find_running() -> Optional[Instance]:
    """The running VAF instance, or None when nothing runs.

    The record answers first. Without one (an instance started by a version
    that kept none, or a record lost with its directory) the process table is
    asked through locate_processes(), and the mode is read from that process's
    environment where the platform allows it; an unreadable environment counts
    as headless, because a windowed VAF started headless still serves, while a
    headless one started windowed may have no display to open a window on."""
    inst = read_record()
    if inst is not None:
        return inst
    found = scan_processes()
    return found[0] if found else None


def locate_processes() -> list:
    """psutil.Process objects of the running service, or []. Never raises.

    The tray holds a singleton listener, so the process owning that port IS
    the service - an identity no command line can fake. Preferred over
    scanning argv, which cannot tell the service from a dashboard wrapper
    watching it (both run "-m vaf.main tray"). The scan that follows matches
    on exact argv ELEMENTS, not the joined string: a shell whose -c payload
    merely QUOTES "vaf.main tray" (a supervisor line, a grep, a script
    wrapper) must never count as VAF - stop would kill it and the dashboard
    would "attach" to it. Deliberately tray-only: `vaf run` is somebody's
    interactive session, not the background service, and stop must not end it.
    """
    try:
        import psutil
    except Exception:
        return []

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
                parts = list(proc.info["cmdline"] or [])
                if "vaf.main" in parts and "tray" in parts:
                    found.append(proc)
            except Exception:
                continue
    except Exception:
        pass
    return found


def scan_processes() -> List[Instance]:
    """locate_processes() as unrecorded Instances, with the mode read from each
    process's environment. Never raises."""
    found: List[Instance] = []
    for proc in locate_processes():
        try:
            # No interpreter: argv[0] of a running process is not what to start
            # it with. A macOS venv python re-executes the framework binary, so
            # argv[0] names an interpreter that cannot see the venv. A relaunch
            # of a scanned instance uses the caller's interpreter instead; only
            # a record carries sys.executable.
            found.append(Instance(
                pid=int(proc.pid),
                mode=_mode_of(proc),
                python="",
                cwd=_safe_call(proc.cwd, ""),
                started_at="",
                recorded=False,
            ))
        except Exception:
            continue
    return found


def is_tray_entry(argv) -> bool:
    """True for a command line that runs the tray entry point: `... -m vaf.main
    tray ...` or the console script `.../vaf tray ...`, matched on exact argv
    elements. Used to validate a record's pid; the process-table scan applies
    the stricter module form only."""
    try:
        parts = [str(a) for a in (argv or [])]
    except Exception:
        return False
    if "tray" not in parts:
        return False
    if "vaf.main" in parts:
        return True
    for tok in parts[:-1]:
        # Both separators: a Windows command line keeps its backslashes.
        base = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base in ("vaf", "vaf.exe"):
            return True
    return False


# ── internals ────────────────────────────────────────────────────────────────

def _mode_of(proc) -> str:
    try:
        env = proc.environ()
    except Exception:
        return MODE_HEADLESS
    return MODE_HEADLESS if env.get(HEADLESS_ENV) == "1" else MODE_TRAY


def _is_live_vaf_pid(pid: int) -> bool:
    """True when `pid` is alive, not a zombie, and runs a tray entry point."""
    try:
        import psutil
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return is_tray_entry(proc.cmdline())
    except Exception:
        return False


def _load_raw(path: Path) -> Optional[dict]:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _safe_cwd() -> str:
    try:
        return os.getcwd()
    except Exception:
        return ""


def _safe_call(fn, default):
    try:
        return fn()
    except Exception:
        return default
