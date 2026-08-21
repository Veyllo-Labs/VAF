# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Per-user browser instances: real partitions, resource-gated, opt-in.

The shared vaf-browser container time-shares one Chromium between everyone
(one lease at a time, a scrub on every change of hands). This pool is the
second stage: when VAF_BROWSER_POOL_MAX is set above zero, each user scope
gets a browser CONTAINER of their own - their own profile volume (history,
saved passwords and downloads become legitimately per-user instead of state
to wipe), their own CDP and stream endpoints, and therefore PARALLEL use:
two users browse at the same time in two different browsers.

Deliberately an env knob like its siblings (VAF_BROWSER_MAX_PARALLEL,
VAF_BROWSER_CDP_URL), default OFF: a pooled instance costs the full container
(~1-2 GB RAM), so the operator opts in with a number they can afford, and a
free-memory floor refuses new instances before the machine starts swapping.
Whenever the pool cannot serve - disabled, at capacity, low memory, docker
unreachable, image unknown - the caller falls back to the shared container
and its handover scrub, so the pool can only ever ADD isolation, never lose
the browser entirely.

Instances are cloned from the shared container's own image and network (read
via docker inspect at first use), published loopback-only on ephemeral host
ports exactly like the shared one, and named by a scope HASH - a container
listing must not leak who uses the machine. Stopped instances keep their
container and profile volume; the reaper only ever `docker stop`s an idle
instance, it never removes data.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from vaf.core.log_helper import append_domain_log

_NAME_PREFIX = "vaf-browser-u-"
_VOLUME_PREFIX = "vaf-browser-profile-"
_NETWORK_PREFIX = "vaf-browser-net-"


def pool_max() -> int:
    """How many per-user instances may RUN at once. 0 (default) disables the pool."""
    try:
        return max(0, int(os.environ.get("VAF_BROWSER_POOL_MAX", "0") or 0))
    except Exception:
        return 0


def _min_free_mb() -> int:
    try:
        return max(0, int(os.environ.get("VAF_BROWSER_POOL_MIN_FREE_MB", "2500") or 2500))
    except Exception:
        return 2500


def _idle_stop_s() -> float:
    try:
        return max(60.0, float(os.environ.get("VAF_BROWSER_POOL_IDLE_S", "900") or 900))
    except Exception:
        return 900.0


def _scope_hash(scope: str) -> str:
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]


@dataclass
class BrowserInstance:
    user_scope_id: str
    container_name: str
    cdp_base: str            # http://127.0.0.1:<port>
    vnc_base: str            # http://127.0.0.1:<port>
    last_used: float


def _docker(args: List[str], timeout: float = 60) -> subprocess.CompletedProcess:
    """One docker CLI call. The single seam the tests stub."""
    from vaf.core.service_stack import resolve_docker_exe
    return subprocess.run([resolve_docker_exe(), *args],
                          capture_output=True, text=True, timeout=timeout)


