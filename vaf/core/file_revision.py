# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Saving a file the person edits never overwrites what changed on disk meanwhile.

WHY THIS EXISTS. The editors' save routes wrote whatever the browser sent. Measured: the
agent edits a file the person has open, the person saves, and the agent's change is gone with
nothing said; two tabs on one file overwrite each other the same way. A save now names the
revision it was edited from, and a file that is no longer at that revision is not written.

- `revision_of(path)`: the SHA-256 of the file's bytes, or None when there is no file. Not the
  modification time: FAT keeps it to two seconds, network shares round it, and a copy can
  carry the old one, so two different contents could share a "revision".
- `write_if_revision(path, data, base)`: under a lock per path, compare `base` with the file
  as it is now and write only when they agree - atomically, keeping the file's mode (a
  document is not a secret, unlike the stores `secure_store` usually writes). `base=None`
  means "a new file": it is refused when one exists. Raises `RevisionConflict` with the
  current revision otherwise.
- `edit_copy_path(path)`: `<name> (bearbeitet)<ext>`, the ONE copy a lossy editor save goes
  to instead of the original (the web server's office routes decide when; see
  `office_loss_report` there).

NAMED BOUNDARY: the lock is per process. Another process writing the same file (the agent's
own `write_file` in a sub-agent, a program of the person's) is caught by the comparison, not
kept out by the lock: between the check and the rename there is a window of one write.
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Dict, Optional

EDIT_COPY_SUFFIX = " (bearbeitet)"

_locks_guard = threading.Lock()
_locks: Dict[str, threading.Lock] = {}


class RevisionConflict(Exception):
    """The file is no longer at the revision the save was edited from."""

    def __init__(self, current: Optional[str]):
        super().__init__("the file changed on disk since it was opened")
        self.current = current


def revision_of(path) -> Optional[str]:
    """The SHA-256 of the file's bytes, or None when there is no such file."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        return None
    return digest.hexdigest()


def _lock_for(path: Path) -> threading.Lock:
    key = str(Path(path).resolve())
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def write_if_revision(path, data: bytes, base: Optional[str]) -> str:
    """Write `data` to `path` only while the file is still at `base` (None: no file yet).
    Returns the new revision; raises RevisionConflict with the current one otherwise."""
    from vaf.core.secure_store import atomic_write_bytes
    target = Path(path)
    with _lock_for(target):
        current = revision_of(target)
        if (base or None) != current:
            raise RevisionConflict(current)
        atomic_write_bytes(target, data, keep_mode=True)
    return hashlib.sha256(data).hexdigest()


def edit_copy_path(path) -> Path:
    """Where a lossy save of `path` goes: `<name> (bearbeitet)<ext>` beside it. Only a lossy
    file is asked: the copy the editor wrote holds nothing it cannot write again, so it is
    saved in place from then on and no second copy appears."""
    target = Path(path)
    return target.with_name(f"{target.stem}{EDIT_COPY_SUFFIX}{target.suffix}")
