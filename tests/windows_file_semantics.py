# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Windows file semantics on any OS, for the files under one folder.

POSIX lets a file be renamed, replaced or deleted while another handle has it open. Windows
does not: a handle opened by Python's `open()` does not grant FILE_SHARE_DELETE, so while it is
open MoveFileEx (`os.replace`, `os.rename`) and DeleteFile (`os.unlink`) are refused with
"Access is denied". And a rename is not one step there: MoveFileEx opens the source by name,
with DELETE access, and then renames the file by that handle, wherever the file is by then.
Code that is right on Linux and macOS can therefore lose writes or files on Windows, and no
local run shows it. `install(monkeypatch, root)` makes it show.

What the seam does, for paths under `root` only (every other path passes through untouched,
so a background thread another test left running is not affected):

- `open()` (`builtins.open`, `io.open`, so `Path.open` and `Path.read_text` too) returns a
  handle that is counted until it is closed; it is refused while a rename holds the file.
- `os.replace` is refused while the source or the destination is open.
- `os.unlink`/`os.remove` is refused while the file is open. During a rename's hold it succeeds
  and dooms that rename, which then fails, as a rename by handle of a deleted file does.
- `os.rename` refuses an open source, holds the file, calls `pause(src)` (a seeded sleep for a
  fuzz, an Event for a deterministic test), then renames the file under its CURRENT name (an
  overlapping rename may have moved it meanwhile; the held file moves with it), and raises
  `FileExistsError` for an existing destination as Windows does.

Every refusal raises `PermissionError(13, "Access is denied")` and is counted in `refusals`.
On Python 3.10 pathlib binds `os.rename`, `os.replace`, `os.unlink` and `io.open` into
`pathlib._NormalAccessor` when it is imported; the seam patches those attributes too.

What it does not model: descriptors from `os.open` (`tempfile.mkstemp` and the exclusive
create), C extensions that open files themselves (sqlite), directories, ACLs and the read-only
attribute. It is a model of Windows, not Windows: it proves a path survives the refusals, the
Windows CI job proves the model right.
"""
import builtins
import io
import os
import pathlib
import threading

ACCESS_DENIED = (13, "Access is denied")


class _Handle:
    """A file object that tells the seam when it is closed."""

    def __init__(self, seam, key, f):
        self._seam = seam
        self._key = key
        self._f = f
        self._open = True

    def _release(self):
        if self.__dict__.get("_open"):
            self._open = False
            self._seam._closed(self._key)

    def close(self):
        try:
            self._f.close()
        finally:
            self._release()

    def __enter__(self):
        self._f.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            return self._f.__exit__(*exc)
        finally:
            self._release()

    def __iter__(self):
        return iter(self._f)

    def __next__(self):
        return next(self._f)

    def __getattr__(self, name):
        if name in ("_f", "_seam", "_key", "_open"):
            raise AttributeError(name)
        return getattr(self._f, name)

    def __del__(self):
        try:
            self._release()
        except Exception:
            pass


class _Hold:
    """MoveFileEx's DELETE handle on the source between its open and its rename."""

    def __init__(self, key):
        self.key = key
        self.doomed = False


