# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Whose state the shared browser holds, and who is using it: one record for every VAF process.

The shared vaf-browser container is driven from more than one process: the web
server (interactive leases, chat agent runs, render_check from the main agent), a
coder child (`vaf subagent run`, a piped child in the web UI, whose render_check
lands here), a spawned browser_agent child (workflows), the terminal app. Each
process has its own InteractiveBrowserManager, and what one of them knew in memory -
whose cookies sit in the jar, whether a lease is live - the others never learned.
Measured with two processes on one container: a coder child's render wiped the
profile under a person's live lease in the web process and pointed their tab at
another user's page; after a child's render the web process still believed the jar
was the previous owner's and skipped the wipe at that person's next lease, so the
child's mirrored files sat in their file picker; a spawned run for one scope left
the web process's tracker on another, whose next lease browsed with the run's
logins.

This record is what every process reads before it trusts its own memory:

- `owner`: whose state the profile holds, as a scope HASH (never the scope), bound
  to the container instance it was written for (docker's container ID plus its last
  start). A record without the format tag, for another container, for an earlier
  start of this one, or cleared by a handover that did not finish vouches for
  nobody: the owner is unknown and the next holder gets the verified wipe. A
  handover clears the owner BEFORE its wipe and names the new one only after the
  wipe is confirmed, so a crash in between leaves "unknown", never a wrong name.
  Binding to the last start keeps the rule that a restarted stack starts unknown.
- `holders`: who is using the browser now, one entry per lease, run or probe, each
  with its process's pid and a mark of when that process started (on Linux the
  boot-relative tick count, which a clock step cannot move). An entry whose process
  is gone, or whose pid now belongs to another process, is dropped, so a crash
  never locks the browser. A live entry of another process makes a claimant back
  off by the rules the managers apply within one process.

One small JSON file per container under the VAF data directory, read and written
under the cross-process lock secure_store uses (filelock), through secure_store's
atomic writer, owner-only. It holds hashes, pids and timestamps and no user
content, so it is not encrypted, like the sandbox environments' state files. A
file that exists but cannot be used (unreadable, not JSON, without this format's
tag or with another one) is never read as "no holders" and never overwritten: the
holder half answers JarRecordUnavailable, so a probe is busy and a handover is
refused, and the log names the file, until it is removed with VAF stopped.

Why on the host and not as a flag file inside the container: the holders are host
processes, whose liveness only the host can check (measured: `docker inspect` 7 ms,
the locked read and write 0.2 ms, a process start mark 0.04 ms), and a flag file inside
the container is written by the browser user - the account a compromised page runs
as. A record the browser itself can forge must never decide whether the next
person's profile is wiped.

Named boundary: the record lives in this VAF data directory. A second VAF
installation under another OS account that drives the same docker container (both
name it vaf-browser) keeps its own record and is not seen here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

# Persisted format identity (see the format-tag practice): a file without it, or
# with another, is not read as a record and vouches for nobody.
JAR_FORMAT = "browserjar-1-6e0d93"

KIND_LEASE = "lease"
KIND_RUN = "run"
KIND_PROBE = "probe"

_LOCK_TIMEOUT_S = 10.0
_thread_lock = threading.RLock()
_started_cache: Dict[int, Optional[float]] = {}


class JarRecordUnavailable(RuntimeError):
    """The record could not be read or written under its lock. Callers treat it as
    an unknown owner (wipe) and an unknown holder (back off): fail-closed."""


def scope_key(scope: str) -> str:
    """The hash the record stores instead of the scope."""
    return hashlib.sha256(str(scope or "").encode("utf-8")).hexdigest()


def _state_dir() -> Path:
    """Where the records live. The single seam the tests point elsewhere."""
    from vaf.core.platform import Platform
    return Platform.vaf_dir() / "browser_jar"


