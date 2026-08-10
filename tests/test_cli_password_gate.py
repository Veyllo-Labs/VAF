# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The terminal door, and the lanes that must never see it.

The shield model has one rule that is easy to break by accident: everything
running INSIDE keeps working unattended. A password prompt that leaks into the
tray, the headless runner, a sub-agent spawn or a cron-driven automation does
not make anything safer - it makes an unattended machine stop working after a
reboot, which is the one outcome the design rules out. So the tests that matter
most here are the negative ones.
"""
import pytest

from vaf.cli import gate


@pytest.fixture(autouse=True)
def _locked():
    gate._unlocked = False
    yield
    gate._unlocked = False


@pytest.fixture
def stored_hash(monkeypatch):
    from vaf.auth.crypto import hash_password

    value = hash_password("correct horse")
    monkeypatch.setattr(gate, "_stored_hash", lambda: (value, False))
    return value


# ── the lanes that must never be prompted ───────────────────────────────────────

def test_a_non_interactive_lane_is_never_prompted(monkeypatch, stored_hash):
    """`vaf run -p`, cron, systemd, a pipe: no tty, no prompt, no blocking."""
    monkeypatch.setattr(gate, "is_interactive", lambda: False)
    monkeypatch.setattr(gate.getpass, "getpass", _explode)

    assert gate.require_admin_password() is True


def test_the_escape_hatch_lets_a_supervisor_through(monkeypatch, stored_hash):
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setenv("VAF_SKIP_PASSWORD_GATE", "1")
    monkeypatch.setattr(gate.getpass, "getpass", _explode)

    assert gate.require_admin_password() is True


def test_turning_the_gate_off_skips_it(monkeypatch, stored_hash):
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate, "gate_enabled", lambda: False)
    monkeypatch.setattr(gate.getpass, "getpass", _explode)

    assert gate.require_admin_password() is True


def test_a_fresh_install_without_a_password_is_not_locked_out(monkeypatch):
    """No web account yet means there is nothing to verify - prompting would be
    an unopenable door, not a secure one."""
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate, "_stored_hash", lambda: ("", False))
    monkeypatch.setattr(gate.getpass, "getpass", _explode)

    assert gate.require_admin_password() is True


def test_the_background_lanes_do_not_import_the_gate():
    """Mutation guard: adding the prompt to a background lane is the failure mode.

    A behavioural test cannot cover every entry point, but the ones that must
    stay silent are enumerable, and importing the gate there is the first step
    of getting it wrong.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for rel in (
        "vaf/core/headless_runner.py",
        "vaf/tray.py",
        "vaf/core/automation.py",
        "vaf/workflows/engine.py",
        "vaf/cli/cmd/subagent.py",
        "vaf/framework.py",
    ):
        assert "cli.gate" not in (root / rel).read_text(encoding="utf-8"), (
            f"{rel} must never prompt - it runs inside the shield"
        )


# ── the door itself ─────────────────────────────────────────────────────────────

def test_an_unreadable_key_store_refuses_instead_of_opening(monkeypatch):
    """Failing OPEN on an error, while announcing "no password set", was the
    worst of both: it let the visitor in exactly when something was wrong."""
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate, "_stored_hash", lambda: ("", True))
    monkeypatch.setattr(gate.getpass, "getpass", _explode)

    assert gate.require_admin_password() is False


def test_only_the_machine_owner_holds_the_door_slot(monkeypatch):
    """A second admin, or a password reset for someone else, must not take it."""
    from vaf.core.config import Config

    real_get = Config.get
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: {
        "local_admin_username": "owner", "local_admin_scope_id": "scope-owner",
    }.get(k, real_get(k, d))))

    assert gate.is_local_admin_account(username="owner") is True
    assert gate.is_local_admin_account(username="someone-else") is False
    assert gate.is_local_admin_account(user_scope_id="scope-owner") is True
    assert gate.is_local_admin_account(user_scope_id="other-scope") is False


def test_the_right_password_opens_it_once(monkeypatch, stored_hash):
    asked = []
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate.getpass, "getpass", lambda _p: asked.append(1) or "correct horse")

    assert gate.require_admin_password() is True
    assert gate.require_admin_password() is True
    assert len(asked) == 1, "one prompt per process, not per call"


def test_three_wrong_attempts_refuse(monkeypatch, stored_hash):
    attempts = []
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate.getpass, "getpass", lambda _p: attempts.append(1) or "nope")

    assert gate.require_admin_password() is False
    assert len(attempts) == 3


def test_ctrl_c_refuses_instead_of_letting_the_caller_in(monkeypatch, stored_hash):
    def _interrupt(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate.getpass, "getpass", _interrupt)

    assert gate.require_admin_password() is False


def test_the_check_needs_no_database(monkeypatch):
    """Verified against the keyring copy: the auth DB is a container and is
    often down on a desktop, and a door that needs it is a lockout."""
    from vaf.auth.crypto import hash_password
    from vaf.core.data_keyring import set_data_secret

    set_data_secret(gate.RING_KEY, hash_password("from the ring"))
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate.getpass, "getpass", lambda _p: "from the ring")

    assert gate.require_admin_password() is True


def test_the_hash_is_mirrored_when_an_admin_password_is_set():
    from vaf.auth.crypto import hash_password, verify_password
    from vaf.core.data_keyring import peek_data_secret

    gate.mirror_admin_password_hash(hash_password("neues passwort"))

    assert verify_password(peek_data_secret(gate.RING_KEY), "neues passwort")


def _explode(_prompt):
    raise AssertionError("this lane must never prompt for a password")
