# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Keeping a caller-supplied path inside the directory it is allowed to touch.

Every surface that takes a path fragment from outside - an HTTP body, a tool
argument, a peer's frame - has to answer the same question: does this name stay
inside the root I opened it against? The answer is only correct when it is
decided on RESOLVED paths. A check built from string prefixes accepts a symlink
that lives inside the root and points anywhere on the host, and the write then
lands at the link's target while every lexical test still says "inside".

That is not hypothetical here: the workspace endpoints compared normalized
strings, so a link planted inside a chat's workspace by any shell-capable tool
run carried mkdir, upload and delete straight out of the jail.

Two helpers, because the same two decisions were being hand-rolled side by side:
one for a path fragment that may address folders (`contained_path`), one for a
single entry name that may not address anything but itself (`safe_entry_name`).

Deliberately free of any web or tool dependency: the callers are an HTTP layer,
an agent-to-agent store and file tools, so the primitive raises `PathEscape` and
lets each surface translate that into its own refusal.

Scope of the conversion so far: the workspace lane and the room workspace, the
two places whose own copies were WRONG. Several correct hand-rolled copies of
the same comparison remain (the coder's documentation write, the threat list,
the skill scanner). Converting them deletes a few lines each and is mechanical,
but each sits in a different subsystem with its own design doc, and none of them
is broken - so they are a follow-up, not a silent drive-by inside this one.
"""
import os
import unicodedata
from pathlib import Path
from typing import Optional

__all__ = ["PathEscape", "WriteRefused", "contained_path", "discard_written", "is_rooted",
           "open_for_write", "safe_entry_name"]


class PathEscape(ValueError):
    """A caller-supplied path or name did not stay inside its root.

    Rejected rather than sanitised. Trimming an escape into something harmless
    would hand the caller a DIFFERENT path than the one they named, and a
    silently redirected write is harder to reason about than a refusal.
    """


def is_rooted(fragment: str) -> bool:
    """True if the fragment names a root of ANY flavour, whatever the host.

    `os.path.isabs` answers for the host, and it does not answer the same
    thing everywhere: Python 3.13 stopped calling a driveless rooted path
    ("/etc/x") absolute on Windows, where 3.10 to 3.12 called it absolute. A
    check built on that call alone therefore lets exactly that fragment past
    on a Windows 3.13 host. The mirror case is just as real on POSIX, where
    "\\etc\\x" is not absolute either and the separator normalisation below
    then turns it into a plain relative path - measured, both of them.

    A fragment reaches these helpers from a JSON body, a tool argument or a
    peer, so it carries the SENDER's convention rather than the host's, and
    both flavours have to be answered here. `os.path.splitroot` would say it
    in one call and is 3.12+, so it cannot be used while 3.10 is supported.

    The twin of this decision lives in `vaf/workflows/engine.py`
    (`_inject_workflow_paths`), which skips rather than refuses; it is correct
    as it stands, so it is a follow-up conversion and not a drive-by here.
    """
    if not fragment:
        return False
    if os.path.isabs(fragment):
        return True
    if fragment[0] in ("/", "\\"):
        return True
    return len(fragment) > 1 and fragment[1] == ":"


def contained_path(root, relative: str = "", *, must_exist: bool = False) -> Path:
    """The absolute, resolved path ``relative`` names inside ``root``.

    Raises `PathEscape` if the fragment is rooted (absolute or drive-anchored,
    in either separator convention - see `is_rooted`), walks upwards, or resolves
    to somewhere outside ``root`` - including by way of a symlink that lives
    inside the root and points out of it, which is exactly the case a prefix
    comparison cannot see.

    Both sides are resolved before they are compared, so a root that itself
    lies under a symlinked parent (a home directory on many systems) does not
    produce a false refusal. Resolution is non-strict, so a path that does not
    exist yet still has a containment answer: callers create entries through
    this (a new folder, an uploaded file) and must be told where the write
    would land BEFORE it happens.

    ``must_exist=True`` additionally requires the resolved path to be an
    existing directory - the shape a listing or a browse needs.

    The returned path is the RESOLVED one, which is the location a write
    actually reaches. Callers that keep displaying the unresolved name are
    showing a label, not a target.
    """
    fragment = str(relative or "").strip()
    # Both separators, whatever the host: a fragment reaches this from a JSON
    # body or a peer, so it carries the sender's convention, not ours.
    normalized = fragment.replace("\\", "/").strip("/")
    if normalized:
        parts = [p for p in normalized.split("/") if p and p != "."]
        if any(p == ".." for p in parts):
            raise PathEscape("path must not walk upwards")
        if any("\x00" in p for p in parts):
            raise PathEscape("path must not contain a null byte")
    else:
        parts = []
    if is_rooted(fragment):
        raise PathEscape("path must be relative")

    root_resolved = Path(root).resolve()
    target = root_resolved.joinpath(*parts).resolve() if parts else root_resolved
    if target != root_resolved and root_resolved not in target.parents:
        raise PathEscape("path must stay inside the root")
    if must_exist and not target.is_dir():
        raise PathEscape("folder not found")
    return target


def safe_entry_name(name: str, *, allow_hidden: bool = False) -> str:
    """One entry name, or `PathEscape`.

    For the case where a caller may name a single file or folder and nothing
    about where it sits: the parent comes from the surface, never from the
    name. So a name carrying a separator is refused rather than trimmed to its
    last component, because trimming would quietly accept ``a/b`` as ``b``.

    Control characters are refused too. They are never part of a name anyone
    meant to write, and a null byte in particular reaches the filesystem call
    as a `ValueError` - which a surface then reports as an internal failure
    rather than as the bad input it is.

    Hidden names stay out by default: the surfaces that hand a name straight to
    a filesystem call are the ones that also hide dotfiles when listing, and a
    name that cannot be seen afterwards is not a name the caller can manage.
    """
    raw = str(name or "").strip()
    if not raw:
        raise PathEscape("name must not be empty")
    if raw in (".", ".."):
        raise PathEscape("name must not be a relative marker")
    if "/" in raw or "\\" in raw:
        raise PathEscape("name must not contain a path separator")
    if os.path.basename(raw) != raw:
        raise PathEscape("name must be a single entry")
    if any(unicodedata.category(ch) == "Cc" for ch in raw):
        raise PathEscape("name must not contain control characters")
    if not allow_hidden and raw.startswith("."):
        raise PathEscape("name must not be hidden")
    return raw


def walk_without_links(root, skip=(), *, onerror=None):
    """Every folder and regular file below `root`, as (relative POSIX path, is_folder), in a
    stable order: links are left out, and so is everything below a linked folder, and every
    entry whose name is in `skip` with all below it. For an upload that must carry this
    folder's own contents and nothing a link points at, and for a cleanup that must delete
    nothing a link points at. `Path.rglob` is not that: before Python 3.13 its `**` followed
    linked folders, so a file under `site/linked -> ~/.ssh` was neither a link itself nor
    skipped. `onerror` is handed to `os.walk`: called with the OSError of a folder that
    could not be listed, so a caller can tell an incomplete walk from a complete one."""
    out = []
    base = Path(root)
    for here, dirs, files in os.walk(base, followlinks=False, onerror=onerror):
        rel_here = Path(here).relative_to(base)
        keep = []
        for name in sorted(dirs):
            if name in skip or os.path.islink(os.path.join(here, name)):
                continue
            keep.append(name)
            out.append(((rel_here / name).as_posix(), True))
        dirs[:] = keep
        for name in sorted(files):
            full = os.path.join(here, name)
            if name in skip or os.path.islink(full) or not os.path.isfile(full):
                continue
            out.append(((rel_here / name).as_posix(), False))
    return sorted(out)



class WriteRefused(PermissionError):
    """`open_for_write` would have written through a link, into something that is not a
    regular file, or into VAF's own state. A PermissionError, so a caller that already turns
    an OSError of its open into a refusal reports this one the same way."""


# The safe branch needs to open a file relative to a directory handle and refuse a link in
# the last component, both in the kernel. Read at call time, so a test can take the other
# branch on a host that has both.
_DIR_FD = (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
           and os.open in os.supports_dir_fd)


def _fd_path(fd: int, fallback: str) -> str:
    """The path an open directory handle really names: the kernel's answer where there is
    one (/proc on Linux, F_GETPATH on macOS), else ``fallback`` resolved, accepted only when
    it is the same directory as the handle."""
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        pass
    try:
        import fcntl
        if hasattr(fcntl, "F_GETPATH"):
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
            return os.fsdecode(raw.split(b"\0", 1)[0])
    except (ImportError, OSError):
        pass
    real = os.path.realpath(fallback)
    if not os.path.samestat(os.fstat(fd), os.stat(real)):
        raise WriteRefused(f"refusing to write into {fallback}: it changed while it was opened")
    return real


def open_for_write(path, mode: str = "wb", *, exclusive: bool = False,
                   encoding: Optional[str] = None, newline: Optional[str] = None,
                   expect_folder=None):
    """Open ``path`` to write it, as a file object, or raise `WriteRefused`.

    For a path a model, a request or a peer chose, and for a temporary ``.part`` beside one.
    Checking a path and then calling ``open()`` on it decides one thing and writes another:
    ``open`` follows a link in the last component, and a folder on the way can be swapped for
    a link between the check and the open. A sandbox environment's code can do both in its
    mounted folder, which the host sees as ordinary entries. So the decision is made on what
    is actually opened:

    - The folder is opened first and the file is created relative to that handle, so the
      folder that was checked is the folder that is written, whatever is renamed meanwhile.
    - The last component is opened with ``O_NOFOLLOW``: a link there is refused, never
      followed (an existing link is not replaced either; delete it first).
    - The folder's real path (the kernel's, from the handle) plus the name must not be VAF's
      own state (`vaf.core.data_stores.is_vaf_state`), and neither may the path as given.
    - Only a regular file is written; a pipe or a device is refused without blocking.
    - An existing file is truncated only after all of that, so a refusal changes nothing.

    ``mode`` is "wb" or "w" (then ``encoding``/``newline`` as for ``open``). ``exclusive``
    refuses an existing file (``O_EXCL``).

    ``expect_folder`` is for a folder somebody else can write: the REAL path (no links on
    the way) the file's folder must have. The opened folder handle's kernel path is compared
    with it, as given and never resolved (resolving it would follow the very link it is
    there to catch), and anything else is refused before the file is opened. That closes
    the window between a caller's check and this open, for every folder on the way: a link
    swapped in for the folder or for any folder above it changes the handle's real path. The
    file is then opened relative to that handle, so later renames cannot redirect it. Without
    a kernel answer (no /proc, no F_GETPATH) the resolved path is accepted only when it is
    the same directory as the handle.

    NAMED BOUNDARY, Windows (no ``O_NOFOLLOW``, no directory handles): a link at the path is
    refused (``islink``), the resolved path is checked before and after the open, and a file
    created in VAF's state through a link that is still in place at the second check is
    removed again. A link or junction swapped
    in between the check and the open is followed for that one open; with ``expect_folder``
    the folder is checked before the open and the path's resolved folder after it, and a
    file this call created elsewhere is removed again and refused, before anything is
    written or truncated. What remains is not narrow: the second check resolves the PATH
    again (``realpath``), not the opened handle, so a link that is in place when ``os.open``
    runs and is removed again before that second check is not seen, and the write lands
    behind it; with this branch simulated on Linux, a process swapping in a tight loop got
    3370 of 20000 writes through. The same window opens VAF's own state, with or without
    ``expect_folder``: ``is_vaf_state`` asks the restored path, so a folder link into VAF's
    state that is gone again by the second check lets the write land there, unrefused and
    not removed. A hard link is a second
    name of the same file and is not seen on any platform; making one needs a process on the
    host with access to both folders (a sandbox environment cannot link to a file outside its
    mount)."""
    from vaf.core.data_stores import is_vaf_state
    if mode not in ("w", "wb"):
        raise ValueError(f"open_for_write writes ('w' or 'wb'), not {mode!r}")
    raw = os.path.abspath(os.path.expanduser(os.fspath(path)))
    parent, name = os.path.split(raw)
    if not name:
        raise WriteRefused(f"refusing to write {raw}: it names a folder, not a file")
    if is_vaf_state(raw):
        raise WriteRefused(f"refusing to write {raw}: it is VAF's own data; choose another folder")
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    if exclusive:
        flags |= os.O_EXCL
    expected = (os.path.normcase(os.path.abspath(os.fspath(expect_folder)))
                if expect_folder is not None else None)
    if _DIR_FD:
        fd = _open_in_folder(parent, name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, raw, expected)
    else:
        fd = _open_without_dir_fd(raw, flags, expected)
    try:
        info = os.fstat(fd)
        import stat as _stat
        if not _stat.S_ISREG(info.st_mode):
            raise WriteRefused(f"refusing to write {raw}: it is not a regular file")
        if not exclusive:
            os.ftruncate(fd, 0)
        if _DIR_FD and hasattr(os, "set_blocking"):
            os.set_blocking(fd, True)
        if mode == "wb":
            return os.fdopen(fd, "wb")
        return os.fdopen(fd, "w", encoding=encoding, newline=newline)
    except BaseException:
        os.close(fd)
        raise


def _folder_moved(raw: str, real: str, expected: str) -> "WriteRefused":
    return WriteRefused(f"refusing to write {raw}: its folder is {real}, not {expected} "
                        "(a link on the way)")


def _open_in_folder(parent: str, name: str, flags: int, raw: str,
                    expected: Optional[str] = None) -> int:
    from vaf.core.data_stores import is_vaf_state
    import errno
    try:
        dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    except NotADirectoryError:
        raise WriteRefused(f"refusing to write {raw}: {parent} is not a folder") from None
    try:
        real_dir = _fd_path(dfd, parent)
        if expected is not None and os.path.normcase(real_dir) != expected:
            raise _folder_moved(raw, real_dir, expected)
        real = os.path.join(real_dir, name)
        if is_vaf_state(real):
            raise WriteRefused(f"refusing to write {raw}: it lands in VAF's own data ({real}); "
                               "choose another folder")
        try:
            return os.open(name, flags, 0o666, dir_fd=dfd)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.EMLINK):  # EMLINK: FreeBSD's O_NOFOLLOW
                raise WriteRefused(f"refusing to write {raw}: it is a link") from None
            if e.errno == errno.ENXIO:
                raise WriteRefused(f"refusing to write {raw}: it is not a regular file") from None
            raise
    finally:
        os.close(dfd)


def discard_written(path, fileobj) -> bool:
    """Remove the file ``fileobj`` (from `open_for_write`) holds at ``path`` after a write
    into it failed, so no partial file stays behind. Call it before closing ``fileobj``.

    Only ever the open file itself: the entry at ``path`` is removed when it is the same
    file (device and inode) as ``fileobj``, so a folder swapped for a link meanwhile can
    never cost another folder its file of the same name. The look and the removal are made
    relative to one folder handle, so nothing is swapped in between them. True if it was
    removed. NAMED BOUNDARY, Windows (no folder handles): the look and the removal are two
    calls by path. Never raises."""
    try:
        raw = os.path.abspath(os.fspath(path))
        parent, name = os.path.split(raw)
        mine = os.fstat(fileobj.fileno())
        if not _DIR_FD:
            if os.path.samestat(os.lstat(raw), mine):
                os.unlink(raw)
                return True
            return False
        dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
        try:
            if not os.path.samestat(os.stat(name, dir_fd=dfd, follow_symlinks=False), mine):
                return False
            os.unlink(name, dir_fd=dfd)
            return True
        finally:
            os.close(dfd)
    except (OSError, ValueError):
        return False


def _open_without_dir_fd(raw: str, flags: int, expected: Optional[str] = None) -> int:
    """The Windows branch (see the named boundary in `open_for_write`)."""
    from vaf.core.data_stores import is_vaf_state
    if os.path.islink(raw):
        raise WriteRefused(f"refusing to write {raw}: it is a link")
    if expected is not None:
        before = os.path.normcase(os.path.realpath(os.path.dirname(raw)))
        if before != expected:
            raise _folder_moved(raw, before, expected)
    # Created by THIS call or not: only a file this call created may be removed again below.
    try:
        fd, created = os.open(raw, flags | os.O_EXCL, 0o666), True
    except FileExistsError:
        if flags & os.O_EXCL:
            raise
        fd, created = os.open(raw, flags & ~os.O_CREAT, 0o666), False
    real = os.path.realpath(raw)
    moved = (expected is not None
             and os.path.normcase(os.path.dirname(real)) != expected)
    if os.path.islink(raw) or is_vaf_state(real) or moved:
        try:
            if created and os.path.samestat(os.fstat(fd), os.stat(real)):
                os.close(fd)
                fd = -1
                os.unlink(real)
        except OSError:
            pass
        finally:
            if fd >= 0:
                os.close(fd)
        raise WriteRefused(f"refusing to write {raw}: it lands in VAF's own data or is a link; "
                           "choose another folder")
    return fd
