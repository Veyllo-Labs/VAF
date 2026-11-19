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
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests

# Links (symbolic or hard) one release archive may hold. A release has a few dozen (the
# version links of its libraries). The data filter resolves every link member as it
# unpacks, and a chain stored in reverse order makes that cost grow with the square of its
# length (26 s at 4000 links), so more than this is refused before anything is unpacked.
MAX_ARCHIVE_LINKS = 512


# A partial file whose last write is this long ago is taken for the leftover of a killed
# download, and the next download of the same file removes it (sweep_stale_partials).
STALE_PARTIAL_SECONDS = 3600.0


# Windows refuses to unlink a file whose read-only attribute is set (owner write missing in
# its mode); a module constant rather than a check of os.name at the call, so a test can
# play Windows without turning every Path into a WindowsPath.
_WINDOWS = os.name == "nt"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class DownloadIntegrityError(RuntimeError):
    """The downloaded bytes do not hash to the digest that was pinned for them."""


def _pinned_size(size) -> int:
    """The pinned size as a positive int, or ValueError: without it the download would have
    no cap, and a missing or zero size is a broken pin, not a reason to download anyway."""
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError(f"a verified download needs the pinned size as a positive int, got {size!r}")
    return size


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
    size: int,
    timeout: float = 30.0,
    chunk_size: int = 1 << 16,
    progress: Optional[Callable[[int], None]] = None,
) -> Path:
    """Stream ``url`` into ``dest`` and return its path once the SHA-256 matches.

    ``sha256`` is the expected hex digest (case-insensitive). ``size`` is the pinned length,
    required: the transfer stops with ``DownloadIntegrityError`` as soon as more bytes
    arrive, so an endless stream cannot fill the disk. ``progress`` is called with the
    bytes received so far. Raises ``DownloadIntegrityError`` on a mismatch (the partial
    file is removed), ``ValueError`` when no digest or no positive size is given, and the
    ``requests`` errors for the transfer itself.

    Every call writes its own partial file next to the destination, created exclusively
    (tempfile.mkstemp: a new name, never opened through an existing file or link), and
    only the bytes that were hashed are in it. Two calls for the same destination at once
    (two processes fetching one voice) therefore cannot mix their bytes: the destination
    only ever receives a file whose own content matched, by an atomic rename, and a call
    that fails removes only its own partial file.
    """
    expected = _expected_digest(sha256)
    limit = _pinned_size(size)
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    sweep_stale_partials(target)
    fd, partial_name = tempfile.mkstemp(dir=target.parent, prefix="." + target.name + ".", suffix=".part")
    partial = Path(partial_name)
    digest = hashlib.sha256()
    received = 0
    moved = False
    try:
        with os.fdopen(fd, "wb") as f, requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                received += len(chunk)
                if received > limit:
                    raise DownloadIntegrityError(
                        f"{target.name}: more than the pinned {limit} bytes from {url}")
                f.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(received)
        actual = digest.hexdigest()
        if actual != expected:
            raise DownloadIntegrityError(
                f"{target.name}: SHA-256 {actual} does not match the pinned {expected} "
                f"({received} bytes from {url})"
            )
        os.replace(partial, target)
        moved = True
        return target
    finally:
        if not moved:
            try:
                partial.unlink()
            except OSError:
                pass


def sweep_stale_partials(target: str | os.PathLike) -> None:
    """Remove the partial files next to `target` (``.<name>.<random>.part``) whose last write
    is older than STALE_PARTIAL_SECONDS: what a download killed hard (power loss, SIGKILL)
    left behind, since the finally of download_verified did not run. A link is removed,
    never followed. The name test also matches the partial files of a destination whose
    name extends this one (``.v.onnx.json.<random>.part`` for ``v.onnx``); those are just as
    stale once that old.

    Named boundary, no lock: a download whose transfer stalls for an hour without writing a
    chunk (the read timeout only bounds each read, a server can trickle below a chunk) can
    have its partial file removed here by another download of the same file. That download
    then fails closed: its final rename finds no file, it raises, and the destination never
    receives bytes that were not verified. On Windows an open file cannot be removed, so
    the sweep skips it there."""
    target = Path(target)
    prefix = "." + target.name + "."
    cutoff = time.time() - STALE_PARTIAL_SECONDS
    try:
        entries = os.scandir(target.parent)
    except OSError:
        return
    with entries:
        for entry in entries:
            if not (entry.name.startswith(prefix) and entry.name.endswith(".part")):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)
            except OSError:
                pass


