# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""vaf top - live server dashboard in the terminal (nvtop-style).

Renders `vaf/core/system_stats.collect_snapshot()` plus the Docker service
health in a self-refreshing view: host/OS/VAF identity, uptimes, provider
configuration, CPU/RAM/disk/network/GPU utilization and the service process
tree. The Docker probe can take seconds (10s docker-info timeout when the
daemon is down), so it runs in a background thread on its own cadence and the
render loop only ever shows the latest finished reading.
"""

import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

from vaf.cli.ui import UI

app = typer.Typer(hidden=True)  # registered directly on the main app

_SERVICES_REFRESH_S = 10.0
_LOG_BACKFILL_LINES = 300
_LOG_BACKFILL_BYTES = 128 * 1024


def _fmt_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "n/a"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_rate(bps: Optional[int]) -> str:
    if bps is None:
        return "n/a"
    for unit, factor in (("GB/s", 1024 ** 3), ("MB/s", 1024 ** 2), ("KB/s", 1024)):
        if bps >= factor:
            return f"{bps / factor:.1f} {unit}"
    return f"{bps} B/s"


def _bar(percent: Optional[float], width: int = 22) -> str:
    if percent is None:
        return "n/a"
    pct = max(0.0, min(100.0, float(percent)))
    filled = int(round(pct / 100 * width))
    color = "green" if pct < 70 else ("yellow" if pct < 90 else "red")
    return f"[{color}]{'|' * filled}[/{color}]{' ' * (width - filled)} {pct:5.1f}%"


def _service_pid() -> Optional[int]:
    """The running VAF service, found through the existing service helpers."""
    try:
        from vaf.cli.cmd.service import _running_pid, _find_vaf_processes
        pid = _running_pid()
        if pid:
            return pid
        procs = _find_vaf_processes()
        return procs[0].pid if procs else None
    except Exception:
        return None


def _build_view(snap: Dict[str, Any], services: Optional[Dict[str, Any]]):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from vaf.cli.tui import VEYLLO_MARK_ART, VAF_LOGO_SUBTITLE

    host = snap.get("host") or {}
    vaf = snap.get("vaf") or {}
    lan = snap.get("lan") or {}
    svc = snap.get("service")

    info = Table.grid(padding=(0, 2))
    info.add_column(style="bold cyan", no_wrap=True)
    info.add_column()
    mode = {True: "server", False: "desktop", None: "n/a"}[vaf.get("server_mode")]
    provider = vaf.get("provider") or "n/a"
    provider_kind = "local" if provider == "local" else "API"
    model = vaf.get("model") or ""
    info.add_row("VAF", f"[bold]v{vaf.get('version', 'unknown')}[/bold]  mode [bold]{mode}[/bold]")
    info.add_row("Provider", f"[bold]{provider}[/bold] ({provider_kind})"
                             + (f"  model [bold]{model}[/bold]" if model else ""))
    info.add_row("OS", f"{host.get('os', '')}  kernel {host.get('kernel', '')}  {host.get('arch', '')}")
    info.add_row("Uptime", f"host {_fmt_duration(snap.get('uptime_s'))}"
                           + (f"   service {_fmt_duration(svc.get('uptime_s'))}"
                              if svc else "   service [red]not running[/red]"))
    if svc:
        info.add_row("Service", f"PID {svc.get('pid')}  {svc.get('processes')} procs  "
                                f"{svc.get('rss_mb')} MB RSS  {svc.get('cpu_percent')}% CPU")
    if lan.get("enabled"):
        urls = "   ".join(lan.get("urls") or []) or "n/a"
        info.add_row("LAN", urls)
    else:
        info.add_row("LAN", "[dim]off (localhost only)[/dim]")

    mark = "\n".join(VEYLLO_MARK_ART)
    head = Table.grid(padding=(0, 3))
    head.add_column(no_wrap=True)
    head.add_column()
    head.add_row(f"[bold cyan]{mark}[/bold cyan]\n[dim]{VAF_LOGO_SUBTITLE}[/dim]", info)

    cpu = snap.get("cpu") or {}
    mem = snap.get("mem") or {}
    disk = snap.get("disk") or {}
    net = snap.get("net") or {}
    util = Table.grid(padding=(0, 2))
    util.add_column(style="bold", no_wrap=True, min_width=6)
    util.add_column(no_wrap=True)
    util.add_column(style="dim")
    load = cpu.get("loadavg")
    util.add_row("CPU", _bar(cpu.get("percent")),
                 f"{cpu.get('cores') or '?'} cores" + (f"  load {load}" if load else ""))
    if mem:
        util.add_row("RAM", _bar(mem.get("percent")),
                     f"{mem.get('used_mb', 0) / 1024:.1f} / {mem.get('total_mb', 0) / 1024:.1f} GB"
                     + (f"  swap {mem.get('swap_used_mb', 0)} MB" if mem.get("swap_used_mb") else ""))
    if disk:
        util.add_row("Disk", _bar(disk.get("percent")),
                     f"{disk.get('used_gb')} / {disk.get('total_gb')} GB  ({disk.get('path')})")
    for gpu in snap.get("gpus") or []:
        mem_part = ""
        if gpu.get("mem_total_mb"):
            mem_part = f"{gpu.get('mem_used_mb') if gpu.get('mem_used_mb') is not None else '?'}" \
                       f" / {gpu.get('mem_total_mb')} MB VRAM"
        util.add_row("GPU", _bar(gpu.get("util_percent")),
                     f"{gpu.get('name', '')}  {mem_part}".strip())

    hostname = host.get("hostname") or "unknown-host"
    parts = [Panel(head, title=f"[bold]vaf top[/bold] @ [bold cyan]{hostname}[/bold cyan]",
                   border_style="cyan"),
             Panel(util, title="Utilization", border_style="cyan")]

    # Network: total rates plus who is connected (inbound connections per IP).
    net_table = Table.grid(padding=(0, 2))
    net_table.add_column(style="bold", no_wrap=True, min_width=6)
    net_table.add_column(no_wrap=True)
    net_table.add_column(style="dim")
    net_table.add_row("Total", f"down {_fmt_rate(net.get('recv_rate_bps'))}   "
                               f"up {_fmt_rate(net.get('sent_rate_bps'))}", "")
    clients = snap.get("clients")
    if clients is not None:
        rows = clients.get("clients") or []
        listen = ", ".join(str(p) for p in clients.get("listen_ports") or [])
        shown = 0
        for c in rows:
            if shown >= 8:
                net_table.add_row("", f"[dim]... and {len(rows) - shown} more[/dim]", "")
                break
            label = "[dim]localhost[/dim]" if c.get("local") else f"[bold]{c.get('ip')}[/bold]"
            ports = " ".join(f":{p}" for p in c.get("ports") or [])
            conn_count = c.get("connections") or 0
            net_table.add_row("Client" if shown == 0 else "", label,
                              f"{conn_count} conn{'s' if conn_count != 1 else ''}  {ports}")
            shown += 1
        if not rows:
            net_table.add_row("Client", "[dim]no active connections[/dim]",
                              f"listening on {listen}" if listen else "")
    else:
        net_table.add_row("Client", "[dim]n/a (service not running)[/dim]", "")
    parts.append(Panel(net_table, title="Network", border_style="cyan"))

    if services is not None:
        svc_table = Table.grid(padding=(0, 2))
        svc_table.add_column(style="bold", no_wrap=True)
        svc_table.add_column(no_wrap=True)
        svc_table.add_column(style="dim")
        docker = services.get("docker") or {}
        if not docker.get("available"):
            svc_table.add_row("docker", "[red]down[/red]", docker.get("reason") or "")
        else:
            for s in services.get("services") or []:
                state = s.get("state") or "unknown"
                color = {"ok": "green", "starting": "yellow"}.get(state, "red")
                svc_table.add_row(s.get("service_key") or s.get("name") or "?",
                                  f"[{color}]{state}[/{color}]",
                                  s.get("reason") or "")
        parts.append(Panel(svc_table, title="Docker services", border_style="cyan"))

    return Group(*parts)


def _resolve_log_source(candidates: Optional[List[Path]] = None,
                        not_older_than: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Where the running service writes its output.

    Measured lanes (each start path logs somewhere else - this is the map):
    server mode's systemd unit logs to the journal (SyslogIdentifier=vaf);
    `vaf start` appends to ~/.vaf/logs/vaf_run.log; vaf.sh writes
    <checkout>/logs/vaf_run.log; start_vaf.sh writes <checkout>/logs/
    tray_debug.log; a terminal- or desktop-started tray tees itself into
    ~/.vaf/logs/vaf_run.log (vaf/core/stdio_tee.py). Among existing files the
    most recently written one wins, because stale logs from an older start
    lane usually survive next to the live one. With `not_older_than` (the
    running service's start time) files last written before it are dropped
    entirely - an old file must not pose as the output of the CURRENT run.
    Returns {"kind": "journal"} | {"kind": "file", "path": Path} | None.
    """
    try:
        from vaf.core.config import Config
        if Config.get("server_mode", False) and shutil.which("journalctl"):
            probe = subprocess.run(["systemctl", "--user", "cat", "vaf"],
                                   capture_output=True, timeout=5)
            if probe.returncode == 0:
                return {"kind": "journal"}
    except Exception:
        pass
    if candidates is None:
        candidates = []
        try:
            from vaf.cli.cmd.service import _log_file
            candidates.append(_log_file())
        except Exception:
            pass
        try:
            import vaf
            root = Path(vaf.__file__).resolve().parent.parent
            candidates += [root / "logs" / "vaf_run.log", root / "logs" / "tray_debug.log"]
        except Exception:
            pass
    # A file qualifies only when it plausibly belongs to the CURRENT run: written
    # after the running service started, or - with no service to compare against -
    # within the last 10 minutes. Anything older is dropped entirely; an old log
    # posing as live output is worse than no pane.
    cutoff = (not_older_than - 120) if not_older_than is not None else (time.time() - 600)
    existing = [p for p in candidates
                if p.is_file() and p.stat().st_mtime >= cutoff]
    if not existing:
        return None
    return {"kind": "file", "path": max(existing, key=lambda p: p.stat().st_mtime)}


