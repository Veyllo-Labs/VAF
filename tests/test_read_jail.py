# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: reading is confined per user too, and reading reaches further than writing.

``write_file`` and ``edit_file`` were jailed while ``read_file``, ``list_files``, ``tree``,
``find_files`` and ``folder_size`` were not. A tenant who could not write outside their own
``VAF_Projects/<uid8>`` could still READ any path the static checks allow - another tenant's
tree, or the machine owner's home. Reading is the half that leaks; writing only the half that
damages.

The root set is not simply the write set, and that was established by measuring rather than
guessing:

- **Attachments need nothing extra.** ``get_session_attachments_dir`` puts uploads in
  ``VAF_Projects/<uid8>/<session>/attachments/``, already inside the caller's own root.
- **Skills do.** ``use_skill`` hands the model absolute paths to a skill's bundled files and
  tells it to open them with ``read_file``, so a read jail limited to the own tree would break
  every skill shipping reference material.
- **Cloud files do not.** ``cloud_storage`` is its own tool with its own actions; it never
  hands the model an absolute path under ``cloud_sync``.

Skills are also where the measuring found a real hole. Visibility is per user
(``shared_with`` in the manifest) but every skill FOLDER sits in one directory, so "allow
skills" would have meant "allow everyone's skills" - including one kept private to another
user. The read jail therefore allows only the folders of skills VISIBLE to the caller, and
those roots stay out of the WRITE jail: seeing a shared skill is not permission to rewrite it
(that authority is ``can_user_edit_skill``, a different question with a different answer).
"""
import re
from pathlib import Path

import pytest

import vaf.core.agent as agent_mod
from vaf.tools.filesystem import (
    FinderTool,
    FolderSizeTool,
    ListFilesTool,
    ReadFileTool,
    TreeTool,
    WriteFileTool,
    compute_user_jail,
)

AGENT_SRC = Path(agent_mod.__file__).read_text(encoding="utf-8")

# Synthetic scopes (public-repo hygiene: never a real scope UUID).
TENANT = "deadbeef-0000-0000-0000-000000000000"
OTHER = "cafe1234-0000-0000-0000-000000000000"
ADMIN = "abcdef12-0000-0000-0000-000000000000"
SECRET = "CONFIDENTIAL-MARKER"

READ_TOOLS = ["read_file", "list_files", "tree", "find_files", "folder_size"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = (tmp_path / "home").resolve()
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.chdir(h)
    return h


def _file(home: Path, *parts) -> Path:
    p = home.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SECRET)
    return p


def _as_tenant(tool, **kw) -> str:
    return tool.run(user_scope_id=TENANT, user_role="user", **kw)


def _denied(out: str) -> bool:
    return "outside your own data" in out.lower()


# ── The regression: a tenant cannot read outside their own tree ──────────────

def test_a_tenant_cannot_read_another_tenants_file(home):
    target = _file(home, "Documents", "VAF_Projects", "cafe1234", "theirs.txt")
    out = _as_tenant(ReadFileTool(), path=str(target))
    assert _denied(out), out
    assert SECRET not in out


@pytest.mark.parametrize("tool,kw", [
    (ListFilesTool, {}),
    (TreeTool, {}),
    (FinderTool, {"pattern": "*"}),
    (FolderSizeTool, {}),
])
def test_the_listing_tools_do_not_expose_another_tenants_tree(home, tool, kw):
    """Names leak too: knowing what a competitor's project folder contains is a leak even
    without opening a single file."""
    target = _file(home, "Documents", "VAF_Projects", "cafe1234", "theirs.txt")
    out = _as_tenant(tool(), path=str(target.parent), **kw)
    assert _denied(out), out
    assert "theirs.txt" not in out


def test_a_tenant_cannot_read_the_owners_home(home):
    """The write jail confines to the own tree only - personal folders are not part of it,
    and reading must match."""
    target = _file(home, "Documents", "private.txt")
    out = _as_tenant(ReadFileTool(), path=str(target))
    assert _denied(out), out
    assert SECRET not in out


def test_a_tenant_still_reads_their_own_files(home):
    target = _file(home, "Documents", "VAF_Projects", "deadbeef", "mine.txt")
    assert SECRET in _as_tenant(ReadFileTool(), path=str(target))


def test_uploaded_attachments_stay_readable(home):
    """Uploads land inside the caller's own root, so the jail must not need a special case
    for them - this pins that the measurement stays true if the upload path moves."""
    from vaf.core.session import get_session_attachments_dir

    d = get_session_attachments_dir("some-session", user_scope_id=TENANT, create=True)
    assert d is not None
    img = Path(d) / "note.txt"
    img.write_text(SECRET)
    assert SECRET in _as_tenant(ReadFileTool(), path=str(img))


# ── Skills: visible ones only ────────────────────────────────────────────────

@pytest.fixture
def skills(home, monkeypatch):
    """Two skills in the ONE skills directory: one visible to the tenant, one not."""
    base = home / ".vaf" / "skills"
    for sid in ("shared_skill", "someone_elses"):
        (base / sid).mkdir(parents=True)
        (base / sid / "reference.md").write_text(SECRET)
    monkeypatch.setattr("vaf.core.skills_registry.get_skills_dir", lambda: base)
    monkeypatch.setattr(
        "vaf.core.skills_registry.get_visible_skill_ids_for_user",
        lambda scope: ["shared_skill"] if scope == TENANT else ["shared_skill", "someone_elses"],
    )
    return base


def test_a_visible_skills_bundled_files_are_readable(home, skills):
    """use_skill promises this: it prints absolute paths and says 'read with read_file'."""
    out = _as_tenant(ReadFileTool(), path=str(skills / "shared_skill" / "reference.md"))
    assert SECRET in out, out


def test_a_skill_kept_private_to_someone_else_is_not(home, skills):
    """THE hole found while measuring the root set: all skill folders live in one directory,
    so allowing "skills" wholesale would have handed over another user's private one."""
    out = _as_tenant(ReadFileTool(), path=str(skills / "someone_elses" / "reference.md"))
    assert _denied(out), out
    assert SECRET not in out


