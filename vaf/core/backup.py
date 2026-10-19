# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Backups: everything a restore on a new machine needs, in one encrypted file.

## The file

A tar archive (POSIX/pax), encrypted with `age` (vaf/core/recovery_kit.py says why age):
``vaf-backup-<UTC time>.tar.age`` with a ``.sha256`` beside it. In an emergency it opens
without VAF: ``age -d -i key.txt backup.tar.age | tar x``. Inside:

- ``manifest.json`` first: the format tag (``BACKUP_FORMAT``), the VAF version, the roots
  as they were on this machine, what was included, what was skipped and why.
- ``files/<root>/<path>``: every entry ``vaf/core/data_stores.py`` marks as backed up
  (keys, data, recreatable logins, the security log). SQLite databases are copied with
  SQLite's own online backup, so a database in use is captured consistent; their
  ``-wal``/``-shm`` files are therefore not taken.
- ``postgres/database.dump`` (``pg_dump -Fc`` as the owner role, inside the database
  container: row-level security hides every row from the app role) and
  ``postgres/globals.sql`` (the roles with their password hashes, so the connection
  strings in the restored ``config.json`` fit the restored database).
- ``keys/machine_key``: the machine key (KEK) the encrypted stores are sealed under, so a
  restore on another machine re-wraps them under that machine's key (absent when the
  stores are sealed under a passphrase instead).
- ``volumes/*.tar``: each sandbox environment volume and each personal browser's profile
  without its caches, read by volume name through a carrier container.
- ``checksums.json`` last: the SHA-256 of every member.

## Who can open it

The file is encrypted to RECIPIENTS, public keys; nothing that decrypts it is needed to
write it, which is what lets a scheduled backup run unattended:

- this machine's backup identity (``backup_identity`` in the data keyring): every backup
  is verified right after it is written, and a restore on the same machine needs nothing
  typed. Whoever holds this machine holds its live data anyway.
- the recovery key's public half (``recovery_kit.recipient()``): the key the owner keeps
  off the machine opens the backup on a new one. A version 1 recovery key has no public
  half; the backup says so and `vaf secure renew-recovery-key` fixes it.

## Restore

`restore_backup` checks every member before anything lands, refuses a backup from a newer
VAF, backs up the current state first, places the files, re-wraps the stores under THIS
machine's key (which it never replaces), restores the database with its roles, gives the
sandbox environments their containers back (`EnvironmentManager.readopt`) and fills the
volumes. docs/setup/BACKUP_RESTORE.md has the order and the live measurement.

## Named boundaries

- Credentials kept in the operating system's keyring (mail, cloud and GitHub sign-ins on
  a desktop with a keyring) are not in the backup; it says so when there are any. On a
  machine without an OS keyring (a server with no desktop session) they are kept in the
  encrypted files, which are backed up.
- A project sandbox environment that mounts a host folder keeps its files in that folder,
  which is the person's own (not VAF's to back up), unless it is under VAF_Projects.
- Temporary sandbox environments expire on their own and are not taken.
- Not on the facade: the stores are VAF's own (vaf/core/data_stores.py).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("vaf.core.backup")

# Chosen once; pinned in tests/test_persisted_format_tags.py.
BACKUP_FORMAT = "vafbackup-1-5e2c90"
SUFFIX = ".tar.age"
IDENTITY_SECRET = "backup_identity"
# Identities this machine held before a restore replaced its keyring: a safety backup was
# written to one of them, and must still open (a JSON list in the data keyring).
PREVIOUS_IDENTITIES_SECRET = "backup_identities_previous"
PG_CONTAINER = "vaf-memory-db"
_SQLITE_MAGIC = b"SQLite format 3\x00"
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
_CHUNK = 1024 * 1024
_PROJECTS_PREFIX = "files/projects/"
# What a Chromium profile rebuilds on its own, by directory name wherever it sits; taking it
# would make a profile several times the size of what is worth keeping (cookies, logins,
# settings, extensions). Measured on a live profile: 243 MB with the caches, most of it
# `.cache` and the Workspace mirror.
_BROWSER_CACHES = frozenset({"Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
                             "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
                             "GPUPersistentCache", "CacheStorage", "ScriptCache",
                             "component_crx_cache", "extensions_crx_cache", "Crashpad",
                             "optimization_guide_model_store",
                             "optimization_guide_hint_cache_store", "AutofillAiModelCache",
                             # Components Chromium downloads again on its own.
                             "WasmTtsEngine", "OnDeviceHeadSuggestModel", "OptGuideOnDeviceModel",
                             "screen_ai", "SODA", "Safe Browsing", "segmentation_platform",
                             "BrowserMetrics", "BrowserMetrics-spare.pma"})
# Top-level folders of the browser's home that are not the profile: the XDG cache, and the
# Workspace and Downloads folders, which mirror the person's own files (backed up where they
# live) and are drained into them (docker/browser/entrypoint.sh).
_BROWSER_NOT_PROFILE = frozenset({".cache", "Workspace", "Downloads"})


class BackupError(RuntimeError):
    """A backup that could not be written completely. Nothing partial is left behind."""


@dataclass
class BackupResult:
    path: Path
    size: int
    sha256: str
    members: int
    verified: bool
    recipients: List[str]
    skipped: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class VerifyResult:
    ok: bool
    members: int = 0
    manifest: Dict = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)


# ── where, and to whom ────────────────────────────────────────────────────────────

def backup_dir_path() -> Path:
    """Where `backup_dir` points, without creating it (the health check reads its disk)."""
    from vaf.core.config import Config
    from vaf.core.platform import Platform
    configured = str(Config.get("backup_dir", "") or "").strip()
    return Path(os.path.expanduser(configured)) if configured else Platform.data_dir() / "backups"


def backup_dir() -> Path:
    """`backup_dir` from the config, else ``<data dir>/backups``; owner-only."""
    from vaf.core.secure_store import harden_dir
    path = backup_dir_path()
    path.mkdir(parents=True, exist_ok=True)
    harden_dir(path)
    return path


def machine_identity():
    """This machine's backup identity, created on first use and kept in the data keyring."""
    from pyrage import x25519

    from vaf.core.data_keyring import peek_data_secret, set_data_secret
    value = peek_data_secret(IDENTITY_SECRET)
    if not value:
        value = str(x25519.Identity.generate())
        set_data_secret(IDENTITY_SECRET, value)
    return x25519.Identity.from_str(value)


def decrypting_identities() -> List[object]:
    """Every identity this machine may open a backup with: the current one, then those it
    held before a restore replaced its keyring."""
    from pyrage import x25519

    from vaf.core.data_keyring import peek_data_secret
    out = [machine_identity()]
    try:
        for value in json.loads(peek_data_secret(PREVIOUS_IDENTITIES_SECRET) or "[]"):
            out.append(x25519.Identity.from_str(str(value)))
    except Exception as e:
        logger.warning("The previous backup identities could not be read: %s", e)
    return out


def _keep_previous_identity(previous: Optional[str]) -> None:
    from vaf.core.data_keyring import peek_data_secret, set_data_secret
    current = peek_data_secret(IDENTITY_SECRET)
    if not previous or previous == current:
        return
    known = json.loads(peek_data_secret(PREVIOUS_IDENTITIES_SECRET) or "[]")
    if previous not in known:
        set_data_secret(PREVIOUS_IDENTITIES_SECRET, json.dumps(known + [previous]))


def recipients() -> List[Tuple[str, object]]:
    """(label, age recipient) for every key a backup is encrypted to."""
    from pyrage import x25519

    from vaf.core.recovery_kit import recipient
    out: List[Tuple[str, object]] = [("this machine", machine_identity().to_public())]
    offline = recipient()
    if offline:
        out.append(("the recovery key", x25519.Recipient.from_str(offline)))
    return out


# ── create ────────────────────────────────────────────────────────────────────────

def create_backup(dest: Optional[Path] = None, *, include_projects: bool = True,
                  include_volumes: bool = True, pg_container: Optional[str] = None,
                  progress: Optional[Callable[[str], None]] = None) -> BackupResult:
    """Write one backup into ``dest`` (default `backup_dir()`), verify it, return what it is.

    Raises BackupError and leaves nothing behind when any part that belongs in it - the
    database above all - could not be captured: a backup that silently lacks the database
    is worse than none, because it is trusted."""
    import pyrage

    say = progress or (lambda _msg: None)
    dest = Path(dest) if dest else backup_dir()
    created = not dest.exists()
    dest.mkdir(parents=True, exist_ok=True)
    from vaf.core.secure_store import _get_filelock_cls, harden_dir, harden_path
    if created:
        # A folder this call made is owner-only. One the person named that was already there
        # (`--to /mnt/usb`, a Documents folder) keeps its permissions: the backup file and its
        # checksum are owner-only either way, and the configured folder hardens itself.
        harden_dir(dest)
    lock_cls = _get_filelock_cls()
    lock = lock_cls(str(dest / ".backup.lock"), timeout=5) if lock_cls else _NoLock()

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = dest / f"vaf-backup-{stamp}{SUFFIX}"
    partial = dest / f".{final.name}.partial"
    to = recipients()
    warnings: List[str] = []
    skipped: List[str] = []
    if len(to) == 1:
        warnings.append("Only this machine can open this backup: the recovery key is version 1 "
                        "and has no public half. Run `vaf secure renew-recovery-key`.")
    try:
        lock.acquire()
    except Exception:
        raise BackupError("Another backup is running.")
    # The plaintext parts (database dump, SQLite copies, volume tars, the machine key) are
    # staged on THIS machine, owner-only, never in `dest`: a backup folder can be a USB stick
    # or a network mount, where a deleted plaintext copy is still recoverable.
    staging = _private_staging(".backup-staging-")
    try:
        say("Collecting files")
        members = _collect_files(staging, include_projects, skipped, warnings, dest)
        say("Dumping the memory database")
        container = pg_container or PG_CONTAINER
        pg_members, pg_info = _dump_postgres(staging, container)
        members += pg_members
        # The machine key, so a restore on another machine can re-wrap every store it sealed,
        # whatever backend held it here (an OS keyring entry is not a file the walk finds).
        from vaf.core.recovery_kit import _machine_kek_for_kit
        machine_key = _machine_kek_for_kit()
        if machine_key:
            keyfile = staging / "keys" / "machine_key"
            keyfile.parent.mkdir(parents=True, exist_ok=True)
            keyfile.write_bytes(machine_key)
            members.append(("keys/machine_key", keyfile))
        volume_info: List[Dict] = []
        if include_volumes:
            say("Copying sandbox and browser volumes")
            vol_members, volume_info = _collect_volumes(staging, warnings, container)
            members += vol_members
        _note_keyring(warnings)
        manifest = _manifest(include_projects, include_volumes, pg_info, volume_info,
                             [label for label, _ in to], skipped, warnings)
        say("Encrypting")
        vanished: List[str] = []
        size, digest, count = _write_encrypted(partial, manifest, members,
                                               [r for _, r in to], pyrage, vanished)
        for name in vanished:
            warnings.append(f"{name} was removed while the backup ran and is not in it.")
        os.replace(partial, final)
        harden_path(final)
        sidecar = final.with_name(final.name + ".sha256")
        sidecar.write_text(f"{digest}  {final.name}\n", encoding="utf-8")
        harden_path(sidecar)
        say("Verifying")
        check = verify_backup(final)
        if not check.ok:
            raise BackupError("The backup did not verify: " + "; ".join(check.problems))
        return BackupResult(final, size, digest, count, True, [label for label, _ in to],
                            skipped, warnings)
    except BackupError:
        _discard(partial, final)
        raise
    except Exception as e:
        _discard(partial, final)
        raise BackupError(f"The backup failed: {type(e).__name__}: {e}") from e
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            lock.release()
        except Exception:
            pass


def _private_staging(prefix: str) -> Path:
    """An owner-only folder in the local data directory for plaintext in transit. A folder
    of the same kind older than a day is a run that crashed before its cleanup, holding
    plaintext nobody needs: it goes first."""
    from vaf.core.platform import Platform
    data = Platform.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    for stale in data.glob(prefix + "*"):
        try:
            if stale.is_dir() and time.time() - stale.stat().st_mtime > 86400:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            pass
    staging = Path(tempfile.mkdtemp(prefix=prefix, dir=data))
    os.chmod(staging, 0o700)
    return staging


class _NoLock:
    def acquire(self):
        pass

    def release(self):
        pass


def _discard(*paths: Path) -> None:
    for path in paths:
        for p in (path, path.with_name(path.name + ".sha256")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning("Could not remove %s: %s", p, e)


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def _sqlite_copy(src: Path, dst: Path) -> None:
    """SQLite's own online backup: consistent while the database is in use."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(src), timeout=30)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


def _collect_files(staging: Path, include_projects: bool, skipped: List[str],
                   warnings: List[str], dest: Path) -> List[Tuple[str, Path]]:
    from vaf.core import data_stores

    members: List[Tuple[str, Path]] = []
    sqlite_bases = set()
    # Never the backups themselves: neither the destination of this one nor the configured
    # backup folder (`--to` elsewhere, or the pre-restore folder below it, while backup_dir
    # sits inside a root). Staging folders are left out by name below.
    excluded = [dest]
    try:
        excluded.append(backup_dir())
    except Exception:
        pass
    walked = list(data_stores.walk(include_projects=include_projects))
    for _root, path, row in walked:
        if data_stores.backed_up(row) and path.is_file() and not path.is_symlink() \
                and _is_sqlite(path):
            sqlite_bases.add(str(path))
    for n, (root, path, row) in enumerate(walked):
        if any(_inside(path, folder) for folder in excluded) or \
                path.name.startswith((".backup-staging-", ".restore-")):
            continue                     # backups, and plaintext in transit: never taken
        if row is None:
            skipped.append(f"{path} (not one of VAF's stores)")
            continue
        if not data_stores.backed_up(row):
            continue
        if path.is_symlink():
            warnings.append(f"{path} is a link and was not followed.")
            continue
        if not path.is_file():
            continue
        if any(str(path).endswith(s) and str(path)[:-len(s)] in sqlite_bases
               for s in _SQLITE_SIDECARS):
            continue
        arc = f"files/{row.root}/{path.relative_to(root).as_posix()}"
        if str(path) in sqlite_bases:
            copy = staging / "sqlite" / f"{n}.db"
            _sqlite_copy(path, copy)
            members.append((arc, copy))
        else:
            members.append((arc, path))
    return members


def _pg_target() -> Dict[str, str]:
    from urllib.parse import unquote, urlsplit

    from vaf.core.config import Config
    url = (str(Config.get("memory_db_owner_url", "") or "").strip()
           or str(Config.get("memory_db_url", "") or "").strip()
           or "postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory")
    parts = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    return {"role": unquote(parts.username or "vaf"),
            "database": (parts.path or "/vaf_memory").lstrip("/") or "vaf_memory",
            "host": parts.hostname or "localhost", "port": str(parts.port or 5432)}


def _docker_exe() -> str:
    from vaf.core.service_stack import resolve_docker_exe
    return resolve_docker_exe()


def _wait_for_postgres(role: str, container: str = PG_CONTAINER, timeout: float = 90) -> None:
    from vaf.core import containers
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = containers.docker(["exec", container, "pg_isready", "-U", role], timeout=15)
        if r.returncode == 0:
            return
        time.sleep(2)
    raise BackupError("The memory database did not become ready.")


def _dump_postgres(staging: Path, container: str = PG_CONTAINER) -> Tuple[List[Tuple[str, Path]], Dict]:
    """The database and its roles, dumped inside the database container as the owner."""
    from vaf.core import containers

    target = _pg_target()
    try:
        state = containers.container_state(container)
    except Exception as e:
        raise BackupError(f"Docker does not answer, so the memory database cannot be dumped: {e}")
    if state is None:
        raise BackupError(f"There is no {container} container, so the memory database cannot "
                          "be dumped. A backup without it would not restore the accounts or the "
                          "memories.")
    started = False
    if state != "running":
        r = containers.docker(["start", container], timeout=60)
        if r.returncode != 0:
            raise BackupError(f"The memory database could not be started: {(r.stderr or '').strip()[:200]}")
        started = True
    try:
        _wait_for_postgres(target["role"], container)
        dump = staging / "postgres" / "database.dump"
        roles = staging / "postgres" / "globals.sql"
        dump.parent.mkdir(parents=True, exist_ok=True)
        for out, argv in ((dump, ["pg_dump", "-U", target["role"], "-d", target["database"], "-Fc"]),
                          (roles, ["pg_dumpall", "-U", target["role"], "--globals-only"])):
            with open(out, "wb") as f:
                r = subprocess.run([_docker_exe(), "exec", container, *argv], stdout=f,
                                   stderr=subprocess.PIPE, timeout=3600,
                                   **containers.windowless_kwargs())
            if r.returncode != 0:
                raise BackupError(f"{argv[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()[:300]}")
        version = containers.docker(["exec", container, "psql", "-U", target["role"], "-d",
                                     target["database"], "-Atc", "show server_version"], timeout=30)
        info = {"container": container, "database": target["database"], "role": target["role"],
                "server_version": (version.stdout or "").strip()}
        return [("postgres/database.dump", dump), ("postgres/globals.sql", roles)], info
    finally:
        if started:
            containers.docker(["stop", container], timeout=60)


def _docker_cp_tar(container: str, path: str, out: Path, drop: frozenset = frozenset(),
                   drop_top: frozenset = frozenset()) -> int:
    """``docker cp <container>:<path> -`` (a tar stream; works on a stopped container) into
    ``out``, leaving out every member below a directory named in ``drop`` wherever it sits,
    and every member below a top-level entry of ``path`` named in ``drop_top``. Returns the
    number of members kept."""
    from vaf.core import containers
    proc = subprocess.Popen([_docker_exe(), "cp", f"{container}:{path}", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **containers.windowless_kwargs())
    kept = 0
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as src, \
                tarfile.open(out, mode="w", format=tarfile.PAX_FORMAT) as dst:
            for member in src:
                parts = Path(member.name).parts
                if drop and any(part in drop for part in parts):
                    continue
                if drop_top and len(parts) > 1 and parts[1] in drop_top:
                    continue
                if member.isfile():
                    dst.addfile(member, src.extractfile(member))
                else:
                    dst.addfile(member)
                kept += 1
    finally:
        # Our end of its stdout first: after an interrupted read, docker cp may be blocked
        # writing into a full pipe, and waiting on it (or reading its stderr) would hang.
        if proc.stdout:
            proc.stdout.close()
        try:
            code = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait(timeout=30)
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        if code != 0:
            raise BackupError(f"docker cp {container}:{path} failed: {err.strip()[:200]}")
    return kept


def _carrier_image(container: str = PG_CONTAINER) -> str:
    """An image that is surely here: the database's. A carrier container is only CREATED
    from it, never started - it exists to mount a volume for `docker cp`."""
    from vaf.core import containers
    r = containers.docker(["inspect", container, "--format", "{{.Config.Image}}"], timeout=30)
    image = (r.stdout or "").strip()
    if r.returncode != 0 or not image:
        raise BackupError("No image to read the volumes with (the database container is missing).")
    return image


def _volume_tar(volume: str, out: Path, image: str, drop: frozenset = frozenset(),
                drop_top: frozenset = frozenset()) -> int:
    """The volume's content as a tar (members under ``v/``), read through a carrier
    container that mounts it read-only. By volume, not by the container that uses it: a
    personal browser's container is recreated, its profile volume stays, and a volume
    whose container is gone must not be lost from the backup."""
    import secrets as _secrets

    from vaf.core import containers
    carrier = f"vaf-backup-carrier-{_secrets.token_hex(4)}"
    r = containers.docker(["create", "--name", carrier, "-v", f"{volume}:/v:ro", image, "true"],
                          timeout=60)
    if r.returncode != 0:
        raise BackupError(f"The volume {volume} could not be mounted: {(r.stderr or '').strip()[:200]}")
    try:
        return _docker_cp_tar(carrier, "/v", out, drop=drop, drop_top=drop_top)
    finally:
        containers.docker(["rm", "-f", carrier], timeout=60)


def _volumes(prefix: str) -> List[str]:
    from vaf.core import containers
    r = containers.docker(["volume", "ls", "--format", "{{.Name}}", "--filter", f"name={prefix}"],
                          timeout=30)
    if r.returncode != 0:
        raise BackupError(f"Docker did not list the volumes: {(r.stderr or '').strip()[:200]}")
    return sorted(n.strip() for n in (r.stdout or "").splitlines() if n.strip().startswith(prefix))


def _collect_volumes(staging: Path, warnings: List[str],
                     container: str = PG_CONTAINER) -> Tuple[List[Tuple[str, Path]], List[Dict]]:
    """Every sandbox environment volume (a person's work, when the environment keeps it in a
    volume rather than a host folder) and every personal browser profile volume."""
    members: List[Tuple[str, Path]] = []
    info: List[Dict] = []
    vol_dir = staging / "volumes"
    vol_dir.mkdir(parents=True, exist_ok=True)
    image = _carrier_image(container)
    for volume in _volumes("vaf-env-vol-"):
        out = vol_dir / f"{volume}.tar"
        _volume_tar(volume, out, image)
        members.append((f"volumes/{volume}.tar", out))
        info.append({"kind": "environment", "volume": volume})
    for volume in _volumes("vaf-browser-profile-"):
        out = vol_dir / f"{volume}.tar"
        try:
            _volume_tar(volume, out, image, drop=_BROWSER_CACHES, drop_top=_BROWSER_NOT_PROFILE)
        except Exception as e:
            # A browser login can be repeated; a backup that fails every night over one
            # profile protects nothing else either.
            warnings.append(f"The browser profile {volume} could not be copied ({e}).")
            continue
        members.append((f"volumes/{volume}.tar", out))
        info.append({"kind": "browser", "volume": volume})
    return members, info


def _note_keyring(warnings: List[str]) -> None:
    try:
        from vaf.core.credential_store import is_keyring_used
        if is_keyring_used():
            warnings.append("Mail, cloud and GitHub sign-ins kept in the operating system's "
                            "keyring are not in this backup; on another machine they are "
                            "signed in again.")
    except Exception:
        pass


def _manifest(include_projects, include_volumes, pg_info, volume_info, recipient_labels,
              skipped, warnings) -> Dict:
    from vaf.core import data_stores
    from vaf.version import __version__
    return {
        "format": BACKUP_FORMAT,
        "vaf_version": __version__,
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "roots": {name: str(path) for name, path in data_stores.roots().items()},
        "includes": {"projects": include_projects, "volumes": include_volumes},
        "postgres": pg_info,
        "volumes": volume_info,
        "recipients": recipient_labels,
        "skipped": skipped,
        "warnings": warnings,
    }


class _HashingWriter:
    def __init__(self, f):
        self._f = f
        self.sha = hashlib.sha256()
        self.size = 0

    def write(self, data) -> int:
        self.sha.update(data)
        self.size += len(data)
        return self._f.write(data)

    def flush(self) -> None:
        self._f.flush()


class _HashingReader:
    def __init__(self, f):
        self._f = f
        self.sha = hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        data = self._f.read(n)
        self.sha.update(data)
        return data


def _write_encrypted(out_path: Path, manifest: Dict, members: List[Tuple[str, Path]],
                     to: List, pyrage, vanished: Optional[List[str]] = None) -> Tuple[int, str, int]:
    """Stream the tar through age into ``out_path``: no plaintext archive on disk. A file
    removed since the walk is skipped and named in ``vanished``."""
    read_fd, write_fd = os.pipe()
    failure: List[BaseException] = []
    count = [0]
    gone = vanished if vanished is not None else []

    def produce() -> None:
        try:
            with os.fdopen(write_fd, "wb") as pipe, \
                    tarfile.open(fileobj=pipe, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                checksums: Dict[str, str] = {}
                _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=1).encode("utf-8"),
                           checksums)
                for arc, src in members:
                    # Opened first, measured on the open file: replaced between a stat and
                    # the open, the header named one size and the read another.
                    try:
                        f = open(src, "rb")
                    except FileNotFoundError:
                        gone.append(str(src))
                        continue
                    with f:
                        st = os.fstat(f.fileno())
                        info = tarfile.TarInfo(arc)
                        info.size = st.st_size
                        info.mtime = int(st.st_mtime)
                        info.mode = _archived_mode(arc, st.st_mode)
                        reader = _HashingReader(f)
                        tar.addfile(info, reader)
                    checksums[arc] = reader.sha.hexdigest()
                    count[0] += 1
                _add_bytes(tar, "checksums.json",
                           json.dumps(checksums, indent=0).encode("utf-8"), None)
        except BaseException as e:      # noqa: BLE001 - reported by the consumer
            failure.append(e)

    worker = threading.Thread(target=produce, name="vaf-backup-tar", daemon=True)
    worker.start()
    try:
        with os.fdopen(read_fd, "rb") as pipe, open(out_path, "wb") as raw:
            writer = _HashingWriter(raw)
            pyrage.encrypt_io(pipe, writer, to)
            raw.flush()
            os.fsync(raw.fileno())
    finally:
        worker.join(timeout=60)
    if failure:
        raise failure[0]
    return writer.size, writer.sha.hexdigest(), count[0]


def _archived_mode(arc: str, st_mode: int) -> int:
    """Owner-only, except in the person's working folders: a script there keeps its
    executable bit, and a file its read bits, through a backup and a restore."""
    return (st_mode & 0o777) if arc.startswith(_PROJECTS_PREFIX) else 0o600


def _restored_mode(name: str, mode: int) -> int:
    """The mode a member gets back: the working folders' as they were, without write bits
    for anyone but the owner (a Windows source reports 0o666); owner-only for the rest."""
    return ((mode & 0o755) | 0o600) if name.startswith(_PROJECTS_PREFIX) else 0o600


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, checksums: Optional[Dict]) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))
    if checksums is not None:
        checksums[name] = hashlib.sha256(data).hexdigest()


# ── verify, list ──────────────────────────────────────────────────────────────────

def _drain(pipe) -> None:
    """Read what follows the archive's end marker. tarfile stops at the first zero block, and
    the writer still has the rest of the last record to put into the pipe: closing it early
    broke that write, and the backup read as "does not open" (measured, intermittently)."""
    while pipe.read(_CHUNK):
        pass


def file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            sha.update(chunk)
    return sha.hexdigest()


def verify_backup(path: Path, identity: Optional[str] = None) -> VerifyResult:
    """Open the backup and check every member against its checksum. ``identity`` is an age
    identity (the recovery key); without one, this machine's backup identity is used."""
    import pyrage

    path = Path(path)
    problems: List[str] = []
    sidecar = path.with_name(path.name + ".sha256")
    if sidecar.exists():
        expected = sidecar.read_text(encoding="utf-8").split()[0]
        if file_sha256(path) != expected:
            return VerifyResult(False, problems=["the file changed after it was written "
                                                 "(its .sha256 does not match)"])
    # The same reading of the key as a restore: a key verify accepts must also restore, and
    # a retyped key with a mixed-case body is invalid Bech32 until it is upper-cased.
    try:
        idents = _identities(identity)
    except BackupError as e:
        return VerifyResult(False, problems=[str(e)])
    except Exception as e:
        return VerifyResult(False, problems=[f"this machine's backup key could not be read: {e}"])

    read_fd, write_fd = os.pipe()
    failure: List[BaseException] = []

    def decrypt() -> None:
        try:
            # The pipe first: a backup that does not open (gone, unreadable) must still
            # close the write end, or the reader waits on it forever.
            with os.fdopen(write_fd, "wb") as pipe, open(path, "rb") as src:
                pyrage.decrypt_io(src, pipe, idents)
        except BaseException as e:      # noqa: BLE001 - reported below
            failure.append(e)

    worker = threading.Thread(target=decrypt, name="vaf-backup-verify", daemon=True)
    worker.start()
    manifest: Dict = {}
    seen: Dict[str, str] = {}
    declared: Optional[Dict[str, str]] = None
    try:
        with os.fdopen(read_fd, "rb") as pipe, tarfile.open(fileobj=pipe, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                f = tar.extractfile(member)
                sha = hashlib.sha256()
                data = b""
                small = member.name in ("manifest.json", "checksums.json")
                for chunk in iter(lambda: f.read(_CHUNK), b""):
                    sha.update(chunk)
                    if small:
                        data += chunk
                if member.name == "manifest.json":
                    manifest = json.loads(data)
                if member.name == "checksums.json":
                    declared = json.loads(data)
                else:
                    seen[member.name] = sha.hexdigest()
            _drain(pipe)
    except Exception as e:
        problems.append(f"the archive could not be read: {type(e).__name__}: {e}")
    worker.join(timeout=60)
    if failure:
        return VerifyResult(False, problems=[f"it does not open with this key: "
                                             f"{type(failure[0]).__name__}: {failure[0]}"])
    if manifest.get("format") != BACKUP_FORMAT:
        problems.append(f"not a VAF backup of a known format ({manifest.get('format')!r})")
    if declared is None:
        problems.append("the checksum list is missing: the archive is cut short")
    else:
        for name, digest in declared.items():
            if seen.get(name) != digest:
                problems.append(f"{name} does not match its checksum")
        extra = set(seen) - set(declared)
        if extra:
            problems.append(f"members without a checksum: {sorted(extra)[:5]}")
    return VerifyResult(not problems, len([n for n in seen if n != "manifest.json"]),
                        manifest, problems)


@dataclass
class BackupInfo:
    path: Path
    size: int
    created: str


def list_backups(dest: Optional[Path] = None) -> List[BackupInfo]:
    """The backups in ``dest`` (default `backup_dir()`), newest first."""
    dest = Path(dest) if dest else backup_dir()
    out = []
    for p in dest.glob(f"vaf-backup-*{SUFFIX}"):
        stamp = p.name[len("vaf-backup-"):-len(SUFFIX)]
        out.append(BackupInfo(p, p.stat().st_size, stamp))
    return sorted(out, key=lambda b: b.created, reverse=True)


# ── restore ───────────────────────────────────────────────────────────────────────

# What a backup may contain, member by member. Anything else is a foreign or tampered
# archive, and the whole restore is refused before one byte lands.
_ROOT_NAMES = ("app", "vaf", "data", "config", "projects")
_VOLUME_NAME = __import__("re").compile(r"^vaf-(env-vol|browser-profile)-[A-Za-z0-9_.-]+$")
# Never placed from a backup: the TARGET machine's key stays, and the stores are re-wrapped
# under it (`rewrap_from_kek`). Overwriting it would orphan every store this machine sealed.
_KEEP_ON_TARGET = frozenset({"secure_store.kek", "secure_store.kek.where", "secure_store.kek.lock"})


@dataclass
class RestoreResult:
    manifest: Dict
    files: int
    rewrapped: List[str]
    volumes: int
    pre_restore: Optional[Path]
    warnings: List[str] = field(default_factory=list)


def read_manifest(path: Path, identity: Optional[str] = None) -> Dict:
    """The manifest alone (the first member), to show what a backup is before restoring it."""
    import pyrage
    idents = _identities(identity)
    read_fd, write_fd = os.pipe()

    def decrypt() -> None:
        try:
            # The pipe first: a backup that does not open (gone, unreadable) must still
            # close the write end, or the reader waits on it forever.
            with os.fdopen(write_fd, "wb") as pipe, open(path, "rb") as src:
                pyrage.decrypt_io(src, pipe, idents)
        except Exception:       # noqa: BLE001 - a short read below says enough
            pass

    worker = threading.Thread(target=decrypt, daemon=True)
    worker.start()
    try:
        with os.fdopen(read_fd, "rb") as pipe, tarfile.open(fileobj=pipe, mode="r|") as tar:
            first = next(iter(tar), None)
            if first is None or first.name != "manifest.json":
                raise BackupError("This file does not open with that key, or it is not a VAF backup.")
            return json.loads(tar.extractfile(first).read())
    except BackupError:
        raise
    except Exception as e:
        raise BackupError(f"This file does not open with that key: {type(e).__name__}") from e


def _identities(identity: Optional[str]) -> List[object]:
    from pyrage import x25519
    if identity:
        try:
            return [x25519.Identity.from_str(identity.strip().upper())]
        except Exception as e:
            raise BackupError(f"That is not a recovery key ({e}).") from e
    return decrypting_identities()


def _safe_member(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        return False
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    if name in ("manifest.json", "checksums.json", "keys/machine_key",
                "postgres/database.dump", "postgres/globals.sql"):
        return True
    if parts[0] == "files" and len(parts) >= 3 and parts[1] in _ROOT_NAMES:
        return True
    if parts[0] == "volumes" and len(parts) == 2 and parts[1].endswith(".tar"):
        return bool(_VOLUME_NAME.match(parts[1][:-len(".tar")]))
    return False


def _extract_verified(path: Path, identity: Optional[str], staging: Path) -> Tuple[Dict, List[str]]:
    """Decrypt and unpack into ``staging``, checking every member's name, type and checksum.
    Raises BackupError on the first thing that does not fit; returns (manifest, members)."""
    import pyrage
    idents = _identities(identity)
    read_fd, write_fd = os.pipe()
    failure: List[BaseException] = []

    def decrypt() -> None:
        try:
            # The pipe first: a backup that does not open (gone, unreadable) must still
            # close the write end, or the reader waits on it forever.
            with os.fdopen(write_fd, "wb") as pipe, open(path, "rb") as src:
                pyrage.decrypt_io(src, pipe, idents)
        except BaseException as e:      # noqa: BLE001 - reported below
            failure.append(e)

    worker = threading.Thread(target=decrypt, name="vaf-restore-decrypt", daemon=True)
    worker.start()
    seen: Dict[str, str] = {}
    declared: Optional[Dict[str, str]] = None
    manifest: Dict = {}
    try:
        with os.fdopen(read_fd, "rb") as pipe, tarfile.open(fileobj=pipe, mode="r|") as tar:
            for member in tar:
                if not member.isfile() or not _safe_member(member.name):
                    raise BackupError(f"The backup holds an entry a VAF backup never has "
                                      f"({member.name!r}); nothing was restored.")
                src = tar.extractfile(member)
                sha = hashlib.sha256()
                if member.name in ("manifest.json", "checksums.json"):
                    data = src.read()
                    sha.update(data)
                    if member.name == "manifest.json":
                        manifest = json.loads(data)
                    else:
                        declared = json.loads(data)
                        continue
                else:
                    out = staging.joinpath(*member.name.split("/"))
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with open(out, "wb") as f:
                        for chunk in iter(lambda: src.read(_CHUNK), b""):
                            sha.update(chunk)
                            f.write(chunk)
                    os.chmod(out, _restored_mode(member.name, member.mode))
                seen[member.name] = sha.hexdigest()
            _drain(pipe)
    except BackupError:
        raise
    except Exception as e:
        raise BackupError(f"The backup could not be read: {type(e).__name__}: {e}") from e
    finally:
        worker.join(timeout=120)
    if failure:
        raise BackupError(f"The backup does not open with this key: {type(failure[0]).__name__}")
    if manifest.get("format") != BACKUP_FORMAT:
        raise BackupError(f"Not a VAF backup of a known format ({manifest.get('format')!r}).")
    if declared is None or set(declared) != set(seen) or any(seen[n] != d for n, d in declared.items()):
        raise BackupError("The backup does not match its checksums; nothing was restored.")
    return manifest, sorted(seen)


def _check_version(manifest: Dict) -> None:
    """A backup written by a NEWER VAF may carry a format this one cannot read."""
    from packaging.version import InvalidVersion, Version

    from vaf.version import __version__
    try:
        if Version(str(manifest.get("vaf_version", "0"))) > Version(__version__):
            raise BackupError(f"This backup was written by VAF {manifest.get('vaf_version')}, "
                              f"newer than this one ({__version__}). Update VAF first.")
    except InvalidVersion:
        raise BackupError(f"The backup names no readable VAF version ({manifest.get('vaf_version')!r}).")


def _psql(container: str, role: str, sql: str, database: str = "postgres") -> None:
    from vaf.core import containers
    r = containers.docker(["exec", container, "psql", "-U", role, "-d", database,
                           "-v", "ON_ERROR_STOP=1", "-c", sql], timeout=300)
    if r.returncode != 0:
        raise BackupError(f"The database refused {sql.split()[0]}: {(r.stderr or '').strip()[:300]}")


def _restore_postgres(staging: Path, manifest: Dict, container: str) -> None:
    """Replace the memory database with the backup's: the roles first (their password hashes,
    so the restored connection strings fit), then a fresh database from the dump."""
    from vaf.core import containers
    info = manifest.get("postgres") or {}
    role = str(info.get("role") or "vaf")
    database = str(info.get("database") or "vaf_memory")
    quoted_db = '"' + database.replace('"', '""') + '"'
    quoted_role = '"' + role.replace('"', '""') + '"'
    _wait_for_postgres(role, container)
    _psql(container, role, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                           f"WHERE datname = '{database.replace(chr(39), chr(39) * 2)}' "
                           "AND pid <> pg_backend_pid()")
    _psql(container, role, f"DROP DATABASE IF EXISTS {quoted_db}")
    # CREATE ROLE for a role that exists fails and is skipped; the ALTER ROLE after it sets
    # the backup's attributes and password hash, which is the point.
    with open(staging / "postgres" / "globals.sql", "rb") as f:
        r = subprocess.run([_docker_exe(), "exec", "-i", container, "psql", "-U", role, "-d",
                            "postgres", "-q"], stdin=f, capture_output=True, timeout=300,
                           **containers.windowless_kwargs())
    # Without ON_ERROR_STOP psql runs past a failing statement and still exits 0; a non-zero
    # exit is psql itself failing (no connection), and then no role has the backup's password.
    if r.returncode != 0:
        raise BackupError("The roles could not be restored: "
                          + r.stderr.decode("utf-8", "replace").strip()[:300])
    _psql(container, role, f"CREATE DATABASE {quoted_db} OWNER {quoted_role}")
    with open(staging / "postgres" / "database.dump", "rb") as f:
        r = subprocess.run([_docker_exe(), "exec", "-i", container, "pg_restore", "-U", role,
                            "-d", database, "--exit-on-error"], stdin=f, capture_output=True,
                           timeout=3600, **containers.windowless_kwargs())
    if r.returncode != 0:
        raise BackupError("pg_restore failed: " + r.stderr.decode("utf-8", "replace").strip()[:400])


def _restore_volume(tar_path: Path, volume: str, image: str,
                    keep_top: frozenset = frozenset(), owner: Optional[str] = None) -> None:
    """Empty the volume (creating it when missing) and unpack the backup's copy into it,
    owners included (`docker cp -a`), through a carrier container. Top-level entries named
    in ``keep_top`` are left alone: what the backup deliberately did not carry (a browser's
    Downloads not yet handed over) is not the backup's to delete."""
    import secrets as _secrets

    from vaf.core import containers
    users = containers.docker(["ps", "-q", "--filter", f"volume={volume}"], timeout=30)
    for cid in (users.stdout or "").split():
        containers.docker(["stop", cid], timeout=60)
    containers.docker(["volume", "create", volume], timeout=60)
    spare = " ".join(f"! -name '{name}'" for name in sorted(keep_top))
    r = containers.docker(["run", "--rm", "-v", f"{volume}:/v", "--entrypoint", "sh", image, "-c",
                           f"find /v -mindepth 1 -maxdepth 1 {spare} -exec rm -rf {{}} +"],
                          timeout=300)
    if r.returncode != 0:
        raise BackupError(f"The volume {volume} could not be emptied: {(r.stderr or '').strip()[:200]}")
    carrier = f"vaf-backup-carrier-{_secrets.token_hex(4)}"
    r = containers.docker(["create", "--name", carrier, "-v", f"{volume}:/v", image, "true"], timeout=60)
    if r.returncode != 0:
        raise BackupError(f"The volume {volume} could not be mounted: {(r.stderr or '').strip()[:200]}")
    try:
        with open(tar_path, "rb") as f:
            r = subprocess.run([_docker_exe(), "cp", "-a", "-", f"{carrier}:/"], stdin=f,
                               capture_output=True, timeout=3600, **containers.windowless_kwargs())
        if r.returncode != 0:
            raise BackupError(f"The volume {volume} could not be filled: "
                              + r.stderr.decode("utf-8", "replace").strip()[:200])
    finally:
        containers.docker(["rm", "-f", carrier], timeout=60)
    if owner:
        # A sandbox environment runs as this machine's user (environments._host_user), which
        # need not be the uid the files had on the old one.
        r = containers.docker(["run", "--rm", "-v", f"{volume}:/v", "--entrypoint", "chown", image,
                               "-R", owner, "/v"], timeout=600)
        if r.returncode != 0:
            raise BackupError(f"The volume {volume} could not be handed to its user: "
                              f"{(r.stderr or '').strip()[:200]}")


def _old_machine_key(staging: Path) -> Optional[bytes]:
    """The backed-up machine's key: the ``keys/machine_key`` member, else the key FILE among
    the backed-up files (a file-backend machine; the member was added for keyring backends)."""
    member = staging / "keys" / "machine_key"
    if member.exists():
        return member.read_bytes()
    import base64
    keyfile = staging / "files" / "app" / "secure_store.kek"
    try:
        key = base64.b64decode(keyfile.read_text(encoding="utf-8").strip())
        return key if len(key) == 32 else None
    except Exception:
        return None


def _staged_project_environments(staging: Path) -> List[str]:
    """The ids of the project sandbox environments among the staged files."""
    out = []
    for record in sorted((staging / "files" / "vaf" / "environments").glob("*.json")):
        if record.name.endswith(".procs.json"):
            continue
        try:
            if json.loads(record.read_text(encoding="utf-8")).get("kind") == "project":
                out.append(record.stem)
        except Exception:
            continue
    return out


def _clear_target(manifest: Dict) -> None:
    """Remove what the backup replaces: every entry of the roots that is backed up, derived
    or runtime state (a cache or queue of the old state would only confuse the restored
    one). Logs, unknown entries, the backup folder and this machine's key stay."""
    from vaf.core import data_stores
    replaced = {data_stores.DATA, data_stores.KEY, data_stores.RECREATABLE, data_stores.AUDIT,
                data_stores.DERIVED, data_stores.RUNTIME}
    include_projects = bool((manifest.get("includes") or {}).get("projects", True))
    projects = data_stores.projects_dir()
    folder = backup_dir()
    for root, rows in data_stores.distinct_roots():
        if not root.is_dir():
            continue
        if not include_projects and data_stores._same(root, projects):
            continue
        for entry in list(root.iterdir()):
            row = data_stores.classify(entry.name, rows)
            if row is None or row.kind not in replaced or entry.name in _KEEP_ON_TARGET:
                continue
            if _inside(folder, entry) or _inside(entry, folder) or entry.name.startswith(".restore-"):
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()


def _place_files(staging: Path) -> int:
    from vaf.core import data_stores
    from vaf.core.secure_store import harden_path
    roots = data_stores.roots()
    placed = 0
    files_dir = staging / "files"
    if not files_dir.is_dir():
        return 0
    for root_name in _ROOT_NAMES:
        base = files_dir / root_name
        if not base.is_dir():
            continue
        for src in sorted(base.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(base)
            if root_name == "app" and rel.as_posix() in _KEEP_ON_TARGET:
                continue
            target = roots[root_name] / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
            if root_name != "projects":       # the working folders keep their modes
                harden_path(target)
            placed += 1
    return placed


def restore_backup(path: Path, *, identity: Optional[str] = None,
                   pg_container: Optional[str] = None, pre_restore: bool = True,
                   progress: Optional[Callable[[str], None]] = None) -> RestoreResult:
    """Replace everything VAF keeps on this machine with the backup's.

    In this order, each step only after the one before succeeded: open and check every
    member (nothing lands from a backup that does not verify); refuse a backup from a newer
    VAF; write a backup of the CURRENT state first, so a wrong restore can be undone; clear
    and place the files; re-wrap every store the backup's machine key sealed under THIS
    machine's key (never overwriting it); replace the database; fill the volumes.

    VAF must not be running: the callers check, because a running instance would write
    into the folders while they are replaced."""
    from vaf.core.secure_store import rewrap_from_kek

    say = progress or (lambda _msg: None)
    container = pg_container or PG_CONTAINER
    staging = _private_staging(".restore-")
    warnings: List[str] = []
    pre: Optional[Path] = None
    changed = False
    try:
        say("Opening and checking the backup")
        manifest, _members = _extract_verified(Path(path), identity, staging)
        _check_version(manifest)
        from vaf.core import containers
        state = containers.container_state(container)
        if state is None:
            raise BackupError(f"There is no {container} container to restore the database into. "
                              "Start VAF's services once (vaf service start), then stop VAF "
                              "and restore again.")
        if state != "running":
            r = containers.docker(["start", container], timeout=60)
            if r.returncode != 0:
                raise BackupError(f"The database could not be started: {(r.stderr or '').strip()[:200]}")
        # A project environment comes back only with a container, and a container needs the
        # environment image. Without it the records would land, nothing could recreate them,
        # and the reaper would delete them with their volumes at the next start: refuse now,
        # before anything is replaced.
        if _staged_project_environments(staging):
            say("Making sure the sandbox environment image is here")
            from vaf.core import environment_image
            if not environment_image.ensure_image():
                raise BackupError("The backup holds project sandbox environments, and the image "
                                  "they run on is not available here (Docker cannot build it). "
                                  "Restoring now would lose them; nothing was changed.")
        if pre_restore:
            say("Writing a backup of the current state first")
            pre = create_backup(backup_dir() / "pre-restore", include_volumes=True,
                                pg_container=container).path
        # This machine's backup identity, read before the keyring is replaced: the safety
        # backup above is encrypted to it, and must still open after the restore.
        try:
            from vaf.core.data_keyring import peek_data_secret
            previous_identity = peek_data_secret(IDENTITY_SECRET) or None
        except Exception:
            previous_identity = None
        from vaf.core.recovery_kit import recipient as recovery_recipient
        previous_recipient = _quiet(recovery_recipient)
        say("Placing the files")
        changed = True
        _clear_target(manifest)
        files = _place_files(staging)
        rewrapped: List[str] = []
        old_key = _old_machine_key(staging)
        if old_key:
            say("Opening the stores with this machine's key")
            from vaf.core import data_stores
            rewrapped = rewrap_from_kek(old_key, data_stores.roots()["data"])
        else:
            warnings.append("The backup carries no machine key, so its stores were sealed under "
                            "a passphrase: set VAF_MASTER_PASSPHRASE as on the old machine.")
        # The keyring on disk is now the backup's (re-wrapped): nothing read before may answer
        # from memory. The file-store key is held for the life of the process and would keep
        # this machine's key; the ring re-checks its wrap file by mtime and size, which a
        # coarse-mtime file system can leave unchanged.
        from vaf.core import data_files, data_keyring
        data_keyring.reset_ring()
        data_files.reset_key_cache()
        # The restored recovery wrap carries the machine key the backup's stores were sealed
        # under; they are sealed under THIS machine's key now, and the recovery key must
        # carry that one, or the next recovery opens the key store alone.
        try:
            from vaf.core.recovery_kit import refresh_recovery_wrap
            dek = data_keyring._ring()._get_dek(create=False)
            if dek is not None:
                refresh_recovery_wrap(dek)
        except Exception as e:
            warnings.append(f"The recovery key still carries the old machine's key ({e}), so it "
                            "would bring back the key store but not the password stores: run "
                            "`vaf secure renew-recovery-key`.")
        # The recovery file is the backup's now: the key that opens this machine is the one
        # it was written for, which is not the key this machine had if the backup came from
        # another machine or from before a renewal.
        restored_recipient = _quiet(recovery_recipient)
        if previous_recipient and restored_recipient != previous_recipient:
            warnings.append(f"The recovery key is now the backup's"
                            f"{f' ({restored_recipient})' if restored_recipient else ''}; the key "
                            f"this machine had ({previous_recipient}) no longer opens it. Keep the "
                            "backup's recovery note, or run `vaf secure renew-recovery-key` for "
                            "a new one.")
        try:
            _keep_previous_identity(previous_identity)
        except Exception as e:
            if pre is not None:
                warnings.append(f"This machine's former backup key could not be kept ({e}): the "
                                f"safety backup {pre.name} opens with the recovery key only.")
        say("Restoring the database")
        _restore_postgres(staging, manifest, container)
        # Every restore, with volumes or without: an environment that mounts a host folder has
        # no volume, and its record would be cleared by the reaper all the same.
        say("Recreating the sandbox environments")
        from vaf.core.environments import EnvironmentManager, _host_user
        manager = EnvironmentManager()
        try:
            report = manager.readopt()
        except Exception as e:
            report = {}
            warnings.append(f"The sandbox environments could not be recreated ({e}). VAF "
                            "deletes them at its next start unless you run this restore again "
                            "first.")
        for key, text in (("dropped", "temporary sandbox environments were dropped (they "
                                      "would have expired)"),
                          ("missing_folder", "project environments mount a folder that does "
                                             "not exist on this machine"),
                          ("failed", "sandbox environments could not be recreated; VAF deletes "
                                     "them at its next start unless you run this restore again "
                                     "first")):
            if report.get(key):
                warnings.append(f"{len(report[key])} {text}: {', '.join(report[key])}.")
        volumes = 0
        vol_dir = staging / "volumes"
        if vol_dir.is_dir() and any(vol_dir.glob("*.tar")):
            say("Restoring the volumes")
            image = _carrier_image(container)
            for tar_path in sorted(vol_dir.glob("*.tar")):
                volume = tar_path.name[:-len(".tar")]
                if volume.startswith("vaf-browser-profile-"):
                    _restore_volume(tar_path, volume, image, keep_top=_BROWSER_NOT_PROFILE)
                else:
                    env_id = volume[len("vaf-env-vol-"):]
                    if not (manager._state_dir() / f"{env_id}.json").exists():
                        continue          # its environment was dropped: no orphan volume
                    _restore_volume(tar_path, volume, image, owner=_host_user())
                volumes += 1
        return RestoreResult(manifest, files, rewrapped, volumes, pre, warnings)
    except (Exception, KeyboardInterrupt) as e:
        if not changed:
            raise
        # Past the clearing, this machine holds part of the backup: whatever stopped it, the
        # person needs to know that, and where the state before it is.
        raise BackupError(_half_way(e, pre)) from e
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _quiet(read: Callable[[], Optional[str]]) -> Optional[str]:
    try:
        return read()
    except Exception:
        return None


def _half_way(error: BaseException, pre: Optional[Path]) -> str:
    if isinstance(error, BackupError):
        reason = str(error).rstrip(".")
    elif isinstance(error, KeyboardInterrupt):
        reason = "it was interrupted"
    else:
        reason = f"{type(error).__name__}: {error}"
    where = (f" The state before it is in the safety backup {pre}: `vaf backup restore {pre}` "
             "puts it back (with --recovery-key if this machine's key no longer opens it)."
             if pre else " There is no safety backup of the state before it "
             "(--no-safety-backup).")
    return f"The restore stopped half way, so this machine holds part of the backup: {reason}.{where}"
