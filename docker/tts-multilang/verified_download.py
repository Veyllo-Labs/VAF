# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Download a file whose SHA-256 is known in advance, or nothing; unpack an archive safely.

The one primitive behind every binary and model VAF pulls from the network and then runs
or loads: the llama.cpp server (``vaf/core/backend.py``), the Piper program and its
voices (``vaf/core/speech.py``), the speaker recognition models
(``vaf/core/speaker_id.py``) and the turn-end model (``vaf/core/voice_vad.py``).

``download_verified``: the bytes are hashed while they stream into a ``.part`` file next
to the destination; the file is renamed into place only when the digest equals the
expected one, and a mismatch deletes the partial file and raises, so a replaced or
truncated release asset never reaches disk under the name the launcher trusts.
Deliberate: no way to skip the check. A download without a known digest is not a
verified download and does not belong here.

``fetch_verified``: the same, but a file already at the destination with the pinned
digest is kept (hashed once per process), and one with any other content is replaced by
verified bytes.

``extract_archive``: unpack a release archive into a staging folder and lift the folder
holding its program to the top, with tarfile's data filter, a cap on the links an
archive may hold, and every link checked again on disk after the lift
(``refuse_escaping_links``).

Module-level imports are the standard library and ``requests`` only, so the speech
container (``docker/tts-multilang``, whose build context holds no ``vaf``) carries a
byte-identical copy of this file (pinned by ``tests/test_speech_assets_pin.py``).
Anything from ``vaf`` is imported inside the function that needs it.

Named boundary, facade: these helpers are not exported from ``vaf/__init__.py``. Counted:
no caller outside ``vaf/core`` (the harness never downloads a runtime asset itself), and
an embedder who uses the facade's voice engine gets the verified speaker and turn-end
downloads because they live in ``vaf/core``.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests

# Links (symbolic or hard) one release archive may hold. A release has a few dozen (the
# version links of its libraries). The data filter resolves every link member as it
# unpacks, and a chain stored in reverse order makes that cost grow with the square of its
# length (26 s at 4000 links), so more than this is refused before anything is unpacked.
MAX_ARCHIVE_LINKS = 512


class DownloadIntegrityError(RuntimeError):
    """The downloaded bytes do not hash to the digest that was pinned for them."""


def _expected_digest(sha256: str) -> str:
    expected = (sha256 or "").strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError(f"a verified download needs a SHA-256 hex digest, got {sha256!r}")
    return expected


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
    expected = _expected_digest(sha256)
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


