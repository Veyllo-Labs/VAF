# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Is the service stack healthy, and can it be repaired without a human?

`service_stack` starts and stops the stack. This module answers the two
questions that come afterwards and had no home: what state is each service in,
and what can be done about a broken one. Both are needed by three callers at
once (the `vaf repair` command, the TUI, the admin API), which is why they live
here and not in whichever of the three was written first.

Shape: probes are subprocess/socket calls, derivations are pure functions over
their results, and every collector takes its probes as arguments so tests run
without Docker (the pattern of vaf/api/security_routes.py, itself copied from
vaf/core/display_platform.py). Everything is synchronous; async callers wrap it
in a thread (`run_in_threadpool`) exactly as the security overview does.

The failure modes this exists for, all seen in the field:
- a container is stopped or crashed;
- a container runs, but the published host port and the port VAF is configured
  to reach disagree, so a healthy service reads as a dead one;
- the docker daemon is down, or its socket is unreadable for this user;
- the OS firewall drops traffic between VAF and its own local containers.

What repair deliberately never does: no `compose down`, no volume or image
removal, no config writes (the port keys are a security decision, so a mismatch
is REPORTED, never silently corrected), no restart of a container runtime that
is already running (that stops the engine for minutes), no privilege escalation
(a Linux daemon that needs `systemctl start docker` gets a named instruction,
not a sudo attempt).
"""
from __future__ import annotations

import json
import logging
import platform
import socket
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from vaf.core.service_stack import (
    SERVICES,
    ServiceSpec,
    attempt_docker_daemon_start,
    diagnose_docker_daemon,
    ensure_service_stack,
    find_stack_root,
    is_docker_daemon_running,
    resolve_docker_exe,
    service_by_container,
)

logger = logging.getLogger(__name__)

# How long a single service probe may take. Short on purpose: the API calls
# this for seven services in a row while a user waits on a dialog.
PROBE_TIMEOUT = 3.0
INSPECT_TIMEOUT = 10.0
# Ceiling for waiting on an engine that was just asked to start. The boot path
# waits far longer (three rounds of 300s), but that is a boot; this is a button.
DAEMON_WAIT_SECONDS = 120


def _run_docker(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    """One docker invocation, resolved and windowless on Windows."""
    kwargs: Dict[str, Any] = {"capture_output": True, "text": True}
    if platform.system() == "Windows" and getattr(subprocess, "CREATE_NO_WINDOW", None) is not None:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run([resolve_docker_exe(), *args], timeout=timeout, **kwargs)


def inspect_containers(names: Sequence[str],
                       timeout: float = INSPECT_TIMEOUT) -> List[Dict[str, Any]]:
    """Raw `docker inspect` for all named containers in one call.

    Missing containers are simply absent from the result - docker still prints
    the ones it found, and their absence IS the answer for the others.
    """
    if not names:
        return []
    try:
        result = _run_docker(["inspect", *names], timeout=timeout)
        parsed = json.loads(result.stdout or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def configured_port(spec: ServiceSpec,
                    config_get: Optional[Callable[[str, Any], Any]] = None,
                    environ: Optional[Dict[str, str]] = None) -> Optional[int]:
    """The host port VAF expects to reach this service on, or None.

    Reads the environment override first (the browser lane has no config key),
    then the config URL, then the compose default. None means the service
    publishes no port at all (the sandbox), and a port comparison is skipped.
    """
    if not spec.default_port:
        return None
    import os

    env = os.environ if environ is None else environ
    if spec.env_url_var:
        raw = env.get(spec.env_url_var) or ""
        port = _port_of(raw)
        if port:
            return port
    if spec.config_url_key:
        if config_get is None:
            try:
                from vaf.core.config import Config
                config_get = Config.get
            except Exception:
                config_get = None
        if config_get is not None:
            try:
                port = _port_of(str(config_get(spec.config_url_key, "") or ""))
                if port:
                    return port
            except Exception:
                pass
    return spec.default_port


def _port_of(url: str) -> Optional[int]:
    """The port in a URL or DSN, or None. Never raises on user-edited text."""
    if not url:
        return None
    try:
        parsed = urlparse(url if "://" in url else "//" + url)
        return int(parsed.port) if parsed.port else None
    except Exception:
        return None


def probe_service(spec: ServiceSpec, port: Optional[int],
                  timeout: float = PROBE_TIMEOUT) -> Optional[Dict[str, Any]]:
    """Talk to the service the way VAF talks to it. None when it has no probe.

    A running container is not a reachable one: this is the step that catches a
    firewall dropping loopback traffic, a service still starting, and a port
    published somewhere other than where VAF looks.
    """
    if not spec.probe:
        return None
    if spec.probe != "postgres" and not port:
        return None
    try:
        if spec.probe == "postgres":
            try:
                from vaf.memory.database import check_db_connection_sync
            except Exception:
                return _probe_tcp(port, timeout)
            ok = bool(check_db_connection_sync(timeout_seconds=timeout))
            return {"kind": "postgres", "ok": ok,
                    "detail": "" if ok else "SELECT 1 did not answer"}
        if spec.probe == "tcp":
            return _probe_tcp(port, timeout)
        if spec.probe.startswith("http:"):
            return _probe_http(spec.probe[len("http:"):], port, timeout)
    except Exception as e:
        return {"kind": spec.probe, "ok": False, "detail": str(e)[:200]}
    return None


def _probe_tcp(port: Optional[int], timeout: float) -> Dict[str, Any]:
    try:
        with socket.create_connection(("127.0.0.1", int(port or 0)), timeout=timeout):
            return {"kind": "tcp", "ok": True, "detail": ""}
    except Exception as e:
        return {"kind": "tcp", "ok": False, "detail": str(e)[:200]}


def _probe_http(path: str, port: Optional[int], timeout: float) -> Dict[str, Any]:
    import urllib.request

    url = f"http://127.0.0.1:{int(port or 0)}{path or '/'}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - loopback only
            code = int(getattr(resp, "status", 0) or 0)
            return {"kind": "http", "ok": 200 <= code < 500, "detail": f"HTTP {code}"}
    except Exception as e:
        return {"kind": "http", "ok": False, "detail": str(e)[:200]}


def derive_service_status(spec: ServiceSpec,
                          inspect: Optional[Dict[str, Any]],
                          cfg_port: Optional[int],
                          probe: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure: one service's row from its probe results.

    States: ok, warn (something is off but nothing the user depends on is
    down), error (a required service is unusable), absent (never created).
    `reason` is one honest sentence, because it is what the dialog and the
    terminal both print.
    """
    exists = inspect is not None
    running = False
    health = "none"
    host_ports: List[Dict[str, str]] = []
    if inspect is not None:
        try:
            state = inspect.get("State") or {}
            running = bool(state.get("Running"))
            health = str(((state.get("Health") or {}).get("Status") or "none"))
            bindings = (inspect.get("HostConfig") or {}).get("PortBindings") or {}
            for cport, binds in (bindings or {}).items():
                for b in (binds or []):
                    host_ports.append({
                        "container_port": str(cport),
                        "host_ip": str(b.get("HostIp") or ""),
                        "host_port": str(b.get("HostPort") or ""),
                    })
        except Exception:
            pass

    bound = {p.get("host_port") for p in host_ports if p.get("host_port")}
    port_mismatch = bool(running and cfg_port and bound and str(cfg_port) not in bound)

    probe_ok: Optional[bool] = None if probe is None else bool(probe.get("ok"))

    if not exists:
        state = "error" if spec.required else "absent"
        reason = ("The container was never created. Repair can bring it up."
                  if spec.required else
                  "Optional service, not installed on this machine.")
    elif not running:
        state = "error" if spec.required else "warn"
        reason = "The container exists but is stopped."
    elif port_mismatch:
        state = "error" if spec.required else "warn"
        reason = (f"The container publishes host port {sorted(bound)[0]}, but VAF is "
                  f"configured to reach it on {cfg_port}.")
    elif probe_ok is False:
        state = "error" if spec.required else "warn"
        detail = str((probe or {}).get("detail") or "").strip()
        reason = "The container runs but does not answer" + (f": {detail}" if detail else ".")
    elif health == "unhealthy":
        state = "warn"
        reason = "The container answers, but its own health check reports unhealthy."
    elif health == "starting":
        state = "warn"
        reason = "The container is still starting up."
    else:
        state = "ok"
        reason = "Connected."

    return {
        "name": spec.container_name,
        "service_key": spec.service_key,
        "required": spec.required,
        "exists": exists,
        "running": running,
        "health": health,
        "host_ports": host_ports,
        "configured_port": cfg_port,
        "port_mismatch": port_mismatch,
        "probe": probe,
        "probe_ok": probe_ok,
        "state": state,
        "reason": reason,
    }


