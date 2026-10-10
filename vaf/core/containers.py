# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The docker calls behind the containers VAF creates itself.

VAF starts some containers outside compose: the per-user browsers
(`browser_pool`) and, built on the same calls, the sandbox environments. Their
lifecycle needs the same handful of docker operations - call the CLI, read a
container's state, count the running ones, create an isolated network, read the
free memory before starting another - and each one used to live as a private copy
in the module that needed it. This is the one copy.

Every docker call goes through `docker()`, read as a MODULE ATTRIBUTE by its
callers (`containers.docker(...)`), so a test that patches it reaches every lane at
once. It resolves the docker binary the stack resolves (`resolve_docker_exe`, which
finds Rancher Desktop on Windows and the Homebrew path on macOS) and opens no
console window on Windows.

`scope_hash` names per-user resources. Its value is part of persisted state: the
browser pool's profile volumes are named by it, so changing the function orphans
every saved browser profile (logins, history). A test pins it to literal values.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional


def windowless_kwargs() -> dict:
    """subprocess options that keep a console window from flashing up on Windows."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def docker(args: List[str], timeout: float = 60, *, input=None,
           env: Optional[Dict[str, str]] = None, binary: bool = False) -> subprocess.CompletedProcess:
    """One docker CLI call, captured as text (bytes with `binary`, for a tar stream).
    The single seam the tests stub. Text is UTF-8 whatever the host's locale is - a
    Windows client would otherwise decode a container's output as cp1252 and fail on
    the first byte that code page does not define - and an undecodable byte becomes
    U+FFFD instead of an exception.

    `env` replaces the client's environment: it is how a value reaches a container
    without appearing on any command line (`docker exec -e NAME` takes the value from
    the client's environment). Raises what subprocess raises (FileNotFoundError when
    there is no docker, TimeoutExpired); callers decide what a failure means."""
    from vaf.core.service_stack import resolve_docker_exe
    text = {} if binary else {"encoding": "utf-8", "errors": "replace"}
    return subprocess.run([resolve_docker_exe(), *args], capture_output=True,
                          timeout=timeout, input=input, env=env, **text, **windowless_kwargs())


def scope_hash(scope: str) -> str:
    """The 12-hex name part for a user scope, so a container or volume listing does
    not reveal who uses the machine. Persisted state is named by it: never change."""
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]


def container_state(name: str) -> Optional[str]:
    """`running`, `exited`, `created`, ... or None when there is no such container."""
    r = docker(["inspect", name, "--format", "{{.State.Status}}"], timeout=20)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def count_running(name_prefix: str) -> int:
    """How many running containers carry this name prefix. Counted at docker, not in
    memory, so containers another VAF process started count too. 0 when docker does
    not answer."""
    r = docker(["ps", "--filter", f"name={name_prefix}", "--format", "{{.Names}}"], timeout=20)
    if r.returncode != 0:
        return 0
    return len([ln for ln in (r.stdout or "").splitlines() if ln.strip()])


def ensure_network(name: str, *, internal: bool = False,
                   labels: Optional[Dict[str, str]] = None,
                   options: Optional[Dict[str, str]] = None) -> bool:
    """A bridge network of this name exists afterwards; True when it does.

    `docker network create` on an existing name fails harmlessly, so the existence
    check and the create are not a race worth locking: a lost race is answered by
    inspecting again. `internal` gives the network no route out; `options` are
    driver options (`-o key=value`)."""
    try:
        r = docker(["network", "inspect", name, "--format", "{{.Name}}"], timeout=20)
        if r.returncode == 0 and name in (r.stdout or ""):
            return True
        args = ["network", "create", "--driver", "bridge"]
        if internal:
            args.append("--internal")
        for key, value in (labels or {}).items():
            args += ["--label", f"{key}={value}"]
        for key, value in (options or {}).items():
            args += ["-o", f"{key}={value}"]
        args.append(name)
        if docker(args, timeout=30).returncode == 0:
            return True
        r2 = docker(["network", "inspect", name, "--format", "{{.Name}}"], timeout=20)
        return r2.returncode == 0
    except Exception:
        return False


def parse_docker_time(raw: str) -> datetime:
    """A docker timestamp (RFC 3339 with nanoseconds, `...58.819793341Z`) as an aware
    datetime. fromisoformat takes at most microseconds, so the fraction is trimmed.
    Raises ValueError for text that is not a timestamp."""
    text = re.sub(r"\.(\d{6})\d*", r".\1", str(raw).strip().replace("Z", "+00:00"))
    value = datetime.fromisoformat(text)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def image_age_days(image: str) -> Optional[float]:
    """How many days ago this image was built, or None when it cannot be known (no
    such image, docker trouble, an unreadable timestamp) - an age gate then stands down
    rather than rebuilding on a guess."""
    try:
        r = docker(["image", "inspect", image, "--format", "{{.Created}}"], timeout=20)
        raw = (r.stdout or "").strip()
        if r.returncode != 0 or not raw:
            return None
        created = parse_docker_time(raw)
        return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400.0)
    except Exception:
        return None


def mem_available_mb() -> Optional[int]:
    """Free-ish memory in MB, or None where /proc/meminfo does not exist (macOS and
    Windows run containers inside a VM with its own budget, so a floor check stands
    down there rather than guessing)."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


