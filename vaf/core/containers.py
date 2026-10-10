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

import collections
import hashlib
import os
import re
import subprocess
import sys
import threading
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
    not answer. Docker's name filter matches anywhere in the name, so the prefix is
    checked again here: `old-vaf-browser-u-...` is not one of ours."""
    r = docker(["ps", "--filter", f"name={name_prefix}", "--format", "{{.Names}}"], timeout=20)
    if r.returncode != 0:
        return 0
    return len([ln for ln in (r.stdout or "").splitlines() if ln.strip().startswith(name_prefix)])


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


# What a run's output may hold in memory, per stream: its head and its tail, the middle counted
# and dropped. communicate() kept everything, so a command that prints without end (`yes`, a
# runaway log) filled VAF's own memory until its timeout. The tail is kept because that is
# where a test run's summary is; the head is generous because the page text a preview renders
# arrives as one dump of the DOM.
CAPTURE_HEAD_CHARS = 6_000_000
CAPTURE_TAIL_CHARS = 2_000_000


class _Capture:
    """Drains one stream on a thread of its own, so a child never blocks on a full pipe,
    and keeps CAPTURE_HEAD_CHARS of the start and CAPTURE_TAIL_CHARS of the end."""

    def __init__(self, stream):
        self.head: List[str] = []
        self.head_len = 0
        self.tail: "collections.deque[str]" = collections.deque()
        self.tail_len = 0
        self.dropped = 0
        self.thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self.thread.start()

    def _drain(self, stream) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                self._add(chunk)
        except (OSError, ValueError):
            return

    def _add(self, chunk: str) -> None:
        room = CAPTURE_HEAD_CHARS - self.head_len
        if room > 0:
            self.head.append(chunk[:room])
            self.head_len += min(room, len(chunk))
            chunk = chunk[room:]
        if not chunk:
            return
        self.tail.append(chunk)
        self.tail_len += len(chunk)
        excess = self.tail_len - CAPTURE_TAIL_CHARS
        while excess > 0:
            first = self.tail[0]
            if len(first) <= excess:
                self.tail.popleft()
                cut = len(first)
            else:
                self.tail[0] = first[excess:]
                cut = excess
            self.tail_len -= cut
            self.dropped += cut
            excess -= cut

    def text(self, wait: float) -> str:
        self.thread.join(wait)
        out = "".join(self.head)
        if self.dropped:
            out += f"\n[... {self.dropped} characters of output left out ...]\n"
        return out + "".join(self.tail)


def exec_bounded(container: str, argv: List[str], *, timeout: float, workdir: str,
                 run_id: str, env_values: Optional[Dict[str, str]] = None,
                 check_stop=None, input_text: Optional[str] = None,
                 user: Optional[str] = None):
    """Run argv in a running container, bounded and stop-aware.

    Bounded twice: `timeout -s KILL` inside the container ends the command on its own
    clock, and a backstop here (timeout + 15 s) kills the docker client and the run's
    processes by their marker. `check_stop` is polled every half second; True ends the
    run the same way. Values in `env_values` reach the run as its environment without
    appearing on a command line (`-e NAME`, the value in the client's environment).
    `user` runs the command as that uid:gid instead of the container's own user.

    Returns (returncode, stdout, stderr, timed_out, cancelled)."""
    from vaf.core.service_stack import resolve_docker_exe
    if not _MARKER_VALUE.match(run_id or ""):
        raise ValueError("run_id must be a plain token")
    seconds = max(1, int(timeout))
    cmd = [resolve_docker_exe(), "exec"]
    if input_text is not None:
        cmd.append("-i")
    cmd += ["-w", workdir, "-e", f"VAF_RUN_ID={run_id}"]
    if user:
        cmd += ["-u", str(user)]
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
    out_cap, err_cap = _Capture(proc.stdout), _Capture(proc.stderr)
    if input_text is not None:
        def _feed():
            try:
                proc.stdin.write(input_text)
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        # Its own thread: a child that never reads stdin must not hold the clock below.
        threading.Thread(target=_feed, daemon=True).start()
    started = time.monotonic()
    deadline = started + seconds + 15
    # As the run's own user: a root run's processes cannot be killed by the container's
    # unprivileged user.
    as_user = ["-u", str(user)] if user else []
    while True:
        try:
            rc = proc.wait(timeout=0.5)
            # A child the command put in the background (`server &`) outlives it with the
            # same marker, and an environment with a marked process reads as busy for good:
            # never stopped when idle, never removed when expired. A process meant to keep
            # running is started as one (VAF_PROC_ID), so what is left here goes.
            try:
                docker(["exec", *as_user, container, "sh", "-c",
                        kill_marked_cmd("VAF_RUN_ID", run_id)], timeout=15)
            except Exception:
                pass
            # timeout -s KILL ends the command with 137 (128 + SIGKILL) when its clock ran
            # out - and so does the memory limit's OOM kill, at any moment. Only a run that
            # lasted its whole budget timed out; one killed early says its exit code.
            ran_out = time.monotonic() - started >= seconds - 1
            return rc, out_cap.text(5), err_cap.text(5), rc in (124, 137) and ran_out, False
        except subprocess.TimeoutExpired:
            pass
        stopped = bool(check_stop and check_stop())
        if stopped or time.monotonic() >= deadline:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                docker(["exec", *as_user, container, "sh", "-c",
                        kill_marked_cmd("VAF_RUN_ID", run_id)], timeout=15)
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            out, err = out_cap.text(5), err_cap.text(5)
            note = "cancelled by stop request" if stopped else f"timed out after {seconds}s"
            return -1, out, f"{err.strip()}\n{note}".strip(), not stopped, stopped
