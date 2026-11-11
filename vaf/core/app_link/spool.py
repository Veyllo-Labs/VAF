# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The files a message to the Veyllo App carries, kept until every device of the account has
them: ``~/.vaf/app_link_spool/<message id>.bin``, written through the encrypted seam
(vaf/core/data_files.py). The outbox keeps message ids, not bytes; without this store a device
that was away got a file's message as its ``[File: <name>]`` line only.

Only the bytes live here. Each file's record (account, name, type, size, SHA-256, time) is in
the sealed device list (devices.py, ``spool``), written in the same write as the outbox entries
of its message (``devices.outbox_publish``): the bytes are written first, so a replay never sees
a message without its file and ``collect`` never sees a file without its entries. Every file ``bridge.deliver`` sends is spooled, also when every device is
connected, because a device that drops before its ``ack`` would lose it otherwise.

Limits: ``FILES_PER_ACCOUNT`` files per account (the oldest goes, its message replays as its
text line), 25 MiB each (the protocol's ``MAX_FILE_BYTES``), as long as the outbox keeps the
message (seven days), and ``FILE_SENDS_PER_DEVICE`` replays per device. A file goes once every
device acked its message, its devices are gone or its seven days have passed (``collect``,
on an ``ack`` and hourly from the sweep). It is runtime data and not in a backup: after a
restore those messages replay as their text line.

NAMED BOUNDARY, memory: a replay queues each file's chunks on the carrier's outgoing queue at
once, which is unbounded (relay.py, app_link_routes.py), so a device coming back may cause up
to ``FILES_PER_ACCOUNT`` x 25 MiB in flight, the same a live send of those files already can.
The replay cap bounds repeats.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.app_link import devices
from vaf.core.app_link import protocol as pr

logger = logging.getLogger(__name__)

FILES_PER_ACCOUNT = 10
FILE_SENDS_PER_DEVICE = 3
# A blob is written before its record; one without a record younger than this may be a
# message still being stored (``stage``), so the sweep leaves it.
ORPHAN_GRACE_SECONDS = 300


def _dir() -> Path:
    from vaf.core.platform import Platform
    return Platform.vaf_dir() / "app_link_spool"


def _blob(message_id: str) -> Optional[Path]:
    """The blob's path, or None for an id that is not a UUID (it never names a file)."""
    try:
        name = str(uuid.UUID(str(message_id)))
    except ValueError:
        return None
    return _dir() / f"{name}.bin"


def _unlink(message_id: str) -> None:
    path = _blob(message_id)
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("app link: a spooled file could not be removed", exc_info=True)


def stage(message_id: str, path) -> Optional[Dict[str, Any]]:
    """Write the bytes of message ``message_id``'s file and return its record, which the
    caller stores together with the message's outbox entries (devices.outbox_publish), so
    no replay sees the one without the other. None (and nothing kept) when the file cannot
    be read, is empty or larger than the protocol takes, or cannot be written; that message
    then replays as its text line. A blob whose record never comes is an orphan ``collect``
    removes after ``ORPHAN_GRACE_SECONDS``."""
    blob = _blob(message_id)
    if blob is None:
        return None
    try:
        data = Path(path).read_bytes()
        if not 1 <= len(data) <= pr.MAX_FILE_BYTES:
            return None
        from vaf.core import data_files
        data_files.write_bytes_atomic(blob, data)
    except Exception:
        logger.debug("app link: a file could not be spooled", exc_info=True)
        _unlink(message_id)
        return None
    name = os.path.basename(str(path))
    return {"name": name, "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "ts": time.time()}


def read(message_id: str) -> Optional[bytes]:
    """The spooled bytes of a message, or None: no record, no blob, or bytes whose hash is
    not the recorded one (the record is dropped then, so the message replays as its text)."""
    record = devices.spool_get(message_id)
    blob = _blob(message_id)
    if record is None or blob is None:
        return None
    try:
        from vaf.core import data_files
        data = data_files.read_bytes(blob)
    except Exception:
        logger.debug("app link: a spooled file could not be read", exc_info=True)
        data = b""
    if not data or hashlib.sha256(data).hexdigest() != str(record.get("sha256") or ""):
        drop([message_id])
        return None
    return data


def drop(ids: List[str]) -> None:
    devices.spool_drop(list(ids))
    for message_id in ids:
        _unlink(message_id)


def collect(now: Optional[float] = None) -> int:
    """Drop the files no outbox holds any more or whose seven days have passed, and every
    blob without a record. How many went."""
    gone = devices.spool_unreferenced(now)
    drop(gone)
    count = len(gone)
    folder = _dir()
    if not folder.is_dir():
        return count
    known = set(devices.spool_ids())
    wall = time.time()
    for path in folder.glob("*.bin"):
        if path.stem in known:
            continue
        try:
            if wall - path.stat().st_mtime < ORPHAN_GRACE_SECONDS:
                continue
            path.unlink()
            count += 1
        except OSError:
            continue
    return count