def _mem_available_mb() -> Optional[int]:
    """Free-ish memory in MB, or None where /proc/meminfo does not exist
    (macOS/Windows hosts run the containers inside a VM with its own budget,
    so the floor check stands down there rather than guessing)."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


class BrowserPool:
    """Singleton allocator of per-user browser containers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._instances: Dict[str, BrowserInstance] = {}   # scope -> instance
        self._template: Optional[tuple] = None             # (image, network)
        self._reaper_alive = False

    # -- template ----------------------------------------------------------
    def _resolve_template(self) -> Optional[tuple]:
        """Image and network for new instances, read from the shared container.

        The shared vaf-browser is the source of truth for both: it always runs
        the image this checkout built, and its network is the isolated
        browser-only net (never the DB net). Reading instead of hardcoding
        keeps the pool correct across compose project names and rebuilds."""
        with self._lock:
            if self._template is not None:
                return self._template
        try:
            r = _docker(["inspect", "vaf-browser", "--format",
                         "{{.Config.Image}}\t{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}"])
            if r.returncode != 0:
                return None
            image, network = r.stdout.strip().split("\t", 1)
            if not image or not network:
                return None
            with self._lock:
                self._template = (image, network)
            return self._template
        except Exception:
            return None

    # -- allocation --------------------------------------------------------
    def resolve(self, user_scope_id: str) -> Optional[BrowserInstance]:
        """The caller's own browser instance, or None meaning: use the shared one.

        BLOCKING (docker calls, health wait): run off the event loop. Never
        raises - every failure is a logged fallback to the shared container."""
        if pool_max() <= 0 or not user_scope_id:
            return None
        try:
            return self._resolve_inner(str(user_scope_id))
        except Exception as e:
            append_domain_log("webui", f"[browser_pool] resolve failed, using shared browser: {e}")
            return None

    def _resolve_inner(self, scope: str) -> Optional[BrowserInstance]:
        with self._lock:
            inst = self._instances.get(scope)
        name = _NAME_PREFIX + _scope_hash(scope)

        state = self._container_state(name)
        if inst is not None and state == "running":
            inst.last_used = time.time()
            self._ensure_reaper()
            return inst

        if state == "running" or state == "exited":
            # Adopt a container from an earlier VAF process (or restart an
            # idle-stopped one). Published ports were fixed at creation and
            # survive stop/start, so re-reading them is enough.
            if state == "exited":
                if not self._may_start_another():
                    return None
                r = _docker(["start", name])
                if r.returncode != 0:
                    return None
            inst = self._read_endpoints(scope, name)
        elif state is None:
            if not self._may_start_another():
                return None
            inst = self._create(scope, name)
        else:
            return None

        if inst is None:
            return None
        if not self._wait_healthy(inst):
            append_domain_log("webui", f"[browser_pool] instance for scope hash "
                                       f"{_scope_hash(scope)} did not become healthy; using shared browser")
            return None
        with self._lock:
            self._instances[scope] = inst
        self._ensure_reaper()
        return inst

    def _container_state(self, name: str) -> Optional[str]:
        r = _docker(["inspect", name, "--format", "{{.State.Status}}"], timeout=20)
        if r.returncode != 0:
            return None
        return r.stdout.strip() or None

    def _may_start_another(self) -> bool:
        limit = pool_max()
        r = _docker(["ps", "--filter", f"name={_NAME_PREFIX}", "--format", "{{.Names}}"], timeout=20)
        running = len([ln for ln in (r.stdout or "").splitlines() if ln.strip()]) if r.returncode == 0 else 0
        if running >= limit:
            append_domain_log("webui", f"[browser_pool] at capacity ({running}/{limit}); using shared browser")
            return False
        free = _mem_available_mb()
        if free is not None and free < _min_free_mb():
            append_domain_log("webui", f"[browser_pool] low memory ({free} MB free, floor "
                                       f"{_min_free_mb()} MB); using shared browser")
            return False
        return True

    def _create(self, scope: str, name: str) -> Optional[BrowserInstance]:
        template = self._resolve_template()
        if template is None:
            append_domain_log("webui", "[browser_pool] no template (is the shared vaf-browser "
                                       "container present?); using shared browser")
            return None
        image, _shared_network = template
        volume = _VOLUME_PREFIX + _scope_hash(scope)
        # ONE NETWORK PER INSTANCE, never the shared browser's. Inside the
        # container Chromium's CDP proxy listens on 0.0.0.0:9222 and KasmVNC on
        # 0.0.0.0:6901 with authentication deliberately switched off - both are
        # safe only because the host publishes them on loopback and the VAF
        # server is the only reachable door. On a SHARED bridge network that
        # stops being true between containers: a page in user A's browser can
        # dial user B's container IP directly and drive it, which is exactly
        # the isolation the pool exists to provide. A per-instance network
        # leaves each browser with no peer at all.
        network = self._ensure_network(scope)
        if network is None:
            append_domain_log("webui", "[browser_pool] could not create an isolated network; "
                                       "using shared browser")
            return None
        args = [
            "run", "-d", "--name", name,
            "--restart", "no",
            "--shm-size", "1g", "--memory", "2g",
            "--network", network,
            # The shared container gets these from compose; a pooled instance is
            # created by us, so the filtering resolvers have to be passed here or
            # the DNS half of the browser hardening silently stops applying.
            "--dns", "1.1.1.2", "--dns", "1.0.0.2",
            "-p", "127.0.0.1::9222", "-p", "127.0.0.1::6901",
            "-e", f"TZ={os.environ.get('VAF_BROWSER_TZ', 'Europe/Berlin')}",
            "-v", f"{volume}:/home/browser",
        ]
        proxy = os.environ.get("VAF_BROWSER_PROXY", "").strip()
        if proxy:
            args += ["-e", f"VAF_BROWSER_PROXY={proxy}"]
        args.append(image)
        r = _docker(args, timeout=120)
        if r.returncode != 0:
            append_domain_log("webui", f"[browser_pool] docker run failed: {(r.stderr or '').strip()[:300]}")
            return None
        return self._read_endpoints(scope, name)

    def _ensure_network(self, scope: str) -> Optional[str]:
        """The instance's own bridge network, created once per scope.

        `docker network create` on an existing name fails harmlessly, so the
        existence check and the create are not a race worth locking: either
        way the name exists afterwards, which is all the run needs."""
        net = _NETWORK_PREFIX + _scope_hash(scope)
        try:
            r = _docker(["network", "inspect", net, "--format", "{{.Name}}"], timeout=20)
            if r.returncode == 0 and net in (r.stdout or ""):
                return net
            c = _docker(["network", "create", "--driver", "bridge", net], timeout=30)
            if c.returncode == 0:
                return net
            # Lost a race against another thread creating the same network?
            r2 = _docker(["network", "inspect", net, "--format", "{{.Name}}"], timeout=20)
            return net if r2.returncode == 0 else None
        except Exception:
            return None

    def _read_endpoints(self, scope: str, name: str) -> Optional[BrowserInstance]:
        def host_port(container_port: str) -> Optional[str]:
            r = _docker(["port", name, container_port], timeout=20)
            if r.returncode != 0:
                return None
            # "127.0.0.1:49153" (possibly one line per address family; loopback first)
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if line.startswith("127.0.0.1:"):
                    return line.rsplit(":", 1)[1]
            return None

        cdp, vnc = host_port("9222/tcp"), host_port("6901/tcp")
        if not cdp or not vnc:
            return None
        return BrowserInstance(
            user_scope_id=scope,
            container_name=name,
            cdp_base=f"http://127.0.0.1:{cdp}",
            vnc_base=f"http://127.0.0.1:{vnc}",
            last_used=time.time(),
        )

    def _wait_healthy(self, inst: BrowserInstance) -> bool:
        from vaf.core.browser_interactive import resolve_cdp_ws_url
        try:
            resolve_cdp_ws_url(inst.cdp_base)
            return True
        except Exception:
            return False

    def peek(self, user_scope_id: str) -> Optional[BrowserInstance]:
        """Cached-only lookup, no docker calls. The run hooks use this: a run
        resolved its instance at its start, so the cache is authoritative for
        the rest of that run's lifetime."""
        if pool_max() <= 0 or not user_scope_id:
            return None
        with self._lock:
            inst = self._instances.get(str(user_scope_id))
            if inst is not None:
                inst.last_used = time.time()
            return inst

    def touch(self, user_scope_id: str) -> None:
        with self._lock:
            inst = self._instances.get(str(user_scope_id))
            if inst is not None:
                inst.last_used = time.time()

    # -- reaper ------------------------------------------------------------
    def _ensure_reaper(self) -> None:
        with self._lock:
            if self._reaper_alive:
                return
            self._reaper_alive = True
        threading.Thread(target=self._reaper_loop, daemon=True,
                         name="browser-pool-reaper").start()

    def _reaper_loop(self) -> None:
        """Stops instances nobody is using. Data is never touched: the container
        and its profile volume stay, only the RAM is given back."""
        try:
            while True:
                time.sleep(30)
                with self._lock:
                    snapshot = list(self._instances.items())
                    if not snapshot:
                        return
                for scope, inst in snapshot:
                    if time.time() - inst.last_used < _idle_stop_s():
                        continue
                    if self._instance_busy(inst):
                        self.touch(scope)
                        continue
                    try:
                        _docker(["stop", "-t", "5", inst.container_name], timeout=60)
                        append_domain_log("webui", f"[browser_pool] idle instance stopped "
                                                   f"({inst.container_name})")
                    except Exception:
                        pass
                    with self._lock:
                        self._instances.pop(scope, None)
        finally:
            with self._lock:
                self._reaper_alive = False
                if self._instances:
                    self._ensure_reaper()

    @staticmethod
    def _instance_busy(inst: BrowserInstance) -> bool:
        """A lease or a live agent run on the instance's own manager keeps it up."""
        try:
            from vaf.core.browser_interactive import peek_manager_for_container
            mgr = peek_manager_for_container(inst.container_name)
            if mgr is None:
                return False
            return mgr.has_activity()
        except Exception:
            return True   # cannot tell: keep it running rather than cutting a live session


_pool: Optional[BrowserPool] = None
_pool_lock = threading.Lock()


def get_browser_pool() -> BrowserPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = BrowserPool()
        return _pool
