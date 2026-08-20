# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Known-bad content: one machine-wide hash list, and the funnel every ingress asks.

## What this is for

The skill scanner reads BYTES and judges them by their shape (regex heuristics over
prose and code). That judgement is expensive to reach and easy to lose: an admin
studies a bundle, decides it is hostile, deletes it - and the identical bytes walk
back in an hour later through a chat attachment, a Telegram document or a room's
shared folder, where nothing was ever looking at them.

This module keeps the judgement. `record_threat` writes the digests of content that
was CONFIRMED dangerous; `inspect_upload` is the single question every ingress lane
asks before it accepts bytes: "have we already decided about exactly this?"

## Deliberately NOT user-scoped

Every other store in this tree keys on the caller's scope, because conversations and
files belong to one person. A verdict about bytes does not: content that is hostile
to the admin is hostile to every account on the machine, and a per-user list would
have to re-learn the same answer once per user. So this file is machine-wide and its
lookups ignore identity entirely.

## What "write protected" means here, stated honestly

Three mechanisms, none of them claiming more than it does:

- The directory is 0700 and the file 0600 (`secure_store.harden_*`). On Windows that
  is a documented no-op - `harden_path` says so - and what protects the file there is
  the profile ACL, which excludes other standard users but not an administrator.
- Every write-side entry point in the product is admin-gated (the REST routes require
  `require_admin`, a delist additionally requires the admin's TOTP; the CLI is the
  machine owner). This module enforces nothing about identity: it is a library, and
  the caller that reached it has already been let through.
- Every mutation emits a security event (`threat_listed` / `threat_delisted`), so a
  list that grew or shrank leaves a trace outside itself.

NOT claimed: encryption (a hash is not a secret, and an admin must be able to read
this file with `cat` when a block needs explaining) and a hash chain over the records
(the event log is the audit trail; a chain can be added later without a format break
because every record already carries `op`).

## Append-only, folded on read

Records are appended, never rewritten - the same choice `a2a/store.py` documents:
a read-modify-write over a growing file is the shape that loses updates when the
server, the CLI and a sync worker all write. A delist appends a tombstone rather than
erasing the original line, so the file answers "why is this no longer blocked?" as
well as "why was it blocked?". `load` folds the records in order; the last `op` for a
digest wins.

## Two hash families, either one matching is a hit

Digests come from `vaf.skills.scanner`, whose allow-list is SHA-2 and SHA-3 and
deliberately excludes md5/sha1. Each record carries a sha256 AND a sha3_256, and a
lookup hits when EITHER matches. That is not redundancy for its own sake: the two
families have unrelated constructions (Merkle-Damgard vs sponge), so a collision
attack that ever became practical against one does not carry over to the other, and
a record written today keeps meaning what it said.

`source` is on every record from the first version so an imported third-party feed
can be told apart from a local verdict later. Only "local" is written today.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.platform import Platform
from vaf.core.secure_store import harden_dir, harden_path

logger = logging.getLogger("vaf.core.threat_db")

# On-disk format tag. A store that writes a file a later version must recognise
# carries one; the digits are arbitrary and fixed once. Pinned as a literal in
# tests/test_persisted_format_tags.py, so changing it fails the suite rather than
# silently orphaning a list an admin has curated. A file without the tag (written
# before it existed) still loads: the loader skips records it does not understand
# and never discards the rest.
THREAT_DB_FORMAT = "threatdb-1-9c2f7b"

# Advisory scanning reads text. Same ceiling the skill scanner uses for a bundled
# file, for the same reason: past it the heuristics cost more than they find.
_ADVISORY_MAX_BYTES = 512 * 1024

_HASH_CHUNK = 64 * 1024

# What a record's `kind` may say. "file" is one blob of bytes; "skill_bundle" is a
# whole folder's fingerprint (scanner.hash_skill_folder). Not validated on read -
# an unknown kind from a newer build is displayed, not dropped.
KIND_FILE = "file"
KIND_SKILL_BUNDLE = "skill_bundle"

_lock = threading.Lock()
_index: Dict[str, Dict[str, Any]] = {}      # sha256 -> record
_index_by_sha3: Dict[str, Dict[str, Any]] = {}
_index_stamp: Optional[tuple] = None        # (path, mtime_ns, size) the index was built from


