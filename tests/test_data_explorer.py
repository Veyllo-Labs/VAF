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
    r = get_user_projects_root("ab12cd34-0000-4000-8000-000000000001")
    assert r is not None and r.name == "ab12cd34"
    assert r == Platform.documents_dir() / "VAF_Projects" / "ab12cd34"
    assert get_user_projects_root("") is None
    assert get_user_projects_root(None) is None


def test_label_roundtrip_and_display_precedence():
    d = pathlib.Path(tempfile.mkdtemp())
    # No label -> fallbacks: live title, then folder name (== session_id)
    assert read_workspace_label(d) is None
    assert resolve_workspace_display_name(d, "green123456", None) == "green123456"
    assert resolve_workspace_display_name(d, "green123456", "Chat Title") == "Chat Title"
    # Explicit label wins and round-trips
    assert write_workspace_label(d, "My Export Run") is True
    assert read_workspace_label(d) == "My Export Run"
    assert resolve_workspace_display_name(d, "green123456", "Chat Title") == "My Export Run"


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
    A, B = "ab12cd34", "cafe1234"
    tok = set_librarian_scope({"is_admin": False, "uid8": A,
                               "allowed_roots": [Platform.documents_dir() / "VAF_Projects" / A]})
    try:
        assert is_safe_path(_p(A, "green123456", "doc.txt"))[0] is True    # own uid8: allowed
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


# ── chat-title preservation on delete (orphans keep a human name) ─────────────

def _mgr_with_workspace(tmp_path, monkeypatch, name):
    """A scopeless session + its (unscoped) workspace folder under the pinned home."""
    import vaf.core.session as session_mod
    mgr = session_mod.SessionManager(storage_dir=str(tmp_path / "sessions"))
    monkeypatch.setattr(session_mod, "_manager", mgr)  # get_session_workspace_dir resolves via the global
    s = mgr.new(name=name)
    mgr.save(s, sync_state=False)
    ws = Platform.documents_dir() / "VAF_Projects" / s.id
    ws.mkdir(parents=True)
    return mgr, s, ws


def test_delete_preserves_chat_title_on_surviving_workspace(hermetic_home, tmp_path, monkeypatch):
    """Deleting a chat whose workspace holds real content must save the chat's
    title as the workspace label - the orphaned folder keeps its human name in
    the Data Explorer instead of showing the raw session-id folder name."""
    mgr, s, ws = _mgr_with_workspace(tmp_path, monkeypatch, "Wetter-Recherche Berlin")
    (ws / "report.html").write_text("<html></html>", encoding="utf-8")
    assert mgr.delete(s.id) is True
    assert ws.is_dir()  # content -> folder survives chat deletion
    assert read_workspace_label(ws) == "Wetter-Recherche Berlin"
    assert resolve_workspace_display_name(ws, s.id, None) == "Wetter-Recherche Berlin"


def test_delete_never_overwrites_a_user_set_label(hermetic_home, tmp_path, monkeypatch):
    mgr, s, ws = _mgr_with_workspace(tmp_path, monkeypatch, "Auto Title")
    (ws / "data.csv").write_text("a,b\n", encoding="utf-8")
    assert write_workspace_label(ws, "My Project") is True  # explicit rename wins
    mgr.delete(s.id)
    assert read_workspace_label(ws) == "My Project"


def test_delete_removes_empty_workspace_and_writes_no_label(hermetic_home, tmp_path, monkeypatch):
    mgr, s, ws = _mgr_with_workspace(tmp_path, monkeypatch, "Empty Chat")
    mgr.delete(s.id)
    assert not ws.exists()  # empty -> cleaned up entirely, nothing left to label


# ── workspace search (names + text-file contents, bounded) ────────────────────

def _search_ws(tmp_path, q, **kw):
    from vaf.core.web_server import _search_one_workspace
    return _search_one_workspace(tmp_path, q, **kw)


def test_search_matches_file_and_folder_names(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "wetter_berlin.html").write_text("<html></html>", encoding="utf-8")
    r = _search_ws(tmp_path, "wetter")
    assert r and any(f["path"] == "reports/wetter_berlin.html" and f["kind"] == "name" for f in r["files"])
    r2 = _search_ws(tmp_path, "REPORT")  # case-insensitive, folder name
    assert r2 and any(f["path"] == "reports" and f["kind"] == "name" for f in r2["files"])


def test_search_matches_text_content_with_snippet(tmp_path):
    (tmp_path / "notes.md").write_text("line one\nthe Quarterly Revenue was strong\nline three", encoding="utf-8")
    r = _search_ws(tmp_path, "quarterly revenue")
    assert r and r["files"][0]["kind"] == "content"
    assert "Quarterly Revenue" in r["files"][0]["snippet"]
    assert "\n" not in r["files"][0]["snippet"]  # snippet is flattened to one line


