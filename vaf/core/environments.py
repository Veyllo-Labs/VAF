# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Sandbox environments: a container, a volume and a network of their own, per person.

An environment is where an agent runs, edits and tests code without touching the
host or anybody else's work. The main agent and the coder reach the same one by its
id; the CLI (`vaf env`) and the web UI list and remove them.

THE MODEL
- Kinds:
  - `temporary`: removed whole (container, volume, network, state) when it
    expires, default 24 h after the last use;
  - `project`: kept across restarts, packages stay installed. Stopped after
    `sandbox_env_idle_stop_minutes` without a running process, never removed
    unasked;
  - the scratch environment: exactly one per person, fixed name, for
    python_sandbox and run_tests. It extends itself on use and is exempt from the
    count and memory limits, because those lanes must keep answering.
- Names are `vaf-env-<scope hash>-<id>`, `vaf-env-vol-<id>`, `vaf-env-net-<id>`.
  The scope hash (containers.scope_hash) keeps a listing from saying who uses the
  machine.
- Docker LABELS on all three carry the identity (`org.veyllo.vaf.env.*`: id, owner,
  kind, network, created). Labels cannot change after creation, so what changes
  (name, expiry, last use, the project path) lives in a state file per environment
  under `<vaf dir>/environments/`, written atomically under a cross-process lock:
  the web server, the CLI and a coder child each run their own manager.
- Ownership is the owner label, checked on every operation. An admin may list and
  delete another person's environment, never run anything in it.
- A missing scope means the machine owner, the one definition everywhere in VAF
  (config.resolve_caller_username); no owner configured means refused. The old
  sandbox's shared "no scope" bucket does not come back.

