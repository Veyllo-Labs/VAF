# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One point-in-time reading of what a server admin wants to see.

`collect_snapshot()` gathers host identity, uptimes, VAF configuration and live
utilization (CPU, RAM, swap, disk, network rates, GPU) into one plain dict. It
is the single collector behind `vaf top`; a later admin API endpoint must reuse
it rather than sampling psutil a second time.

Contract: never raises. Every probe is wrapped; a value that cannot be read
becomes None (or an empty list) so renderers can show "n/a" instead of dying.
Rates (network bytes/s) need two snapshots: pass the previous one as `prev`.

What is deliberately NOT here: VAF-internal queue/worker/session metrics. They
exist only inside the running service process; reading them from a separate CLI
process needs an authenticated admin endpoint, which is the planned harness half
of this collector.
"""

import os
import shutil
import subprocess
import time
import platform as _platform
from pathlib import Path
from typing import Any, Dict, List, Optional

# GPU inventory (names, vendors, total VRAM) spawns vendor tools, so it is read
# once per process; only the live utilization query runs per snapshot.
_gpu_inventory: Optional[List[Dict[str, Any]]] = None

# psutil.Process.cpu_percent() measures BETWEEN calls on the same object; fresh
# objects every tick would always read 0.0, so the service's Process handles are
# kept for the life of this process.
_proc_cache: Dict[int, Any] = {}


def _os_pretty_name() -> str:
    try:
        return _platform.freedesktop_os_release().get("PRETTY_NAME", "") or _platform.system()
    except Exception:
        return _platform.system()


def _vaf_version() -> str:
    try:
        from vaf.version import __version__
        return __version__
    except Exception:
        return "unknown"


def _config_summary() -> Dict[str, Any]:
    try:
        from vaf.core.config import Config
        provider = str(Config.get("provider", "local") or "local")
        if provider == "local":
            # The bare "model" key IS the local model; API providers keep their
            # active model in api_model_{provider} (see api_backend model resolution).
            model = str(Config.get("model", "") or "")
        else:
            model = (str(Config.get(f"api_model_{provider}", "") or "")
                     or Config.get_default_model(provider))
        return {
            "server_mode": bool(Config.get("server_mode", False)),
            "provider": provider,
            "model": model,
            "lan_enabled": bool(Config.get("local_network_enabled", False)),
        }
    except Exception:
        return {"server_mode": None, "provider": None, "model": None, "lan_enabled": None}


def gpu_inventory() -> List[Dict[str, Any]]:
    """Static GPU list (vendor, name, total VRAM), detected once per process."""
    global _gpu_inventory
    if _gpu_inventory is None:
        gpus: List[Dict[str, Any]] = []
        try:
            from vaf.core.gpu_detection import detect_all_gpus
            for g in detect_all_gpus():
                gpus.append({
                    "vendor": getattr(g, "vendor", "") or "",
                    "name": getattr(g, "name", "") or "",
                    "vram_total_mb": getattr(g, "vram_mb", None),
                })
        except Exception:
            gpus = []
        _gpu_inventory = gpus
    return _gpu_inventory


def gpu_live() -> List[Dict[str, Any]]:
    """Live GPU utilization. NVIDIA via nvidia-smi; other vendors fall back to
    the static inventory with util/mem as None (AMD/Intel live counters are a
    known gap, not an error)."""
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode == 0 and out.stdout.strip():
                gpus = []
                for line in out.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 4:
                        gpus.append({
                            "vendor": "nvidia",
                            "name": parts[0],
                            "util_percent": _to_int(parts[1]),
                            "mem_used_mb": _to_int(parts[2]),
                            "mem_total_mb": _to_int(parts[3]),
                        })
                if gpus:
                    return gpus
        except Exception:
            pass
    return [
        {**g, "util_percent": None, "mem_used_mb": None, "mem_total_mb": g.get("vram_total_mb")}
        for g in gpu_inventory()
    ]


def _to_int(value: str) -> Optional[int]:
    try:
        return int(float(value))
    except Exception:
        return None


def _lan_summary(lan_enabled: Optional[bool], hostname: str) -> Dict[str, Any]:
    """LAN reachability: the effective access port and the URLs clients can use.
    The hostname URL is listed first - the auto-generated certificate carries the
    hostname as a DNS SAN, so it survives DHCP address changes."""
    if not lan_enabled:
        return {"enabled": bool(lan_enabled), "access_port": None, "urls": []}
    try:
        from vaf.network.binding import get_all_local_ips, resolve_lan_access_ports
        access_port, _ = resolve_lan_access_ports(wait_for_proxy=False)
        suffix = "" if access_port == 443 else f":{access_port}"
        urls = [f"https://{hostname}{suffix}"] if hostname else []
        urls += [f"https://{ip}{suffix}" for _, ip in get_all_local_ips()]
        return {"enabled": True, "access_port": access_port,
                "urls": list(dict.fromkeys(urls))}
    except Exception:
        return {"enabled": True, "access_port": None, "urls": []}


def _service_process(service_pid: Optional[int]) -> Optional[Dict[str, Any]]:
    """CPU/RSS/uptime of the VAF service process TREE (tray + web + children)."""
    if not service_pid:
        return None
    try:
        import psutil
        procs = []
        root = _proc_cache.get(service_pid)
        if root is None or not root.is_running():
            root = psutil.Process(service_pid)
            _proc_cache.clear()
            _proc_cache[service_pid] = root
        procs.append(root)
        for child in root.children(recursive=True):
            cached = _proc_cache.get(child.pid)
            if cached is None or not cached.is_running():
                _proc_cache[child.pid] = child
                cached = child
            procs.append(cached)
        cpu = 0.0
        rss = 0
        for p in procs:
            try:
                cpu += p.cpu_percent(None)
                rss += p.memory_info().rss
            except Exception:
                continue
        return {
            "pid": service_pid,
            "uptime_s": max(0, int(time.time() - root.create_time())),
            "cpu_percent": round(cpu, 1),
            "rss_mb": rss // (1024 * 1024),
            "processes": len(procs),
        }
    except Exception:
        return None


def _service_clients(service_pid: Optional[int]) -> Optional[Dict[str, Any]]:
    """Who is connected to the service: inbound TCP connections of the service
    process tree, grouped by remote IP.

    "Inbound" is derived from the same connection list: a connection counts only
    when its LOCAL port is one the tree is listening on, which cleanly excludes
    the service's own outbound connections (postgres, provider APIs). Per-IP
    byte rates are deliberately absent - they need packet capture (root); the
    honest per-IP signal available to an unprivileged process is connections.
    Relies on the process cache filled by _service_process in the same snapshot.
    """
    if not service_pid or not _proc_cache:
        return None
    try:
        import ipaddress
        import psutil
        conns = []
        for proc in list(_proc_cache.values()):
            try:
                fn = getattr(proc, "net_connections", None) or proc.connections
                conns.extend(fn(kind="tcp"))
            except Exception:
                continue
        listen_ports = {c.laddr.port for c in conns
                        if c.status == psutil.CONN_LISTEN and c.laddr}
        clients: Dict[str, Dict[str, Any]] = {}
        for c in conns:
            if c.status != psutil.CONN_ESTABLISHED or not c.raddr or not c.laddr:
                continue
            if c.laddr.port not in listen_ports:
                continue
            ip = c.raddr.ip
            entry = clients.setdefault(ip, {"connections": 0, "ports": set()})
            entry["connections"] += 1
            entry["ports"].add(c.laddr.port)
        out = []
        for ip, entry in sorted(clients.items(), key=lambda kv: -kv[1]["connections"]):
            try:
                is_local = ipaddress.ip_address(ip.split("%")[0]).is_loopback
            except Exception:
                is_local = False
            out.append({"ip": ip, "local": is_local,
                        "connections": entry["connections"],
                        "ports": sorted(entry["ports"])})
        return {"listen_ports": sorted(listen_ports), "clients": out}
    except Exception:
        return None


def collect_snapshot(prev: Optional[Dict[str, Any]] = None,
                     service_pid: Optional[int] = None) -> Dict[str, Any]:
    """One reading. Pass the previous snapshot as `prev` to get network rates."""
    snap: Dict[str, Any] = {
        "ts": time.time(),
        "monotonic": time.monotonic(),
    }

    snap["host"] = {
        "hostname": _platform.node(),
        "os": _os_pretty_name(),
        "kernel": _platform.release(),
        "arch": _platform.machine(),
    }
    snap["vaf"] = {"version": _vaf_version(), **_config_summary()}
    snap["lan"] = _lan_summary(snap["vaf"].get("lan_enabled"), snap["host"]["hostname"])

    try:
        import psutil
        snap["uptime_s"] = max(0, int(time.time() - psutil.boot_time()))
    except Exception:
        snap["uptime_s"] = None

    try:
        import psutil
        snap["cpu"] = {
            "percent": psutil.cpu_percent(None),
            "cores": psutil.cpu_count(logical=True),
            "loadavg": tuple(round(x, 2) for x in os.getloadavg()) if hasattr(os, "getloadavg") else None,
        }
    except Exception:
        snap["cpu"] = {"percent": None, "cores": None, "loadavg": None}

    try:
        import psutil
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        snap["mem"] = {
            "total_mb": vm.total // (1024 * 1024),
            "used_mb": (vm.total - vm.available) // (1024 * 1024),
            "percent": vm.percent,
            "swap_total_mb": sw.total // (1024 * 1024),
            "swap_used_mb": sw.used // (1024 * 1024),
        }
    except Exception:
        snap["mem"] = None

    try:
        import psutil
        data_dir = Path.home() / ".vaf"
        target = data_dir if data_dir.exists() else Path.home()
        du = psutil.disk_usage(str(target))
        snap["disk"] = {
            "path": str(target),
            "total_gb": round(du.total / (1024 ** 3), 1),
            "used_gb": round(du.used / (1024 ** 3), 1),
            "percent": du.percent,
        }
    except Exception:
        snap["disk"] = None

    try:
        import psutil
        io = psutil.net_io_counters()
        snap["net"] = {"bytes_sent": io.bytes_sent, "bytes_recv": io.bytes_recv,
                       "sent_rate_bps": None, "recv_rate_bps": None}
        if prev and prev.get("net") and prev.get("monotonic") is not None:
            dt = snap["monotonic"] - prev["monotonic"]
            if dt > 0:
                snap["net"]["sent_rate_bps"] = max(0, int((io.bytes_sent - prev["net"]["bytes_sent"]) / dt))
                snap["net"]["recv_rate_bps"] = max(0, int((io.bytes_recv - prev["net"]["bytes_recv"]) / dt))
    except Exception:
        snap["net"] = None

    snap["gpus"] = gpu_live()
    snap["service"] = _service_process(service_pid)   # fills the process cache
    snap["clients"] = _service_clients(service_pid)
    return snap