def file_sha256(path: str | os.PathLike, chunk_size: int = 1 << 20) -> str:
    """The SHA-256 of the file at ``path`` (hex), read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Files this process found to carry their pinned digest, keyed by what would change if the
# file were replaced (path, size, modification time) and the digest it was compared with.
_verified_files: set = set()
_verified_lock = threading.Lock()


def has_pinned_digest(path: str | os.PathLike, sha256: str, size: Optional[int] = None) -> bool:
    """True when ``path`` is a regular file (a link does not count, it is never followed)
    whose size equals ``size`` (when given) and whose SHA-256 equals ``sha256``. Hashed once
    per process: a 114 MB voice is read the first time it is used, not at every reply."""
    expected = _expected_digest(sha256)
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or (size is not None and st.st_size != int(size)):
        return False
    key = (os.path.abspath(path), st.st_size, st.st_mtime_ns, expected)
    with _verified_lock:
        if key in _verified_files:
            return True
    try:
        ok = file_sha256(path) == expected
    except OSError:
        return False
    if ok:
        with _verified_lock:
            _verified_files.add(key)
    return ok


def fetch_verified(
    url: str,
    sha256: str,
    dest: str | os.PathLike,
    *,
    size: Optional[int] = None,
    timeout: float = 30.0,
    progress: Optional[Callable[[int], None]] = None,
) -> Path:
    """``dest`` with the pinned digest: kept when it already has it (has_pinned_digest),
    otherwise downloaded with ``download_verified``, which replaces whatever is there (a
    truncated or foreign file) only once the new bytes verified. Raises what
    ``download_verified`` raises."""
    target = Path(dest)
    if has_pinned_digest(target, sha256, size):
        return target
    return download_verified(url, sha256, target, timeout=timeout, progress=progress)


def link_leaves_folder(link: str, depth: int, real_root: str) -> str:
    """Why the link at `link`, `depth` folders below the root whose resolved path is
    `real_root`, can point outside it, or "" when it cannot (see refuse_escaping_links)."""
    from vaf.core.path_jail import is_rooted

    try:
        target = os.readlink(link)
    except OSError as e:
        return f"unreadable link: {e}"
    if is_rooted(target) or os.path.isabs(target):
        return f"absolute target {target!r}"
    # Only leading ".." count as climbing: "d/.." goes wherever d's target goes when d is a
    # link (d -> . makes d/.. the folder's PARENT), so a ".." after a named part is refused.
    # Each link then stays inside on its own, whatever the folder is called, also where the
    # extraction left its target unnormalized (tarfile before the 2025 fix: 3.10.12-3.10.17,
    # 3.11.4-3.11.12, 3.12.0-3.12.10, 3.13.0-3.13.3).
    level = depth
    named = False
    seps = os.sep + (os.altsep or "")
    for part in re.split("[" + re.escape(seps) + "]", target):
        if part in ("", "."):
            continue
        if part == "..":
            if named:
                return f"target {target!r} goes back up after a named part"
            level -= 1
            if level < 0:
                return f"target {target!r} climbs above the folder"
        else:
            named = True
            level += 1
    try:
        os.stat(link)
    except FileNotFoundError:
        pass                              # dangling: nothing is reached through it
    except OSError as e:
        return f"cannot be followed: {e.strerror or e}"
    real = os.path.realpath(link)
    try:
        inside = os.path.commonpath([real, real_root]) == real_root
    except ValueError:                    # another drive on Windows
        inside = False
    return "" if inside else f"resolves outside to {real!r}"


def refuse_escaping_links(root: str, what: str = "backend") -> None:
    """RuntimeError when a link anywhere below `root` could point outside the folder
    once it is installed. Each link is decided on its path on disk (entry.path, its
    name exactly as stored, odd characters included), three ways:

    - by its target as written (os.readlink): an absolute or drive-anchored target
      (path_jail.is_rooted), one whose leading ".." climb above `root` from the link's
      own folder, and one with a ".." after a named part (``d/..`` is d's target's
      parent when d is a link) are refused. This does not depend on the folder's name,
      so a link that leaves and comes back by the temporary name
      (``../llama-cpp.staging/x``, or ``d/../llama-cpp.staging/x`` with d -> .) is
      refused although it resolves inside while the folder still has that name;
    - by the kernel's own resolution (os.stat): a chain the kernel refuses to follow
      (more than its hop limit, 40 on Linux, or a loop) is refused, which also keeps the
      next step linear in the number of links;
    - by where it resolves (os.path.realpath, through every other link): outside
      `root` is refused. With the rules above every link of a listed folder stays
      inside by itself, so this step cannot change the outcome (it may name the reason
      first, for a link that runs through another one the rules refuse later in the
      listing); it guards those rules rather than deciding a shape of its own.

    A dangling link that stays inside is accepted (it cannot reach anything), and so are
    a release's relative library links (libllama.so -> libllama.so.1). The folders are
    listed without following links (path_jail.walk_without_links). `what` names the
    folder in the message ("backend", "Piper")."""
    from vaf.core.path_jail import walk_without_links

    real_root = os.path.realpath(root)
    folders = [""] + [rel for rel, is_folder in walk_without_links(root) if is_folder]
    for rel in folders:
        depth = len(rel.split("/")) if rel else 0
        with os.scandir(os.path.join(root, *rel.split("/")) if rel else root) as entries:
            for entry in entries:
                if not entry.is_symlink():
                    continue
                name = f"{rel}/{entry.name}" if rel else entry.name
                reason = link_leaves_folder(entry.path, depth, real_root)
                if reason:
                    raise RuntimeError(f"the archive holds a link that leaves the {what} folder: {name!r} ({reason})")


def remove_tree_or_link(path: str) -> None:
    """Remove `path` whatever it is: a link (to a folder or not) is unlinked, never
    followed, a folder is removed with everything in it, a file is unlinked. rmtree
    refuses a link, so a linked leftover would otherwise stay and block every later swap.
    OSError when anything of it is still there afterwards (rmtree skips what it cannot
    remove): the next install must not unpack on top of debris."""
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.unlink(path)
    if os.path.lexists(path):
        raise OSError(f"{path} could not be removed completely")


def extract_archive(archive_path: str, into: str, *, program: str, what: str = "backend") -> None:
    """Unpack a release archive into `into` and lift the folder holding `program` to its top.

    Tarballs go through tarfile's "data" filter, which refuses an absolute link and one
    that climbs out of `into`. A Python without that filter (3.10.0 to 3.10.11) gets no
    unfiltered fallback: the install fails instead. The filter judges a link where it
    is unpacked, though, and the lift then moves build/bin/ two levels up without
    rewriting links, so `build/bin/d -> ../x` passes the filter and points out of the
    folder afterwards. Every link is therefore checked again after the lift, on its
    path on disk (refuse_escaping_links): one whose target is absolute or climbs above
    the folder, that the kernel will not follow, or that resolves outside `into` fails
    the install. The relative library links of a release (libllama.so ->
    libllama.so.1 -> libllama.so.1.0.0) stay inside and are kept. A tarball with more
    than MAX_ARCHIVE_LINKS links is refused before it is unpacked. Zip archives cannot
    hold links, and zipfile strips ".." and absolute names itself.

    The lift first renames the folder holding `program` to a fresh name directly below
    `into` and moves its entries up from there: Piper's archive keeps its program in a
    folder of the same name (piper/piper), and moving that entry to into/piper would
    otherwise remove the very folder being lifted. An entry whose name is already taken
    at the top replaces it (never followed when it is a link). The archive itself is
    removed. `what` names the folder in the messages ("backend", "Piper")."""
    if archive_path.endswith(".tar.gz") or archive_path.endswith(".tgz"):
        with tarfile.open(archive_path, "r:gz") as tar_ref:
            links = sum(1 for m in tar_ref.getmembers() if m.issym() or m.islnk())
            if links > MAX_ARCHIVE_LINKS:
                raise RuntimeError(f"the archive holds {links} links, more than the {MAX_ARCHIVE_LINKS} "
                                   "a release can need")
            try:
                tar_ref.extractall(into, filter="data")
            except TypeError as e:     # a Python without the filter argument
                raise RuntimeError(
                    f"this Python cannot unpack the {what} safely (tarfile has no data filter); "
                    "update Python to 3.10.12 or newer, 3.11.4 or newer, or 3.12 or newer") from e
            except OSError as e:
                # The filter resolves link targets strictly on newer Pythons: a target that
                # runs through a regular file raises NotADirectoryError there. Refused as
                # an archive error like every other shape the filter rejects.
                raise RuntimeError(f"the archive cannot be unpacked into the {what} folder: {e}") from e
    else:
        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            zip_ref.extractall(into)
    os.remove(archive_path)
    # GitHub archives often carry a top-level folder; the launcher expects the files
    # directly at the top.
    parent_dir = None
    for root, dirs, files in os.walk(into):
        if root != into and program in files:
            parent_dir = root
            break
    if parent_dir:
        aside = tempfile.mkdtemp(prefix=".lift-", dir=into)
        lifted = os.path.join(aside, "d")
        os.rename(parent_dir, lifted)
        for item in os.listdir(lifted):
            if item == os.path.basename(aside):
                raise RuntimeError(f"the archive holds an entry named like the {what} unpack folder: {item!r}")
            dst = os.path.join(into, item)
            if os.path.lexists(dst):
                remove_tree_or_link(dst)
            os.rename(os.path.join(lifted, item), dst)
        os.rmdir(lifted)
        os.rmdir(aside)
    refuse_escaping_links(into, what)