ISOLATION, measured on Docker 29.7 before this was written
- Every environment runs non-root (the caller's uid:gid on Linux, so files in a
  mounted project stay theirs; the image's uid 10001 elsewhere), with `--init`,
  every capability dropped, `no-new-privileges`, a pids limit, memory and CPU limits.
  No docker socket and no host path besides the project are ever mounted, and no host
  secret is passed in.
- Network profiles, each on the environment's own network:
  - `none`: an `--internal` network created with
    `com.docker.network.bridge.gateway_mode_ipv4=isolated`. A plain internal network
    still reached the host through its own gateway (measured: VAF's port 8443
    answered from one); with the isolated gateway mode the host was unreachable on
    every address. That mode needs Docker 28; on an older engine the network is
    created without it and the environment says so (`degraded`).
  - `registries`: the same isolated network plus the shared proxy `vaf-env-proxy`
    (tinyproxy from the environment image, `FilterDefaultDeny`, anchored host
    patterns from `sandbox_env_registry_hosts`, CONNECT to ports 443 and 80 only).
    Measured: pip and npm installed through it, while example.com, an IP literal
    and a look-alike host were refused with 403 and the direct route was closed.
  - `open`: an ordinary bridge with internet access.
  - scratch: open, plus `host.docker.internal` for the Tool Bridge, which is how
    python_sandbox always worked.
- NAMED BOUNDARIES, also said in the tools' descriptions:
  - `open` is not "internet only": the gateway reaches whatever listens on the host's
    0.0.0.0 (the Tool Bridge, token-protected; VAF in LAN mode, login-protected), and
    the LAN is reachable.
  - `registries` does not stop data leaving through the allowed hosts themselves
    (`npm publish`, a push with a token the code brought along).
  - No disk quota: overlay2 has none without xfs project quotas. The count of
    environments per person is limited and their size is shown.

LIFECYCLE
- The reaper (a thread in the long-lived process; `start_reaper`) removes expired
  temporary environments and stops idle project ones. "Busy" is asked of docker - a
  process carrying a VAF run or process marker inside the container - never of this
  process's memory, because another process may be the one using it. Removal is
  idempotent: several reapers may race.
- VAF's quit stops every environment that is not busy (`stop_all_at_quit`); they are
  started with `docker run`, so the stack's `compose stop` never sees them.
- An account whose access is taken away has its environments stopped (a revocation
  listener).
"""

from __future__ import annotations

import io
import json
import os
import posixpath
import re
import secrets
import tarfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from vaf.core import containers, environment_image

LABEL = "org.veyllo.vaf.env"
KINDS = ("temporary", "project")
SCRATCH_KIND = "scratch"
# Ordered by how much reaches out; `sandbox_env_network_max` caps along this order.
NETWORKS = ("none", "registries", "open")
WORKSPACE = "/workspace"
IMAGE_UID = "10001:10001"
PROXY_CONTAINER = "vaf-env-proxy"
PROXY_NETWORK = "vaf-env-proxy-out"
PROXY_PORT = 8888
ISOLATED_GATEWAY = {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
READ_LIMIT_BYTES = 200_000
PROC_DIR = "/tmp/vaf-env-proc"
_PROC_ID_RE = re.compile(r"^p[0-9a-f]{8}$")
TRANSFER_LIMIT_BYTES = 200 * 1024 * 1024

# The defaults live here as well as in Config.DEFAULTS: an embedder building on the
# facade without a config file still gets working limits. A guard test pins the two
# copies together.
DEFAULTS: Dict[str, Any] = {
    "max_per_user": 3,
    "memory_mb": 1024,
    "memory_max_mb": 4096,
    "cpus": 1.0,
    "pids": 512,
    "temp_ttl_hours": 24,
    "idle_stop_minutes": 30,
    "min_free_mb": 1500,
    "process_max_hours": 8,
    "network_max": "open",
    "registry_hosts": [
        "pypi.org", "files.pythonhosted.org",
        "registry.npmjs.org", "registry.yarnpkg.com",
        "github.com", "codeload.github.com", "objects.githubusercontent.com",
    ],
}
SCRATCH_MEMORY_MB = 512
SCRATCH_CPUS = 0.5

_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")


def housekeeping_off() -> bool:
    """`VAF_SANDBOX_ENV_HOUSEKEEPING_OFF`: no reaper thread, no stop on quit or
    revocation. The test suite sets it session-wide: a reaper there would judge the
    developer's REAL environments against the suite's scratch state directory and clear
    their volumes as orphans."""
    return str(os.environ.get("VAF_SANDBOX_ENV_HOUSEKEEPING_OFF", "")).strip().lower() in ("1", "true", "yes")


class EnvironmentRefused(Exception):
    """An operation that cannot be done, with the reason a person can act on."""


def setting(name: str) -> Any:
    """One limit: `VAF_SANDBOX_ENV_<NAME>` first, then the admin-only config key
    `sandbox_env_<name>`, then DEFAULTS. Typed like the default; a value that does
    not parse falls back to the default rather than switching a limit off."""
    default = DEFAULTS[name]
    raw: Any = os.environ.get(f"VAF_SANDBOX_ENV_{name.upper()}")
    if raw is None or str(raw).strip() == "":
        try:
            from vaf.core.config import Config
            raw = Config.get(f"sandbox_env_{name}")
        except Exception:
            raw = None
    if raw is None:
        return default
    try:
        if isinstance(default, bool):
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
        if isinstance(default, list):
            if isinstance(raw, str):
                raw = [p for p in re.split(r"[,\s]+", raw) if p]
            return [str(x).strip() for x in raw if str(x).strip()]
        return str(raw).strip()
    except Exception:
        return default


def resolve_owner(scope: Any) -> str:
    """The scope an environment belongs to. None or blank means the machine owner;
    with no owner configured there is nobody to give it to."""
    s = str(scope or "").strip()
    if s:
        return s
    try:
        from vaf.core.config import get_local_admin_scope_id
        owner = str(get_local_admin_scope_id() or "").strip()
    except Exception:
        owner = ""
    if not owner:
        raise EnvironmentRefused("no account to own the environment (no scope, and no "
                                 "local owner is configured)")
    return owner


@dataclass
class Environment:
    id: str
    kind: str
    network: str
    owner: str                      # scope hash, never the scope
    container: str
    volume: str
    net: str
    name: str = ""
    project_path: str = ""
    image: str = ""
    created: float = 0.0
    expires: float = 0.0            # 0: does not expire
    last_used: float = 0.0
    memory_mb: int = 0
    session_id: str = ""
    degraded: str = ""
    state: str = ""                 # docker's view, filled in when listed

    def describe(self) -> str:
        bits = [f"{self.id}", f"kind={self.kind}", f"network={self.network}",
                f"state={self.state or 'unknown'}"]
        if self.name:
            bits.insert(1, f"name={self.name!r}")
        if self.project_path:
            bits.append(f"project={self.project_path}")
        if self.expires:
            left = max(0, int((self.expires - time.time()) / 3600))
            bits.append(f"expires in ~{left} h")
        if self.degraded:
            bits.append(f"degraded: {self.degraded}")
        return ", ".join(bits)


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


def _host_user() -> str:
    """The uid:gid an environment runs as. The caller's own on a POSIX host, so files
    written into a mounted project belong to them; the image's user elsewhere (Docker
    Desktop maps ownership on its own)."""
    if hasattr(os, "getuid") and hasattr(os, "getgid") and os.name == "posix":
        return f"{os.getuid()}:{os.getgid()}"
    return IMAGE_UID


def _container_path(path: str) -> str:
    """A path inside the environment: relative paths resolve against /workspace.
    posixpath on purpose - the container is Linux whatever the host is."""
    p = str(path or "").strip() or "."
    return posixpath.normpath(p if p.startswith("/") else posixpath.join(WORKSPACE, p))


class EnvironmentManager:
    """Creates, finds, runs in and removes sandbox environments. One per process
    (`get_environment_manager`); state shared through docker and the state files."""

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self._state_dir_override = Path(state_dir) if state_dir else None
        self._thread_lock = threading.RLock()
        self._reaper_alive = False
        self._closing = False

    # -- state files ---------------------------------------------------------
    def _state_dir(self) -> Path:
        if self._state_dir_override is not None:
            d = self._state_dir_override
        else:
            from vaf.core.platform import Platform
            d = Path(Platform.vaf_dir()) / "environments"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _file_lock(self):
        from vaf.core.secure_store import _get_filelock_cls
        cls = _get_filelock_cls()
        if cls is None:
            return None
        try:
            return cls(str(self._state_dir() / ".lock"), timeout=30)
        except Exception:
            return None

    def _locked(self):
        manager = self

        class _Ctx:
            def __enter__(self_inner):
                manager._thread_lock.acquire()
                self_inner.lock = manager._file_lock()
                if self_inner.lock is not None:
                    self_inner.lock.acquire()
                return self_inner

            def __exit__(self_inner, *exc):
                try:
                    if self_inner.lock is not None:
                        self_inner.lock.release()
                finally:
                    manager._thread_lock.release()
                return False

        return _Ctx()

    def _read_state(self, env_id: str) -> Optional[Dict[str, Any]]:
        if not _ID_RE.match(env_id or ""):
            return None
        try:
            return json.loads((self._state_dir() / f"{env_id}.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_state(self, env: Environment) -> None:
        data = asdict(env)
        data.pop("state", None)
        path = self._state_dir() / f"{env.id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        try:
            from vaf.core.secure_store import harden_path
            harden_path(path)
        except Exception:
            pass

    def _delete_state(self, env_id: str) -> None:
        for suffix in (".json", ".procs.json"):
            try:
                (self._state_dir() / f"{env_id}{suffix}").unlink()
            except FileNotFoundError:
                pass

    # Background processes: the RECORD (who started it, from which chat, when) is kept
    # here on the host, because the wake turn needs the person's identity and code in the
    # environment must not be able to rewrite it; WHETHER it runs, its exit code and its
    # output are read from the container (a VAF_PROC_ID marker, files under PROC_DIR), so
    # the web server, the CLI and a coder child all see the same truth.
    def _read_procs(self, env_id: str) -> Dict[str, Dict[str, Any]]:
        if not _ID_RE.match(env_id or ""):
            return {}
        try:
            data = json.loads((self._state_dir() / f"{env_id}.procs.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_procs(self, env_id: str, procs: Dict[str, Dict[str, Any]]) -> None:
        path = self._state_dir() / f"{env_id}.procs.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(procs, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        try:
            from vaf.core.secure_store import harden_path
            harden_path(path)
        except Exception:
            pass

    # -- docker views ----------------------------------------------------------
    def _labels(self, env: Environment) -> Dict[str, str]:
        return {
            LABEL: "1",
            f"{LABEL}.id": env.id,
            f"{LABEL}.owner": env.owner,
            f"{LABEL}.kind": env.kind,
            f"{LABEL}.network": env.network,
            f"{LABEL}.created": str(int(env.created)),
        }

    def _docker_rows(self, owner_hash: Optional[str] = None, *,
                     strict: bool = False) -> List[Dict[str, str]]:
        """Every environment container docker knows, optionally one owner's. A docker
        that does not answer reads as none, except with `strict`, which raises instead:
        the reaper must not take an outage for an empty machine and clear every record
        as an orphan."""
        args = ["ps", "-a", "--filter", f"label={LABEL}=1"]
        if owner_hash:
            args += ["--filter", f"label={LABEL}.owner={owner_hash}"]
        args += ["--format", "{{.Names}}\t{{.State}}\t{{.Label \"" + LABEL + ".id\"}}\t"
                 "{{.Label \"" + LABEL + ".owner\"}}\t{{.Label \"" + LABEL + ".kind\"}}\t"
                 "{{.Label \"" + LABEL + ".network\"}}"]
        r = containers.docker(args, timeout=30)
        if r.returncode != 0:
            if strict:
                raise EnvironmentRefused(f"docker did not list the environments: "
                                         f"{(r.stderr or '').strip()[:200]}")
            return []
        rows = []
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 6 and parts[2]:
                rows.append(dict(zip(("container", "state", "id", "owner", "kind", "network"), parts)))
        return rows

    def _from_row(self, row: Dict[str, str]) -> Environment:
        st = self._read_state(row["id"]) or {}
        env = Environment(
            id=row["id"], kind=row["kind"], network=row["network"], owner=row["owner"],
            container=row["container"],
            volume=st.get("volume", ""), net=st.get("net", ""),
        )
        for key in ("name", "project_path", "image", "created", "expires", "last_used",
                    "memory_mb", "session_id", "degraded"):
            if key in st:
                setattr(env, key, st[key])
        env.state = row["state"]
        return env

    # -- finding ---------------------------------------------------------------
    def list(self, owner_scope: Any = None, *, everyone: bool = False) -> List[Environment]:
        """One person's environments, or with `everyone` all of them (the admin's view;
        the caller decides who may ask for it)."""
        owner_hash = None if everyone else containers.scope_hash(resolve_owner(owner_scope))
        return [self._from_row(r) for r in self._docker_rows(owner_hash)]

    def get(self, owner_scope: Any, env_id: str, *, admin: bool = False) -> Environment:
        """The caller's environment by id. Someone else's answers like a missing one,
        so an id cannot be probed; `admin` lets an admin reach it to stop or delete."""
        env_id = str(env_id or "").strip()
        if not _ID_RE.match(env_id):
            raise EnvironmentRefused(f"no environment {env_id!r}")
        owner_hash = containers.scope_hash(resolve_owner(owner_scope))
        for row in self._docker_rows(None if admin else owner_hash):
            if row["id"] == env_id:
                return self._from_row(row)
        raise EnvironmentRefused(f"no environment {env_id!r}")

    @staticmethod
    def _same_dir(a: str, b: str) -> bool:
        try:
            return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
        except Exception:
            return False

    def find_for_project(self, owner_scope: Any, project_dir: str) -> Optional[Environment]:
        """The caller's project environment whose /workspace is this host directory, or
        None. Compared on resolved, case-normalised paths (a symlinked home, Windows)."""
        try:
            envs = self.list(owner_scope)
        except EnvironmentRefused:
            return None
        for env in envs:
            if env.kind == "project" and env.project_path and self._same_dir(env.project_path, project_dir):
                return env
        return None

    def bind_for_project(self, owner_scope: Any, project_dir: str,
                         env_id: Optional[str] = None) -> Optional[Environment]:
        """The environment a coder run in `project_dir` works in. With `env_id`, that one,
        which must be the caller's project environment mounted at exactly this directory
        (refused otherwise: a bind mount is fixed when the container is made, so a run
        elsewhere would edit files the environment never sees). Without it, the caller's
        project environment for this directory if there is one, else None."""
        if not env_id:
            return self.find_for_project(owner_scope, project_dir)
        env = self.get(owner_scope, env_id)
        if env.kind != "project" or not env.project_path:
            raise EnvironmentRefused(f"environment {env_id} has no project folder; create one "
                                     f"with kind='project' and project_path")
        if not self._same_dir(env.project_path, project_dir):
            raise EnvironmentRefused(f"environment {env_id} works in {env.project_path}, not in "
                                     f"{project_dir}")
        return env

    # -- creating ----------------------------------------------------------------
    def _network_rank(self, network: str) -> int:
        return NETWORKS.index(network)

    def create(self, owner_scope: Any, *, kind: str = "temporary", name: str = "",
               project_path: Optional[str] = None, network: Optional[str] = None,
               memory_mb: Optional[int] = None, session_id: str = "",
               user_role: Optional[str] = None, wait_for_image: bool = False) -> Environment:
        """A new temporary or project environment. Refused, with the reason, when a
        limit says no, the network profile is above the admin's cap, the project path
        is not one this person may mount, or the image is not built yet (it then starts
        building; `wait_for_image` waits for it instead - the CLI does)."""
        owner_scope = resolve_owner(owner_scope)
        if kind not in KINDS:
            raise EnvironmentRefused(f"kind must be one of {', '.join(KINDS)}")
        network = (network or ("registries" if kind == "project" else "none")).strip()
        if network not in NETWORKS:
            raise EnvironmentRefused(f"network must be one of {', '.join(NETWORKS)}")
        cap = setting("network_max")
        if cap in NETWORKS and self._network_rank(network) > self._network_rank(cap):
            raise EnvironmentRefused(f"network {network!r} is above what the administrator "
                                     f"allows ({cap!r})")
        mem = int(memory_mb or setting("memory_mb"))
        if mem < 128 or mem > int(setting("memory_max_mb")):
            raise EnvironmentRefused(f"memory must be between 128 and "
                                     f"{setting('memory_max_mb')} MB")
        owner_hash = containers.scope_hash(owner_scope)
        mine = [r for r in self._docker_rows(owner_hash) if r["kind"] in KINDS]
        if len(mine) >= int(setting("max_per_user")):
            raise EnvironmentRefused(f"you have {len(mine)} environments, the limit is "
                                     f"{setting('max_per_user')}: delete one first")
        free = containers.mem_available_mb()
        if free is not None and free < int(setting("min_free_mb")):
            raise EnvironmentRefused(f"not enough free memory ({free} MB, the floor is "
                                     f"{setting('min_free_mb')} MB)")
        mount = self._project_mount(project_path, owner_scope, user_role)
        image = environment_image.image_tag()
        if not environment_image.image_present(image):
            if wait_for_image:
                image = environment_image.ensure_image()
            else:
                environment_image.start_background_build()
                image = None
            if not image:
                raise EnvironmentRefused("the environment image is not built yet; it is being "
                                         "built now (the first time takes a few minutes)")
        env_id = secrets.token_hex(4)
        now = time.time()
        env = Environment(
            id=env_id, kind=kind, network=network, owner=owner_hash,
            container=f"vaf-env-{owner_hash}-{env_id}",
            volume="" if mount else f"vaf-env-vol-{env_id}",
            net=f"vaf-env-net-{env_id}",
            name=str(name or "")[:80], project_path=mount or "", image=image,
            created=now, last_used=now, memory_mb=mem, session_id=str(session_id or ""),
            expires=now + float(setting("temp_ttl_hours")) * 3600 if kind == "temporary" else 0.0,
        )
        with self._locked():
            self._write_state(env)            # first, so a crash leaves a findable record
        try:
            self._start_new(env, memory_mb=mem, cpus=float(setting("cpus")))
        except Exception:
            self._remove(env)
            raise
        return env

    def _project_mount(self, project_path: Optional[str], owner_scope: str,
                       user_role: Optional[str]) -> str:
        """The resolved project directory to mount at /workspace, or "" for none."""
        if not project_path:
            return ""
        from vaf.core.workspace_guard import assert_safe_workspace, is_unsafe_project_dir
        p = os.path.realpath(os.path.expanduser(str(project_path)))
        if not os.path.isdir(p):
            raise EnvironmentRefused(f"{project_path} is not a directory")
        if is_unsafe_project_dir(p):
            raise EnvironmentRefused(f"{p} cannot be mounted: it is the home directory, a "
                                     f"standard folder itself, VAF's own data or VAF's code")
        try:
            assert_safe_workspace(p)
        except ValueError as e:
            raise EnvironmentRefused(str(e))
        try:
            from vaf.tools.filesystem import jail_allows
            allowed = jail_allows(p, user_scope_id=owner_scope, user_role=user_role, mode="write")
        except Exception:
            allowed = False
        if not allowed:
            raise EnvironmentRefused(f"{p} is outside your own project folder")
        return p

    def _start_new(self, env: Environment, *, memory_mb: int, cpus: float,
                   host_gateway: bool = False) -> None:
        labels = self._labels(env)
        if env.network in ("none", "registries"):
            ok = containers.ensure_network(env.net, internal=True, labels=labels,
                                           options=ISOLATED_GATEWAY)
            if not ok:
                # Docker before 28 knows no isolated gateway mode: the network still has
                # no route out, but the host answers on its gateway address.
                ok = containers.ensure_network(env.net, internal=True, labels=labels)
                if ok:
                    env.degraded = ("this Docker has no isolated gateway mode (Docker 28 or "
                                    "newer): the host is reachable on the network's gateway")
        else:
            ok = containers.ensure_network(env.net, labels=labels)
        if not ok:
            raise EnvironmentRefused("the environment's network could not be created")
        if env.volume:
            r = containers.docker(["volume", "create", *self._label_args(labels), env.volume],
                                  timeout=30)
            if r.returncode != 0:
                raise EnvironmentRefused(f"the environment's volume could not be created: "
                                         f"{(r.stderr or '').strip()[:200]}")
        built = env.image != environment_image.FALLBACK_IMAGE
        args = [
            "run", "-d", "--name", env.container, "--hostname", "sandbox",
            *self._label_args(labels),
            "--init", "--restart", "no",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", str(int(setting("pids"))),
            "--memory", f"{int(memory_mb)}m", "--cpus", str(cpus),
            "--network", env.net,
            "--user", _host_user(),
            "-e", f"HOME={'/home/sandbox' if built else '/tmp'}",
        ]
        if env.project_path:
            # :z relabels the project for SELinux (enforcing on this kind of host);
            # assert_safe_workspace already refused HOME and /, which must never be
            # relabelled.
            args += ["-v", f"{env.project_path}:{WORKSPACE}:z"]
        elif env.volume:
            args += ["-v", f"{env.volume}:{WORKSPACE}"]
        if env.network == "registries":
            proxy = f"http://{PROXY_CONTAINER}:{PROXY_PORT}"
            for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                args += ["-e", f"{var}={proxy}"]
            args += ["-e", "NO_PROXY=localhost,127.0.0.1", "-e", "no_proxy=localhost,127.0.0.1"]
        if host_gateway:
            args += ["--add-host", "host.docker.internal:host-gateway"]
        args += ["-w", WORKSPACE if (env.project_path or env.volume) else "/tmp",
                 env.image, "sleep", "infinity"]
        r = containers.docker(args, timeout=120)
        if r.returncode != 0:
            raise EnvironmentRefused(f"the environment could not be started: "
                                     f"{(r.stderr or '').strip()[:300]}")
        env.state = "running"
        if env.network == "registries":
            self._attach_proxy(env)
        with self._locked():
            self._write_state(env)

    @staticmethod
    def _label_args(labels: Dict[str, str]) -> List[str]:
        out: List[str] = []
        for key, value in labels.items():
            out += ["--label", f"{key}={value}"]
        return out

    # -- the registries proxy ------------------------------------------------------
    @staticmethod
    def _proxy_filter(hosts: List[str]) -> str:
        """Anchored, escaped host patterns, one per line, for tinyproxy's filter."""
        lines = []
        for h in hosts:
            h = h.strip().lower()
            if re.fullmatch(r"[a-z0-9.-]+", h) and "." in h:
                lines.append("^" + re.escape(h) + "$")
        return "\n".join(lines)

    def _proxy_hash(self) -> str:
        import hashlib
        return hashlib.sha256(self._proxy_filter(setting("registry_hosts")).encode()).hexdigest()[:12]

    def _ensure_proxy(self) -> None:
        """The one shared proxy, on an open network of its own, recreated when the
        allowed hosts changed. Runs from the environment image as its non-root user."""
        want = self._proxy_hash()
        r = containers.docker(["inspect", PROXY_CONTAINER, "--format",
                               "{{.State.Status}}\t{{index .Config.Labels \"" + LABEL + ".proxy\"}}"],
                              timeout=20)
        if r.returncode == 0:
            status, _, have = (r.stdout or "").strip().partition("\t")
            if have == want and status == "running":
                return
            if have == want and status == "exited":
                if containers.docker(["start", PROXY_CONTAINER], timeout=60).returncode == 0:
                    return
            containers.docker(["rm", "-f", PROXY_CONTAINER], timeout=60)
        image = environment_image.image_tag()
        if not environment_image.image_present(image):
            raise EnvironmentRefused("the registries network needs the environment image, "
                                     "which is not built yet")
        if not containers.ensure_network(PROXY_NETWORK, labels={LABEL: "1", f"{LABEL}.proxy": want}):
            raise EnvironmentRefused("the proxy's network could not be created")
        conf = "\n".join([
            f"Port {PROXY_PORT}", "Listen 0.0.0.0", "Timeout 120", "MaxClients 100",
            "Allow 0.0.0.0/0", "ConnectPort 443", "ConnectPort 80",
            'Filter "/tmp/vaf-proxy/filter"', "FilterDefaultDeny Yes",
            "FilterExtended Yes", "FilterCaseSensitive No", "LogLevel Connect",
        ])
        script = ('mkdir -p /tmp/vaf-proxy && printf "%s\\n" "$VAF_PROXY_CONF" > /tmp/vaf-proxy/conf '
                  '&& printf "%s\\n" "$VAF_PROXY_FILTER" > /tmp/vaf-proxy/filter '
                  '&& exec tinyproxy -d -c /tmp/vaf-proxy/conf')
        args = ["run", "-d", "--name", PROXY_CONTAINER, "--restart", "no", "--init",
                "--label", f"{LABEL}=1", "--label", f"{LABEL}.proxy={want}",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--memory", "128m", "--pids-limit", "128",
                "--network", PROXY_NETWORK,
                "-e", "VAF_PROXY_CONF", "-e", "VAF_PROXY_FILTER",
                image, "sh", "-c", script]
        env_values = {**os.environ, "VAF_PROXY_CONF": conf,
                      "VAF_PROXY_FILTER": self._proxy_filter(setting("registry_hosts"))}
        r = containers.docker(args, timeout=120, env=env_values)
        if r.returncode != 0:
            raise EnvironmentRefused(f"the proxy could not be started: {(r.stderr or '').strip()[:200]}")
        # A new proxy container sits on none of the registries networks the old one had
        # joined: every other registries environment would lose its package access.
        self._reconnect_registries()

    def _attach_proxy(self, env: Environment) -> None:
        self._ensure_proxy()
        r = containers.docker(["network", "connect", env.net, PROXY_CONTAINER], timeout=30)
        if r.returncode != 0 and "already exists" not in (r.stderr or ""):
            raise EnvironmentRefused(f"the proxy could not join the environment's network: "
                                     f"{(r.stderr or '').strip()[:200]}")

    def _reconnect_registries(self) -> None:
        """After the proxy was (re)created, every registries environment needs it back
        on its network."""
        for row in self._docker_rows():
            if row["network"] == "registries":
                st = self._read_state(row["id"]) or {}
                if st.get("net"):
                    containers.docker(["network", "connect", st["net"], PROXY_CONTAINER], timeout=30)

    # -- running -------------------------------------------------------------------
    def _ensure_running(self, env: Environment) -> None:
        if env.state == "running":
            return
        if env.kind != SCRATCH_KIND:
            free = containers.mem_available_mb()
            if free is not None and free < int(setting("min_free_mb")):
                raise EnvironmentRefused(f"not enough free memory to start it ({free} MB, the "
                                         f"floor is {setting('min_free_mb')} MB)")
        r = containers.docker(["start", env.container], timeout=60)
        if r.returncode != 0:
            raise EnvironmentRefused(f"the environment could not be started: "
                                     f"{(r.stderr or '').strip()[:200]}")
        env.state = "running"
        if env.network == "registries":
            self._attach_proxy(env)

    def _touch(self, env: Environment) -> None:
        env.last_used = time.time()
        if env.kind in ("temporary", SCRATCH_KIND):
            env.expires = env.last_used + float(setting("temp_ttl_hours")) * 3600
        with self._locked():
            if self._read_state(env.id) is not None:
                self._write_state(env)

    def stop(self, owner_scope: Any, env_id: str, *, admin: bool = False) -> Environment:
        env = self.get(owner_scope, env_id, admin=admin)
        containers.docker(["stop", "-t", "5", env.container], timeout=60)
        env.state = "exited"
        return env

    def delete(self, owner_scope: Any, env_id: str, *, admin: bool = False) -> Environment:
        """Container, volume, network and state, all of them. Idempotent: what is
        already gone is skipped, so racing reapers and a person clicking twice agree."""
        env = self.get(owner_scope, env_id, admin=admin)
        self._remove(env)
        return env

    def _remove(self, env: Environment) -> None:
        containers.docker(["rm", "-f", env.container], timeout=60)
        if env.network == "registries" and env.net:
            containers.docker(["network", "disconnect", "-f", env.net, PROXY_CONTAINER], timeout=30)
        if env.volume:
            containers.docker(["volume", "rm", "-f", env.volume], timeout=60)
        if env.net:
            containers.docker(["network", "rm", env.net], timeout=30)
        with self._locked():
            self._delete_state(env.id)

    # -- the scratch environment ---------------------------------------------------
    def scratch_for(self, owner_scope: Any) -> Environment:
        """This person's scratch environment, created or started on demand. Fixed
        name, so concurrent callers in different processes converge on one container
        (the loser of a creation race adopts the winner's). Runs on the built image when
        it exists, else on the fallback image."""
        owner_scope = resolve_owner(owner_scope)
        owner_hash = containers.scope_hash(owner_scope)
        env_id = f"s-{owner_hash}"
        for row in self._docker_rows(owner_hash):
            if row["id"] == env_id:
                env = self._from_row(row)
                if self._read_state(env_id) is None:
                    # Adopted from a process whose record is gone: write one, or the
                    # reaper would never see this scratch environment expire.
                    env.net = env.net or f"vaf-env-net-{env_id}"
                    env.name, env.memory_mb = "scratch", SCRATCH_MEMORY_MB
                    env.created = env.created or time.time()
                    with self._locked():
                        self._write_state(env)
                self._ensure_running(env)
                self._touch(env)
                return env
        now = time.time()
        env = Environment(
            id=env_id, kind=SCRATCH_KIND, network="open", owner=owner_hash,
            container=f"vaf-env-{owner_hash}-scratch", volume="",
            net=f"vaf-env-net-{env_id}", name="scratch",
            image=environment_image.usable_image(), created=now, last_used=now,
            memory_mb=SCRATCH_MEMORY_MB,
            expires=now + float(setting("temp_ttl_hours")) * 3600,
        )
        with self._locked():
            self._write_state(env)
        try:
            self._start_new(env, memory_mb=SCRATCH_MEMORY_MB, cpus=SCRATCH_CPUS,
                            host_gateway=True)
        except EnvironmentRefused:
            if containers.container_state(env.container) is None:
                self._remove(env)
                raise
            # Another process created it in the meantime: use theirs.
            self._ensure_running(env)
        return env

    # -- working in it ---------------------------------------------------------------
    def _workdir(self, env: Environment, cwd: Optional[str]) -> str:
        if cwd:
            return _container_path(cwd)
        return WORKSPACE if (env.project_path or env.volume) else "/tmp"

    def exec_in(self, env: Environment, argv: List[str], *, timeout: float = 120,
                cwd: Optional[str] = None, env_values: Optional[Dict[str, str]] = None,
                check_stop: Optional[Callable[[], bool]] = None,
                input_text: Optional[str] = None, run_id: Optional[str] = None) -> ExecResult:
        """Run argv in an environment the caller already holds (ownership checked by
        whoever fetched it). Bounded twice: `timeout -s KILL` inside the container, and
        a backstop here that also kills the run's own processes by their marker - the
        docker client dying does not stop them."""
        self._ensure_running(env)
        run_id = run_id or secrets.token_hex(6)
        rc, out, err, timed_out, cancelled = containers.exec_bounded(
            env.container, argv, timeout=timeout, workdir=self._workdir(env, cwd),
            env_values=env_values, run_id=run_id, check_stop=check_stop,
            input_text=input_text)
        self._touch(env)
        return ExecResult(rc, out, err, timed_out=timed_out, cancelled=cancelled,
                          extra={"run_id": run_id})

    def exec(self, owner_scope: Any, env_id: str, command: str, *, timeout: float = 120,
             cwd: Optional[str] = None, env_values: Optional[Dict[str, str]] = None,
             check_stop: Optional[Callable[[], bool]] = None) -> ExecResult:
        """A shell command in one of the caller's environments. Never someone else's,
        admin or not."""
        env = self.get(owner_scope, env_id)
        return self.exec_in(env, ["sh", "-c", command], timeout=timeout, cwd=cwd,
                            env_values=env_values, check_stop=check_stop)

    def read_file(self, owner_scope: Any, env_id: str, path: str,
                  max_bytes: int = READ_LIMIT_BYTES) -> str:
        env = self.get(owner_scope, env_id)
        p = _container_path(path)
        r = self.exec_in(env, ["head", "-c", str(int(max_bytes) + 1), "--", p], timeout=30)
        if r.returncode != 0:
            raise EnvironmentRefused(f"cannot read {p}: {(r.stderr or '').strip()[:200]}")
        text = r.stdout
        if len(text.encode("utf-8", errors="replace")) > max_bytes:
            text = text[:max_bytes] + f"\n[... truncated at {max_bytes} bytes]"
        return text

    def write_file(self, owner_scope: Any, env_id: str, path: str, content: str) -> str:
        env = self.get(owner_scope, env_id)
        p = _container_path(path)
        r = self.exec_in(env, ["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", p],
                         timeout=60, input_text=str(content))
        if r.returncode != 0:
            raise EnvironmentRefused(f"cannot write {p}: {(r.stderr or '').strip()[:200]}")
        return p

    def list_files(self, owner_scope: Any, env_id: str, path: str = ".", depth: int = 2) -> str:
        env = self.get(owner_scope, env_id)
        p = _container_path(path)
        depth = max(1, min(int(depth), 6))
        r = self.exec_in(env, ["find", p, "-maxdepth", str(depth), "-not", "-path", "*/.git/*",
                               "-printf", "%y %10s %P\\n"], timeout=30)
        if r.returncode != 0:
            raise EnvironmentRefused(f"cannot list {p}: {(r.stderr or '').strip()[:200]}")
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        return "\n".join(lines[:500]) + ("\n[... more entries]" if len(lines) > 500 else "")

    def copy_in(self, owner_scope: Any, env_id: str, host_path: str, dest: str = ".") -> str:
        """A host file or folder into the environment, owned by the environment's user.
        The caller vouches for the host path (the tool layer runs it through the
        person's file jail); this refuses a path that is not a file or a folder."""
        env = self.get(owner_scope, env_id)
        src = Path(host_path)
        if not (src.is_file() or src.is_dir()):
            raise EnvironmentRefused(f"{host_path} is not a file or a folder")
        buf = io.BytesIO()
        total = 0
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for item in ([src] if src.is_file() else [src, *sorted(src.rglob("*"))]):
                if item.is_symlink():
                    continue
                if item.is_file():
                    total += item.stat().st_size
                    if total > TRANSFER_LIMIT_BYTES:
                        raise EnvironmentRefused(f"{host_path} is larger than "
                                                 f"{TRANSFER_LIMIT_BYTES // (1024 * 1024)} MB")
                arc = item.name if item == src else str(Path(src.name) / item.relative_to(src))
                tar.add(str(item), arcname=arc.replace(os.sep, "/"), recursive=False)
        target = _container_path(dest)
        r = containers.docker(["exec", "-i", env.container, "sh", "-c",
                               'mkdir -p "$1" && tar -xf - -C "$1" --no-same-owner', "sh", target],
                              timeout=300, input=buf.getvalue(), binary=True)
        if r.returncode != 0:
            raise EnvironmentRefused(f"copy into the environment failed: "
                                     f"{(r.stderr or b'').decode(errors='replace').strip()[:200]}")
        self._touch(env)
        return posixpath.join(target, src.name)

    def copy_out(self, owner_scope: Any, env_id: str, path: str, host_dir: str) -> List[str]:
        """A file or folder out of the environment into a host folder. Only regular
        files and folders arrive: a link, a device or a name that climbs out of the
        target is dropped, so code in the environment cannot plant a pointer to a host
        file (docker cp would have copied a link as a link)."""
        env = self.get(owner_scope, env_id)
        p = _container_path(path)
        parent, base = posixpath.split(p)
        r = containers.docker(["exec", env.container, "tar", "-cf", "-", "-C", parent or "/", base],
                              timeout=300, binary=True)
        if r.returncode != 0:
            raise EnvironmentRefused(f"cannot copy {p}: "
                                     f"{(r.stderr or b'').decode(errors='replace').strip()[:200]}")
        if len(r.stdout) > TRANSFER_LIMIT_BYTES:
            raise EnvironmentRefused(f"{p} is larger than {TRANSFER_LIMIT_BYTES // (1024 * 1024)} MB")
        dest_root = Path(host_dir).resolve()
        dest_root.mkdir(parents=True, exist_ok=True)
        written: List[str] = []
        with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r") as tar:
            for member in tar.getmembers():
                if not (member.isfile() or member.isdir()):
                    continue
                target = (dest_root / member.name).resolve()
                if target != dest_root and dest_root not in target.parents:
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink():
                    target.unlink()
                src = tar.extractfile(member)
                if src is None:
                    continue
                with open(target, "wb") as f:
                    f.write(src.read())
                written.append(str(target))
        self._touch(env)
        return written

    # -- looking at what it serves ---------------------------------------------------------
    def render(self, owner_scope: Any, env_id: str, target: str, *, width: int = 1280,
               height: int = 800, wait_ms: int = 1500) -> Dict[str, Any]:
        """One look at a page, from INSIDE the environment: `target` is a URL (localhost
        is the environment itself, so a dev server bound to 127.0.0.1 works) or a path
        under /workspace. chromium-headless-shell in the environment's own container
        takes the screenshot, logs the console and dumps the DOM; no browser container
        joins the environment's network, nothing is published, no CDP port exists.

        Returns the dict render_check formats: ok, url, title, page_errors, console,
        failed_requests (None: not measured this way), text, screenshot_b64,
        screenshot_ext ("png")."""
        import base64
        env = self.get(owner_scope, env_id)
        url = self._render_url(target)
        width = min(max(320, int(width)), 3840)
        height = min(max(240, int(height)), 2160)
        budget = min(max(0, int(wait_ms)), 10000)
        run = secrets.token_hex(6)
        out_dir = f"/tmp/vaf-preview/{run}"
        common = ["chromium-headless-shell", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                  f"--virtual-time-budget={budget}", "--no-first-run"]
        try:
            mk = self.exec_in(env, ["mkdir", "-p", out_dir], timeout=20)
            if mk.returncode != 0:
                raise EnvironmentRefused(f"the preview could not be prepared: {mk.stderr.strip()[:200]}")
            shot = self.exec_in(env, [*common, f"--user-data-dir={out_dir}/p1", "--enable-logging=stderr",
                                      "--v=0", f"--window-size={width},{height}",
                                      f"--screenshot={out_dir}/shot.png", url], timeout=60)
            if shot.returncode == 127 or "not found" in (shot.stderr or "")[:400]:
                raise EnvironmentRefused("this environment has no browser for previews (it runs on "
                                         "the fallback image); create a new one once the "
                                         "environment image is built")
            dom = self.exec_in(env, [*common, f"--user-data-dir={out_dir}/p2", "--dump-dom", url],
                               timeout=60)
            png = containers.docker(["exec", env.container, "cat", f"{out_dir}/shot.png"],
                                    timeout=30, binary=True)
        finally:
            try:
                self.exec_in(env, ["rm", "-rf", out_dir], timeout=20)
            except Exception:
                pass
        console, errors = [], []
        for line in (shot.stderr or "").splitlines():
            if ":CONSOLE" not in line:
                continue
            msg = line.split("] ", 1)[-1].strip()
            (errors if '"Uncaught ' in msg else console).append(msg[:300])
        html = dom.stdout or ""
        title, text = _page_title_and_text(html)
        ok = png.returncode == 0 and bool(png.stdout)
        result: Dict[str, Any] = {
            "ok": ok, "url": url, "title": title, "page_errors": errors[:20],
            "console": console[:30], "failed_requests": None, "text": text[:6000],
            "screenshot_b64": base64.b64encode(png.stdout).decode("ascii") if ok else "",
            "screenshot_ext": "png",
        }
        if not ok:
            result["error"] = ((shot.stderr or "").strip().splitlines() or ["no screenshot"])[-1][:300]
        return result

    @staticmethod
    def _render_url(target: str) -> str:
        t = str(target or "").strip()
        if not t:
            raise EnvironmentRefused("nothing to render")
        low = t.lower()
        if low.startswith(("http://", "https://")):
            return t
        if low.startswith("file://"):
            t = t[len("file://"):]
        return "file://" + _container_path(t)

    # -- background processes ----------------------------------------------------------
    @staticmethod
    def process_handle(env_id: str, proc_id: str) -> str:
        """The id a person and the agent use for a background process: e-<env>-<proc>."""
        return f"e-{env_id}-{proc_id}"

    @staticmethod
    def parse_process_handle(handle: str):
        """(env_id, proc_id) for an e-<env>-<proc> handle, or None."""
        m = re.fullmatch(r"e-([a-z0-9-]{1,40})-(p[0-9a-f]{8})", str(handle or "").strip())
        return (m.group(1), m.group(2)) if m else None

    def start_process(self, owner_scope: Any, env_id: str, command: str, *,
                      session_id: str = "", username: Optional[str] = None,
                      user_role: Optional[str] = None, cwd: Optional[str] = None) -> str:
        """Start a command that keeps running (a dev server) and return its handle. Its
        output goes to a log inside the environment; it ends when it exits, is stopped,
        the environment stops, or after `sandbox_env_process_max_hours`."""
        owner_scope = resolve_owner(owner_scope)
        env = self.get(owner_scope, env_id)
        self._ensure_running(env)
        proc = "p" + secrets.token_hex(4)
        script = (f'mkdir -p {PROC_DIR} && cd "$VAF_CWD" && '
                  f'( sh -c "$VAF_CMD" > {PROC_DIR}/{proc}.log 2>&1; '
                  f'echo $? > {PROC_DIR}/{proc}.exit )')
        values = {**os.environ, "VAF_CMD": str(command), "VAF_CWD": self._workdir(env, cwd)}
        r = containers.docker(["exec", "-d", "-e", f"VAF_PROC_ID={proc}", "-e", "VAF_CMD",
                               "-e", "VAF_CWD", env.container, "sh", "-c", script],
                              timeout=30, env=values)
        if r.returncode != 0:
            raise EnvironmentRefused(f"the process could not be started: "
                                     f"{(r.stderr or '').strip()[:200]}")
        with self._locked():
            procs = self._read_procs(env.id)
            procs[proc] = {"command": str(command)[:400], "session_id": str(session_id or ""),
                           "user_scope_id": owner_scope, "username": username, "role": user_role,
                           "started": time.time(), "stopped": False, "notified": False}
            self._write_procs(env.id, procs)
        self._touch(env)
        return self.process_handle(env.id, proc)

    def _proc_status(self, env: Environment):
        """(running proc ids, {proc id: exit text}) read from inside the container."""
        if env.state != "running":
            return set(), {}
        script = (f'for d in /proc/[0-9]*; do tr "\\0" "\\n" < "$d/environ" 2>/dev/null '
                  f'| sed -n "s/^VAF_PROC_ID=//p"; done | sort -u | sed "s/^/alive /"; '
                  f'for f in {PROC_DIR}/*.exit; do [ -f "$f" ] && '
                  f'printf "exit %s %s\\n" "$(basename "$f" .exit)" "$(cat "$f")"; done; true')
        try:
            r = containers.docker(["exec", env.container, "sh", "-c", script], timeout=20)
        except Exception:
            return set(), {}
        alive, exits = set(), {}
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "alive":
                alive.add(parts[1])
            elif len(parts) >= 3 and parts[0] == "exit":
                exits[parts[1]] = parts[2]
        return alive, exits

    def processes(self, owner_scope: Any, *, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """This person's background processes in all their environments, optionally only
        those one chat started. Each: handle, env, command, state, started."""
        out: List[Dict[str, Any]] = []
        for env in self.list(owner_scope):
            procs = self._read_procs(env.id)
            if not procs:
                continue
            alive, exits = self._proc_status(env)
            for proc, rec in procs.items():
                if session_id is not None and rec.get("session_id") != str(session_id):
                    continue
                if proc in alive:
                    state = "running"
                elif rec.get("stopped"):
                    state = "stopped"
                elif proc in exits:
                    state = f"exited with code {exits[proc]}"
                else:
                    state = "ended (the environment stopped)"
                out.append({"handle": self.process_handle(env.id, proc), "env": env.id,
                            "command": rec.get("command", ""), "state": state,
                            "started": rec.get("started", 0.0)})
        return out

    def process_log(self, owner_scope: Any, handle: str, max_chars: int = 4000) -> str:
        env_id, proc = self._own_process(owner_scope, handle)
        env = self.get(owner_scope, env_id)
        if env.state != "running":
            return "(the environment is stopped; its processes ended with it)"
        r = containers.docker(["exec", env.container, "tail", "-c", str(int(max_chars)),
                               f"{PROC_DIR}/{proc}.log"], timeout=20)
        return r.stdout if r.returncode == 0 else "(no output yet)"

    def stop_process(self, owner_scope: Any, handle: str) -> str:
        env_id, proc = self._own_process(owner_scope, handle)
        env = self.get(owner_scope, env_id)
        if env.state == "running":
            containers.docker(["exec", env.container, "sh", "-c",
                               containers.kill_marked_cmd("VAF_PROC_ID", proc)], timeout=20)
        with self._locked():
            procs = self._read_procs(env.id)
            if proc in procs:
                procs[proc]["stopped"] = True
                procs[proc]["notified"] = True
                self._write_procs(env.id, procs)
        return f"stopped {handle}"

    def _own_process(self, owner_scope: Any, handle: str):
        parsed = self.parse_process_handle(handle)
        if parsed is None or parsed[1] not in self._read_procs(parsed[0]):
            raise EnvironmentRefused(f"no process {handle!r}")
        self.get(owner_scope, parsed[0])            # someone else's reads as missing
        return parsed

    def _watch_processes(self, env: Environment, now: float) -> None:
        """The reaper's look at one environment's processes: one that ended on its own
        wakes the chat that started it; one past `sandbox_env_process_max_hours` is
        stopped, and its chat is told."""
        procs = self._read_procs(env.id)
        pending = {p: r for p, r in procs.items() if not r.get("notified")}
        if not pending:
            return
        alive, exits = self._proc_status(env)
        limit = float(setting("process_max_hours")) * 3600
        changed = False
        for proc, rec in pending.items():
            note = ""
            if proc in alive:
                if now - float(rec.get("started") or now) <= limit:
                    continue
                containers.docker(["exec", env.container, "sh", "-c",
                                   containers.kill_marked_cmd("VAF_PROC_ID", proc)], timeout=20)
                note = (f"it was ended after {setting('process_max_hours')} h "
                        f"(sandbox_env_process_max_hours)")
            elif proc in exits:
                note = f"exit {exits[proc]}"
            elif env.state != "running":
                note = "the environment stopped"
            else:
                continue
            rec["notified"] = True
            changed = True
            self._wake(env, proc, rec, note)
        if changed:
            with self._locked():
                fresh = self._read_procs(env.id)
                for proc in pending:
                    if proc in fresh and procs[proc].get("notified"):
                        fresh[proc]["notified"] = True
                self._write_procs(env.id, fresh)

    def _wake(self, env: Environment, proc: str, rec: Dict[str, Any], note: str) -> None:
        if not rec.get("session_id"):
            return
        handle = self.process_handle(env.id, proc)
        tail = ""
        if env.state == "running":
            try:
                r = containers.docker(["exec", env.container, "tail", "-c", "1500",
                                       f"{PROC_DIR}/{proc}.log"], timeout=20)
                tail = (r.stdout or "").strip()
            except Exception:
                tail = ""
        text = (f"Background command in sandbox environment {env.id} finished: "
                f"{str(rec.get('command', ''))[:160]} ({handle}, {note}).\n"
                f"End of its output:\n{tail or '(no output)'}\n\n"
                f"Continue with what it was started for, or tell the user how it went. "
                f"host_process(action=\"log\", id=\"{handle}\") shows more of the output.")
        try:
            from vaf.core.task_queue import enqueue_wake_turn
            enqueue_wake_turn(kind="process", session_id=rec["session_id"], text=text,
                              user_scope_id=rec.get("user_scope_id"), username=rec.get("username"),
                              role=rec.get("role"), extra={"process_id": handle})
        except Exception:
            pass

    # -- housekeeping ----------------------------------------------------------------
    def busy(self, env: Environment) -> bool:
        """Whether anything VAF started is running inside: a process carrying a run or
        process marker in its environment. Asked of docker, because the process using
        the environment may be another one. Cannot tell counts as busy - a stopped
        environment costs a restart, a cut run costs the work."""
        if env.state != "running":
            return False
        try:
            r = containers.docker(["exec", env.container, "sh", "-c", containers.MARKED_PROCESSES_CMD],
                                  timeout=20)
        except Exception:
            return True
        if r.returncode != 0:
            return True
        return bool((r.stdout or "").strip())

    def reap_once(self, now: Optional[float] = None) -> Dict[str, int]:
        """One pass: expired temporary environments are removed, idle project ones are
        stopped, records left by a crash are cleared. Never raises."""
        now = time.time() if now is None else now
        summary = {"removed": 0, "stopped": 0, "orphans": 0}
        try:
            rows = self._docker_rows(strict=True)
        except Exception:
            return summary
        seen = set()
        registries_running = False
        for row in rows:
            seen.add(row["id"])
            if row["network"] == "registries" and row["state"] == "running":
                registries_running = True
            try:
                env = self._from_row(row)
                self._watch_processes(env, now)
                if env.kind in ("temporary", SCRATCH_KIND):
                    if env.expires and env.expires < now and not self.busy(env):
                        self._remove(env)
                        summary["removed"] += 1
                elif env.kind == "project" and env.state == "running":
                    idle = now - float(env.last_used or env.created or now)
                    if idle > float(setting("idle_stop_minutes")) * 60 and not self.busy(env):
                        containers.docker(["stop", "-t", "5", env.container], timeout=60)
                        summary["stopped"] += 1
            except Exception:
                continue
        summary["orphans"] = self._clear_orphans(seen, now)
        if not registries_running:
            # Nobody needs the proxy now; the next registries environment starts it again.
            try:
                if containers.container_state(PROXY_CONTAINER) == "running":
                    containers.docker(["stop", "-t", "2", PROXY_CONTAINER], timeout=30)
            except Exception:
                pass
        return summary

    def _clear_orphans(self, live_ids: set, now: float) -> int:
        """State files without a container (older than ten minutes, so a creation in
        flight is left alone), and labelled volumes and networks whose environment is
        gone."""
        cleared = 0
        try:
            for f in self._state_dir().glob("*.json"):
                env_id = f.stem
                # `<id>.procs.json` is the process record of an environment, not an
                # environment: its stem never matches an id, and treating it as one
                # deleted every live environment's process records on each pass.
                if f.name.endswith(".procs.json") or not _ID_RE.match(env_id):
                    continue
                if env_id in live_ids:
                    continue
                st = self._read_state(env_id) or {}
                if now - float(st.get("created") or 0) > 600:
                    for kind, name in (("volume", st.get("volume")), ("network", st.get("net"))):
                        if name:
                            containers.docker([kind, "rm", name], timeout=30)
                    with self._locked():
                        self._delete_state(env_id)
                    cleared += 1
        except Exception:
            pass
        for kind in ("volume", "network"):
            try:
                r = containers.docker([kind, "ls", "--filter", f"label={LABEL}=1", "--format",
                                       "{{.Name}}\t{{.Label \"" + LABEL + ".id\"}}"], timeout=30)
                for line in (r.stdout or "").splitlines():
                    name, _, env_id = line.partition("\t")
                    if env_id and env_id not in live_ids and not self._read_state(env_id):
                        if containers.docker([kind, "rm", name], timeout=30).returncode == 0:
                            cleared += 1
            except Exception:
                continue
        return cleared

    def prune(self) -> Dict[str, int]:
        """What `vaf env prune` runs: one reaper pass now."""
        return self.reap_once()

    def start_reaper(self, interval_s: float = 60.0) -> None:
        """The housekeeping thread, for the long-lived process (the web server or the
        tray). The CLI does not start it; a short command must not leave a thread."""
        if housekeeping_off():
            return
        with self._thread_lock:
            if self._reaper_alive:
                return
            self._reaper_alive = True

        def _loop():
            while not self._closing:
                time.sleep(interval_s)
                if self._closing:
                    break
                self.reap_once()
            with self._thread_lock:
                self._reaper_alive = False

        threading.Thread(target=_loop, daemon=True, name="sandbox-env-reaper").start()

    def stop_all_at_quit(self, timeout_s: float = 20.0) -> int:
        """VAF's quit: every running environment that is not busy is stopped, and the
        proxy with them. Stopped, not removed - the next use starts them again. A busy
        one is left alone: a `vaf env exec` or a coder in another process is using it."""
        self._closing = True
        if housekeeping_off():
            return 0
        names: List[str] = []
        for row in self._docker_rows():
            if row["state"] != "running":
                continue
            env = self._from_row(row)
            if not self.busy(env):
                names.append(env.container)
        workers = []
        for name in names + [PROXY_CONTAINER]:
            t = threading.Thread(target=containers.docker, args=(["stop", "-t", "5", name],),
                                 kwargs={"timeout": 60}, daemon=True)
            t.start()
            workers.append(t)
        deadline = time.monotonic() + timeout_s
        for t in workers:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        return len(names)

    def stop_all_for(self, owner_scope: Any) -> int:
        """An account's access was taken away: stop its environments now (the work in
        them ends with them)."""
        try:
            owner_hash = containers.scope_hash(resolve_owner(owner_scope))
        except EnvironmentRefused:
            return 0
        stopped = 0
        for row in self._docker_rows(owner_hash):
            if row["state"] == "running":
                containers.docker(["stop", "-t", "5", row["container"]], timeout=60)
                stopped += 1
        return stopped


def _page_title_and_text(html: str):
    """The <title> and the visible text of a DOM dump (scripts and styles left out)."""
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts, self.title, self._skip, self._in_title = [], "", 0, False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "noscript"):
                self._skip += 1
            if tag == "title":
                self._in_title = True

        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript") and self._skip:
                self._skip -= 1
            if tag == "title":
                self._in_title = False

        def handle_data(self, data):
            if self._in_title:
                self.title += data
            elif not self._skip and data.strip():
                self.parts.append(data.strip())

    parser = _Text()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    return parser.title.strip(), "\n".join(parser.parts)


_manager: Optional[EnvironmentManager] = None
_manager_lock = threading.Lock()


def _on_revocation(user_scope_id: str) -> None:
    if housekeeping_off():
        return
    try:
        get_environment_manager().stop_all_for(user_scope_id)
    except Exception:
        pass


def get_environment_manager() -> EnvironmentManager:
    """The process's manager. Its first use registers the revocation listener."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = EnvironmentManager()
            try:
                from vaf.core.revocation import add_revocation_listener
                add_revocation_listener(_on_revocation)
            except Exception:
                pass
        return _manager