def threat_db_dir() -> Path:
    """The machine-wide security directory, created on demand.

    Resolved on every CALL, not at import: a test with a scratch HOME, and a
    Windows profile that moves, both need the answer to follow the environment.
    """
    directory = Platform.vaf_dir() / "security"
    directory.mkdir(parents=True, exist_ok=True)
    harden_dir(directory)
    return directory


def threat_db_path() -> Path:
    """The list itself. May not exist yet - an empty list is a valid state."""
    return threat_db_dir() / "threat_db.jsonl"


# ── digests ──────────────────────────────────────────────────────────────────────

def digests_of_bytes(data: bytes) -> Dict[str, str]:
    """The two digests a record is keyed by. Bytes already in memory."""
    from vaf.skills.scanner import hash_bytes
    return {"sha256": hash_bytes(data, "sha256"),
            "sha3_256": hash_bytes(data, "sha3_256")}


def digests_of_file(path: Path | str) -> Dict[str, str]:
    """The two digests of a file, streamed so a large upload stays bounded."""
    import hashlib
    h2 = hashlib.new("sha256")
    h3 = hashlib.new("sha3_256")
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            h2.update(chunk)
            h3.update(chunk)
    return {"sha256": h2.hexdigest(), "sha3_256": h3.hexdigest()}


def short(digest: str) -> str:
    """The 12-character prefix used in logs and UI. Never the identity itself."""
    return str(digest or "")[:12]


# ── the file ─────────────────────────────────────────────────────────────────────

def _stamp(path: Path) -> Optional[tuple]:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _read_records(path: Path) -> List[Dict[str, Any]]:
    """Every well-formed record in file order. A torn or foreign line is skipped,
    never fatal: one bad line must not blind the whole list."""
    out: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return out
    return out