def test_search_skips_binary_content_but_not_binary_names(tmp_path):
    (tmp_path / "photo_berlin.png").write_bytes(b"\x89PNG\x00\x00berlin-inside")
    r = _search_ws(tmp_path, "berlin")
    assert r and r["files"] == [{"path": "photo_berlin.png", "kind": "name"}]
    assert _search_ws(tmp_path, "inside") is None  # NUL sniff: binary content never matches


def test_search_no_match_returns_none_and_skips_dotfiles(tmp_path):
    (tmp_path / ".vaf_workspace.json").write_text('{"label": "secretlabel"}', encoding="utf-8")
    assert _search_ws(tmp_path, "secretlabel") is None
    assert _search_ws(tmp_path, "nothing-here") is None


def test_search_hit_cap_sets_truncated(tmp_path):
    for i in range(8):
        (tmp_path / f"berlin_{i}.txt").write_text("x", encoding="utf-8")
    r = _search_ws(tmp_path, "berlin", per_ws_hits=5)
    assert r and len(r["files"]) == 5 and r["truncated"] is True


def test_search_file_scan_cap(tmp_path):
    for i in range(20):
        (tmp_path / f"f{i:02d}.txt").write_text("needle", encoding="utf-8")
    r = _search_ws(tmp_path, "needle", max_files=10, per_ws_hits=50)
    assert r and r["truncated"] is True and len(r["files"]) <= 10


# ── search hardening (post-review): symlink containment, bounds, budget ───────

def test_search_never_reads_content_through_a_symlink_escaping_the_workspace(tmp_path):
    """User-isolation critical: a symlink planted in the workspace that points at a
    file OUTSIDE it (another user's VAF_Projects file, or any host file) must never
    have its content read/leaked. It may still match by NAME (its name is genuinely
    in this workspace); only the content read is blocked."""
    import os
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("TOPSECRET cross-user data", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    os.symlink(str(secret), str(ws / "link_secret.txt"))  # symlink escaping the workspace
    # Content of the target must NOT leak:
    assert _search_ws(ws, "TOPSECRET") is None
    assert _search_ws(ws, "cross-user") is None
    # The symlink's own NAME still matches (no content, no leak):
    r = _search_ws(ws, "link_secret")
    assert r and r["files"] == [{"path": "link_secret.txt", "kind": "name"}]


def test_search_reads_content_through_a_symlink_that_stays_inside(tmp_path):
    """A symlink pointing at a file INSIDE the same workspace is legitimate content."""
    import os
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "real.txt").write_text("quarterly revenue up", encoding="utf-8")
    os.symlink(str(ws / "real.txt"), str(ws / "alias.txt"))  # stays inside root
    r = _search_ws(ws, "quarterly revenue")
    paths = {f["path"] for f in r["files"]}
    assert "alias.txt" in paths or "real.txt" in paths  # at least one content hit inside root


def test_search_dirname_match_at_the_depth_boundary_is_not_lost(tmp_path):
    """#6a: the directory-name match must run BEFORE the depth cutoff prunes the
    level, so a folder sitting exactly on the boundary is still found."""
    d = tmp_path / "a" / "b" / "quarterly"
    d.mkdir(parents=True)
    r = _search_ws(tmp_path, "quarterly", max_depth=2)
    assert r and any(f["path"].endswith("quarterly") and f["kind"] == "name" for f in r["files"])


def test_search_wide_empty_tree_is_bounded_not_hung(tmp_path):
    """#2: breadth is bounded by an entry cap, not just the file cap - a wide tree of
    empty non-matching dirs returns promptly (truncated), never a full enumeration."""
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(5000):
        (ws / f"dir_{i:05d}").mkdir()  # 5000 empty dirs, none matching, zero files
    # max_files small so the file cap alone would never trip; the entry cap must.
    r = _search_ws(ws, "needle", max_files=10)
    assert r is None or r.get("truncated") is True  # bounded, no hit, no hang


def test_search_budget_exhausted_stops_immediately(tmp_path):
    """#3: an exhausted whole-request budget (files/entries/deadline) halts the walk."""
    from vaf.core.web_server import _SearchBudget
    import time as _t
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "match.txt").write_text("needle here", encoding="utf-8")
    # Deadline already in the past -> exhausted before the first entry is considered.
    spent = _SearchBudget(_t.monotonic() - 1.0, 1000, 1000)
    assert spent.exhausted() is True
    assert _search_ws(ws, "needle", budget=spent) is None
    # A fresh budget still finds it.
    ok = _SearchBudget(_t.monotonic() + 5.0, 1000, 1000)
    assert _search_ws(ws, "needle", budget=ok) is not None
