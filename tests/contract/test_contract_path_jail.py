# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: vaf.contained_path, vaf.safe_entry_name, vaf.PathEscape.

docs/EMBEDDING.md promises an embedder that a tool taking a path from a model,
a request body or a peer can decide containment with a supported primitive
instead of hand-rolling it. Pinned here: the containment holds against a
SYMLINK inside the root (the case a prefix comparison cannot see, and the one
that made this a primitive), a path that does not exist yet still gets an
answer, and the refusal is one exception type an embedder can catch.
"""
import os

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
