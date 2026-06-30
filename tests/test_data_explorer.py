# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Central per-user Data Explorer: workspace-label helpers + the librarian per-user jail.

Pins the security-critical pieces: the per-user root derivation, the display-label store (which survives
chat deletion so orphans stay renamable), and the librarian filesystem jail — a remote user can read only
their own VAF_Projects/<uid8>, never another user's; the local admin keeps full access; the jail is a
no-op (and resets cleanly) for every other caller.
"""
import pathlib
import tempfile

import pytest

from vaf.core.platform import Platform
from vaf.core.session import (
    get_user_projects_root,
    read_workspace_label,
    write_workspace_label,
    resolve_workspace_display_name,
)
from vaf.tools.filesystem import is_safe_path, set_librarian_scope, reset_librarian_scope


@pytest.fixture
def hermetic_home(tmp_path, monkeypatch):
    """Pin Path.home() (and therefore Platform.documents_dir()) to a clean temp dir.

    The jail tests assert that ordinary user folders (Downloads, VAF_Projects/<uid8>) are
    allowed/denied. is_safe_path() blocks OS locations by name (e.g. "Windows", "System32"),
    so the result depends on WHERE the test process's home directory lives. On a clean CI
    runner that home can be the service/system profile (e.g.
    C:\\Windows\\system32\\config\\systemprofile), whose path itself contains those blocked
    names — making every home-based path wrongly come back denied. Pinning home to a neutral
    temp directory makes these tests deterministic on every platform without changing the
    production blocking behavior.
    """
    fake_home = tmp_path / "vaf_home"
    fake_home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: fake_home))
    return fake_home


# ── workspace label helpers ───────────────────────────────────────────────────

def test_get_user_projects_root_uid8():
    r = get_user_projects_root("a74e6e21-3516-4305-85a0-ecaef07111e8")
    assert r is not None and r.name == "a74e6e21"
    assert r == Platform.documents_dir() / "VAF_Projects" / "a74e6e21"
    assert get_user_projects_root("") is None
    assert get_user_projects_root(None) is None


def test_label_roundtrip_and_display_precedence():
    d = pathlib.Path(tempfile.mkdtemp())
    # No label -> fallbacks: live title, then folder name (== session_id)
    assert read_workspace_label(d) is None
    assert resolve_workspace_display_name(d, "orange641045", None) == "orange641045"
    assert resolve_workspace_display_name(d, "orange641045", "Chat Title") == "Chat Title"
    # Explicit label wins and round-trips
    assert write_workspace_label(d, "My Export Run") is True
    assert read_workspace_label(d) == "My Export Run"
    assert resolve_workspace_display_name(d, "orange641045", "Chat Title") == "My Export Run"


def test_label_corrupt_json_falls_back(tmp_path):
    (tmp_path / ".vaf_workspace.json").write_text("{not valid json")
    assert read_workspace_label(tmp_path) is None  # never raises
    assert resolve_workspace_display_name(tmp_path, "blue123", None) == "blue123"


def test_write_label_rejects_nonexistent_folder():
    assert write_workspace_label(pathlib.Path("/no/such/dir/xyz"), "x") is False


# ── librarian per-user jail (the cross-user isolation invariant) ───────────────

def _p(*parts):
    return str(Platform.documents_dir().joinpath("VAF_Projects", *parts))


def test_jail_noop_when_unset(hermetic_home):
    # No librarian scope set -> normal behavior (a Downloads path is allowed).
    assert is_safe_path(str(pathlib.Path.home() / "Downloads" / "x.txt"))[0] is True


def test_jail_remote_user_is_isolated(hermetic_home):
    A, B = "a74e6e21", "cafe1234"
    tok = set_librarian_scope({"is_admin": False, "uid8": A,
                               "allowed_roots": [Platform.documents_dir() / "VAF_Projects" / A]})
    try:
        assert is_safe_path(_p(A, "orange641045", "doc.txt"))[0] is True    # own uid8: allowed
        assert is_safe_path(_p(B, "secret.txt"))[0] is False                # other uid8: DENIED
        assert is_safe_path(str(pathlib.Path.home() / "Downloads"))[0] is False  # no personal folders
    finally:
        reset_librarian_scope(tok)
    # Clean reset -> back to normal
    assert is_safe_path(str(pathlib.Path.home() / "Downloads"))[0] is True


def test_jail_local_admin_has_full_access(hermetic_home):
    tok = set_librarian_scope({"is_admin": True, "uid8": None, "allowed_roots": []})
    try:
        assert is_safe_path(str(pathlib.Path.home() / "Downloads"))[0] is True
        assert is_safe_path(_p("cafe1234", "x.txt"))[0] is True  # admin may read any uid8
    finally:
        reset_librarian_scope(tok)


def test_jail_never_overrides_repo_block():
    # The VAF program root stays blocked even for the local admin.
    import vaf.tools.filesystem as _fs
    repo_file = str(pathlib.Path(_fs.__file__).resolve().parents[2] / "vaf" / "core" / "config.py")
    tok = set_librarian_scope({"is_admin": True, "uid8": None, "allowed_roots": []})
    try:
        assert is_safe_path(repo_file)[0] is False
    finally:
        reset_librarian_scope(tok)
