# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A second-factor code works once (vaf/auth/second_factor.py).

`pyotp.verify` accepts the current step and the ones beside it and remembers nothing, so a
code read over a shoulder or relayed by a phishing page worked again for up to 90 seconds
(measured: the same code accepted twice, 200 and 200).
"""
import ast

import pytest

import auth_harness
from auth_harness import Harness, code_at


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


def _spent(r):
    assert r.status_code == 400 and r.json()["code"] == "code_spent", r.text


def test_a_code_is_spent_by_its_first_use(h):
    """A code works once: used again it is refused as spent (400 code_spent, so the page can
    say "wait for the next code"). MUTATION: drop the last-step comparison - red."""
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    c = h.client("192.168.8.1")
    code = code_at(secret, 1)
    temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
    assert h.verify(c, temp, code).status_code == 200
    temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
    _spent(h.verify(c, temp, code))
    # A code of an earlier step than the last accepted one is spent as well.
    temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
    _spent(h.verify(c, temp, code_at(secret, 0)))


def test_a_spent_code_is_not_a_guess(h):
    """Typing the code just used to sign in again (a password change or an admin's
    confirmation within the same 30 seconds) counted as a wrong code and closed the window on
    its owner. It is a right code: not counted, in neither window.
    MUTATION: count a spent code in the second-factor window - red."""
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    c = h.client("192.168.8.2")
    code = code_at(secret, 1)
    temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
    assert h.verify(c, temp, code).status_code == 200
    for _ in range(6):
        temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
        _spent(h.verify(c, temp, code))
    # Neither the account's code nor the address is held: the next code signs in.
    from vaf.auth import second_factor
    second_factor.forget_spent_codes()
    temp = h.login(c, "alice", "alice-password-1").json()["temp_token"]
    assert h.verify(c, temp, code_at(secret, 0)).status_code == 200


def test_an_expired_step_says_so(h):
    """The step between password and code lasts 15 minutes; when it ran out, the answer says
    so (sign_in_expired) instead of "invalid code"."""
    r = h.verify(h.client("192.168.8.3"), "not-a-token", "123456")
    assert r.status_code == 401 and r.json()["code"] == "sign_in_expired"


def test_the_check_answers_with_a_reason(h):
    from vaf.auth import second_factor
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    h.add_user("bob", "bob-password-12")
    alice, bob = h.user("alice"), h.user("bob")
    assert second_factor.check_second_factor(bob, "123456") == second_factor.NOT_SET_UP
    assert second_factor.check_second_factor(alice, "abcdef") == second_factor.WRONG_CODE
    assert second_factor.check_second_factor(alice, code_at(secret)) is None
    assert second_factor.check_second_factor(alice, code_at(secret)) == second_factor.SPENT
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


def test_a_refused_new_password_does_not_spend_the_code(h):
    """The new password is judged before the code is checked: a code spent on a change that
    was then refused for its new password made the person wait for the next one.
    MUTATION: check the code first again - red."""
    _id, secret = h.add_user("alice", "alice-password-1", totp=True)
    token = h.sign_in(h.client("192.168.8.5"), "alice", "alice-password-1", secret)
    c = h.client("192.168.8.6")
    code = code_at(secret, 1)
    headers = {"Authorization": f"Bearer {token}"}
    for new in ("alice-password-1", "short"):
        r = c.post("/api/auth/change-password", headers=headers,
                   json={"current_password": "alice-password-1", "new_password": new, "code": code})
        assert r.json()["code"] in ("same_password", "password_too_short"), r.text
    r = c.post("/api/auth/change-password", headers=headers,
               json={"current_password": "alice-password-1", "new_password": "alice-password-2",
                     "code": code})
    assert r.status_code == 200, r.text


# A TOTP secret literal in a test must be as long as the one the product makes:
# pyotp.random_base32() gives 32 characters (160 bits), and pyotp 2.12 refuses a secret under
# 112 bits. A 16-character fixture passed on the locked pyotp and failed only on a CI runner
# that resolved the newer one, so the length is pinned here rather than on the runner.
_MIN_TOTP_CHARS = 32
_TOTP_CALLEES = {"TOTP", "HOTP", "code_at"}


def _base32(node) -> bool:
    import re
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and re.fullmatch(r"[A-Z2-7]+=*", node.value) is not None)


def _short_totp_literals(source: str) -> list:
    """Line numbers of base32 literals under 32 characters handed to a TOTP call, or bound to a
    name or keyword that says it holds a TOTP secret."""
    found = []
    for node in ast.walk(ast.parse(source)):
        values = []
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in _TOTP_CALLEES or "totp" in name.lower():
                values += node.args
            values += [k.value for k in node.keywords if k.arg and "totp" in k.arg.lower()]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any("totp" in getattr(t, "id", getattr(t, "attr", "")).lower() for t in targets):
                values.append(node.value)
        found += [v.lineno for v in values
                  if _base32(v) and len(v.value.rstrip("=")) < _MIN_TOTP_CHARS]
    return found


def test_no_test_hands_totp_a_short_secret():
    """MUTATION: put the 16-character secret back into test_users_cli.py - red."""
    from pathlib import Path
    tests = Path(__file__).resolve().parent
    offenders = [f"{p.name}:{line}" for p in sorted(tests.rglob("*.py"))
                 for line in _short_totp_literals(p.read_text(encoding="utf-8"))]
    assert not offenders, offenders


def test_the_short_secret_detector_actually_detects():
    short, full = "JBSWY3DPEHPK3PXP", "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    for shape in ('pyotp.TOTP("{s}").now()', 'encrypt_totp_secret("{s}")', 'code_at("{s}", 1)',
                  '_TOTP_SECRET = "{s}"', 'make(totp_secret="{s}")'):
        assert _short_totp_literals(shape.format(s=short)) == [1], shape
        assert _short_totp_literals(shape.format(s=full)) == [], shape
    assert _short_totp_literals('print("JBSWY3DPEHPK3PXP")') == []