def file_sha256(path: str | os.PathLike, chunk_size: int = 1 << 20) -> str:
    """The SHA-256 of the file at ``path`` (hex), read in chunks. Deliberately its own
    helper rather than an import of the same loop elsewhere in vaf (backup, learn_ledger):
    this file is copied into the speech container, whose build context holds no vaf
    package, so it imports nothing from vaf at module level."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Files this process found to carry their pinned digest, keyed by what changes when the
# file is replaced or rewritten (path, device and inode, size, modification and change
# time; on POSIX the change time cannot be set back by a program, on Windows it is the
# creation time and the device and inode carry the check) and the digest it was compared
# with.
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
    key = (os.path.abspath(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, expected)
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
    size: int,
    timeout: float = 30.0,
    progress: Optional[Callable[[int], None]] = None,
) -> Path:
    """``dest`` with the pinned digest and size (required): kept when it already has them
    (has_pinned_digest),
    otherwise downloaded with ``download_verified``, which replaces whatever is there (a
    truncated or foreign file) only once the new bytes verified. Raises what
    ``download_verified`` raises."""
    target = Path(dest)
    if has_pinned_digest(target, sha256, _pinned_size(size)):
        return target
    return download_verified(url, sha256, target, size=size, timeout=timeout, progress=progress)


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


def _is_link_like(st: os.stat_result) -> bool:
    """A symbolic link, or on Windows any reparse point (a directory junction lstat reports
    as a folder): neither is entered nor changed by the removal."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _make_removable(path: str) -> None:
    """Give every real folder at and below `path` its owner's rwx back, and on Windows clear
    the read-only attribute of a file no other name links to, so that a second rmtree can
    remove what the first one skipped. Every entry is looked at with lstat: a link (or a
    Windows reparse point) is neither changed nor entered, so nothing outside `path` is
    touched and a link cycle cannot hold up the walk. A folder is changed BEFORE it is
    listed, because listing an x-less folder fails. A file's mode is shared by all of its
    hard links, so on POSIX no file is changed at all (unlinking needs only the folder's
    bits there) and on Windows only one with a single link. Errors are swallowed: the
    caller's final check decides."""
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if _is_link_like(st):
            continue
        if stat.S_ISDIR(st.st_mode):
            try:
                os.chmod(p, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
            except OSError:
                pass
            try:
                with os.scandir(p) as entries:
                    stack.extend(entry.path for entry in entries)
            except OSError:
                pass
        elif _WINDOWS and stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and not st.st_mode & stat.S_IWUSR:
            try:
                os.chmod(p, stat.S_IMODE(st.st_mode) | stat.S_IWUSR)
            except OSError:
                pass


def remove_tree_or_link(path: str) -> None:
    """Remove `path` whatever it is: a link (to a folder or not) is unlinked, never
    followed, a folder is removed with everything in it, a file is unlinked. rmtree
    refuses a link, so a linked leftover would otherwise stay and block every later swap.
    OSError when anything of it is still there afterwards (rmtree skips what it cannot
    remove): the next install must not unpack on top of debris.

    A folder that lost its owner's permissions is still removed: when the first rmtree
    leaves something, every real folder of the tree gets its owner's rwx back (on Windows
    a file's read-only attribute is cleared too) and rmtree runs once more
    (_make_removable). An archive can cause that: a hard link member whose target is an
    in-archive link to a folder makes tarfile apply the member's mode (0644) to that
    folder through the link, and without x on it neither rmtree nor `rm -rf` gets inside,
    so every later install failed on the leftover. Links are never followed and the parent
    of `path` is never changed. Deliberate: a second pass rather than an onerror/onexc
    handler, because rmtree has already given up on a folder it could not open when the
    handler runs (a retry from it fails), and the handler's name and signature differ
    between the supported Python versions. Named boundary: a real folder that is swapped
    for a link between its lstat and its chmod is followed; only someone who can already
    write to the folder being removed can do that."""
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
        if os.path.lexists(path):
            _make_removable(path)
            shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.unlink(path)
    if os.path.lexists(path):
        raise OSError(f"{path} could not be removed completely")


def remove_quietly(path: str | os.PathLike) -> None:
    """remove_tree_or_link where a failure must not end the caller (the staging folder of a
    failed install): what stays is a leftover the next install removes first, and that
    install stops with a message if it still cannot."""
    try:
        remove_tree_or_link(os.fspath(path))
    except OSError:
        pass


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