def _linux_start_ticks(pid: int) -> Optional[float]:
    """Field 22 of /proc/<pid>/stat: when the process started, in clock ticks since
    boot. None only for a process that is known to be gone (or a zombie); a read
    that fails for another reason - a full file-descriptor table (EMFILE), say -
    answers 0.0, "exists", because reading a live holder as gone writes it out of
    the record and lets another process take the browser under it."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        if os.path.isdir("/proc/self"):
            return None
        # No /proc at all: nothing says the process is gone. Existence without a
        # start mark, so a crash is still noticed.
        from vaf.core.platform import Platform
        return 0.0 if Platform.is_process_running(int(pid)) else None
    except OSError:
        return 0.0
    try:
        # The command name (field 2) is in parentheses and may hold spaces or
        # parentheses itself; everything after the LAST ")" is fields 3 onward.
        fields = raw[raw.rindex(b")") + 2:].split()
        if fields[0] in (b"Z", b"X"):
            return None
        return float(int(fields[19]))
    except Exception:
        return 0.0


def _process_started(pid: int) -> Optional[float]:
    """A mark of when a process started, or None when it does not exist. Compared
    only with a mark this same function recorded, so its unit is its own.

    On Linux the boot-relative start in clock ticks (`/proc/<pid>/stat`), not
    psutil's create_time: that one adds the boot time, which /proc/stat derives
    from the wall clock in whole seconds, so a clock step moved it - measured, a
    forward step of 15 ms changed it by a full second, every start a long-running
    process had recorded no longer matched, and its live leases read as gone to
    every process started after the step (a coder child's render then wiped the
    browser under a person's live lease). The tick count never changes for the
    life of the process. Elsewhere psutil's create_time, which Windows and macOS
    fix when the process is created."""
    if sys.platform.startswith("linux"):
        return _linux_start_ticks(pid)
    try:
        import psutil
    except Exception:
        from vaf.core.platform import Platform
        return 0.0 if Platform.is_process_running(pid) else None
    try:
        return float(psutil.Process(pid).create_time())
    except psutil.NoSuchProcess:
        return None
    except Exception:
        # AccessDenied and the like: the process exists. 0.0 matches any recorded
        # start, which keeps the entry, the conservative side.
        return 0.0


def _own_identity() -> Tuple[int, float]:
    pid = os.getpid()
    if pid not in _started_cache:
        _started_cache[pid] = _process_started(pid)
    return pid, float(_started_cache[pid] or 0.0)


def _alive(entry: Dict[str, Any]) -> bool:
    try:
        pid = int(entry.get("pid") or 0)
        recorded = float(entry.get("started") or 0.0)
    except Exception:
        return False
    if pid <= 0:
        return False
    actual = _process_started(pid)
    if actual is None:
        return False
    if actual == 0.0 or recorded == 0.0:
        return True
    # The same process reports the same mark; a reused pid does not. On Linux the
    # marks are whole clock ticks and match exactly; elsewhere they are seconds.
    tolerance = 0.5 if sys.platform.startswith("linux") else 1.0
    return abs(actual - recorded) < tolerance


def container_instance(container: str) -> str:
    """`<container id>@<last start>`, or "" when docker does not answer. Read
    whenever an owner is decided: an owner is only ever vouched for on the
    instance and the start it was recorded on."""
    try:
        from vaf.core import containers
        r = containers.docker(["inspect", container, "--format",
                               "{{.Id}}@{{.State.StartedAt}}"], timeout=20)
        if r.returncode != 0:
            return ""
        out = (r.stdout or "").strip()
        return out if "@" in out and not out.startswith("@") else ""
    except Exception:
        return ""


class JarRecord:
    """The record of one container. Cheap to construct; every call locks, reads,
    and (when it changes something) writes the file."""

    def __init__(self, container: str) -> None:
        self.container = container

    # -- file --------------------------------------------------------------
    def _path(self) -> Path:
        d = _state_dir()
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            try:
                from vaf.core.secure_store import harden_dir
                harden_dir(d)
            except Exception:
                pass
        return d / f"{self.container}.json"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        from vaf.core.secure_store import _get_filelock_cls
        with _thread_lock:
            cls = _get_filelock_cls()
            if cls is None:
                # filelock is a hard dependency; without it the record would be read
                # and written with no cross-process exclusion at all, which is the
                # record's whole point. Unavailable, so every caller fails closed.
                raise JarRecordUnavailable("no cross-process lock (filelock is not importable)")
            try:
                lock = cls(str(self._path()) + ".lock", timeout=_LOCK_TIMEOUT_S)
                lock.acquire()
            except Exception as e:
                raise JarRecordUnavailable(f"record lock not acquired: {e}") from e
            try:
                yield
            finally:
                try:
                    lock.release()
                except Exception:
                    pass

    def _load(self, *, strict: bool) -> Dict[str, Any]:
        """The record. A missing file is an empty record. A file that exists but
        cannot be used - unreadable, not JSON, without this format's tag or with
        another - vouches for nobody; with `strict` it raises JarRecordUnavailable
        instead, because the holder half and every write must not read it as "no
        holders" and overwrite it: the holders it lists would be lost, and a tag
        of another VAF version would be clobbered by an older one."""
        path = self._path()
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {"holders": []}
        except Exception as e:
            if strict:
                raise JarRecordUnavailable(f"record {path} not readable: {e}") from e
            return {"holders": []}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = None
        if not isinstance(data, dict) or data.get("format") != JAR_FORMAT:
            if strict:
                raise JarRecordUnavailable(
                    f"record {path} is not a {JAR_FORMAT} record; left as it is")
            return {"holders": []}
        holders = data.get("holders")
        data["holders"] = [h for h in holders if isinstance(h, dict)] if isinstance(holders, list) else []
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        """Through the store writer every at-rest store uses: a 0600 temp file,
        fsync, and a replace that retries the sharing violation Windows raises
        while a scanner or the indexer holds the file open."""
        from vaf.core.secure_store import atomic_write_bytes
        data["format"] = JAR_FORMAT
        path = self._path()
        try:
            atomic_write_bytes(path, json.dumps(data, indent=1).encode("utf-8"))
        except Exception as e:
            raise JarRecordUnavailable(f"record {path} not written: {e}") from e
        try:
            from vaf.core.secure_store import harden_path
            harden_path(path)
        except Exception:
            pass

    # -- owner ---------------------------------------------------------------
    def owner_is(self, scope: str, instance: str) -> bool:
        """Does the record vouch that the profile holds this scope's state? Only on
        the instance it was written for; anything else is an unknown owner."""
        if not instance:
            return False
        with self._locked():
            data = self._load(strict=False)
        owner = data.get("owner")
        return bool(owner) and owner == scope_key(scope) and data.get("instance") == instance

    def set_owner(self, scope: Optional[str], instance: str) -> None:
        """Name the owner (after a confirmed wipe, or a lane of the same scope), or
        clear it (None, before a wipe). Raises JarRecordUnavailable."""
        with self._locked():
            data = self._load(strict=True)
            data["owner"] = scope_key(scope) if scope else None
            data["instance"] = instance or ""
            data["owner_at"] = time.time()
            self._save(data)

    # -- holders ---------------------------------------------------------------
    def claim(self, kind: str, scope: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """Record this process as a holder of `kind`, unless a LIVE holder of
        another process exists. Answers (token, None) on success and
        (None, the other holder) otherwise. A lease is one per process (a second
        claim returns the same token); runs are counted, one entry per run.
        Raises JarRecordUnavailable."""
        pid, started = _own_identity()
        with self._locked():
            data = self._load(strict=True)
            live = [h for h in data["holders"] if _alive(h)]
            foreign = [h for h in live if int(h.get("pid") or 0) != pid]
            if foreign:
                if len(live) != len(data["holders"]):
                    data["holders"] = live
                    self._save(data)
                return None, dict(foreign[0])
            if kind == KIND_LEASE:
                for h in live:
                    if h.get("kind") == KIND_LEASE:
                        h["scope"] = scope_key(scope)
                        data["holders"] = live
                        self._save(data)
                        return str(h.get("token") or ""), None
            token = secrets.token_hex(8)
            live.append({"kind": kind, "scope": scope_key(scope), "pid": pid,
                         "started": started, "since": time.time(), "token": token})
            data["holders"] = live
            self._save(data)
            return token, None

    def release(self, token: Optional[str]) -> None:
        """Drop the entry this token names. Never raises: a release that cannot be
        written leaves an entry that ends with its process, and until then other
        processes answer busy - so the failure is logged, once per release."""
        if not token:
            return
        try:
            with self._locked():
                data = self._load(strict=True)
                kept = [h for h in data["holders"] if h.get("token") != token and _alive(h)]
                if kept != data["holders"]:
                    data["holders"] = kept
                    self._save(data)
        except Exception as e:
            try:
                from vaf.core.log_helper import append_domain_log
                append_domain_log("webui", f"[browser_jar] holder entry not released, other "
                                           f"processes see the browser busy until this one "
                                           f"ends: {e}")
            except Exception:
                pass

    def foreign_holders(self) -> List[Dict[str, Any]]:
        """Live holders of other processes. Raises JarRecordUnavailable."""
        pid, _started = _own_identity()
        with self._locked():
            data = self._load(strict=True)
        return [dict(h) for h in data["holders"]
                if _alive(h) and int(h.get("pid") or 0) != pid]


def record_for(container: str) -> JarRecord:
    return JarRecord(container)
