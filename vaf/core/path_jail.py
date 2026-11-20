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

The cost of an answer is bounded by the length of the text, not by its square. A path
fragment reaches these helpers from an HTTP body (up to the server's body cap), and both
resolving it and comparing it with pathlib grow with the square of its length: CPython's
``realpath`` rebuilds the path string once per component, and ``root in target.parents``
builds every parent and compares each one. Measured with ``"a/" * n``: 1.3 s for 32 KB in
one call, on the event loop of every route that asks inline. So the text is refused first,
while it is still text, when it is longer than any file system opens (`is_overlong_path`,
`PATH_CHARS_MAX`; a single name: `NAME_CHARS_MAX`), and the containment comparison asks the
one parent that can equal the root (`_within`) instead of all of them.
"""
import logging
import os
import unicodedata
from pathlib import Path
from typing import Optional, Tuple

__all__ = ["FolderMoved", "NAME_CHARS_MAX", "PATH_CHARS_MAX", "PathEscape", "WriteRefused",
           "contained_path", "describe_error", "discard_written", "is_overlong_path", "is_remote_path",
           "is_rooted", "open_folder", "open_for_read", "open_for_write", "safe_entry_name"]

# Longer than any path a Linux or macOS system call opens: PATH_MAX is 4096 bytes on Linux
# (the terminating NUL included) and 1024 on macOS, and every character is at least one byte
# in the file system encoding (UTF-8, or surrogateescape for undecodable bytes). A text this
# long joined to any root therefore ends in ENAMETOOLONG, whatever it names.
PATH_CHARS_MAX = 4096
# Longer than any single entry name: NAME_MAX is 255 bytes on ext4 and APFS and 255 UTF-16
# units on NTFS, and every code point is at least one of either.
NAME_CHARS_MAX = 255


class PathEscape(ValueError):
    """A caller-supplied path or name did not stay inside its root.

    Rejected rather than sanitised. Trimming an escape into something harmless
    would hand the caller a DIFFERENT path than the one they named, and a
    silently redirected write is harder to reason about than a refusal.
    """


class FolderMoved(PathEscape):
    """`open_folder` opened a folder whose real path is not the one the caller judged: a link
    was swapped in on the way, or the folder was renamed after the caller's check."""


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


def is_remote_path(spelled: str) -> bool:
    """True if a path names a network share or a Windows device namespace, whatever the host.

    On Windows, merely resolving or testing such a path reaches out: ``Path.resolve()`` (in
    3.13 ``ntpath.realpath`` calls ``nt._getfinalpathname`` first) opens ``\\\\host\\share``,
    which is an SMB connection to that host carrying the Windows account's NTLM login attempt,
    before any root comparison can refuse it. A path a caller hands in (an HTTP query, a tool
    argument) must therefore be refused while it is still text.

    Lexical, in both separator conventions on every host, because the text carries the
    sender's convention: ``\\\\host\\share`` and ``//host/share`` (UNC), ``\\\\?\\`` and
    ``\\\\.\\`` (the Win32 device and long-path namespaces, ``\\\\?\\UNC\\host`` included),
    and ``\\??\\`` (the NT object namespace, which Win32 passes through unchanged). On POSIX a
    leading ``//`` is an implementation-defined spelling of ``/`` that nothing needs, so
    refusing it only ever errs towards refusing.

    Asked first by: the web file routes (``_allowed_file_path``); the file tools' screen
    (``is_safe_path``) and the two tools that touched their path before it (``codesearch``,
    ``download_file``); the project-folder checks (``is_unsafe_project_dir``,
    ``project_dir_refusal``) and the lanes that probed a folder before acting on them (the
    coder's ``project_path``, a folder named in its task text and its stored last project,
    ``run_project_git``'s ``_resolve_project``, ``run_in_workspace``, a sandbox environment's
    mount); ``jail_allows``; and the coding agent's own checks on a file tool's path
    (``_remote_path_refusal``, the duplicate-write check ``_identical_rewrite``) and the
    folder of the six inner tools the main agent has as well, the four git tools,
    ``update_codex`` and ``add_memory`` (``tool_work_folder``), while ``git_add_commit``'s files
    go through ``contained_path``. ``tests/test_remote_path_refused.py`` drives those lanes with share
    paths and records every disk call, and scans every function that screens with one of the
    four screens above (``is_safe_path``, ``project_dir_refusal``, ``is_unsafe_project_dir``,
    ``jail_allows``) for a disk call on the screened argument (by name, directly) before the
    screen's answer is acted on. The scan sees only functions that call one of those four: a
    lane that touches an outside path without calling any of them (the coding agent's own
    checks and ``git_add_commit`` were such lanes) is pinned by the behavioral tests in the
    same file, not by the scan. The host shell (``host_bash``) stays outside: its commands
    reach whatever the machine reaches, which is a question of its own, not a path screen.
    Where nobody confirmed the command (the coding agent, a workflow step), the command
    policy's ``share_path`` category refuses one that names a share or device path, read from
    the raw text (``share_paths`` in vaf/core/command_policy.py, which asks this function).

    NAMED BOUNDARY: ``run_project_git`` opens its ``cwd`` (`open_folder`; on Windows
    ``os.path.isdir``) without asking here, because every caller has refused a share or
    device path for it already: the coder's run folder (``_choose_base_dir``,
    ``_base_dir_tool``), ``project_git``'s
    ``_resolve_project`` (which the code audit tool asks too, before ``jail_allows``), the
    folder a person typed for ``vaf audit``, the snapshots' own working folder, and the git
    tools' folder (``tool_work_folder``, which asks ``project_dir_refusal`` for the caller the
    tools declare, this question first). Measured: no caller hands it a share or device path.

    On the facade as ``vaf.is_remote_path``, because the route recipe in docs/EMBEDDING.md asks
    primitives that resolve the path (``vaf.is_vaf_state``), so a surface built to it would
    reach out as well. A relative fragment through ``vaf.contained_path`` needs no extra
    question: ``is_rooted`` refuses every fragment that starts with a separator.

    NAMED BOUNDARY: a drive letter mapped to a share (``Z:\\x``) and a link or junction
    inside a served root that points at a share are not seen here; both are set up on the
    machine by its owner, not spelled by a caller. The same holds for a home folder that is
    itself a share (a roaming profile whose ``Path.home()`` is ``\\\\server\\...``): its
    Documents and Downloads are then share paths, so the web file routes refuse them (every
    file chip and picture of such an account answers 403) and the file tools refuse a path
    written that way. A path the tools build from a folder name (``Documents``) goes through,
    because the caller did not spell the share. The WSL file shares typed as a path
    (``\\\\wsl$\\...``, ``\\\\wsl.localhost\\...``) are UNC paths as well and are refused on
    Windows; a Linux distribution's files are reached from Linux, not through them.
    """
    text = str(spelled or "").lstrip()
    norm = text.replace("\\", "/")
    return norm.startswith("//") or norm.startswith("/??/")


def is_overlong_path(spelled) -> bool:
    """True if a path text is longer than any file system opens (`PATH_CHARS_MAX` characters,
    every character counted), whatever the host.

    Ask it first, before anything resolves, stats or compares the path, `is_remote_path`
    included. Both of CPython's ways to answer a containment question grow with the square of
    the path's length: ``Path.resolve()`` (``posixpath.realpath`` builds ``path + name`` once
    per component; measured 0.05 s at 32 KB, 0.48 s at 128 KB, 1.85 s at 256 KB) and pathlib's
    ``root in target.parents`` / ``is_relative_to`` (1.27 s at 32 KB; ``jail_allows`` 3.8 s).
    A request body may carry megabytes, and a route that asks inline holds the event loop for
    every other request while it waits. Such a path opens nowhere anyway: Linux refuses more
    than 4096 bytes, macOS more than 1024, with ENAMETOOLONG.

    Lexical and constant-time; it touches no disk. The raw length is measured, whitespace at
    the ends included, because the callers resolve the text they were given and not a
    stripped copy of it: a leading pad of N spaces in front of a path just under the cap is
    one long first component, which realpath copies again for every later part (measured
    14 s for a 10 MB pad on a save route before this counted it). A caller that strips the
    text before it uses it (`contained_path`) asks about the stripped text.

    Asked first by: `contained_path` (so the workspace routes, the room folder routes and
    every other caller of it), `jail_allows`, the file tools' screen ``is_safe_path`` and the
    web file routes (``_allowed_file_path``, whose admin branch never reaches ``jail_allows``).
    On the facade as ``vaf.is_overlong_path`` for a surface built to the route recipe in
    docs/EMBEDDING.md, which has the same admin shape.

    NAMED BOUNDARY, Windows: with long paths enabled Windows opens up to 32 767 characters,
    so a path between 4097 and 32 767 characters is refused there although the OS could open
    it. No caller produces one: every source is a folder the UI shows, a room member, a cloud
    file name, a chat file or a git name.

    NAMED BOUNDARY, the screens a model's text reaches without an account's request:
    ``project_dir_refusal`` and ``is_unsafe_project_dir`` (vaf/core/workspace_guard.py),
    ``run_in_workspace``, ``codesearch``, ``download_file``, the coder's project folder checks
    and a sandbox environment's mount ask `is_remote_path` but not this. Their cost is bounded
    by what a model writes in one call (measured about 5 s for a 64 KB path on the thinking
    workspace lane before `contained_path` asked this), and no account can send them a request
    body. The same holds for the remaining ``parents`` / ``is_relative_to`` comparisons in
    vaf/: every one an account's request reaches sits behind one of the four screens above,
    where a path is at most 4096 characters (``jail_allows`` measured 0.14 s at 4 KB). They
    are candidates for `_within` if a profile ever shows them.
    """
    return len(str(spelled or "")) > PATH_CHARS_MAX


def _within(target: Path, root: Path) -> bool:
    """``target == root or root in target.parents``, in time linear in the path's length.

    The membership test builds every parent and compares each one with ``root``, which is
    quadratic in the number of parts. Only one parent can be equal: pathlib's equality
    (case-folded for the Windows flavour, exact for POSIX) holds only between paths with the
    same number of parts, so the candidate is the parent with as many parts as ``root``.
    Comparing that one keeps pathlib's own equality, which a string prefix test would not:
    it compares case-exact where Windows folds case, and ``root + sep`` refuses everything
    below an anchor root (``/``, ``C:\\``, ``\\\\srv\\share\\``) that already ends in one.

    The parent at index ``i`` has ``len(target.parts) - 1 - i`` parts, so when ``depth``
    exceeds the number of parents no parent has as many parts as ``root`` and the answer is
    False: an anchored target against a root with no parts at all (``Path(".")``), which
    the membership test answered False and an unbounded index would raise on.
    """
    if target == root:
        return True
    depth = len(target.parts) - len(root.parts)
    return 0 < depth <= len(target.parents) and target.parents[depth - 1] == root


def contained_path(root, relative: str = "", *, must_exist: bool = False) -> Path:
    """The absolute, resolved path ``relative`` names inside ``root``.

    Raises `PathEscape` if the fragment is rooted (absolute or drive-anchored,
    in either separator convention - see `is_rooted`), names a drive in any later
    part (``a/Z:/x``; all of these are decided as text, before anything is
    resolved), walks upwards, or resolves
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
    existing directory - the shape a listing or a browse needs. A path the OS
    cannot even look at (too long for it, no permission) is "folder not found"
    as well, never a raw `OSError`: it is not a folder a listing can use. Without
    ``must_exist``, a path the OS refuses to resolve at all (on Windows, past
    MAX_PATH without long-path support) is ``PathEscape("path cannot be resolved")``.

    A fragment longer than `PATH_CHARS_MAX` characters (whitespace at its ends
    stripped first, as for every check here) is refused with "path is
    too long" before anything else, the separator and upward-walk checks
    included: no file system opens it, and resolving and comparing it would
    cost time that grows with the square of its length (see `is_overlong_path`).
    The containment comparison itself is linear (`_within`).

    The returned path is the RESOLVED one, which is the location a write
    actually reaches. Callers that keep displaying the unresolved name are
    showing a label, not a target.
    """
    fragment = str(relative or "").strip()
    # First, while it is still text and before any split or Path: the cheapest refusal, and
    # the one that bounds every step below.
    if is_overlong_path(fragment):
        raise PathEscape("path is too long")
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
    # A drive in a LATER part (`a/Z:/x`, `sub/b:x`): on Windows joining it replaces everything
    # before it with a drive-relative path (`Z:x`), and resolving that probes the drive, which
    # may be mapped to a share. Refused as text, in every part and on every host: a POSIX name
    # whose second character is a colon is refused with it.
    if any(len(p) >= 2 and p[1] == ":" for p in parts):
        raise PathEscape("path must not name a drive")

    try:
        root_resolved = Path(root).resolve()
        target = root_resolved.joinpath(*parts).resolve() if parts else root_resolved
    except OSError:
        # A non-strict resolve still raises an error it does not expect: on Windows
        # ntpath.realpath re-raises winerror 206 (ERROR_FILENAME_EXCED_RANGE, a path past
        # MAX_PATH without long-path support), which is outside its allow list. No folder a
        # listing can use, and no target a write can be told about, so a refusal instead of
        # a raw OSError that a route would answer with a 500.
        if must_exist:
            raise PathEscape("folder not found")
        raise PathEscape("path cannot be resolved")
    if not _within(target, root_resolved):
        raise PathEscape("path must stay inside the root")
    if must_exist:
        # Path.is_dir() swallows only "does not exist"-like errors and re-raises the rest
        # (ENAMETOOLONG, EACCES), which a route would answer with a 500.
        try:
            is_dir = target.is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
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

    A name longer than `NAME_CHARS_MAX` characters is refused with "name is too
    long" before every other check: no file system holds one, and the
    character scan below would otherwise run over all of it.
    """
    raw = str(name or "").strip()
    if len(raw) > NAME_CHARS_MAX:
        raise PathEscape("name is too long")
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


# The WARNING keeps the withheld detail on the server; a `shutil.Error` with thousands of
# pairs would otherwise write one line of megabytes.
_LOGGED_CHARS = 2000


def _class_name(exc) -> str:
    try:
        name = type(exc).__name__
    except Exception:
        return "Error"
    return name if type(name) is str else "Error"


def _withheld(exc) -> str:
    name = _class_name(exc)
    try:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None) if response is not None else None
    except Exception:
        status = None
    if type(status) is int:
        return f"{name} (HTTP {status})"
    return f"{name} (details in the server log)"


def _exact_text(exc):
    """``str(exc)`` as an exact `str`, or None when it cannot be read. A ``__str__`` may
    return a subclass whose own methods raise; ``str.__str__`` copies it into a plain one."""
    try:
        text = str(exc)
        return text if type(text) is str else str.__str__(text)
    except Exception:
        return None


def describe_error(exc: BaseException, *, log: bool = True) -> str:
    """The text of ``exc`` that is safe to send back to whoever made the request.

    An error raised by the operating system quotes the absolute path it failed on
    (``[Errno 36] File name too long: '/home/user/.../workspace/bbb...'``), so an answer built
    from ``str(e)`` hands the caller the outside of the root it was just contained in: the home
    folder, the account name, the data directory's layout. That is the second way a contained
    root leaks, after the path itself, and it is reached by text alone: a name over 255
    characters makes ``is_file()``, ``mkdir`` and ``write_bytes`` raise exactly that.

    In order:

    (a) Not an `OSError`: ``str(exc)``, the text its raiser wrote, unchanged.
    (b) An `OSError` with an integer ``errno``: ``os.strerror(errno)``, the operating
        system's own words for it ("File name too long", "No space left on device",
        "Connection refused"), which never contain a path. Not the error's ``strerror``: any
        raiser can put text there (``OSError(13, "cannot read /srv/x/key.pem", ...)``).
    (c) An `OSError` with an ``errno`` that is not an integer, or without one but with a
        ``strerror``, a ``filename`` or a ``filename2``, or a `WriteRefused` (every one names
        the path it refused): the withheld text.
    (d) Any other `OSError`: ``str(exc)`` when its class comes from ``builtins`` or from VAF
        (``vaf.``), whose own raise sites put an id in the message ("Session not found: <id>",
        "Task not found"); the withheld text for every other module, whose messages quote
        paths: ``shutil.Error`` (the pairs it failed to copy), Pillow's
        ``UnidentifiedImageError`` (the file it could not read), a ``requests`` error (the
        URL and its query).

    The withheld text is ``"<ClassName> (details in the server log)"``, or
    ``"<ClassName> (HTTP <code>)"`` when the error carries a ``response`` with an integer
    ``status_code`` (``requests``' and ``huggingface_hub``'s HTTP errors are OSErrors): a
    refused login, a gated download and a missing file stay told apart without the URL. With
    ``log`` true the full ``str(exc)`` (its first `_LOGGED_CHARS` characters) goes to this
    module's logger at WARNING, so the detail stays on the server. ``log=False`` is for a
    caller that logs the exception itself. Nothing is logged when nothing was withheld.

    Never raises an `Exception`, whatever the exception class does: a ``str()`` that raises
    or returns a ``str`` subclass, a class whose ``__name__`` raises, a logging handler or
    filter that raises. One ``str()`` and a few attribute reads; no pattern is matched.
    """
    text = _exact_text(exc)
    try:
        answer = _describe(exc, text)
    except Exception:
        answer = _withheld(exc)
    try:
        if log and answer != text:
            shown = text if text is not None else "(its text could not be read)"
            if len(shown) > _LOGGED_CHARS:
                shown = shown[:_LOGGED_CHARS] + f"... ({len(shown)} characters)"
            logging.getLogger(__name__).warning("%s withheld from a reply: %s",
                                                _class_name(exc), shown)
    except Exception:
        pass
    return answer


def _describe(exc, text) -> str:
    if not isinstance(exc, OSError):
        return text if text is not None else _class_name(exc)
    if exc.errno is not None:
        if type(exc.errno) is int:
            try:
                return os.strerror(exc.errno)
            except (ValueError, OverflowError):
                pass
        return _withheld(exc)
    if (exc.strerror is not None or exc.filename is not None or exc.filename2 is not None
            or isinstance(exc, WriteRefused)):
        return _withheld(exc)
    module = str(getattr(type(exc), "__module__", "") or "")
    if text is not None and (module == "builtins" or module.startswith("vaf.")):
        return text
    return _withheld(exc)


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


def _protected(path) -> str:
    """What ``path`` would write into that no writer of a chosen name may touch, or "":
    VAF's own state (`vaf.core.data_stores.is_vaf_state`) or the home directory's
    configuration (`vaf.core.workspace_guard.is_host_config`), which runs outside every
    containment at the next login, shell or Python start."""
    from vaf.core.data_stores import is_vaf_state
    from vaf.core.workspace_guard import is_host_config
    if is_vaf_state(path):
        return "VAF's own data"
    if is_host_config(path):
        return "the home directory's configuration"
    return ""


def open_for_write(path, mode: str = "wb", *, exclusive: bool = False,
                   encoding: Optional[str] = None, newline: Optional[str] = None,
                   expect_folder=None, dir_fd: Optional[int] = None):
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
      own state (`vaf.core.data_stores.is_vaf_state`) or the home directory's configuration
      (`vaf.core.workspace_guard.is_host_config`), and neither may the path as given.
    - Only a regular file is written; a pipe or a device is refused without blocking.
    - An existing file is truncated only after all of that, so a refusal changes nothing.

    ``mode`` is "wb" or "w" (then ``encoding``/``newline`` as for ``open``), or "ab" or "a"
    to append: the same checks, the file opened with ``O_APPEND`` and never truncated. ``exclusive``
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

    ``dir_fd`` is a handle of the file's folder that the caller already holds: the file is
    opened relative to it, and the folder is not opened again by ``path`` (which still names
    the file for the checks and the messages; its folder's kernel path is the handle's). For
    a caller that decided on a folder by its identity: another folder moved to the same name
    has the same real path, and opening by path would truncate that folder's file before any
    later comparison. Only where folder handles exist (`_DIR_FD`); elsewhere it is refused
    (ValueError).

    NAMED BOUNDARY, Windows (no ``O_NOFOLLOW``, no directory handles): a link at the path is
    refused (``islink``), the resolved path is checked before and after the open, and a file
    created in VAF's state through a link that is still in place at the second check is
    removed again, best effort only: an error of that removal is ignored and the write is
    still refused, so an empty file can stay in VAF's state. A link or junction swapped
    in between the check and the open is followed for that one open; with ``expect_folder``
    the folder is checked before the open and the path's resolved folder after it, and a
    file this call created elsewhere is refused and removed again (best effort, as above),
    before anything is written or truncated. What remains is not narrow: the second check resolves the PATH
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
    if mode not in ("w", "wb", "a", "ab"):
        raise ValueError(f"open_for_write writes ('w', 'wb', 'a' or 'ab'), not {mode!r}")
    append = mode.startswith("a")
    raw = os.path.abspath(os.path.expanduser(os.fspath(path)))
    parent, name = os.path.split(raw)
    if not name:
        raise WriteRefused(f"refusing to write {raw}: it names a folder, not a file")
    what = _protected(raw)
    if what:
        raise WriteRefused(f"refusing to write {raw}: it is {what}; choose another folder")
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    if exclusive:
        flags |= os.O_EXCL
    if append:
        flags |= os.O_APPEND
    expected = (os.path.normcase(os.path.abspath(os.fspath(expect_folder)))
                if expect_folder is not None else None)
    if dir_fd is not None and not _DIR_FD:
        raise ValueError("open_for_write(dir_fd=) needs folder handles")
    if _DIR_FD:
        fd = _open_in_folder(parent, name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, raw, expected,
                             dir_fd=dir_fd)
    else:
        fd = _open_without_dir_fd(raw, flags, expected)
    try:
        info = os.fstat(fd)
        import stat as _stat
        if not _stat.S_ISREG(info.st_mode):
            raise WriteRefused(f"refusing to write {raw}: it is not a regular file")
        if not exclusive and not append:
            os.ftruncate(fd, 0)
        if _DIR_FD and hasattr(os, "set_blocking"):
            os.set_blocking(fd, True)
        if mode.endswith("b"):
            return os.fdopen(fd, mode)
        return os.fdopen(fd, mode, encoding=encoding, newline=newline)
    except BaseException:
        os.close(fd)
        raise


def _folder_moved(raw: str, real: str, expected: str) -> "WriteRefused":
    return WriteRefused(f"refusing to write {raw}: its folder is {real}, not {expected} "
                        "(a link on the way)")


def open_folder(path, *, dir_fd: Optional[int] = None, expect_folder=None,
                follow: bool = True, create: bool = False,
                path_hint=None) -> Tuple[int, str]:
    """``path`` opened as a folder handle: ``(fd, real)``, ``real`` being the kernel's path
    for the handle. The caller closes ``fd``.

    For work on a folder that somebody else can rename or replace with a link: everything
    that is done relative to the handle (``dir_fd=``, ``/proc/self/fd/<fd>``) reaches the
    folder that was opened, whatever is swapped in under its name afterwards.

    - ``dir_fd``: ``path`` is opened relative to that folder handle (a single name, ``..``).
    - ``expect_folder``: the REAL path the opened folder must have. Compared with ``real``
      as given and never resolved (resolving it would follow the very link it is there to
      catch); anything else closes the handle and raises `FolderMoved`, a `PathEscape` ("a
      link on the way"). A link swapped in for the folder or any folder above it changes ``real``.
    - ``follow=False``: a link as the last part is refused (`PathEscape`), never followed;
      so is an entry that is not a folder.
    - ``create=True`` (only with ``dir_fd`` and ``follow=False``): a missing folder is made
      first, relative to ``dir_fd`` (``mkdir`` with ``dir_fd``), never by path, so a folder
      above it swapped for a link at that moment gets nothing; an entry already there is
      opened as above (a link refused).

    - ``path_hint``: where the opened folder is believed to be, for a host without a kernel
      answer for a handle (no /proc, no F_GETPATH): ``real`` is then that path resolved,
      accepted only when it is the same folder as the handle (``samestat``). Needed with
      ``dir_fd`` and no ``expect_folder``, where ``path`` alone ("." or "..") says nothing.

    Any other ``OSError`` (missing, not a folder, no permission) is raised as it is. Only
    where `_DIR_FD` holds (no folder handles on Windows): callers branch on it.

    NAMED BOUNDARY, the facade: not on ``vaf/__init__.py``. Measured: no module outside
    ``vaf/core`` needs a raw folder handle, and the two public lanes that carry one are
    ``vaf.open_for_write(expect_folder=)`` and ``vaf.run_project_git(expect_folder=)``. Its
    users are `open_for_write`, `open_for_read`, the project git runner
    (`vaf.core.git_runner`), the project notes (`vaf.core.persistence.PersistenceManager`)
    and the code audit's state (`vaf.core.code_audit`)."""
    import errno
    spelled = os.fspath(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    if not follow:
        flags |= os.O_NOFOLLOW
    if create:
        if dir_fd is None or follow:
            raise ValueError("open_folder(create=True) makes a folder relative to dir_fd, "
                             "with follow=False")
        try:
            os.mkdir(spelled, dir_fd=dir_fd)
        except FileExistsError:
            pass
    try:
        if dir_fd is None:
            fd = os.open(spelled, flags)
        else:
            fd = os.open(spelled, flags, dir_fd=dir_fd)
    except OSError as e:
        # ELOOP: a link under O_NOFOLLOW (EMLINK on FreeBSD); ENOTDIR: no folder at all.
        if not follow and e.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            raise PathEscape(f"{spelled} is a link or not a folder") from None
        raise
    try:
        fallback = (os.fspath(path_hint) if path_hint is not None
                    else spelled if dir_fd is None or expect_folder is None
                    else os.fspath(expect_folder))
        try:
            real = _fd_path(fd, fallback)
        except WriteRefused as e:
            raise PathEscape(str(e)) from None
        if expect_folder is not None:
            expected = os.path.normcase(os.path.abspath(os.fspath(expect_folder)))
            if os.path.normcase(real) != expected:
                raise FolderMoved(f"its folder is {real}, not {expected} (a link on the way)")
        return fd, real
    except BaseException:
        os.close(fd)
        raise


def open_for_read(path, *, expect_folder=None):
    """Open ``path`` to read it, as a binary file object, or raise `PathEscape` (`FolderMoved`
    for a folder that is not ``expect_folder``) or ``OSError`` (missing, no permission).

    The read twin of `open_for_write`, for a file in a folder somebody else can fill with
    links (a project a sandbox environment mounts) whose text goes somewhere that matters, a
    model's prompt above all. Reading by path decides on one thing and reads another: ``open``
    follows a link in the last component, and a folder on the way can be swapped for a link
    between the caller's check and the read. So:

    - The file's folder is opened first (`open_folder`) and, with ``expect_folder``, its
      handle's kernel path must be that REAL path (compared as given, never resolved). A link
      swapped in for the folder or any folder above it is refused (`FolderMoved`).
    - The file is opened relative to that handle with ``O_NOFOLLOW``: a link as the last
      component is refused, never followed.
    - Only a regular file is read; a pipe or a device is refused without blocking
      (``O_NONBLOCK`` for the open, blocking again before the file object is returned).

    NAMED BOUNDARY, Windows (no folder handles, no ``O_NOFOLLOW``): a link at the name is
    refused (``lstat``), the opened file must be the very file checked before the open
    (``samestat``: a file or folder link swapped in only for the moment of the open opens
    another file and is refused), and with ``expect_folder`` the file's resolved folder is
    compared after the open. A folder link in place over both the check and the open, and gone
    again before that comparison, is still read; simulated on Linux with this branch forced
    and a racer exchanging the folder and a link in one step, 635 of 20000 reads of the
    project notes (7053 without the ``samestat``). Closing it means comparing the opened
    handle's final path (``GetFinalPathNameByHandleW``), which needs a Windows host to verify.
    NAMED BOUNDARY: a hard link is the same file under a second name and passes, as for every
    writer.

    NAMED BOUNDARY, the facade: not on ``vaf/__init__.py``. Measured: no module outside
    ``vaf/core`` calls it; the public lane that carries it is ``vaf.code_audit(expect_folder=)``.
    It joins the facade with its first caller outside the core (the file tools' read is the
    named candidate). Its users are the project notes (`vaf.core.persistence`) and the code
    audit (`vaf.core.code_audit`)."""
    import errno
    import stat as _stat
    raw = os.path.abspath(os.fspath(path))
    parent, name = os.path.split(raw)
    if not name:
        raise PathEscape(f"{raw} names a folder, not a file")
    if _DIR_FD:
        dfd, _real = open_folder(parent, expect_folder=expect_folder)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.EMLINK):   # EMLINK: FreeBSD's O_NOFOLLOW
                raise PathEscape(f"{raw} is a link") from None
            raise
        finally:
            os.close(dfd)
    else:
        checked = os.lstat(raw)
        if not _stat.S_ISREG(checked.st_mode):
            raise PathEscape(f"{raw} is a link or not a regular file")
        fd = os.open(raw, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            raise PathEscape(f"{raw} is not a regular file")
        if not _DIR_FD:
            # The file that was opened must be the one just checked: a link swapped in for the
            # file, or for the folder, for the moment of the open opens another file, even
            # when the name is restored before the comparison below.
            if not os.path.samestat(info, checked):
                raise PathEscape(f"{raw} changed while it was opened")
            if expect_folder is not None:
                expected = os.path.normcase(os.path.abspath(os.fspath(expect_folder)))
                real = os.path.normcase(os.path.dirname(os.path.realpath(raw)))
                if real != expected:
                    raise FolderMoved(f"its folder is {real}, not {expected} (a link on the way)")
        elif hasattr(os, "set_blocking"):
            os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _open_in_folder(parent: str, name: str, flags: int, raw: str,
                    expected: Optional[str] = None, dir_fd: Optional[int] = None) -> int:
    import errno
    try:
        # With the caller's handle the folder is reopened through it ("."), never by its path:
        # the same folder, whatever is at its name now, compared like any other.
        dfd, real_dir = (open_folder(parent, expect_folder=expected) if dir_fd is None
                         else open_folder(".", dir_fd=dir_fd, expect_folder=expected,
                                          path_hint=parent))
    except NotADirectoryError:
        raise WriteRefused(f"refusing to write {raw}: {parent} is not a folder") from None
    except PathEscape as e:
        raise WriteRefused(f"refusing to write {raw}: {e}") from None
    try:
        real = os.path.join(real_dir, name)
        what = _protected(real)
        if what:
            raise WriteRefused(f"refusing to write {raw}: it lands in {what} ({real}); "
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
    calls by path, and ``fileobj`` is closed between them, because Windows refuses to
    remove a file that is still open (Python's ``open`` does not share delete access);
    closing it again afterwards is harmless. Never raises."""
    try:
        raw = os.path.abspath(os.fspath(path))
        parent, name = os.path.split(raw)
        mine = os.fstat(fileobj.fileno())
        if not _DIR_FD:
            if os.path.samestat(os.lstat(raw), mine):
                fileobj.close()
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
    if os.path.islink(raw) or _protected(real) or moved:
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
        raise WriteRefused(f"refusing to write {raw}: it lands in VAF's own data or the home "
                           "directory's configuration, or is a link; choose another folder")
    return fd
