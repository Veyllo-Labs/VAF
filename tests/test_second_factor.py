# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A second-factor code works once (vaf/auth/second_factor.py).

`pyotp.verify` accepts the current step and the ones beside it and remembers nothing, so a
code read over a shoulder or relayed by a phishing page worked again for up to 90 seconds
(measured: the same code accepted twice, 200 and 200).
"""
import pytest

import auth_harness
from auth_harness import Harness, code_at


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


def test_a_code_is_spent_by_its_first_use(h):
    """MUTATION: drop the last-step comparison - red."""
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    c = h.client("192.168.8.1")
    code = code_at(secret, 1)
    temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
    assert h.verify(c, temp, code).status_code == 200
    temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
    assert h.verify(c, temp, code).status_code == 401
    # A code of an earlier step than the last accepted one is spent as well.
    temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
    assert h.verify(c, temp, code_at(secret, 0)).status_code == 401


def test_the_check_answers_with_a_reason(h):
    from vaf.auth import second_factor
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    h.add_user("bob", "bob-password-12")
    alice, bob = h.user("alice"), h.user("bob")
    assert second_factor.check_second_factor(bob, "123456") == second_factor.NOT_SET_UP
    assert second_factor.check_second_factor(alice, "abcdef") == second_factor.WRONG_CODE
    assert second_factor.check_second_factor(alice, code_at(secret)) is None
    alice.totp_nonce = b"\x00" * 12
    assert second_factor.check_second_factor(alice, code_at(secret, 1)) == second_factor.UNREADABLE


def test_the_old_verifier_is_gone():
    """Three doors, one check: nothing may call a TOTP verifier of its own again."""
    from pathlib import Path

    from vaf.auth import crypto
    assert not hasattr(crypto, "verify_totp")
    root = Path(__file__).resolve().parents[1] / "vaf"
    offenders = [p.relative_to(root.parent).as_posix() for p in root.rglob("*.py")
                 if p.name != "second_factor.py"
                 and (".verify(code" in p.read_text(encoding="utf-8")
                      or "pyotp.TOTP(secret).verify" in p.read_text(encoding="utf-8"))]
    assert not offenders, offenders
    assert auth_harness.JWT_SECRET
