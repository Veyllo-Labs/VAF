# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: the shared path rule resolves symlinks and re-checks the real target.

``is_safe_path`` ran BLOCKED_DIRS and the ``~/.vaf`` credential-store check against the
UNRESOLVED path (``os.path.abspath`` follows no symlinks), while the downstream ``open()``
follows the link. So a symlink inside an allowed folder pointing at ``~/.vaf/config.json``
or ``/etc/...`` passed every static check and was opened at its target.

Who was exposed splits by jail state, and the tests mirror that split:

- A JAILED caller (non-admin scope) was already covered: ``_librarian_jail_ok`` resolves
  for itself before deciding. That half is kept here as the pair (the
  ``tests/test_data_explorer.py`` precedent: escaping refused / inside allowed, together).
- An UNJAILED caller (admin role, or no scope at all - every direct consumer) had NO
  resolving check whatsoever. That half is what the recheck in ``is_safe_path`` adds:
  resolve, and when the real path differs, ask the same rule about the real one.

``send_mail`` used to close this hand-rolled (resolve-then-recheck in its own
``_resolve_path``); that hand-roll is deleted in favor of the shared rule, so the
through-``run()`` test here runs AS AN ADMIN - the only caller class whose protection
now comes from the framework recheck alone.
"""
import os
from pathlib import Path

import pytest

from vaf.tools.filesystem import is_safe_path, user_jail

# Synthetic scopes (public-repo hygiene: never a real scope UUID).
TENANT = "deadbeef-0000-0000-0000-000000000000"
ADMIN_SCOPE = "abcdef12-0000-0000-0000-000000000000"
SECRET = "CONFIDENTIAL-MARKER"


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


# ── The half the recheck ADDS: unjailed callers ──────────────────────────────

def test_escaping_symlink_is_refused_for_an_unjailed_caller(home):
    """No jail is installed here (direct consumer / admin class). Before the recheck,
    the static checks saw only the link path under Documents and passed it; the
    credential store behind the link was reachable."""
    target = _file(home, ".vaf", "config.json")
    link = home / "Documents" / "reports" / "summary.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)

    safe, verdict = is_safe_path(str(link))
    assert not safe
    assert "VAF's own data directory" in verdict


def test_symlink_inside_the_allowed_tree_is_allowed_and_returns_the_unresolved_path(home):
    """The recheck must not break legitimate links, and the RETURN VALUE contract
    stays: callers receive the unresolved abs_path, never the resolved target."""
    real = _file(home, "Documents", "project", "report.txt")
    alias = home / "Documents" / "project" / "latest.txt"
    os.symlink(real, alias)

    safe, result = is_safe_path(str(alias))
    assert safe, result
    assert result == str(alias)
    assert result != str(real)


# ── The pair half that already held: jailed callers ──────────────────────────

def test_escaping_symlink_is_refused_inside_the_jail(home):
    """A link inside the caller's own tree pointing at another tenant's file. The jail
    half resolves for itself (_librarian_jail_ok), kept as the pair to the tests above."""
    theirs = _file(home, "Documents", "VAF_Projects", "cafe1234", "theirs.txt")
    own = home / "Documents" / "VAF_Projects" / "deadbeef"
    own.mkdir(parents=True, exist_ok=True)
    link = own / "borrowed.txt"
    os.symlink(theirs, link)

    with user_jail(TENANT, "user"):
        safe, verdict = is_safe_path(str(link))
    assert not safe
    assert "outside your own data" in verdict


# ── Through run(): the lane that lost its hand-roll ──────────────────────────

def test_send_mail_symlink_attachment_is_refused_through_run(home, monkeypatch):
    """AS AN ADMIN: no jail installs (compute_user_jail answers is_admin), so the only
    thing standing between a symlinked attachment and the credential store is the
    shared rule's recheck - exactly the coverage send_mail's deleted hand-roll carried."""
    import vaf.tools.send_mail as sm

    target = _file(home, ".vaf", "config.json")
    link = home / "Documents" / "invoice.pdf"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)

    calls = {"n": 0}
    monkeypatch.setattr(sm, "list_accounts_for_user", lambda *a, **k: ["user@example.com"])
    monkeypatch.setattr(sm, "get_account", lambda *a, **k: {"provider": "imap", "email": "user@example.com"})
    monkeypatch.setattr(sm.sender, "send",
                        lambda msg: calls.__setitem__("n", calls["n"] + 1) or sm.sender.SendResult(True, "ok"))

    out = sm.SendMailTool().run(
        to="rcpt@example.com",
        subject="Report",
        body="see attachment",
        attachment_paths=[str(link)],
        username="alice",
        user_scope_id=ADMIN_SCOPE,
        user_role="admin",
    )
    assert "VAF's own data directory" in out
    assert calls["n"] == 0, "the sender must not be reached with a smuggled path"