class _LogTail:
    """Follows a log source into a bounded deque the renderer reads from.

    File mode polls (read appended bytes, reopen on truncation) so it works on
    any filesystem; journal mode streams `journalctl -f`. Reading runs in a
    daemon thread; `lines(n)` is safe from the render loop because deque
    appends are atomic.
    """

    def __init__(self, source: Dict[str, Any]):
        self._source = source
        self._stop = threading.Event()
        self._buffer: deque = deque(maxlen=2000)
        self._proc: Optional[subprocess.Popen] = None
        self._offset = 0
        self._carry = b""

    @property
    def description(self) -> str:
        if self._source["kind"] == "journal":
            return "journalctl --user -u vaf"
        return str(self._source["path"])

    @property
    def stale_seconds(self) -> Optional[float]:
        """Seconds since the file was last written; None for the journal."""
        if self._source["kind"] != "file":
            return None
        try:
            return max(0.0, time.time() - self._source["path"].stat().st_mtime)
        except Exception:
            return None

    def lines(self, n: int) -> List[str]:
        if n <= 0:
            return []
        return list(self._buffer)[-n:]

    # -- file mode ------------------------------------------------------------
    def _backfill_file(self, path: Path) -> None:
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                fh.seek(max(0, size - _LOG_BACKFILL_BYTES))
                chunk = fh.read()
            text = chunk.decode("utf-8", errors="replace")
            for line in text.splitlines()[-_LOG_BACKFILL_LINES:]:
                self._buffer.append(line)
            self._offset = size
        except Exception:
            self._offset = 0

    def poll_file(self) -> None:
        """One synchronous poll step (also the unit-test seam)."""
        path = self._source["path"]
        try:
            size = path.stat().st_size
            if size < self._offset:      # truncated or rotated
                self._offset = 0
                self._carry = b""
            if size == self._offset:
                return
            with open(path, "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
            self._offset = size
            data = self._carry + chunk
            lines = data.split(b"\n")
            self._carry = lines.pop()    # unfinished last line waits for its newline
            for raw in lines:
                self._buffer.append(raw.decode("utf-8", errors="replace"))
        except Exception:
            pass

    # -- journal mode ---------------------------------------------------------
    def _run_journal(self) -> None:
        try:
            self._proc = subprocess.Popen(
                ["journalctl", "--user", "-u", "vaf", "-f",
                 "-n", str(_LOG_BACKFILL_LINES), "-o", "cat"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            for line in self._proc.stdout:  # type: ignore[union-attr]
                if self._stop.is_set():
                    break
                self._buffer.append(line.rstrip("\n"))
        except Exception:
            pass

    def _run_file(self) -> None:
        self._backfill_file(self._source["path"])
        while not self._stop.is_set():
            self.poll_file()
            self._stop.wait(0.5)

    def start(self) -> None:
        target = self._run_journal if self._source["kind"] == "journal" else self._run_file
        threading.Thread(target=target, daemon=True, name="vaf-top-logs").start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass


def _build_log_panel(tail: _LogTail, content_lines: int, width: int):
    from rich.panel import Panel
    from rich.text import Text
    text = Text(no_wrap=True, overflow="ellipsis")
    shown = tail.lines(content_lines)
    for i, line in enumerate(shown):
        text.append(line[: max(0, width - 4)] + ("\n" if i < len(shown) - 1 else ""), style="dim")
    stale = tail.stale_seconds
    # A start lane that logs to a file may not be the lane that started the
    # CURRENT service (it logs elsewhere or nowhere); say so instead of
    # presenting an old file as live output.
    stale_note = " [yellow](stale - not written recently)[/yellow]" \
        if stale is not None and stale > 300 else ""
    return Panel(text, title=f"Log  [dim]{tail.description}[/dim]{stale_note}",
                 border_style="cyan", height=content_lines + 2)


class _ServicesPoller:
    """Latest Docker service status, refreshed on its own slow cadence."""

    def __init__(self, refresh_s: float = _SERVICES_REFRESH_S):
        self._refresh_s = refresh_s
        self._stop = threading.Event()
        self.latest: Optional[Dict[str, Any]] = None

    def _loop(self):
        from vaf.core.service_health import collect_service_status
        while not self._stop.is_set():
            try:
                self.latest = collect_service_status()
            except Exception:
                pass
            self._stop.wait(self._refresh_s)

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="vaf-top-services").start()

    def stop(self):
        self._stop.set()


def cmd_top(
    interval: float = typer.Option(2.0, "--interval", "-i", min=0.5,
                                   help="Refresh interval in seconds"),
    once: bool = typer.Option(False, "--once",
                              help="Print one snapshot and exit (for scripts; no log pane)"),
    logs: bool = typer.Option(True, "--logs/--no-logs",
                              help="Follow the service log below the dashboard"),
):
    """Live server dashboard: uptime, configuration, utilization, services, log."""
    from vaf.core.system_stats import collect_snapshot

    pid = _service_pid()

    if once:
        from vaf.core.service_health import collect_service_status
        try:
            services = collect_service_status()
        except Exception:
            services = None
        prime = collect_snapshot(service_pid=pid)  # primes CPU counters
        time.sleep(0.3)
        snap = collect_snapshot(prev=prime, service_pid=pid)
        UI.console.print(_build_view(snap, services))
        return

    from rich.console import Group
    from rich.live import Live

    def _service_start_ts(service_pid: Optional[int]) -> Optional[float]:
        if not service_pid:
            return None
        try:
            import psutil
            return psutil.Process(service_pid).create_time()
        except Exception:
            return None

    def _make_tail(service_pid: Optional[int]) -> Optional[_LogTail]:
        source = _resolve_log_source(not_older_than=_service_start_ts(service_pid))
        if source is None:
            return None
        t = _LogTail(source)
        t.start()
        return t

    poller = _ServicesPoller()
    poller.start()
    tail: Optional[_LogTail] = _make_tail(pid) if logs else None
    prev: Optional[Dict[str, Any]] = None
    # The FIRST snapshot can take seconds (GPU vendor detection spawns tools),
    # and an empty alternate screen reads as a hang - which invites the Ctrl+C
    # that tears everything down. Enter Live with an instant placeholder frame.
    from rich.panel import Panel
    placeholder = Panel("[bold cyan]vaf top[/bold cyan]  collecting the first snapshot "
                        "(GPU detection takes a moment)...\n"
                        "[dim]Ctrl+C exits the dashboard.[/dim]",
                        border_style="cyan")
    try:
        with Live(placeholder, console=UI.console, screen=True, auto_refresh=False) as live:
            live.refresh()
            while True:
                snap = collect_snapshot(prev=prev, service_pid=pid)
                view = _build_view(snap, poller.latest)
                if tail is not None:
                    # The dashboard's real height (wrapping included) decides how
                    # many log lines fit below it in the current terminal.
                    dash_height = len(UI.console.render_lines(view, pad=False))
                    remaining = UI.console.size.height - dash_height - 2
                    if remaining >= 4:
                        view = Group(view, _build_log_panel(tail, remaining, UI.console.size.width))
                live.update(view, refresh=True)
                prev = snap
                time.sleep(interval)
                if snap.get("service") is None:
                    pid = _service_pid()  # the service may have started meanwhile
                if logs and tail is None:
                    tail = _make_tail(pid)  # a log source may have appeared
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        if tail is not None:
            tail.stop()