# -- running inside a container ----------------------------------------------------
# A run started with `docker exec` keeps running when the docker client is killed, so
# a timeout or a Stop has to end it INSIDE the container. Every run VAF starts carries
# a marker in its environment (VAF_RUN_ID for a bounded command, VAF_PROC_ID for a
# background process); children inherit it, and /proc/<pid>/environ is readable for
# the container's own user. Killing by marker ends exactly that run - the cwd or
# command-line match the shared sandbox used would kill every process in an
# environment whose working directory is /workspace, the dev server included.
MARKER_VARS = ("VAF_RUN_ID", "VAF_PROC_ID")
_MARKER_VALUE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def kill_marked_cmd(var: str, value: str) -> str:
    """Pure-sh killer for every process whose environment carries `var=value`. Needs
    only procfs and shell builtins (the fallback image ships no procps). The scanning
    shell skips itself."""
    if var not in MARKER_VARS or not _MARKER_VALUE.match(value or ""):
        raise ValueError("not a VAF run marker")
    return ('for d in /proc/[0-9]*; do p="${d##*/}"; [ "$p" = "$$" ] && continue; '
            f'if tr "\\0" "\\n" < "$d/environ" 2>/dev/null | grep -qx "{var}={value}"; '
            'then kill -9 "$p" 2>/dev/null; fi; done')


# The pids of every process carrying either marker, one per line; empty when none.
MARKED_PROCESSES_CMD = (
    'for d in /proc/[0-9]*; do p="${d##*/}"; [ "$p" = "$$" ] && continue; '
    'if tr "\\0" "\\n" < "$d/environ" 2>/dev/null | grep -qE "^(VAF_RUN_ID|VAF_PROC_ID)="; '
    'then echo "$p"; fi; done'
)


def _popen(argv, **kwargs):
    """subprocess.Popen, behind a seam the tests stub."""
    return subprocess.Popen(argv, **kwargs)


def exec_bounded(container: str, argv: List[str], *, timeout: float, workdir: str,
                 run_id: str, env_values: Optional[Dict[str, str]] = None,
                 check_stop=None, input_text: Optional[str] = None):
    """Run argv in a running container, bounded and stop-aware.

    Bounded twice: `timeout -s KILL` inside the container ends the command on its own
    clock, and a backstop here (timeout + 15 s) kills the docker client and the run's
    processes by their marker. `check_stop` is polled every half second; True ends the
    run the same way. Values in `env_values` reach the run as its environment without
    appearing on a command line (`-e NAME`, the value in the client's environment).

    Returns (returncode, stdout, stderr, timed_out, cancelled)."""
    from vaf.core.service_stack import resolve_docker_exe
    if not _MARKER_VALUE.match(run_id or ""):
        raise ValueError("run_id must be a plain token")
    seconds = max(1, int(timeout))
    cmd = [resolve_docker_exe(), "exec"]
    if input_text is not None:
        cmd.append("-i")
    cmd += ["-w", workdir, "-e", f"VAF_RUN_ID={run_id}"]
    for name in (env_values or {}):
        cmd += ["-e", name]
    cmd += [container, "timeout", "-s", "KILL", str(seconds), *argv]
    try:
        proc = _popen(cmd, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      encoding="utf-8", errors="replace",
                      env=({**os.environ, **env_values} if env_values else None),
                      **windowless_kwargs())
    except Exception as e:
        return -1, "", str(e), False, False
    pending_input = input_text
    started = time.monotonic()
    deadline = started + seconds + 15
    while True:
        try:
            out, err = proc.communicate(input=pending_input, timeout=0.5)
            rc = proc.returncode
            # timeout -s KILL ends the command with 137 (128 + SIGKILL) when its clock ran
            # out - and so does the memory limit's OOM kill, at any moment. Only a run that
            # lasted its whole budget timed out; one killed early says its exit code.
            ran_out = time.monotonic() - started >= seconds - 1
            return rc, out or "", err or "", rc in (124, 137) and ran_out, False
        except subprocess.TimeoutExpired:
            pending_input = None
        stopped = bool(check_stop and check_stop())
        if stopped or time.monotonic() >= deadline:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                docker(["exec", container, "sh", "-c", kill_marked_cmd("VAF_RUN_ID", run_id)],
                       timeout=15)
            except Exception:
                pass
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = "", ""
            note = "cancelled by stop request" if stopped else f"timed out after {seconds}s"
            return -1, out or "", f"{(err or '').strip()}\n{note}".strip(), not stopped, stopped
