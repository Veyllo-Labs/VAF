# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: "admin" must mean the same thing for files as it does everywhere else.

VAF answers "is this caller an admin" in two halves - the DB role, or the configured
``local_admin_scope_id`` for the machine owner who has no role claim (tokenless desktop,
CLI, automations). Roughly thirty gates use both halves. Three file-access gates used the
scope half ALONE:

- ``compute_user_jail`` - the per-user filesystem jail behind ``librarian_agent``,
  ``write_file`` and ``send_mail`` attachments,
- the ``VAF_Projects/<uid8>`` ownership check in ``GET /api/file``,
- the same check in ``POST /api/image/describe``.

Only ONE scope can be the local admin, so every additional admin account - and user
management explicitly supports them, it only refuses to delete the LAST one - was treated
as an ordinary tenant there while being a full admin everywhere else. The clearest proof
sat inside a single function: ``/api/image/describe`` decided file ownership scope-only and
then, twenty-five lines later, session ownership role-aware. Same request, same person, two
answers.

The direction of this fix is unusual for a security change - it GRANTS access - so the
trust chain matters and is pinned below: the role only ever arrives as a claim from a
signature-verified JWT (issued from ``LocalUser.role``), and the tool dispatcher ASSIGNS it
over whatever the model put in the arguments. A second admin gains nothing here they could
not already reach: ``config_for_user`` hands role-admins the full config including every
API key.
"""
import re
from pathlib import Path

import pytest

import vaf.core.agent as agent_mod
from vaf.core.config import get_local_admin_scope_id, is_admin_identity
from vaf.tools.filesystem import WriteFileTool, compute_user_jail

AGENT_SRC = Path(agent_mod.__file__).read_text(encoding="utf-8")

# Synthetic scopes (public-repo hygiene: never a real scope UUID).
SECOND_ADMIN_SCOPE = "abcdef12-0000-0000-0000-000000000000"
PLAIN_USER_SCOPE = "12345678-1234-1234-1234-123456789abc"


# ── The shared definition ────────────────────────────────────────────────────

def test_role_admin_is_admin_even_with_a_foreign_scope():
    """THE regression: a second admin account carries its own scope UUID."""
    assert is_admin_identity("admin", SECOND_ADMIN_SCOPE) is True


def test_the_machine_owner_is_admin_without_any_role():
    """The scope half is not redundant: the tokenless desktop, the CLI and automations
    resolve to the local-admin scope and carry no role claim at all."""
    assert is_admin_identity(None, get_local_admin_scope_id()) is True


def test_an_ordinary_user_is_not_admin_either_way():
    assert is_admin_identity("user", PLAIN_USER_SCOPE) is False
    assert is_admin_identity(None, PLAIN_USER_SCOPE) is False
    assert is_admin_identity(None, None) is False


@pytest.mark.parametrize("role", ["ADMIN", " Admin ", "admin"])
def test_role_matching_tolerates_case_and_padding(role):
    """The role travels through JWT claims and session metadata; the ~30 other gates
    lowercase it, so this one must not be stricter than they are."""
    assert is_admin_identity(role, PLAIN_USER_SCOPE) is True


@pytest.mark.parametrize("role", ["administrator", "admin ", "superadmin", "adm"])
def test_only_the_exact_role_counts(role):
    """No prefix or substring matching - "administrator" is not a VAF role."""
    if role.strip().lower() == "admin":
        return
    assert is_admin_identity(role, PLAIN_USER_SCOPE) is False


# ── Consumer 1: the filesystem jail ──────────────────────────────────────────

def test_the_jail_lets_a_second_admin_out():
    assert compute_user_jail(SECOND_ADMIN_SCOPE, "admin")["is_admin"] is True


def test_the_jail_still_confines_an_ordinary_user():
    info = compute_user_jail(PLAIN_USER_SCOPE, "user")
    assert info["is_admin"] is False
    assert info["uid8"] == "12345678"
    assert info["allowed_roots"], "a jailed user still needs their own root"


def test_the_jail_is_unchanged_when_no_role_is_passed():
    """Direct consumers (coder, workflow engine, automations) pass no role. Their
    behavior must be byte-for-byte what it was before the parameter existed."""
    assert compute_user_jail(None)["is_admin"] is True
    assert compute_user_jail(get_local_admin_scope_id())["is_admin"] is True
    assert compute_user_jail(PLAIN_USER_SCOPE)["is_admin"] is False


def test_write_file_stays_jailed_for_a_user_whose_role_is_not_admin(tmp_path, monkeypatch):
    """End-to-end through the tool, since WriteFileTool installs the jail itself
    (the dispatcher's contextvar would not survive the bounded-run worker thread)."""
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(home)
    foreign = home / "Documents" / "VAF_Projects" / "cafe1234" / "x.txt"
    out = WriteFileTool().run(
        path=str(foreign), content="x",
        user_scope_id="deadbeef-0000-0000-0000-000000000000", user_role="user",
    )
    assert "outside your own data" in out.lower(), out
    assert not foreign.exists()


# ── The trust chain: the model must not be able to name its own role ─────────

@pytest.mark.parametrize("tool", ["librarian_agent", "write_file", "send_mail"])
def test_the_dispatcher_assigns_the_role_it_never_defaults_it(tool):
    """tool_args starts life as the arguments the MODEL produced, so the role must be
    ASSIGNED from the session context - a setdefault would turn the jail into a suggestion.

    The mechanism moved: instead of a branch keyed on each tool's NAME, the tool DECLARES
    that it needs the role and execute_tool assigns it generically. Pinned in two halves -
    the tool still asks for the role, and the dispatcher still overwrites rather than
    defaults (the runtime proof lives in tests/test_identity_kwargs_declaration.py)."""
    from vaf.tools.base import BaseTool

    cls = next(
        (c for c in _all_subclasses(BaseTool) if getattr(c, "name", None) == tool), None
    )
    assert cls is not None, f"{tool} no longer resolves to a tool class"
    assert "user_role" in (getattr(cls, "identity_kwargs", ()) or ()), (
        f"{tool} must declare user_role to stay role-aware"
    )

    # The assignment moved out of the dispatcher into the shared funnel module, so this
    # anchors on the function that performs it rather than on a region of agent.py.
    import inspect

    from vaf.core.tool_dispatch import assign_declared_identity

    region = inspect.getsource(assign_declared_identity)
    assert "args[key] = available[key]" in region, "identity is no longer assigned generically"
    assert "setdefault" not in region, "identity must be assigned, never defaulted"


def _all_subclasses(base):
    import importlib
    import inspect
    import pkgutil

    import vaf.core
    import vaf.tools
    out = []
    for pkg in (vaf.tools, vaf.core):
        for m in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
            try:
                mod = importlib.import_module(m.name)
            except Exception:
                continue
            for _, obj in inspect.getmembers(mod, inspect.isclass):
                if issubclass(obj, base) and obj is not base:
                    out.append(obj)
    return out


def test_the_jailed_tools_read_the_role_from_the_injected_argument():
    """The three consumers must actually forward what the dispatcher injects - an
    injection nobody reads is the failure mode this pins."""
    from vaf.tools import filesystem as filesystem_mod
    from vaf.tools import librarian as librarian_mod
    from vaf.tools import send_mail as send_mail_mod

    fs_src = Path(filesystem_mod.__file__).read_text(encoding="utf-8")
    assert 'kwargs.pop("user_role", None)' in fs_src, "WriteFileTool must consume user_role"
    lib_src = Path(librarian_mod.__file__).read_text(encoding="utf-8")
    assert 'kwargs.get("user_role")' in lib_src
    assert "VAF_USER_ROLE" in lib_src, (
        "the sub-agent terminal lane carries the scope via env and must carry the role too, "
        "or an admin is jailed in one lane and free in the other"
    )
    mail_src = Path(send_mail_mod.__file__).read_text(encoding="utf-8")
    assert 'kwargs.get("user_role")' in mail_src


# ── Consumers 2+3: the HTTP file gates that had drifted ──────────────────────

def test_the_project_file_gates_use_the_shared_definition():
    """Both ``VAF_Projects/<uid8>`` ownership checks in web_server (GET /api/file and
    POST /api/image/describe) must go through is_admin_identity. Pinned as source
    because reconstructing the comparison inline is exactly how they drifted."""
    import vaf.core.web_server as ws_mod

    src = Path(ws_mod.__file__).read_text(encoding="utf-8")
    # Anchor on the uid8 prefix comparison - the expression unique to these two gates.
    gates = list(re.finditer(r'\.replace\("-", ""\)\.lower\(\)\.startswith\(', src))
    assert len(gates) == 2, f"expected the two VAF_Projects ownership gates, found {len(gates)}"
    for g in gates:
        preceding = src[max(0, g.start() - 400):g.start()]
        assert "is_admin_identity(" in preceding, (
            "a VAF_Projects ownership gate decides 'is admin' on its own instead of using "
            "the shared definition - that is exactly how these two drifted"
        )
