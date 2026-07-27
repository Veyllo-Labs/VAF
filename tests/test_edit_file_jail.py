# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: edit_file is the other half of the write surface and needs the same jail.

The main agent offers a remote user two ways to change a file. ``write_file`` was jailed:
the dispatcher injects the caller's scope and ``WriteFileTool.run`` installs the per-user
contextvar. ``edit_file`` was not injected at all, and it delegates its write to a nested
``WriteFileTool()`` call that carries no scope of its own - so the jailed and the unjailed
path sat next to each other in the same tool list. A tenant confined to their own
``VAF_Projects/<uid8>`` could edit any file the static checks allow, including another
tenant's.

Editing is worse than creating here: the denied ``write_file`` could only have made a NEW
file in a foreign tree, while ``edit_file`` silently rewrites content that is already there.

It is also a READ primitive. When a search block misses, the tool answers with a
"nearest region" slice of the file to help the model retarget - real content, from a file
the caller may not open. The jail therefore wraps the whole body, not just the write:
installing it around the delegate alone would still leak through the miss path.

Its docstring claimed the jail was "inherited" from WriteFileTool. It was not, and that is
the shape of this class of bug - the inner call inherits the contextvar only once someone
sets it.
"""
import re
from pathlib import Path

import pytest

import vaf.core.agent as agent_mod
from vaf.tools.filesystem import EditFileTool

AGENT_SRC = Path(agent_mod.__file__).read_text(encoding="utf-8")

# Synthetic scopes (public-repo hygiene: never a real scope UUID).
TENANT = "deadbeef-0000-0000-0000-000000000000"      # the caller
FOREIGN_UID8 = "cafe1234"                            # somebody else's tree
SECOND_ADMIN = "abcdef12-0000-0000-0000-000000000000"

BODY = "line one\nAPI_TOKEN=confidential\nline three\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = (tmp_path / "home").resolve()
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.chdir(h)
    return h


def _file(home: Path, uid8: str) -> Path:
    p = home / "Documents" / "VAF_Projects" / uid8 / "notes.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(BODY)
    return p


def test_a_tenant_cannot_rewrite_another_tenants_file(home):
    """THE regression: same call the dispatcher makes, against a foreign tree."""
    target = _file(home, FOREIGN_UID8)
    out = EditFileTool().run(
        path=str(target), edits=[{"search": "API_TOKEN", "replace": "PWNED"}],
        user_scope_id=TENANT, user_role="user",
    )
    assert "outside your own data" in out.lower(), out
    assert target.read_text() == BODY, "the foreign file was modified"


def test_the_miss_path_does_not_leak_a_foreign_file(home):
    """A near-miss search normally answers with a slice of the file. Denied paths must
    not reach that branch at all - otherwise edit_file is a read tool for anything."""
    target = _file(home, FOREIGN_UID8)
    out = EditFileTool().run(
        path=str(target), edits=[{"search": "API_TOKEN=wrong-value", "replace": "ZZZZ"}],
        user_scope_id=TENANT, user_role="user",
    )
    assert "outside your own data" in out.lower(), out
    assert "confidential" not in out, "the file's content came back in the error message"


def test_the_miss_path_still_helps_inside_the_callers_own_tree(home):
    """The retarget hint is genuinely useful - the jail must not cost it where it is allowed."""
    target = _file(home, "deadbeef")
    out = EditFileTool().run(
        path=str(target), edits=[{"search": "API_TOKEN=wrong-value", "replace": "ZZZZ"}],
        user_scope_id=TENANT, user_role="user",
    )
    assert "EDIT FAILED" in out
    assert "confidential" in out, "the nearest-region hint disappeared for the owner"


def test_a_tenant_still_edits_their_own_files(home):
    target = _file(home, "deadbeef")
    out = EditFileTool().run(
        path=str(target), edits=[{"search": "API_TOKEN", "replace": "ROTATED"}],
        user_scope_id=TENANT, user_role="user",
    )
    assert "applied 1 change" in out, out
    assert "ROTATED" in target.read_text()


def test_the_home_directory_is_off_limits_for_a_tenant(home):
    """A jailed user's allowed roots are ONLY their own VAF_Projects/<uid8> - matching
    write_file, where personal folders stay off-limits too."""
    (home / "Documents").mkdir(exist_ok=True)
    target = home / "Documents" / "private.txt"
    target.write_text(BODY)
    out = EditFileTool().run(
        path=str(target), edits=[{"search": "API_TOKEN", "replace": "PWNED"}],
        user_scope_id=TENANT, user_role="user",
    )
    assert "outside your own data" in out.lower(), out
    assert target.read_text() == BODY


def test_an_admin_is_not_confined(home):
    """Role-aware, like every other file gate (tests/test_admin_identity_is_role_aware.py)."""
    target = _file(home, FOREIGN_UID8)
    out = EditFileTool().run(
        path=str(target), edits=[{"search": "API_TOKEN", "replace": "ROTATED"}],
        user_scope_id=SECOND_ADMIN, user_role="admin",
    )
    assert "applied 1 change" in out, out


def test_direct_consumers_without_a_scope_are_unchanged(home):
    """The coder, the workflow engine and automations call the tool with no scope kwargs.
    Their behavior must be exactly what it was before the parameter existed."""
    target = _file(home, FOREIGN_UID8)
    out = EditFileTool().run(path=str(target), edits=[{"search": "API_TOKEN", "replace": "OK"}])
    assert "applied 1 change" in out, out


def test_the_whole_file_rescue_branch_is_inside_the_jail(home):
    """A single edit whose search covers ~the whole file is rescued as a write_file call.
    That nested call gets no scope of its own, so it must inherit the contextvar this
    tool installs - the exact assumption the old docstring got wrong."""
    target = _file(home, FOREIGN_UID8)
    out = EditFileTool().run(
        path=str(target), edits=[{"search": BODY, "replace": "completely new content\n"}],
        user_scope_id=TENANT, user_role="user",
    )
    assert "outside your own data" in out.lower(), out
    assert target.read_text() == BODY


def test_the_dispatcher_injects_scope_and_role_for_edit_file():
    """An injection nobody performs leaves the tool unjailed no matter what run() does.
    ASSIGNED, never defaulted: tool_args starts out as the arguments the model produced."""
    # Identity no longer arrives from a branch keyed on this tool's NAME - the tool DECLARES
    # what it needs (BaseTool.identity_kwargs) and execute_tool obeys the declaration for any
    # tool, including one an embedder registers. The guarantee is unchanged and the
    # behaviour-neutrality of that migration is pinned in
    # tests/test_identity_kwargs_declaration.py; here we pin THIS tool's requirement.
    declared = set(getattr(EditFileTool, "identity_kwargs", ()) or ())
    assert {"user_scope_id", "user_role"} <= declared, (
        f"edit_file lost its identity declaration: {declared}"
    )