class WindowsFileSemantics:
    def __init__(self, root, pause=None):
        self.root = self._key(root)
        self.pause = pause or (lambda src: None)
        self.refusals = 0
        self.opened = 0
        self._lock = threading.Lock()
        self._handles = {}
        self._holds = []
        self._real = {
            "open": builtins.open,
            "replace": os.replace,
            "rename": os.rename,
            "unlink": os.unlink,
        }

    @staticmethod
    def _key(path):
        return os.path.normcase(os.path.abspath(os.fsdecode(os.fspath(path))))

    def _ours(self, path):
        if isinstance(path, int):
            return None
        try:
            key = self._key(path)
        except TypeError:
            return None
        return key if key == self.root or key.startswith(self.root + os.sep) else None

    def _refuse(self, path):
        self.refusals += 1
        return PermissionError(*ACCESS_DENIED, os.fsdecode(os.fspath(path)))

    def _held(self, key):
        return any(h.key == key for h in self._holds)

    def _closed(self, key):
        with self._lock:
            left = self._handles.get(key, 0) - 1
            if left > 0:
                self._handles[key] = left
            else:
                self._handles.pop(key, None)

    # -- the patched calls --------------------------------------------------------------------

    def open(self, file, *args, **kwargs):
        key = self._ours(file)
        if key is None:
            return self._real["open"](file, *args, **kwargs)
        with self._lock:
            if self._held(key):
                raise self._refuse(file)
            f = self._real["open"](file, *args, **kwargs)
            self._handles[key] = self._handles.get(key, 0) + 1
            self.opened += 1
        return _Handle(self, key, f)

    def replace(self, src, dst, *args, **kwargs):
        skey, dkey = self._ours(src), self._ours(dst)
        if skey is None and dkey is None:
            return self._real["replace"](src, dst, *args, **kwargs)
        with self._lock:
            for key, path in ((skey, src), (dkey, dst)):
                if key is not None and (self._handles.get(key) or self._held(key)):
                    raise self._refuse(path)
            return self._real["replace"](src, dst, *args, **kwargs)

    def unlink(self, path, *args, **kwargs):
        key = self._ours(path)
        if key is None:
            return self._real["unlink"](path, *args, **kwargs)
        with self._lock:
            if self._handles.get(key):
                raise self._refuse(path)
            self._real["unlink"](path, *args, **kwargs)
            for hold in self._holds:
                if hold.key == key:
                    hold.doomed = True

    def rename(self, src, dst, *args, **kwargs):
        skey, dkey = self._ours(src), self._ours(dst)
        if skey is None and dkey is None:
            return self._real["rename"](src, dst, *args, **kwargs)
        with self._lock:
            if self._handles.get(skey):
                raise self._refuse(src)
            if not os.path.lexists(src):
                raise FileNotFoundError(2, "The system cannot find the file specified", os.fsdecode(os.fspath(src)))
            hold = _Hold(skey)
            self._holds.append(hold)
        try:
            self.pause(os.fsdecode(os.fspath(src)))
        except BaseException:
            with self._lock:
                self._holds.remove(hold)
            raise
        with self._lock:
            self._holds.remove(hold)
            if hold.doomed:
                raise self._refuse(src)
            if os.path.lexists(dst):
                raise FileExistsError(183, "Cannot create a file when that file already exists",
                                      os.fsdecode(os.fspath(dst)))
            self._real["rename"](hold.key, dst, *args, **kwargs)
            moved = self._key(dst)
            for other in self._holds:
                if other.key == hold.key:
                    other.key = moved


def install(monkeypatch, root, pause=None):
    """Install the seam for the files under `root` until the test ends; returns it."""
    seam = WindowsFileSemantics(root, pause)
    monkeypatch.setattr(builtins, "open", seam.open)
    monkeypatch.setattr(io, "open", seam.open)
    monkeypatch.setattr(os, "replace", seam.replace)
    monkeypatch.setattr(os, "rename", seam.rename)
    monkeypatch.setattr(os, "unlink", seam.unlink)
    monkeypatch.setattr(os, "remove", seam.unlink)
    accessor = getattr(pathlib, "_NormalAccessor", None)
    if accessor is not None:
        # Python 3.10: class attributes, so plain functions would be bound as methods.
        for name, fn in (("open", seam.open), ("replace", seam.replace),
                         ("rename", seam.rename), ("unlink", seam.unlink)):
            if name in vars(accessor):
                monkeypatch.setattr(accessor, name, staticmethod(fn))
    return seam


def retry_like_windows(monkeypatch):
    """The store writer's retry as it runs on Windows (`secure_store._on_windows`), with the
    machine key pinned to the file backend so nothing reaches for the OS keyring."""
    from vaf.core import secure_store
    monkeypatch.setattr(secure_store, "_on_windows", lambda: True)
    monkeypatch.setattr(secure_store, "_default_kek_backend", lambda: "file")