def test_seeing_a_skill_is_not_permission_to_rewrite_it(home, skills):
    """Visibility (shared_with) and edit authority (can_user_edit_skill) are different
    questions. The skill roots are added in READ mode only, so the write jail is unchanged."""
    out = WriteFileTool().run(
        path=str(skills / "shared_skill" / "reference.md"), content="overwritten",
        user_scope_id=TENANT, user_role="user",
    )
    assert _denied(out), out
    assert (skills / "shared_skill" / "reference.md").read_text() == SECRET


def test_write_mode_carries_no_skill_roots(home, skills):
    """Pinned directly on the jail, not only through a tool."""
    assert len(compute_user_jail(TENANT, "user")["allowed_roots"]) == 1
    assert len(compute_user_jail(TENANT, "user", mode="read")["allowed_roots"]) == 2


def test_a_broken_skill_lookup_does_not_widen_anything(home, monkeypatch):
    """Fail-closed: the caller keeps their own root and loses skill reads."""
    def _boom(_scope):
        raise RuntimeError("manifest unreadable")

    monkeypatch.setattr("vaf.core.skills_registry.get_visible_skill_ids_for_user", _boom)
    assert len(compute_user_jail(TENANT, "user", mode="read")["allowed_roots"]) == 1


# ── Unchanged for everyone else ──────────────────────────────────────────────

def test_an_admin_reads_everything(home):
    target = _file(home, "Documents", "VAF_Projects", "cafe1234", "theirs.txt")
    assert SECRET in ReadFileTool().run(path=str(target), user_scope_id=ADMIN, user_role="admin")


def test_direct_consumers_without_a_scope_are_unchanged(home):
    """The coder, the librarian, automations and the workflow engine register these tools and
    pass no scope kwargs. The librarian additionally installs its OWN jail around its whole
    run, which keeps applying - the inner tool adds none of its own."""
    target = _file(home, "Documents", "VAF_Projects", "cafe1234", "theirs.txt")
    assert SECRET in ReadFileTool().run(path=str(target))


# ── The wiring ───────────────────────────────────────────────────────────────

def test_the_dispatcher_injects_scope_and_role_for_every_read_tool():
    """An injection nobody performs leaves the tools unjailed no matter what run() does."""
    # Identity no longer arrives from a branch keyed on this tool's NAME - the tool DECLARES
    # what it needs (BaseTool.identity_kwargs) and execute_tool obeys the declaration for any
    # tool, including one an embedder registers. The guarantee is unchanged and the
    # behaviour-neutrality of that migration is pinned in
    # tests/test_identity_kwargs_declaration.py; here we pin THIS tool's requirement.
    from vaf.tools.filesystem import (
        FinderTool, FolderSizeTool, ListFilesTool, ReadFileTool, TreeTool,
    )

    for cls in (ReadFileTool, ListFilesTool, TreeTool, FinderTool, FolderSizeTool):
        declared = set(getattr(cls, "identity_kwargs", ()) or ())
        assert {"user_scope_id", "user_role"} <= declared, (
            f"{cls.__name__} lost its identity declaration: {declared}"
        )


@pytest.mark.parametrize("cls", [ReadFileTool, ListFilesTool, TreeTool, FinderTool, FolderSizeTool])
def test_every_read_tool_installs_the_read_jail(cls):
    """The jail is DECLARED now, not installed by hand, so this asks the declaration.

    Strictly stronger than what it replaced. The old form read `run`'s source for a
    `user_jail(..., mode="read")` call, which could only ever be true for a tool that
    remembered to write it; five of twenty-two had not. `file_access` is validated when the
    class is defined - declaring a mode without the matching identity_kwargs is a TypeError
    at import - so the failure mode this test was guarding against cannot reach a test run
    at all.
    """
    assert cls.file_access == "read", (
        f"{cls.__name__} no longer declares the read jail; declaring nothing means no "
        "boundary on any lane, including the direct .run() calls the coder and the workflow "
        "engine make"
    )
    assert {"user_scope_id", "user_role"} <= set(cls.identity_kwargs), (
        f"{cls.__name__} declares file_access without the identity to resolve it - user_jail "
        "installs nothing for a falsy scope, so it would run unconfined while looking confined"
    )
    assert getattr(cls.run, "_vaf_jailed", False), (
        f"{cls.__name__}.run is not wrapped - the declaration exists but nothing acts on it"
    )
