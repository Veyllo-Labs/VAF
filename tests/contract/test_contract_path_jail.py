# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: vaf.contained_path, vaf.safe_entry_name, vaf.PathEscape, vaf.is_remote_path,
vaf.is_overlong_path, vaf.describe_error.

docs/EMBEDDING.md promises an embedder that a tool taking a path from a model,
a request body or a peer can decide containment with a supported primitive
instead of hand-rolling it. Pinned here: the containment holds against a
SYMLINK inside the root (the case a prefix comparison cannot see, and the one
that made this a primitive), a path that does not exist yet still gets an
answer, and the refusal is one exception type an embedder can catch. And an
absolute path from outside can be refused while it is still text when it names a
network share or a device path, which resolving it on Windows already contacts.
And a path or a name longer than any file system opens is refused as text, because
resolving and comparing it costs time that grows with the square of its length.
And when the file operation itself fails, the text that may be sent back carries the
operating system's reason, never the absolute path its error quotes.
"""
import builtins
import errno
import logging
import os
import random
import shutil

import pytest

import vaf


def test_a_relative_fragment_resolves_inside_the_root(tmp_path):
    (tmp_path / "reports" / "q1").mkdir(parents=True)
    assert vaf.contained_path(tmp_path, "reports/q1") == (tmp_path / "reports" / "q1").resolve()
    # No fragment addresses the root itself, which is what a browse starts from.
    assert vaf.contained_path(tmp_path) == tmp_path.resolve()


def test_a_symlink_inside_the_root_does_not_carry_the_caller_out(tmp_path):
    """The reason this is a primitive rather than a comparison at each caller.

    A lexical check (normpath plus a startswith prefix test) accepts this: the
    joined string never leaves the root. Only resolving both sides sees it.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.symlink(outside, root / "shared")

    with pytest.raises(vaf.PathEscape):
        vaf.contained_path(root, "shared")
    with pytest.raises(vaf.PathEscape):
        vaf.contained_path(root, "shared/deeper")


def test_upward_walks_and_absolute_paths_are_refused(tmp_path):
    for bad in ("..", "../elsewhere", "a/../../b"):
        with pytest.raises(vaf.PathEscape):
            vaf.contained_path(tmp_path, bad)
    with pytest.raises(vaf.PathEscape):
        vaf.contained_path(tmp_path, os.sep + "etc")


def test_a_rooted_fragment_is_refused_in_EITHER_convention(tmp_path):
    """The promise is refusal, and refusal must not depend on the host.

    A fragment arrives from a request body, a tool argument or a peer, so it
    carries the SENDER's convention. `os.path.isabs` only ever answers for the
    host, so a check built on it alone reads a Windows-rooted fragment on
    POSIX (and, since Python 3.13 stopped calling a driveless rooted path
    absolute on Windows, a POSIX-rooted one there) as plain relative text.
    Nothing escapes when that happens - the resolve step below still holds the
    target inside the root - but the caller is handed a DIFFERENT path than
    the one they named, which is the one thing docs/EMBEDDING.md promises
    these helpers never do.

    MEASURED before the fix, on Linux: "\\etc\\escape.txt" was accepted and
    became <root>/etc/escape.txt, and "\\\\srv\\share\\x" became
    <root>/srv/share/x.
    """
    for bad in ("/etc/escape.txt", "\\etc\\escape.txt",
                "C:/escape.txt", "C:\\escape.txt",
                "//srv/share/escape.txt", "\\\\srv\\share\\escape.txt"):
        with pytest.raises(vaf.PathEscape, match="must be relative"):
            vaf.contained_path(tmp_path, bad)

    # And the other direction, so the guard cannot be satisfied by refusing
    # everything: an ordinary fragment still resolves, in both conventions.
    (tmp_path / "sub").mkdir()
    assert vaf.contained_path(tmp_path, "sub/ok.txt") == tmp_path.resolve() / "sub" / "ok.txt"
    assert vaf.contained_path(tmp_path, "sub\\ok.txt") == tmp_path.resolve() / "sub" / "ok.txt"


def test_a_path_that_does_not_exist_yet_still_gets_an_answer(tmp_path):
    """Callers create through this - a new folder, an uploaded file - so the
    answer must come BEFORE the write, when the target is still absent."""
    target = vaf.contained_path(tmp_path, "brand/new/leaf")
    assert not target.exists()
    assert str(target).startswith(str(tmp_path.resolve()))
    with pytest.raises(vaf.PathEscape):
        vaf.contained_path(tmp_path, "brand/new/leaf", must_exist=True)


def test_an_entry_name_may_not_address_anything_but_itself(tmp_path):
    assert vaf.safe_entry_name("notes.md") == "notes.md"
    assert vaf.safe_entry_name("  spaced name  ") == "spaced name"
    for bad in ("", "   ", ".", "..", "a/b", "a\\b", ".hidden", "with\x00null", "with\nnewline"):
        with pytest.raises(vaf.PathEscape):
            vaf.safe_entry_name(bad)
    # A hidden name is a policy, not a safety rule, so an embedder can opt in.
    assert vaf.safe_entry_name(".config", allow_hidden=True) == ".config"


def test_one_exception_type_covers_both_helpers(tmp_path):
    """An embedder writes one except clause, and it is a ValueError so a
    caller that only catches the stdlib type still catches it."""
    assert issubclass(vaf.PathEscape, ValueError)
    for call in (lambda: vaf.contained_path(tmp_path, ".."),
                 lambda: vaf.safe_entry_name("a/b")):
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize("spelled", [
    "\\\\host\\share\\x.png", "//host/share/x.png", "\\\\?\\UNC\\host\\share",
    "\\\\.\\pipe\\x", "\\??\\UNC\\host\\share", "\\\\wsl$\\Ubuntu\\home",
])
def test_a_share_or_device_path_is_named_before_anything_touches_it(spelled):
    # Lexical, in either separator convention, whatever the host: the answer is the same here
    # as on Windows, where resolving any of these opens a connection or a device.
    assert vaf.is_remote_path(spelled) is True


@pytest.mark.parametrize("spelled", ["C:\\Users\\a\\x.png", "/home/user/x.png", "relative/x", "", "~/x"])
def test_a_local_path_is_not_remote(spelled):
    assert vaf.is_remote_path(spelled) is False


def test_a_drive_in_a_later_part_is_refused_before_anything_resolves(tmp_path, monkeypatch):
    """Windows joins a later part that names a drive as a drive-relative path and drops what
    came before it, so resolving `<root>/a/Z:/x` would probe drive Z (possibly a mapped share)
    before the containment comparison could refuse it. The refusal is decided as text."""
    import pathlib
    from pathlib import PureWindowsPath

    assert str(PureWindowsPath("C:/proj").joinpath("a", "Z:", "x")) == "Z:x"
    assert str(PureWindowsPath("C:/proj").joinpath("sub", "b:x")) == "b:x"
    resolved = []
    real = pathlib.Path.resolve
    monkeypatch.setattr(pathlib.Path, "resolve",
                        lambda self, *a, **k: resolved.append(str(self)) or real(self, *a, **k))
    for bad in ("a/Z:/x", "sub/b:x", "a\\Z:\\x", "sub\\b:x", "a/Z:"):
        with pytest.raises(vaf.PathEscape, match="must not name a drive"):
            vaf.contained_path(tmp_path, bad)
    assert resolved == [], resolved
    # A colon further into a name is not a drive.
    assert vaf.contained_path(tmp_path, "sub/notes:v2.txt") == tmp_path.resolve() / "sub" / "notes:v2.txt"


def test_a_path_longer_than_any_file_system_opens_is_named_as_text():
    assert vaf.is_overlong_path("a" * 4096) is False
    assert vaf.is_overlong_path("a" * 4097) is True
    # Every character counts, whitespace at the ends included: a caller resolves the text it
    # was given, and a leading pad is one long first component that resolving copies again
    # for every later part.
    assert vaf.is_overlong_path(" " * 10 + "a" * 4087) is True
    assert vaf.is_overlong_path(" " * 10 + "a" * 4086) is False
    assert vaf.is_overlong_path("") is False and vaf.is_overlong_path(None) is False


def test_an_overlong_fragment_or_name_is_one_more_PathEscape(tmp_path):
    with pytest.raises(vaf.PathEscape, match="too long"):
        vaf.contained_path(tmp_path, "a" * 4097)
    with pytest.raises(vaf.PathEscape, match="too long"):
        vaf.safe_entry_name("n" * 256)
    assert vaf.safe_entry_name("n" * 255) == "n" * 255


# ---- vaf.describe_error ----------------------------------------------------------------------

_WITHHELD_SUFFIX = " (details in the server log)"


class _Unprintable(Exception):
    def __str__(self):
        raise RuntimeError("no text")


def _foreign(module, name="UnidentifiedImageError"):
    """An OSError subclass that another library defines, as its module names it."""
    return type(name, (OSError,), {"__module__": module})


class _RaisingName(type):
    """A metaclass whose classes have no readable name."""

    @property
    def __name__(cls):
        raise RuntimeError("no name")


class _NamelessOSError(OSError, metaclass=_RaisingName):
    pass


class _HostileText(str):
    """A ``str`` whose comparisons raise: what a hostile ``__str__`` may hand back."""

    def __eq__(self, other):
        raise RuntimeError("no comparison")

    def __ne__(self, other):
        raise RuntimeError("no comparison")

    __hash__ = str.__hash__


class _HostileStr(Exception):
    def __str__(self):
        return _HostileText("plain words")


def _http_error(status):
    exc = _foreign("requests.exceptions", "HTTPError")(
        "401 Client Error: Unauthorized for url: https://h.example/x?token=t0k3n")
    exc.response = type("Response", (), {"status_code": status})()
    return exc


@pytest.mark.parametrize("exc, expected", [
    # (b) the operating system's words for the errno, whatever path the error quotes
    (OSError(errno.ENAMETOOLONG, "File name too long", "/home/user/x"),
     os.strerror(errno.ENAMETOOLONG)),
    (OSError(errno.ENOSPC, "No space left on device"), os.strerror(errno.ENOSPC)),
    # (b) a strerror the raiser wrote is not trusted: it can carry the path
    (OSError(errno.EACCES, "cannot read /srv/x/key.pem", "/srv/x/key.pem"),
     os.strerror(errno.EACCES)),
    # (c) an errno that is not a number: withheld, strerror and all
    (OSError("x", "cannot read /home/user/x"), "OSError" + _WITHHELD_SUFFIX),
    # (d) VAF's own and the builtin classes keep the text their raiser wrote
    (FileNotFoundError("Task not found"), "Task not found"),
    (_foreign("vaf.x", "StoreGone")("Session not found: ab12cd34"), "Session not found: ab12cd34"),
    # (d) another module's message quotes paths: withheld
    (shutil.Error([("/home/user/a", "/home/user/b", "why")]), "Error" + _WITHHELD_SUFFIX),
    (_foreign("PIL")("cannot identify image file '/home/user/a.png'"),
     "UnidentifiedImageError" + _WITHHELD_SUFFIX),
    # (d) an HTTP error keeps its status code and drops the URL
    (_http_error(401), "HTTPError (HTTP 401)"),
    (_http_error(True), "HTTPError" + _WITHHELD_SUFFIX),
    (_http_error("401"), "HTTPError" + _WITHHELD_SUFFIX),
    # (c) a file name without an errno: withheld, a strerror beside it too
    (OSError(None, None, b"/home/user/x"), "OSError" + _WITHHELD_SUFFIX),
    (OSError(None, "cannot read /home/user/x", "/home/user/x"), "OSError" + _WITHHELD_SUFFIX),
    (OSError(None, "cannot read /home/user/x"), "OSError" + _WITHHELD_SUFFIX),
    # (c) WriteRefused names the path it refused
    (vaf.WriteRefused("refusing to write /home/user/x: it is a link"),
     "WriteRefused" + _WITHHELD_SUFFIX),
    # (a) not an OSError: unchanged
    (ValueError("bad"), "bad"),
    # never raises
    (_Unprintable(), "_Unprintable"),
])
def test_describe_error_gives_the_reason_and_never_the_path(exc, expected):
    answer = vaf.describe_error(exc, log=False)
    assert type(answer) is str and str.__eq__(answer, expected), (answer, expected)


@pytest.mark.parametrize("log", [False, True])
def test_describe_error_never_raises_for_a_hostile_class(caplog, log):
    """A ``__str__`` handing back a ``str`` whose comparison raises, and a class whose name
    cannot be read; with ``log`` the comparison with the text and the log call run as well.
    Built here and not as parameters: a failure would otherwise be reported through their
    own broken repr."""
    caplog.set_level(logging.WARNING)
    answer = vaf.describe_error(_HostileStr(), log=log)
    assert type(answer) is str and str.__eq__(answer, "plain words")
    answer = vaf.describe_error(_NamelessOSError(None, None, "/home/user/x"), log=log)
    assert type(answer) is str and str.__eq__(answer, "Error" + _WITHHELD_SUFFIX)


def test_a_failing_log_handler_does_not_escape(monkeypatch):
    class Refusing(logging.Handler):
        def emit(self, record):
            raise OSError("logging sink offline")

    logger = logging.getLogger("vaf.core.path_jail")
    handler = Refusing()
    logger.addHandler(handler)
    try:
        assert vaf.describe_error(OSError(None, None, "/home/user/x")) == \
            "OSError" + _WITHHELD_SUFFIX
    finally:
        logger.removeHandler(handler)


def test_describe_error_keeps_the_withheld_text_in_the_log(caplog):
    caplog.set_level(logging.WARNING)
    vaf.describe_error(OSError(None, None, "/home/user/x"))
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1 and "/home/user/x" in records[0].getMessage()

    caplog.clear()
    vaf.describe_error(OSError(None, None, "/home/user/x"), log=False)
    vaf.describe_error(ValueError("bad"))
    vaf.describe_error(FileNotFoundError("Task not found"))
    assert caplog.records == []


def test_the_logged_detail_is_capped(caplog):
    """A shutil.Error with thousands of pairs is one WARNING of bounded length, not megabytes."""
    caplog.set_level(logging.WARNING)
    pairs = [(f"/home/user/a{i}", f"/home/user/b{i}", "why") for i in range(5000)]
    vaf.describe_error(shutil.Error(pairs))
    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "/home/user/a0" in record.getMessage() and len(record.getMessage()) < 2200


# Fuzz: every exception shape describe_error can meet, against the invariant it promises.
_OS_CLASSES = sorted({c for c in vars(builtins).values()
                      if isinstance(c, type) and issubclass(c, OSError)}, key=lambda c: c.__name__)
_OS_CLASSES += [shutil.Error, shutil.SameFileError, shutil.SpecialFileError, shutil.ReadError,
                vaf.WriteRefused]
_OS_CLASSES += [_foreign(m, f"Synthetic{i}") for i, m in
                enumerate(("builtins", "vaf.x", "PIL", "requests.exceptions", "thirdparty"))]
_REASONS = ["File name too long", "No space left on device", "Permission denied", "Connection refused",
            "cannot read {root}"]
_ROOT_CHARS = "abcXYZ019 _-.'\"\u00e9\u20ac\u4e2d"


def _draw(rng):
    root = "/" + "".join(rng.choice(_ROOT_CHARS) for _ in range(rng.randint(8, 40)))
    filename = rng.choice([None, root + "/x", (root + "/y").encode("utf-8", "surrogateescape")])
    filename2 = rng.choice([None, None, root + "/z"])
    cls = rng.choice(_OS_CLASSES + [ValueError, RuntimeError, _Unprintable])
    err = rng.choice([None] + sorted(errno.errorcode))
    reason = rng.choice([None] + _REASONS)
    reason = reason.format(root=root) if reason else reason
    message = rng.choice([f"failed on {root}", "plain words", ""])
    try:
        if cls is _Unprintable:
            exc = cls()
        elif not issubclass(cls, OSError):
            exc = cls(message)
        elif err is None and reason is None and filename is None:
            exc = cls(message)
        elif filename2 is not None:
            exc = cls(err, reason, filename, None, filename2)
        else:
            exc = cls(err, reason, filename)
    except TypeError:  # a class with its own constructor (shutil.Error takes one argument)
        exc = cls(message)
    return exc, root


def _check(exc, root):
    answer = vaf.describe_error(exc, log=False)
    assert isinstance(answer, str)
    if isinstance(exc, _Unprintable):
        assert answer == "_Unprintable"
        return
    if not isinstance(exc, OSError):
        assert answer == str(exc)
        return
    module = type(exc).__module__
    guarded = (exc.errno is not None or exc.strerror is not None or exc.filename is not None
               or exc.filename2 is not None
               or isinstance(exc, vaf.WriteRefused)
               or not (module == "builtins" or module.startswith("vaf.")))
    if not guarded:
        assert answer == str(exc)
        return
    assert root not in answer and (exc.filename is None or str(exc.filename) not in answer)
    if type(exc.errno) is int:
        assert answer == os.strerror(exc.errno), (exc, answer)
    else:
        assert answer == type(exc).__name__ + _WITHHELD_SUFFIX, (exc, answer)


def test_describe_error_holds_its_invariant_over_a_seeded_corpus():
    """3000 draws, seed 4450: the class, errno, reason (one of them quoting the root), file
    names and message vary freely; the answer never contains the drawn root and is the
    operating system's words for the errno, the withheld text, or the unchanged text exactly
    where rules (a) and (d) say so."""
    rng = random.Random(4450)
    for _ in range(3000):
        _check(*_draw(rng))