def collect_service_status(
    daemon_probe: Callable[[], Dict[str, Any]] = diagnose_docker_daemon,
    inspect_probe: Callable[[Sequence[str]], List[Dict[str, Any]]] = inspect_containers,
    port_reader: Callable[[ServiceSpec], Optional[int]] = configured_port,
    service_probe: Callable[[ServiceSpec, Optional[int]], Optional[Dict[str, Any]]] = probe_service,
) -> Dict[str, Any]:
    """Every service's state in one snapshot.

    With the daemon down nothing else is attempted: no inspect, no probes. That
    keeps the worst case at one `docker info` (10s) instead of seven timeouts,
    which matters because a web request waits on this.
    """
    daemon = daemon_probe()
    root = find_stack_root()
    services: List[Dict[str, Any]] = []

    if not daemon.get("ok"):
        for spec in SERVICES:
            services.append({
                "name": spec.container_name,
                "service_key": spec.service_key,
                "required": spec.required,
                "exists": None,
                "running": False,
                "health": "none",
                "host_ports": [],
                "configured_port": None,
                "port_mismatch": False,
                "probe": None,
                "probe_ok": None,
                "state": "unknown",
                "reason": "Docker is not reachable, so this container's state is unknown.",
            })
    else:
        inspects = inspect_probe([s.container_name for s in SERVICES])
        by_name: Dict[str, Dict[str, Any]] = {}
        for ins in inspects:
            try:
                by_name[str(ins.get("Name") or "").lstrip("/")] = ins
            except Exception:
                continue
        for spec in SERVICES:
            ins = by_name.get(spec.container_name)
            cfg_port = port_reader(spec)
            probe = None
            if ins is not None and bool((ins.get("State") or {}).get("Running")):
                probe = service_probe(spec, cfg_port)
            services.append(derive_service_status(spec, ins, cfg_port, probe))

    return {
        "docker": {
            "available": bool(daemon.get("ok")),
            "reason": str(daemon.get("reason") or ""),
            "detail": str(daemon.get("detail") or ""),
        },
        "stack_root": str(root) if root else None,
        "services": services,
        "checked_at": _now_iso(),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _daemon_hint() -> str:
    """What to type when the daemon is down, per platform."""
    system = platform.system()
    if system == "Linux":
        return ("Start it with `sudo systemctl start docker` (or start Docker Desktop) "
                "and run the repair again.")
    if system == "Darwin":
        return "Start Docker Desktop, or run `colima start`, and repair again."
    return "Start Rancher Desktop or Docker Desktop and run the repair again."


def _firewall_hint() -> str:
    """Where to look when a container runs, publishes its port, and still cannot be reached."""
    system = platform.system()
    if system == "Linux":
        return ("Check the local firewall (iptables/nftables, the DOCKER-USER chain) "
                "for a rule dropping traffic to the published loopback ports.")
    if system == "Darwin":
        return "Check the macOS application firewall for a rule blocking the container ports."
    return ("Check Windows Defender Firewall for a rule blocking loopback traffic to "
            "the container ports.")


def repair_service_stack(
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    status_probe: Callable[[], Dict[str, Any]] = collect_service_status,
) -> Dict[str, Any]:
    """Try to make the stack healthy, and say what was done at every step.

    Returns {ok, steps: [{step, action, ok, message}], status_after}. `progress`
    is called with each step as it finishes, so the terminal and the dialog can
    show a run that takes minutes instead of a spinner that says nothing.
    """
    steps: List[Dict[str, Any]] = []

    def add(step: str, action: str, ok: bool, message: str) -> None:
        entry = {"step": step, "action": action, "ok": bool(ok), "message": message}
        steps.append(entry)
        if log is not None:
            try:
                log(f"[{'ok' if ok else 'failed'}] {step}: {message}")
            except Exception:
                pass
        if progress is not None:
            try:
                progress(dict(entry))
            except Exception:
                pass

    def finish(status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        final_status = status if status is not None else status_probe()
        failed_required = [
            s for s in final_status.get("services", [])
            if s.get("required") and s.get("state") in ("error", "unknown")
        ]
        # Anything still not green, optional included. `ok` decides the exit
        # code and stays required-only (an optional image that will not build
        # must not fail a repair), but a caller that says "healthy" out loud
        # needs to know about the rest - saying it while a container the user
        # can SEE listed as a problem is still down is how a report stops
        # being believed.
        degraded = [str(s.get("name")) for s in final_status.get("services", [])
                    if s.get("state") not in ("ok", "absent")]
        return {
            "ok": not failed_required and all(s["ok"] for s in steps),
            "degraded": degraded,
            "steps": steps,
            "status_after": final_status,
        }

    # 1. The engine itself.
    daemon = diagnose_docker_daemon()
    if daemon.get("ok"):
        add("daemon", "check", True, "The Docker daemon is reachable.")
    elif daemon.get("reason") == "permission_denied":
        add("daemon", "check", False,
            "Docker refuses this user access to its socket. Add yourself to the docker "
            "group (`sudo usermod -aG docker $USER`) and log in again, or use rootless "
            "Docker. VAF will not change system permissions for you.")
        return finish()
    elif daemon.get("reason") == "no_cli":
        add("daemon", "check", False,
            "No docker executable was found. Install Docker (or Rancher Desktop) and "
            "run the repair again.")
        return finish()
    else:
        launched = attempt_docker_daemon_start(log)
        ready = False
        if launched:
            deadline = time.time() + DAEMON_WAIT_SECONDS
            while time.time() < deadline:
                if is_docker_daemon_running():
                    ready = True
                    break
                time.sleep(2)
        if ready:
            add("daemon", "start", True, "The container engine was started and is ready.")
        else:
            add("daemon", "start", False,
                "The container engine did not come up. " + _daemon_hint())
            return finish()

    # 2. The compose file. A pip install has none, and that is not a defect -
    #    so this step passes. Marking it failed would drag the whole run's
    #    verdict down while its own message says nothing is wrong, and the
    #    services themselves already decide the verdict.
    root = find_stack_root()
    if root is None:
        add("stack_root", "check", True,
            "No compose file was found, so there is no stack to manage here. This is "
            "normal for a pip install; the containers are managed wherever the "
            "compose file lives.")
        return finish()
    add("stack_root", "check", True, f"Using the compose file in {root}.")

    # 3. Anything missing or stopped: one idempotent `compose up`. Optional
    #    services count here too - `compose up` starts them best-effort anyway,
    #    and a stopped optional container is shown to the user as a problem, so
    #    a repair that skips it answers a question nobody asked.
    before = status_probe()
    down = [s for s in before.get("services", [])
            if not s.get("exists") or not s.get("running")]
    if down:
        names = ", ".join(str(s.get("name")) for s in down)
        lines: List[str] = []
        started = ensure_service_stack(log=lines.append)
        tail = " ".join(lines[-2:]).strip()
        add("compose_up", "up", bool(started),
            (f"Started the missing services ({names})." if started
             else f"Could not start {names}." ) + (f" {tail}" if tail else ""))
    else:
        add("compose_up", "skip", True, "Every container was already running.")

    # 4. Running but unreachable: restart the container itself. A port mismatch
    #    is excluded here on purpose - restarting cannot fix a disagreement
    #    about which port to use, it only hides the reason for a while.
    after_up = status_probe()
    for svc in after_up.get("services", []):
        if not svc.get("running") or svc.get("port_mismatch"):
            continue
        unhealthy = svc.get("health") == "unhealthy"
        unreachable = svc.get("probe_ok") is False
        if not (unhealthy or unreachable):
            continue
        name = str(svc.get("name"))
        try:
            result = _run_docker(["restart", "-t", "5", name], timeout=90)
            ok = result.returncode == 0
            detail = ((result.stderr or "") or (result.stdout or "")).strip()[:200]
        except Exception as e:
            ok, detail = False, str(e)[:200]
        add(f"restart:{name}", "restart", ok,
            f"Restarted {name}." if ok else f"Could not restart {name}: {detail}")

    # 5. Port mismatches: named, never corrected.
    for svc in after_up.get("services", []):
        if not svc.get("port_mismatch"):
            continue
        spec = service_by_container(str(svc.get("name")))
        where = (f"the config key `{spec.config_url_key}`" if spec and spec.config_url_key
                 else (f"the environment variable {spec.env_url_var}" if spec and spec.env_url_var
                       else "the configured URL"))
        add(f"config:{svc.get('name')}", "report", False,
            f"{svc.get('reason')} Change {where} to the published port, or set the "
            f"matching port variable in ~/.vaf/compose.env and start the stack again. "
            f"VAF does not rewrite this for you.")

    # 6. Whatever is still unreachable after a restart: the network in between.
    final = status_probe()
    for svc in final.get("services", []):
        if svc.get("running") and svc.get("probe_ok") is False and not svc.get("port_mismatch"):
            add(f"firewall:{svc.get('name')}", "report", False,
                f"{svc.get('name')} runs and publishes its port but still does not "
                f"answer. " + _firewall_hint())

    return finish(final)