def _fold(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Fold the append-only log into the live list: last op per sha256 wins."""
    live: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        sha = str(rec.get("sha256") or "").strip().lower()
        if not sha:
            continue                      # the format-tag header record, or junk
        op = str(rec.get("op") or "list")
        if op == "delist":
            live.pop(sha, None)
        elif op == "list":
            live[sha] = rec
    return live


def _refresh_locked() -> None:
    """Rebuild the in-memory index when the file changed. Caller holds _lock."""
    global _index, _index_by_sha3, _index_stamp
    path = threat_db_path()
    stamp = _stamp(path)
    if stamp == _index_stamp and _index_stamp is not None:
        return
    if stamp is None:
        _index, _index_by_sha3, _index_stamp = {}, {}, None
        return
    live = _fold(_read_records(path))
    _index = live
    _index_by_sha3 = {}
    for rec in live.values():
        sha3 = str(rec.get("sha3_256") or "").strip().lower()
        if sha3:
            _index_by_sha3[sha3] = rec
    _index_stamp = stamp


def _append_locked(record: Dict[str, Any]) -> None:
    """Append one record and drop the index stamp. Caller holds _lock.

    The format-tag header is written with the first record of a fresh file, so a
    reader can tell which build's format it is looking at without a side file.
    """
    global _index_stamp
    path = threat_db_path()
    fresh = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8") as f:
        if fresh:
            f.write(json.dumps({"format": THREAT_DB_FORMAT}, ensure_ascii=False) + "\n")
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    harden_path(path)
    _index_stamp = None


def _cross_process_lock():
    """The same optional cross-process lock secure_store uses. The server, the CLI
    and a sync worker all append here; without it two appends can interleave a
    half-written line. Absent filelock degrades to the threading lock alone, which
    is what secure_store already accepts."""
    from vaf.core.secure_store import _get_filelock_cls
    cls = _get_filelock_cls()
    if cls is None:
        return None
    try:
        return cls(str(threat_db_path()) + ".lock", timeout=5)
    except Exception:
        return None


# ── writing ──────────────────────────────────────────────────────────────────────

def record_threat(*, sha256: str, sha3_256: str = "", size: int = 0,
                  kind: str = KIND_FILE, name: str = "", reason: str = "",
                  source: str = "local", skill_id: str = "",
                  listed_by: str = "") -> Dict[str, Any]:
    """List one digest as known-bad. Idempotent: re-listing an already-listed
    sha256 returns the existing record and appends nothing.

    Never raises on a write failure - a store that cannot be written must not take
    down the admin action that reached it; the return value says what happened.
    """
    sha = str(sha256 or "").strip().lower()
    if not sha:
        raise ValueError("record_threat needs a sha256")
    record = {
        "op": "list",
        "sha256": sha,
        "sha3_256": str(sha3_256 or "").strip().lower(),
        "size": int(size or 0),
        "kind": str(kind or KIND_FILE)[:32],
        "name": str(name or "")[:200],
        "reason": str(reason or "")[:300],
        "source": str(source or "local")[:32],
        "listed_at": datetime.now().isoformat(timespec="seconds"),
        "listed_by": str(listed_by or "")[:80],
    }
    if skill_id:
        record["skill_id"] = str(skill_id)[:120]

    flock = _cross_process_lock()
    with _lock:
        try:
            if flock is not None:
                flock.acquire()
        except Exception:
            flock = None
        try:
            _refresh_locked()
            existing = _index.get(sha)
            if existing is not None:
                return dict(existing)
            _append_locked(record)
        except Exception as e:
            logger.warning("threat_db: listing %s failed: %s", short(sha), e)
            return record
        finally:
            if flock is not None:
                try:
                    flock.release()
                except Exception:
                    pass

    _emit_threat_event("threat_listed", username=listed_by, path=short(sha),
          detail=f"{record['kind']}:{record['name']} reason={record['reason']}"[:200])
    return record


def record_bytes_threat(data: bytes, *, kind: str = KIND_FILE, name: str = "",
                        reason: str = "", source: str = "local",
                        skill_id: str = "", listed_by: str = "") -> Dict[str, Any]:
    """List bytes already in memory: hashes them, then lists them."""
    d = digests_of_bytes(data)
    return record_threat(sha256=d["sha256"], sha3_256=d["sha3_256"], size=len(data),
                         kind=kind, name=name, reason=reason, source=source,
                         skill_id=skill_id, listed_by=listed_by)


def record_file_threat(path: Path | str, *, kind: str = KIND_FILE, name: str = "",
                       reason: str = "", source: str = "local",
                       skill_id: str = "", listed_by: str = "") -> Dict[str, Any]:
    """List a file on disk: streams it, then lists it."""
    p = Path(path)
    d = digests_of_file(p)
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    return record_threat(sha256=d["sha256"], sha3_256=d["sha3_256"], size=size,
                         kind=kind, name=name or p.name, reason=reason, source=source,
                         skill_id=skill_id, listed_by=listed_by)


# The floor below which a file is not worth listing on its own. A short, generic
# file (an empty __init__.py, a one-line README) has a digest thousands of harmless
# bundles share, and listing it would refuse them all.
_MIN_LISTABLE_FILE_BYTES = 64


def flagged_files_of_scan(folder: Path | str, scan: Optional[Dict[str, Any]]) -> List[Path]:
    """Which files inside a judged bundle are worth listing individually.

    The bundle digest alone only catches a byte-identical re-upload: repack the same
    hostile script with one extra blank line in the README and the folder hash is new.
    The payload file's own hash is not, which is why files are listed too.

    But listing EVERY file would poison the list with whatever ordinary code the
    bundle happened to ship. So the scan's own findings choose: a finding names the
    file it fired in, and those are the files that earned the verdict. When no finding
    names a file (a bundle quarantined by hand, with nothing for the rules to match),
    the choice falls back to every file above the size floor - a human already decided
    this whole thing is hostile.
    """
    folder = Path(folder)
    base = folder.resolve()
    labels = {str(f.get("file") or "") for f in ((scan or {}).get("findings") or [])}
    labels.discard("")

    def _inside(p: Path) -> bool:
        try:
            return p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(base)
        except OSError:
            return False

    if labels:
        picked = [folder / rel for rel in sorted(labels)]
        return [p for p in picked if _inside(p)]

    out: List[Path] = []
    for p in sorted(folder.rglob("*")):
        try:
            if _inside(p) and p.stat().st_size >= _MIN_LISTABLE_FILE_BYTES:
                out.append(p)
        except OSError:
            continue
    return out


def record_skill_threat(folder: Path | str, *, skill_id: str, reason: str,
                        scan: Optional[Dict[str, Any]] = None,
                        listed_by: str = "") -> List[Dict[str, Any]]:
    """List a confirmed-hostile skill: the bundle's fingerprint AND its guilty files.

    Called when a human has decided, not when the scanner merely scored high. The
    scanner's HIGH already has its own answer (block with an admin override); turning
    every HIGH into a permanent list entry would quietly remove that override, because
    a listed digest is refused with no way past it.
    """
    folder = Path(folder)
    out: List[Dict[str, Any]] = []
    try:
        from vaf.skills.scanner import hash_skill_folder
        bundle_sha = hash_skill_folder(folder, "sha256")
        bundle_sha3 = hash_skill_folder(folder, "sha3_256")
        out.append(record_threat(sha256=bundle_sha, sha3_256=bundle_sha3,
                                 kind=KIND_SKILL_BUNDLE, name=skill_id, reason=reason,
                                 skill_id=skill_id, listed_by=listed_by))
    except Exception as e:
        logger.warning("threat_db: bundle digest for %s failed: %s", skill_id, e)

    for path in flagged_files_of_scan(folder, scan):
        try:
            rel = path.relative_to(folder).as_posix()
        except ValueError:
            rel = path.name
        try:
            out.append(record_file_threat(path, kind=KIND_FILE, name=f"{skill_id}/{rel}",
                                          reason=reason, skill_id=skill_id,
                                          listed_by=listed_by))
        except OSError:
            continue
    return out


def check_skill_folder(folder: Path | str) -> Optional[Dict[str, Any]]:
    """Is this bundle, or anything inside it, already listed as known-bad?

    Checks the bundle fingerprint first (the exact thing that was judged), then every
    file - which is what catches the repack. Returns the first matching record.
    """
    folder = Path(folder)
    try:
        from vaf.skills.scanner import hash_skill_folder
        hit = check_hashes(hash_skill_folder(folder, "sha256"),
                           hash_skill_folder(folder, "sha3_256"))
        if hit is not None:
            return hit
    except Exception:
        pass
    base = folder.resolve()
    for p in sorted(folder.rglob("*")):
        try:
            if not p.is_file() or p.is_symlink():
                continue
            if not p.resolve().is_relative_to(base):
                continue
        except OSError:
            continue
        hit = check_file(p)
        if hit is not None:
            return hit
    return None


def remove_threat(sha256: str, *, by: str = "") -> bool:
    """Delist a digest by appending a tombstone. True when it was listed.

    The original listing line stays in the file on purpose: an audit that erases
    the thing it is auditing answers neither of the two questions worth asking.
    """
    sha = str(sha256 or "").strip().lower()
    if not sha:
        return False
    flock = _cross_process_lock()
    with _lock:
        try:
            if flock is not None:
                flock.acquire()
        except Exception:
            flock = None
        try:
            _refresh_locked()
            if sha not in _index:
                return False
            _append_locked({"op": "delist", "sha256": sha,
                            "at": datetime.now().isoformat(timespec="seconds"),
                            "by": str(by or "")[:80]})
        except Exception as e:
            logger.warning("threat_db: delisting %s failed: %s", short(sha), e)
            return False
        finally:
            if flock is not None:
                try:
                    flock.release()
                except Exception:
                    pass
    _emit_threat_event("threat_delisted", username=by, path=short(sha))
    return True


# ── reading ──────────────────────────────────────────────────────────────────────

def check_hashes(sha256: str = "", sha3_256: str = "") -> Optional[Dict[str, Any]]:
    """The listed record for either digest, or None. EITHER family matching is a hit."""
    with _lock:
        _refresh_locked()
        sha = str(sha256 or "").strip().lower()
        if sha and sha in _index:
            return dict(_index[sha])
        sha3 = str(sha3_256 or "").strip().lower()
        if sha3 and sha3 in _index_by_sha3:
            return dict(_index_by_sha3[sha3])
    return None


def check_bytes(data: bytes) -> Optional[Dict[str, Any]]:
    """The listed record for these bytes, or None."""
    d = digests_of_bytes(data)
    return check_hashes(d["sha256"], d["sha3_256"])


def check_file(path: Path | str) -> Optional[Dict[str, Any]]:
    """The listed record for this file's bytes, or None."""
    try:
        d = digests_of_file(path)
    except OSError:
        return None
    return check_hashes(d["sha256"], d["sha3_256"])


def list_threats() -> List[Dict[str, Any]]:
    """Every currently listed record, newest listing first."""
    with _lock:
        _refresh_locked()
        items = [dict(r) for r in _index.values()]
    items.sort(key=lambda r: str(r.get("listed_at") or ""), reverse=True)
    return items


def threat_count() -> int:
    """How many digests are listed right now."""
    with _lock:
        _refresh_locked()
        return len(_index)


def reset_cache() -> None:
    """Drop the in-memory index. For tests that move HOME between cases - the
    index stamp includes the path, but an empty list has no file to stamp."""
    global _index, _index_by_sha3, _index_stamp
    with _lock:
        _index, _index_by_sha3, _index_stamp = {}, {}, None


# ── the funnel every ingress lane asks ───────────────────────────────────────────

@dataclass
class UploadVerdict:
    """What one piece of arriving content is allowed to do.

    `blocked` is the only field a caller must honour. `advisory` carries the static
    scanner's opinion and is deliberately NOT a reason to block: those heuristics
    have false positives by design (a legitimate deployment script uses os.system),
    and a chat attachment is not a skill the agent will follow as instructions.
    """
    blocked: bool = False
    reason: str = ""
    record: Optional[Dict[str, Any]] = None
    sha256: str = ""
    sha3_256: str = ""
    size: int = 0
    advisory_level: str = "clean"
    advisory: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        """The advisory scan found something worth an admin's attention."""
        return self.advisory_level in ("medium", "high")

    def message(self, filename: str = "") -> str:
        """One line for a user who just had their upload refused."""
        name = filename or (self.record or {}).get("name") or "This file"
        return (f"{name} was blocked: its content is on this machine's list of "
                f"known dangerous files ({short(self.sha256)}). "
                f"An administrator can clear it in the security dashboard.")


def _scan_enabled() -> bool:
    try:
        from vaf.core.config import Config
        return bool(Config.get("upload_threat_scan_enabled", True))
    except Exception:
        return True


def _advisory_enabled() -> bool:
    try:
        from vaf.core.config import Config
        return bool(Config.get("upload_scan_advisory_enabled", True))
    except Exception:
        return True


def _advisory_scan(data: bytes, label: str) -> tuple:
    """Run the static rules over text-ish content. Returns (level, findings)."""
    if len(data) > _ADVISORY_MAX_BYTES:
        return "clean", []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "clean", []               # binary: these rules read source, not blobs
    try:
        from vaf.skills.scanner import scan_text_content
        result = scan_text_content(text, label)
    except Exception:
        return "clean", []
    return str(result.get("level", "clean")), list(result.get("findings") or [])


def inspect_upload(data: bytes, *, filename: str = "", origin: str = "",
                   username: str = "", ip: str = "") -> UploadVerdict:
    """THE question every lane that accepts foreign bytes asks, before it accepts them.

    Hashes the content, looks it up, and (when nothing is listed) lets the static
    scanner have a non-binding opinion. Emits `upload_blocked` on a hit and
    `upload_flagged` on an advisory finding, so both reach the dashboard without
    the caller having to remember to log anything.

    `origin` names the lane ("web_chat", "telegram", "a2a_room", ...) and travels
    into the event's `channel` field. Never raises: a guard that throws is a guard
    that gets wrapped in a bare except and stops guarding.
    """
    verdict = UploadVerdict(size=len(data or b""))
    if not data:
        return verdict
    try:
        d = digests_of_bytes(data)
        verdict.sha256, verdict.sha3_256 = d["sha256"], d["sha3_256"]
    except Exception as e:                       # pragma: no cover - defensive
        logger.warning("threat_db: hashing %r failed: %s", filename, e)
        return verdict

    if _scan_enabled():
        record = check_hashes(verdict.sha256, verdict.sha3_256)
        if record is not None:
            verdict.blocked = True
            verdict.record = record
            verdict.reason = str(record.get("reason") or "listed as dangerous")
            _emit_threat_event("upload_blocked", username=username, ip=ip, channel=origin,
                  path=short(verdict.sha256),
                  detail=f"{filename or record.get('name') or '?'}: {verdict.reason}"[:200])
            return verdict

    if _advisory_enabled():
        level, findings = _advisory_scan(data, filename or "upload")
        verdict.advisory_level, verdict.advisory = level, findings
        if verdict.flagged:
            cats = ",".join(sorted({str(f.get("category", "")) for f in findings if f.get("category")}))
            _emit_threat_event("upload_flagged", username=username, ip=ip, channel=origin,
                  path=short(verdict.sha256),
                  detail=f"{filename or '?'} level={level} cats={cats}"[:200])
    return verdict


def inspect_upload_file(path: Path | str, *, filename: str = "", origin: str = "",
                        username: str = "", ip: str = "") -> UploadVerdict:
    """`inspect_upload` for content that is already a file on disk.

    Streams the digests so a large download stays bounded, and only reads the bytes
    back for the advisory pass when the file is small enough for it anyway.
    """
    p = Path(path)
    name = filename or p.name
    verdict = UploadVerdict()
    try:
        st = p.stat()
        verdict.size = st.st_size
        d = digests_of_file(p)
        verdict.sha256, verdict.sha3_256 = d["sha256"], d["sha3_256"]
    except OSError as e:
        logger.warning("threat_db: reading %s failed: %s", p, e)
        return verdict

    if _scan_enabled():
        record = check_hashes(verdict.sha256, verdict.sha3_256)
        if record is not None:
            verdict.blocked = True
            verdict.record = record
            verdict.reason = str(record.get("reason") or "listed as dangerous")
            _emit_threat_event("upload_blocked", username=username, ip=ip, channel=origin,
                  path=short(verdict.sha256),
                  detail=f"{name}: {verdict.reason}"[:200])
            return verdict

    if _advisory_enabled() and verdict.size <= _ADVISORY_MAX_BYTES:
        try:
            data = p.read_bytes()
        except OSError:
            return verdict
        level, findings = _advisory_scan(data, name)
        verdict.advisory_level, verdict.advisory = level, findings
        if verdict.flagged:
            cats = ",".join(sorted({str(f.get("category", "")) for f in findings if f.get("category")}))
            _emit_threat_event("upload_flagged", username=username, ip=ip, channel=origin,
                  path=short(verdict.sha256),
                  detail=f"{name} level={level} cats={cats}"[:200])
    return verdict


def refuse_known_bad(data: bytes, *, filename: str = "", origin: str = "",
                     username: str = "") -> bool:
    """True when these bytes are listed and the caller should drop them.

    The boolean form of `inspect_upload`, for the lanes that have no way to answer
    the sender: a messenger bridge, a sync worker, anything holding raw bytes deep
    inside a callback. They all want the same three lines - inspect, honour
    `blocked`, behave as if the transfer failed - and this is that shape written
    once. Written here rather than in each bridge because it had three callers the
    day it was needed, and a guard copied three times is a guard that drifts.

    Never raises, including on a failed import: fail-open is the deliberate
    direction, because a list that cannot be read has not refused anything.
    """
    try:
        return bool(inspect_upload(data, filename=filename, origin=origin,
                                   username=username).blocked)
    except Exception as e:                       # pragma: no cover - defensive
        logger.warning("threat_db: inspection unavailable for %r: %s", filename, e)
        return False


def emit_threat_block(origin: str, name: str, record: Dict[str, Any],
                      username: str = "") -> None:
    """Audit a refusal a caller decided for itself.

    `inspect_upload` logs its own blocks, but a lane that looks up a whole FOLDER
    (the skill installer) or that already holds the record still owes the log the
    same line. Same event, same per-content key, one place that knows the shape.
    """
    _emit_threat_event("upload_blocked", username=username, channel=origin,
          path=short(str(record.get("sha256") or "")),
          detail=f"{name}: {record.get('reason') or 'listed as dangerous'}"[:200])


def _emit_threat_event(kind: str, *, username: str = "", ip: str = "", channel: str = "",
          path: str = "", detail: str = "") -> None:
    """Mirror one decision into the security event log. Lazy import, never raises:
    auditing must not be able to break an upload path.

    `path` carries the digest prefix on purpose. The writer throttles on
    (kind, ip, username, channel, path) for five seconds, so without a per-content
    key a bulk upload of ten blocked files would log the first and swallow nine.
    """
    try:
        from vaf.core.security_events import log_security_event
        log_security_event(kind, username=username, ip=ip, channel=channel,
                           path=path, detail=detail)
    except Exception:
        pass
