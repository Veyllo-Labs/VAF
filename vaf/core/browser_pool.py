# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Per-user browser instances: real partitions, resource-gated, on by default.

Each user scope gets a browser CONTAINER of their own - their own profile
volume (history, saved passwords and downloads become legitimately per-user
instead of state to wipe), their own CDP and stream endpoints, and therefore
PARALLEL use: two users browse at the same time in two different browsers.
The shared vaf-browser container is what remains for everyone the pool cannot
serve; there one Chromium is time-shared (one lease at a time, a scrub on
every change of hands), which is isolation by lease rather than by partition.

`browser_pool_max` decides how many instances may run at once, default 2,
because an instance costs a full container (~1-2 GB RAM) and two is the
smallest number at which a second person is not sharing a browser. Raising it
is an operator decision about RAM: see the pool section in
docs/agents/BROWSER_AGENT.md. A free-memory floor refuses new instances before
the machine starts swapping, and 0 turns the pool off entirely. All three
knobs are config keys with a VAF_BROWSER_POOL_* environment override, so a
deployment can pin them without writing the config file.

Whenever the pool cannot serve - switched off, at capacity, low memory, docker
unreachable, image unknown - the caller falls back to the shared container and
its handover scrub, so the pool can only ever ADD isolation, never lose the
browser entirely.

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


# The three defaults live here as well as in Config.DEFAULTS: an embedder that
# builds on vaf.core without a config file still gets a working pool. A guard
# test pins the two copies together so they cannot drift.
DEFAULT_POOL_MAX = 2
DEFAULT_MIN_FREE_MB = 2500
DEFAULT_IDLE_S = 900.0


def _config_get(key: str):
    """One config value, or None where there is no readable config (an embedder
    without a config file, a test with no home). The single seam the tests stub."""
    try:
        from vaf.core.config import Config
        return Config.get(key)
    except Exception:
        return None


def _setting(env_var: str, config_key: str):
    """One pool knob: the environment override first, then the config key.

    The env var is the operator's escape hatch (pin it in a deployment without
    writing the config file), the config key is what Settings writes. Returns
    None when neither answers. An explicit 0 is a REAL value here ("pool off",
    "no memory floor"), so this never folds a value away with `or <default>` -
    that idiom would silently turn a deliberate 0 back into the default.
    """
    raw = os.environ.get(env_var)
    if raw is not None and str(raw).strip() != "":
        return raw
    return _config_get(config_key)


def pool_max() -> int:
    """How many per-user instances may RUN at once. 0 disables the pool."""
    try:
        raw = _setting("VAF_BROWSER_POOL_MAX", "browser_pool_max")
        return DEFAULT_POOL_MAX if raw is None else max(0, int(raw))
    except Exception:
        return DEFAULT_POOL_MAX


def _min_free_mb() -> int:
    try:
        raw = _setting("VAF_BROWSER_POOL_MIN_FREE_MB", "browser_pool_min_free_mb")
        return DEFAULT_MIN_FREE_MB if raw is None else max(0, int(raw))
    except Exception:
        return DEFAULT_MIN_FREE_MB


def _idle_stop_s() -> float:
    try:
        raw = _setting("VAF_BROWSER_POOL_IDLE_S", "browser_pool_idle_seconds")
        return DEFAULT_IDLE_S if raw is None else max(60.0, float(raw))
    except Exception:
        return DEFAULT_IDLE_S


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


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    """One HTTP readiness probe. The single seam the tests stub."""
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= int(getattr(r, "status", 200)) < 400
    except Exception:
        return False


def _vnc_wait_s() -> float:
    """Budget for the stream half of the health probe. Deliberately short: the
    container brings KasmVNC up within seconds, and resolve() sits on the
    blocking path of opening a browser, so a long second wait would show up as
    a hang instead of as a fall back to the shared container."""
    try:
        return max(0.0, float(os.environ.get("VAF_BROWSER_VNC_WAIT_S", "8")))
    except Exception:
        return 8.0


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
            # Same host name the compose browser gets: render_check rewrites
            # localhost targets to host.docker.internal, and a pooled browser
            # must resolve it too or the dev-server loop works for the shared
            # browser only.
            "--add-host", "host.docker.internal:host-gateway",
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
        """Both halves have to answer. CDP is what the agent drives, the KasmVNC
        stream on the other port is what the person actually sees, and they can
        fail apart: an image built before the stream existed serves CDP happily
        while nothing listens on the stream port. A CDP-only probe called that
        container healthy, so the pool handed it out and the ticket route then
        answered 502 - the stream was never asked about until a human clicked.

        The probe uses the same path the ticket route fetches (index.html), so
        what is checked here is what is served later.
        """
        from vaf.core.browser_interactive import resolve_cdp_ws_url
        try:
            resolve_cdp_ws_url(inst.cdp_base)
        except Exception:
            append_domain_log("webui", f"[browser_pool] {inst.container_name}: CDP did not answer")
            return False
        deadline = time.monotonic() + _vnc_wait_s()
        while True:
            if _http_ok(inst.vnc_base.rstrip("/") + "/index.html"):
                return True
            if time.monotonic() >= deadline:
                append_domain_log("webui", f"[browser_pool] {inst.container_name}: CDP is up but the "
                                           f"KasmVNC stream did not answer; rebuild the browser image "
                                           f"(docker compose -f docker-compose.memory.yml build vaf-browser)")
                return False
            time.sleep(0.5)

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
