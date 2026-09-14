# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Download a file whose SHA-256 is known in advance, or nothing.

The one primitive behind every binary VAF pulls from the network and then runs (today
the llama.cpp server, see ``vaf/core/backend.py``). The bytes are hashed while they
stream into a ``.part`` file next to the destination; the file is renamed into place only
when the digest equals the expected one, and a mismatch deletes the partial file and
raises, so a replaced or truncated release asset never reaches disk under the name the
launcher trusts. Deliberate: no way to skip the check. A download without a known digest
is not a verified download and does not belong here.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Optional

import requests


class DownloadIntegrityError(RuntimeError):
    """The downloaded bytes do not hash to the digest that was pinned for them."""


def download_verified(
    url: str,
    sha256: str,
    dest: str | os.PathLike,
    *,
    timeout: float = 30.0,
    chunk_size: int = 1 << 16,
    progress: Optional[Callable[[int], None]] = None,
) -> Path:
    """Stream ``url`` into ``dest`` and return its path once the SHA-256 matches.

    ``sha256`` is the expected hex digest (case-insensitive). ``progress`` is called with
    the bytes received so far. Raises ``DownloadIntegrityError`` on a mismatch (the partial
    file is removed), ``ValueError`` when no digest is given, and the ``requests`` errors
    for the transfer itself.
    """
    expected = (sha256 or "").strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError(f"a verified download needs a SHA-256 hex digest, got {sha256!r}")
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    received = 0
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(partial, "wb") as f:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received)
        actual = digest.hexdigest()
        if actual != expected:
            raise DownloadIntegrityError(
                f"{target.name}: SHA-256 {actual} does not match the pinned {expected} "
                f"({received} bytes from {url})"
            )
        if target.exists():
            target.unlink()
        os.replace(partial, target)
        return target
    finally:
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass
