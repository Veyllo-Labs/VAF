# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A path from outside costs time linear in its length, never its square.

`contained_path` decided containment with ``root in target.parents``, which builds every
parent and compares each one: 1.3 s for a 32 KB fragment, growing four-fold per doubling, on
the event loop of the workspace routes that ask it inline. ``Path.resolve()`` has the same
shape with a smaller constant, so a linear comparison alone does not bound a request body of
megabytes; only refusing the text before anything resolves does (`is_overlong_path`). And a
path the OS cannot stat escaped `must_exist` as a raw OSError, which the routes answered 500.

Pinned here:
- an overlong fragment, name or path is refused before any Path exists or anything resolves,
  in `contained_path`, `safe_entry_name`, `jail_allows`, `is_safe_path` and the web file routes;
- below the cap `contained_path` answers exactly what it answered before (a seeded fuzz over a
  real tree with links, against the previous implementation copied verbatim);
- `_within` answers exactly ``t == r or r in t.parents`` for both path flavours on every host
  (the Windows semantics: case folding, drive letters, UNC anchors), and grows linearly;
- `must_exist` answers "folder not found" for a path the OS cannot stat.

The fuzz seed is fixed and printed on failure; ``VAF_FUZZ_DRAWS`` raises the draw count for a
run outside the suite. Inputs in assertion messages go through ``ascii()``: a cp1252 console
cannot print every fuzz word.
"""
import asyncio
import os
import random
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from vaf.core import path_jail
from vaf.core.path_jail import (NAME_CHARS_MAX, PATH_CHARS_MAX, PathEscape, _within,
                                contained_path, is_overlong_path, is_rooted, safe_entry_name)

SEED = 20261120


def _draws(default: int) -> int:
    return int(os.environ.get("VAF_FUZZ_DRAWS", str(default)))


def _reference_contained_path(root, relative: str = "", *, must_exist: bool = False) -> Path:
    """`contained_path` as it was before the length cap and the linear comparison, verbatim."""
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
    # A drive in a LATER part (`a/Z:/x`, `sub/b:x`): on Windows joining it replaces everything
    # before it with a drive-relative path (`Z:x`), and resolving that probes the drive, which
    # may be mapped to a share. Refused as text, in every part and on every host: a POSIX name
    # whose second character is a colon is refused with it.
    if any(len(p) >= 2 and p[1] == ":" for p in parts):
        raise PathEscape("path must not name a drive")

    root_resolved = Path(root).resolve()
    target = root_resolved.joinpath(*parts).resolve() if parts else root_resolved
    if target != root_resolved and root_resolved not in target.parents:
        raise PathEscape("path must stay inside the root")
    if must_exist and not target.is_dir():
        raise PathEscape("folder not found")
    return target


def _outcome(call):
    try:
        return ("path", str(call()))
    except PathEscape as e:
        return ("refused", str(e))
    except Exception as e:  # noqa: BLE001 - the type is the outcome being compared
        return ("raised", type(e).__name__)


class _NoPath:
    """Stands in for pathlib.Path inside path_jail: constructing one is the failure."""

    def __init__(self, *a, **k):
        raise AssertionError("a Path was constructed for an overlong fragment")


# ── 1. an overlong fragment never becomes a Path ─────────────────────────────

def test_an_overlong_fragment_is_refused_before_anything_resolves(tmp_path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(path_jail, "Path", _NoPath)
        with pytest.raises(PathEscape, match="too long"):
            contained_path(tmp_path, "a/" * 5_000_000)
        with pytest.raises(PathEscape, match="too long"):
            contained_path(tmp_path, "a" * (PATH_CHARS_MAX + 1))
        # Too long wins over every other reason, the cheapest refusal first.
        with pytest.raises(PathEscape, match="too long"):
            contained_path(tmp_path, "/" + "a" * PATH_CHARS_MAX)
    # At the cap and below the answer is the previous one, whitespace at the ends not counted.
    for fragment in ("a/" * 2047 + "a", "  " + "a/" * 2047 + "a" + "\n", "b" * PATH_CHARS_MAX):
        assert len(fragment.strip()) <= PATH_CHARS_MAX
        got = _outcome(lambda: contained_path(tmp_path, fragment))
        want = _outcome(lambda: _reference_contained_path(tmp_path, fragment))
        assert got == want, (len(fragment), got, want)
    assert PATH_CHARS_MAX == 4096


def _fuzz_tree(base: Path):
    """A workspace with links that point in, out, at themselves, nowhere, and at a sibling
    whose name shares the root's string prefix; and the same root reached through a linked
    parent, the macOS /var -> /private/var case."""
    real = base / "real"
    ws = real / "ws"
    (ws / "sub" / "deep").mkdir(parents=True)
    (real / "ws2").mkdir()
    (real / "outside").mkdir()
    (ws / "a").mkdir()
    (ws / "file.txt").write_text("x", encoding="utf-8")
    roots = [ws]
    links = [(ws / "out", real / "outside"), (ws / "in", ws / "sub"), (ws / "loop", ws / "loop"),
             (ws / "dang", real / "nowhere"), (ws / "sib", Path("..") / "ws2")]
    for link, to in links:
        try:
            os.symlink(to, link)
        except (OSError, NotImplementedError):
            pass  # a host without link rights: the name is then a plain missing entry
    try:
        os.symlink(real, base / "linkroot")
        roots.append(base / "linkroot" / "ws")
    except (OSError, NotImplementedError):
        pass
    return roots


_WORDS = ["sub", "deep", "out", "in", "loop", "dang", "sib", ".", "..", "", " ", "a", "A", "C:",
          "c:x", "\x00", "~", ".hidden", "x.", "\\", "/", "//", "ws", "ws2", "\u00e9", "\u03a3",
          "file.txt"]


def test_contained_path_equals_the_head_implementation(tmp_path):
    rng = random.Random(SEED)
    roots = _fuzz_tree(tmp_path)
    mismatches = []
    draws = _draws(1000)
    for root in roots:
        for _ in range(draws):
            words = [rng.choice(_WORDS) for _ in range(rng.randint(0, 6))]
            fragment = rng.choice(["/", "\\", ""]).join(words)
            if rng.random() < 0.1:
                fragment = rng.choice([" ", "\t", "\n "]) + fragment
            must_exist = rng.random() < 0.3
            got = _outcome(lambda: contained_path(root, fragment, must_exist=must_exist))
            want = _outcome(lambda: _reference_contained_path(root, fragment,
                                                              must_exist=must_exist))
            if got != want:
                mismatches.append((ascii(str(root)), ascii(fragment), must_exist, got, want))
    assert not mismatches, (f"seed {SEED}, {len(mismatches)} mismatches, first: "
                            f"{ascii(mismatches[:5])}")


# ── 2. the comparison: pathlib's answer, in linear time, both flavours ─────

# "." and "C:" are the relative and the drive-relative case: a root with no parts against an
# anchored target is where an unbounded parent index would raise.
_POSIX_ANCHORS = ["/", "."]
_WINDOWS_ANCHORS = ["C:\\", "c:\\", "D:\\", "\\\\srv\\share\\", "\\\\SRV\\Share\\", "\\\\?\\C:\\",
                    ".", "C:"]
_PURE_WORDS = ["a", "A", "b", "\u03a3", "\u03c3", "\u03c2", "\u0130", "i", "\u00df", "\u1e9e", "K",
               "\u212a", ".x", "x.", " ", "\u00e9", "e\u0301", "C:", "c", "~1", ":"]


def _flip(rng, text):
    return "".join(ch.swapcase() if rng.random() < 0.5 else ch for ch in text)


def _pure_pair(rng, cls, anchors):
    root = cls(rng.choice(anchors), *[rng.choice(_PURE_WORDS) for _ in range(rng.randint(0, 3))])
    roll = rng.random()
    if roll < 0.4:
        # A descendant (or the root itself), every part possibly in another case.
        head = [_flip(rng, p) for p in root.parts]
        target = cls(*head, *[rng.choice(_PURE_WORDS) for _ in range(rng.randint(0, 3))])
    elif roll < 0.7:
        # A sibling that shares the root's string prefix: /a/b against /a/bc/x.
        parts = list(root.parts)
        if len(parts) > 1:
            parts[-1] = parts[-1] + rng.choice(_PURE_WORDS + ["x"])
        else:
            parts = [rng.choice(anchors)]
        target = cls(*parts, *[rng.choice(_PURE_WORDS) for _ in range(rng.randint(0, 2))])
    else:
        target = cls(rng.choice(anchors),
                     *[rng.choice(_PURE_WORDS) for _ in range(rng.randint(0, 5))])
    return target, root


@pytest.mark.parametrize("cls,anchors", [(PurePosixPath, _POSIX_ANCHORS),
                                         (PureWindowsPath, _WINDOWS_ANCHORS)],
                         ids=["posix", "windows"])
def test_within_matches_parents_membership_in_both_flavours(cls, anchors):
    rng = random.Random(SEED)
    mismatches = []
    for _ in range(_draws(10000)):
        target, root = _pure_pair(rng, cls, anchors)
        want = target == root or root in target.parents
        if _within(target, root) != want:
            mismatches.append((ascii(str(target)), ascii(str(root)), want))
    # The cases a string prefix test gets wrong are in the corpus, named so they cannot drop out.
    named = [(cls(anchors[0], "a", "b"), cls(anchors[0], "a"), True),
             (cls(anchors[0], "ab"), cls(anchors[0], "a"), False),
             (cls(anchors[0], "a"), cls(anchors[0]), True),
             (cls(anchors[0]), cls("."), False),
             (cls(anchors[0], "a", "b"), cls("."), False),
             (cls("a", "b"), cls("."), True)]
    if cls is PureWindowsPath:
        named += [(cls("c:\\users\\x"), cls("C:\\Users"), True),
                  (cls("\\\\srv\\share\\x"), cls("\\\\SRV\\Share\\"), True),
                  (cls("D:\\Users\\x"), cls("C:\\Users"), False)]
    for target, root, want in named:
        assert (target == root or root in target.parents) is want, (str(target), str(root))
        if _within(target, root) is not want:
            mismatches.append((ascii(str(target)), ascii(str(root)), want))
    assert not mismatches, f"seed {SEED}: {len(mismatches)} mismatches, first: {mismatches[:5]}"


def _min_time(make, check, repeat=5):
    best = float("inf")
    for _ in range(repeat):
        target, root = make()
        start = time.perf_counter()
        check(target, root)
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.parametrize("cls,anchor", [(PurePosixPath, "/"), (PureWindowsPath, "C:\\")],
                         ids=["posix", "windows"])
def test_within_grows_linearly(cls, anchor):
    """A growth ratio, not a time: a slow runner slows both sizes alike. Sixteen times the
    parts measured 15.3x (POSIX) and 15.6x (Windows flavour) with `_within`, against 156x and
    152x with ``root in target.parents``."""
    def make(n):
        return lambda: (cls(anchor, "r", *(["a"] * n)), cls(anchor, "r"))

    small = _min_time(make(1024), _within)
    large = _min_time(make(16384), _within)
    ratio = large / max(small, 1e-9)
    assert ratio < 64, f"16x the parts cost {ratio:.1f}x the time"


# ── 3. must_exist on a path the OS cannot stat ─────────────────────────────

def test_must_exist_answers_not_found_for_a_path_the_os_cannot_stat(tmp_path):
    # 4095 characters: under the cap, and past Linux's PATH_MAX once joined to any root (a
    # shorter fragment under a short root stays below it, and stat then answers ENOENT).
    fragment = "a/" * 2047 + "a"
    assert not is_overlong_path(fragment)
    assert len(str(tmp_path.resolve())) + 1 + len(fragment) > PATH_CHARS_MAX
    with pytest.raises(PathEscape, match="^folder not found$"):
        contained_path(tmp_path, fragment, must_exist=True)
    import vaf.core.web_server as ws
    with pytest.raises(HTTPException) as e:
        ws._resolve_workspace_subdir(str(tmp_path), fragment)
    assert e.value.status_code == 404


# ── 4. names ───────────────────────────────────────────────────────────────

def test_an_overlong_name_is_refused_before_the_character_scan(monkeypatch):
    assert safe_entry_name("n" * NAME_CHARS_MAX) == "n" * NAME_CHARS_MAX
    assert safe_entry_name(" " + "n" * NAME_CHARS_MAX + " ") == "n" * NAME_CHARS_MAX
    with pytest.raises(PathEscape, match="too long"):
        safe_entry_name("n" * (NAME_CHARS_MAX + 1))

    class _NoScan:
        @staticmethod
        def category(ch):
            raise AssertionError("the character scan ran over an overlong name")

    monkeypatch.setattr(path_jail, "unicodedata", _NoScan)
    with pytest.raises(PathEscape, match="too long"):
        safe_entry_name("n" * 10_000_000)
    assert NAME_CHARS_MAX == 255


def test_the_length_boundaries_hold_for_any_padding():
    rng = random.Random(SEED)
    pads = ["", " ", "\t", "\n", "\r\n", "  \u3000", "\x0b"]
    bad = []
    block = "".join(rng.choice("ab/\\:. \t") for _ in range(2 * PATH_CHARS_MAX))
    for _ in range(_draws(2000)):
        n = rng.randint(PATH_CHARS_MAX - 6, PATH_CHARS_MAX + 4)
        at = rng.randint(0, PATH_CHARS_MAX)
        # Whitespace inside the text counts; only the ends' does not.
        text = rng.choice(pads) + "z" + block[at:at + n - 2] + "z" + rng.choice(pads)
        if is_overlong_path(text) is not (len(text) > PATH_CHARS_MAX):
            bad.append(("path", n, ascii(text[:8])))
        m = rng.randint(NAME_CHARS_MAX - 5, NAME_CHARS_MAX + 5)
        name = rng.choice(pads) + "n" * m + rng.choice(pads)
        try:
            ok = safe_entry_name(name) == "n" * m
        except PathEscape as e:
            ok = m > NAME_CHARS_MAX and "too long" in str(e)
        if not ok:
            bad.append(("name", m, ascii(name[:8])))
    assert not bad, f"seed {SEED}: {bad[:5]}"


# ── 6. jail_allows ─────────────────────────────────────────────────────────

def test_jail_allows_refuses_an_overlong_path_for_every_caller(tmp_path, monkeypatch):
    from vaf.tools import filesystem
    asked = []

    def _admits_everyone(*a, **k):
        asked.append(a)
        return {"is_admin": True}

    monkeypatch.setattr(filesystem, "compute_user_jail", _admits_everyone)
    long_path = str(tmp_path) + "/" + "a/" * 3000
    for scope, role in (("", None), ("ab12cd34-0000-0000-0000-000000000000", "admin"),
                        ("ab12cd34-0000-0000-0000-000000000001", "user")):
        assert filesystem.jail_allows(long_path, user_scope_id=scope, user_role=role) is False, \
            (scope, role)
    assert asked == [], "the jail was computed for a path that is refused as text"
    # A path of ordinary length still gets the jail's answer.
    assert filesystem.jail_allows(str(tmp_path / "x"), user_scope_id="", user_role=None) is True


# ── 7. the web file routes ─────────────────────────────────────────────────

def test_allowed_file_path_refuses_overlong_before_it_resolves(monkeypatch):
    import vaf.core.web_server as ws
    from vaf.core.platform import Platform

    def _boom(*a, **k):
        raise AssertionError("an overlong path reached a later step")

    monkeypatch.setattr(Platform, "normalize_path", staticmethod(_boom))
    monkeypatch.setattr(path_jail, "is_remote_path", _boom)
    request = SimpleNamespace(state=SimpleNamespace(user={}), headers={}, client=None)
    with pytest.raises(HTTPException) as e:
        ws._allowed_file_path("/home/user/Documents/" + "a/" * 5_000_000, request)
    assert e.value.status_code == 400 and e.value.detail == "Invalid path"


def test_the_length_screen_comes_first():
    import inspect

    import vaf.core.web_server as ws
    from vaf.tools import filesystem
    for fn in (ws._allowed_file_path, filesystem.jail_allows, filesystem.is_safe_path):
        src = inspect.getsource(fn)
        assert src.index("is_overlong_path(") < src.index("is_remote_path("), fn.__name__


# ── 8. the file tools' screen ──────────────────────────────────────────────

def test_is_safe_path_refuses_overlong_before_the_alias(monkeypatch):
    from vaf.tools import filesystem

    def _boom(*a, **k):
        raise AssertionError("the alias was resolved for an overlong path")

    monkeypatch.setattr(filesystem, "_resolve_folder_alias", _boom)
    assert filesystem.is_safe_path("DOCUMENTS/" + "a/" * 3000) == (
        False, filesystem.PATH_TOO_LONG_REFUSAL)
    assert str(PATH_CHARS_MAX) in filesystem.PATH_TOO_LONG_REFUSAL
    assert filesystem.PATH_TOO_LONG_REFUSAL.isascii()


# ── a pad at the ends counts: the screens resolve the text they were given ─

_PAD = " " * 1_000_000


@pytest.mark.parametrize("where", ["leading", "trailing"])
def test_a_padded_path_is_refused_by_every_screen_before_it_resolves(where, monkeypatch):
    """A leading pad in front of a path under the cap is one long first component, and
    realpath copies it again for every later part: measured 14 s on a save route for a 10 MB
    pad while the screen measured the stripped text. Every screen that resolves the text it
    was given must count the pad."""
    import vaf.core.web_server as ws
    from vaf.core.platform import Platform
    from vaf.tools import filesystem

    def _boom(*a, **k):
        raise AssertionError("a padded path reached a step that resolves it")

    path = "/home/user/Documents/" + "a/" * 1000 + "x.md"
    padded = _PAD + path if where == "leading" else path + _PAD
    assert not is_overlong_path(path)

    request = SimpleNamespace(state=SimpleNamespace(user={}), headers={}, client=None)
    with monkeypatch.context() as m:
        m.setattr(Platform, "normalize_path", staticmethod(_boom))
        m.setattr(path_jail, "is_remote_path", _boom)
        with pytest.raises(HTTPException) as e:
            ws._allowed_file_path(padded, request)
    assert (e.value.status_code, e.value.detail) == (400, "Invalid path")

    # The real is_remote_path from here on: jail_allows answers False to any error, so a
    # raising stand-in would hide a missing length screen. For the same reason the jail
    # stand-in admits everyone and records that it was asked.
    asked = []
    monkeypatch.setattr(filesystem, "compute_user_jail",
                        lambda *a, **k: asked.append(a) or {"is_admin": True})
    for scope, role in (("", None), ("ab12cd34-0000-0000-0000-000000000001", "user")):
        assert filesystem.jail_allows(padded, user_scope_id=scope, user_role=role) is False
    assert asked == []

    monkeypatch.setattr(filesystem, "_resolve_folder_alias", _boom)
    assert filesystem.is_safe_path(padded) == (False, filesystem.PATH_TOO_LONG_REFUSAL)


# ── a resolve the OS refuses (Windows past MAX_PATH) ───────────────────────

def _resolve_refused_past(monkeypatch, allowed):
    """``Path.resolve`` as Windows answers a path past MAX_PATH without long-path support:
    ntpath.realpath re-raises winerror 206 even when it is not strict, because 206 is not in
    its allow list. Paths in ``allowed`` resolve as usual. A seam, so the Windows class is
    caught on every host."""
    import errno
    import pathlib

    real = pathlib.Path.resolve

    def resolve(self, *a, **k):
        if self in allowed:
            return real(self, *a, **k)
        # The fourth argument is winerror on Windows and ignored elsewhere.
        raise OSError(errno.ENAMETOOLONG, "The filename or extension is too long", str(self),
                      206)

    monkeypatch.setattr(pathlib.Path, "resolve", resolve)


def test_a_resolve_the_os_refuses_is_a_refusal_not_an_oserror(tmp_path, monkeypatch):
    import vaf.core.web_server as ws
    root = tmp_path.resolve()
    _resolve_refused_past(monkeypatch, {tmp_path, root})

    with pytest.raises(PathEscape, match="^folder not found$"):
        contained_path(root, "a/b", must_exist=True)
    with pytest.raises(PathEscape, match="^path cannot be resolved$"):
        contained_path(root, "a/b")
    with pytest.raises(HTTPException) as e:
        ws._resolve_workspace_subdir(str(root), "a/b")
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        ws._a2a_workspace_member_path(root, "a/b")
    assert e.value.status_code == 400 and "cannot be resolved" in str(e.value.detail)

    # The root itself refused (a workspace folder past MAX_PATH): the same two refusals.
    _resolve_refused_past(monkeypatch, set())
    with pytest.raises(PathEscape, match="^folder not found$"):
        contained_path(root, "", must_exist=True)
    with pytest.raises(PathEscape, match="^path cannot be resolved$"):
        contained_path(root, "a")


# ── 9. the routes an account reaches ───────────────────────────────────────

def _delete_route(monkeypatch, tmp_path):
    import vaf.core.web_server as ws

    async def _root(*a, **k):
        return str(tmp_path)

    monkeypatch.setattr(ws, "_workspace_root", _root)
    return ws


def test_workspace_delete_answers_an_overlong_subpath_at_once(tmp_path, monkeypatch):
    ws = _delete_route(monkeypatch, tmp_path)
    request = SimpleNamespace(client=None, headers={})
    loop = asyncio.new_event_loop()
    try:
        def answer(subpath):
            req = ws.WorkspaceDeleteRequest(sessionId="s", name="x", subpath=subpath)
            coro = ws.delete_session_workspace_entry(req, request)
            start = time.perf_counter()
            with pytest.raises(HTTPException) as e:
                loop.run_until_complete(coro)
            return time.perf_counter() - start, e.value

        with monkeypatch.context() as m:
            m.setattr(path_jail, "Path", _NoPath)
            _, err = answer("a/" * 5_000_000)
            assert (err.status_code, err.detail) == (400, "Invalid subpath")

        times = {}
        for size in (16 * 1024, 64 * 1024):
            best = float("inf")
            for _ in range(5):
                took, err = answer("a/" * (size // 2))
                assert (err.status_code, err.detail) == (400, "Invalid subpath"), size
                best = min(best, took)
            times[size] = best
        ratio = times[64 * 1024] / max(times[16 * 1024], 1e-9)
        assert ratio < 4, f"four times the subpath cost {ratio:.1f}x the time: {times}"
    finally:
        loop.close()
    assert sorted(os.listdir(tmp_path)) == []


def test_a2a_member_path_tells_the_peer_the_path_is_too_long(tmp_path):
    import vaf.core.web_server as ws
    with pytest.raises(HTTPException) as e:
        ws._a2a_workspace_member_path(tmp_path, "a/" * 3000)
    assert e.value.status_code == 400 and "too long" in str(e.value.detail)
