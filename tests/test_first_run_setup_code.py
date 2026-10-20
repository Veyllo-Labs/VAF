# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The first visitor no longer claims the admin (vaf/auth/user_admin.py, the bootstrap).

Measured before: while no admin existed, `/needs-setup` and `/bootstrap` were open to every
admitted address, and the bootstrap token is 2FA-verified for a day. From `10.1.2.3`:
`needs_setup: true`, then bootstrap 200 with role admin, and the token opened `/api` as the
admin. On a VPS the admitted private ranges include the provider's network and the Docker
bridges.

Now a browser that is not on the machine itself needs the setup code, which only a terminal
on the machine can show (`vaf setup code`). The desktop window and `vaf setup` never need it.
"""
import re

import pytest

from auth_harness import Harness

STRANGER = "10.1.2.3"


@pytest.fixture
def h(monkeypatch):
    harness = Harness(monkeypatch)
    from vaf.auth import user_admin
    from vaf.core.config import Config
    saved = {}
    monkeypatch.setattr(Config, "load", staticmethod(lambda *a, **k: dict(saved)))
    monkeypatch.setattr(Config, "save", staticmethod(lambda c, *a, **k: saved.update(c)))
    monkeypatch.setattr(user_admin, "_prepare_user_home", lambda *a, **k: None)
    return harness


def _bootstrap(c, **extra):
    return c.post("/api/auth/bootstrap",
                  json={"username": "owner", "password": "owner-password-1", **extra})


def test_the_setup_code_is_minted_once_and_retired(h):
    """MUTATION: mint on every call; keep it after the admin exists; compare without the
    normalization - red."""
    from vaf.auth import user_admin
    from vaf.core.data_keyring import peek_data_secret
    first = user_admin.first_run_setup_code()
    assert first == user_admin.first_run_setup_code()
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){3}", first), first
    assert peek_data_secret("first_run_setup_code") == first.replace("-", "")
    # Case, dashes and spaces do not matter; an empty or wrong one never matches.
    assert user_admin.setup_code_matches(first.lower())
    assert user_admin.setup_code_matches(" ".join(first.replace("-", "")))
    assert not user_admin.setup_code_matches("")
    assert not user_admin.setup_code_matches(first[:-1] + ("0" if first[-1] != "0" else "1"))
    # Creating the first admin retires it.
    r = _bootstrap(h.client(None))
    assert r.status_code == 200, r.text
    assert user_admin.first_run_setup_code() == ""
    assert not user_admin.setup_code_matches(first)
    assert peek_data_secret("first_run_setup_code") == ""


def test_no_stored_code_matches_nothing(h):
    from vaf.auth import user_admin
    assert not user_admin.setup_code_matches("ABCD-EFGH-JKMN-PQRS")


def test_a_stranger_cannot_claim_the_admin(h):
    """MUTATION: skip the check; gate it on server_mode instead of the client's locality - red."""
    from vaf.auth import user_admin
    code = user_admin.first_run_setup_code()
    stranger = h.client(STRANGER)
    assert stranger.get("/api/auth/needs-setup").json()["setup_code_required"] is True
    for attempt in ({}, {"setup_code": "WRONG-CODE"}):
        r = _bootstrap(stranger, **attempt)
        assert r.status_code == 401 and r.json()["code"] == "setup_code", r.text
    assert h.sessions() == [] and not _accounts(h)
    r = _bootstrap(stranger, setup_code=code.lower())
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "admin"


def test_the_machine_itself_needs_no_code(h):
    local = h.client(None)
    assert local.get("/api/auth/needs-setup").json()["setup_code_required"] is False
    r = _bootstrap(local)
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"


def test_wrong_codes_count_in_the_address_window(h):
    """A wrong code at the bootstrap is a 401 the address window counts."""
    from vaf.auth import user_admin
    user_admin.first_run_setup_code()
    stranger = h.client(STRANGER)
    for _ in range(5):
        assert _bootstrap(stranger, setup_code="WRONG").status_code == 401
    assert _bootstrap(stranger, setup_code="WRONG").status_code == 429


def test_the_setup_code_route(h):
    """MUTATION: leave the route out of the rate-limited paths - red (no 429)."""
    from vaf.auth import user_admin
    code = user_admin.first_run_setup_code()
    stranger = h.client(STRANGER)
    assert stranger.post("/api/auth/setup-code", json={"code": code}).json() == {"ok": True}
    r = stranger.post("/api/auth/setup-code", json={"code": "nope"})
    assert r.status_code == 401 and r.json()["code"] == "setup_code"
    for _ in range(4):
        assert stranger.post("/api/auth/setup-code", json={"code": "nope"}).status_code == 401
    assert stranger.post("/api/auth/setup-code", json={"code": code}).status_code == 429
    # Once an admin exists the route is closed.
    assert _bootstrap(h.client(None)).status_code == 200
    assert h.client("10.1.2.4").post("/api/auth/setup-code", json={"code": code}).status_code == 403


def test_the_setup_code_route_needs_no_token():
    from vaf.auth.middleware import AUTH_EXEMPT_PATHS
    from vaf.auth.rate_limit import _RATE_LIMITED_PATHS
    assert "/api/auth/setup-code" in AUTH_EXEMPT_PATHS
    assert "/api/auth/setup-code" in _RATE_LIMITED_PATHS


def _accounts(h):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from vaf.auth.models import LocalUser
    with Session(h.engine) as s:
        return list(s.execute(select(LocalUser)).scalars().all())


def test_the_wizard_asks_for_the_code_first():
    """AC: the wizard shows the setup_code sub-step when the server asks for one, checks the
    code at /api/auth/setup-code, sends it with the bootstrap and comes back to it on a
    bootstrap 401 setup_code. MUTATION: drop any of the three - red."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "web" / "app" / "login" / "page.tsx").read_text(
        encoding="utf-8")
    assert "setup_code_required" in src
    assert re.search(r"fetch\(`\$\{getApiBase\(\)\}/api/auth/setup-code`", src), \
        "the sub-step must ask the server, not only name the route"
    assert re.search(r"setup_code:\s*setupCode", src)
    assert re.search(r"code === 'setup_code'", src)
    assert "createAdminSubStep === 'setup_code'" in src
