# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf users reset-password`: the way back in when an admin's password is gone.

It needs a person at the terminal and a proof (the admin password or the recovery key from
VAF-BackThisUp.md), never the group door, which opens for a caller without a terminal and
asks for exactly the password that is gone. The account layer is faked; what is pinned is
who gets through and what the reset sets.
"""
import contextlib
import uuid
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import vaf.cli.cmd.users as users_cmd


@pytest.fixture
def world(monkeypatch):
    """A fake account store, a recovery key, prompts answered from a list."""
    import vaf.auth.database as database
    import vaf.auth.user_admin as user_admin
    import vaf.cli.gate as gate
    import vaf.core.recovery_kit as recovery_kit
    import vaf.core.security_events as security_events

    state = {"user": SimpleNamespace(username="alice", role="admin", seal="x"), "resets": [],
             "events": [], "answers": [], "sealed": True}

    @contextlib.asynccontextmanager
    async def _db():
        yield object()

    async def _find(_db, name):
        return state["user"] if name.lower() == "alice" else None

    async def _reset(_db, user, password):
        state["resets"].append((user.username, password))

    monkeypatch.setattr(database, "get_auth_db", _db)
    monkeypatch.setattr(user_admin, "find_user", _find)
    monkeypatch.setattr(user_admin, "reset_password_from_terminal", _reset)
    from vaf.auth import account_seal
    monkeypatch.setattr(account_seal, "is_sealed", lambda u: state["sealed"])
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate, "_stored_hash", lambda: ("", False))
    wrap = SimpleNamespace(exists=lambda: True)
    monkeypatch.setattr(recovery_kit, "recovery_wrap_path", lambda: wrap)
    monkeypatch.setattr(recovery_kit, "unwrap_with_secret",
                        lambda s: b"dek" if s == "the-real-key" else None)
    monkeypatch.setattr(security_events, "log_security_event",
                        lambda kind, **kw: state["events"].append((kind, kw.get("detail", ""))))
    monkeypatch.setattr(users_cmd.getpass, "getpass", lambda _p="": state["answers"].pop(0))
    return state


def _run(args, input_text=""):
    return CliRunner().invoke(users_cmd.app, args, input=input_text)


def test_a_reset_needs_a_person_at_the_terminal(world, monkeypatch):
    """The agent's shell and scripts have no terminal; the group door would have let them in.
    MUTATION: drop the is_interactive check - red."""
    import vaf.cli.gate as gate
    monkeypatch.setattr(gate, "is_interactive", lambda: False)
    world["answers"] = ["the-real-key", "new-password-123", "new-password-123"]
    r = _run(["reset-password", "alice", "--recovery-key"])
    assert r.exit_code == 1 and world["resets"] == []


def test_a_wrong_recovery_key_resets_nothing_and_is_recorded(world):
    world["answers"] = ["guessed"]
    r = _run(["reset-password", "alice", "--recovery-key"])
    assert r.exit_code == 1 and world["resets"] == []
    assert world["events"] and world["events"][0][0] == "cli_password_gate_failed"


def test_the_recovery_key_lets_the_owner_who_forgot_the_password_back_in(world):
    """MUTATION: accept the recovery key without unwrapping it - the wrong-key test goes red;
    drop the reset - this one goes red."""
    world["answers"] = ["the-real-key", "new-password-123", "new-password-123"]
    r = _run(["reset-password", "alice", "--recovery-key"])
    assert r.exit_code == 0, r.output
    assert world["resets"] == [("alice", "new-password-123")]
    assert ("password_changed", "reset in a terminal on this machine (recovery key)") in world["events"]


def test_without_a_recorded_password_the_admin_password_route_points_to_the_key(world):
    r = _run(["reset-password", "alice"])
    assert r.exit_code == 1 and "--recovery-key" in r.output and world["resets"] == []


def test_a_record_changed_outside_vaf_is_shown_and_asked_about(world):
    """Resetting accepts the record as it stands (its role included): the person sees that
    first. MUTATION: skip the question - red."""
    world["sealed"] = False
    world["answers"] = ["the-real-key"]
    r = _run(["reset-password", "alice", "--recovery-key"], input_text="n\n")
    assert r.exit_code == 1 and "changed outside VAF" in r.output and world["resets"] == []


def test_the_terminal_reset_sets_a_fresh_second_factor_and_reseals(monkeypatch):
    """On a real row (SQLite): a new hash, the second factor set up again, and a row changed
    outside VAF re-sealed under the trusted rewrite."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import Session

    from vaf.auth import account_seal
    from vaf.auth.crypto import verify_password
    from vaf.auth.models import LocalUser, UserSession
    from vaf.auth.user_admin import apply_terminal_reset

    @compiles(JSONB, "sqlite")
    def _jsonb(_t, _c, **_kw):
        return "JSON"

    monkeypatch.setattr(account_seal, "_key_cache", b"k" * 32)
    monkeypatch.setattr(account_seal, "_enforced_cache", True)
    eng = create_engine("sqlite://")
    LocalUser.__table__.create(eng)
    UserSession.__table__.create(eng)
    with Session(eng) as s:
        s.add(LocalUser(username="alice", password_hash="old", role="admin",
                        totp_secret=b"s", totp_nonce=b"n", requires_2fa_setup=False))
        s.commit()
    with eng.begin() as conn:
        conn.execute(text("UPDATE local_users SET role = 'user'"))   # an outside change
    with Session(eng) as s:
        user = s.query(LocalUser).one()
        assert not account_seal.is_sealed(user)
        apply_terminal_reset(user, "new-password-123")
        with account_seal.trusted_rewrite():
            s.commit()
        assert verify_password(user.password_hash, "new-password-123")
        assert user.totp_secret is None and user.requires_2fa_setup is True
        assert account_seal.is_sealed(user)
    account_seal.reset_caches()
    _ = uuid
