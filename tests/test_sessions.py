# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Sessions: every token is its own, and ending one ends it.

Measured before: two sign-ins of one account in the same second minted byte-identical tokens
(no `jti`), and the second session row failed on the unique token hash: a double-submitted
2FA code answered 500.
"""
import re
from datetime import datetime as _real_datetime, timezone

import pytest

from auth_harness import Harness


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


@pytest.fixture
def frozen_second(monkeypatch):
    """Every token minted in the same second."""
    from vaf.auth import crypto
    fixed = _real_datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)

    class _Frozen(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(crypto, "datetime", _Frozen)


def test_tokens_are_unique_per_mint(frozen_second, monkeypatch):
    """MUTATION: remove the jti claim - red."""
    from vaf.auth import crypto
    monkeypatch.setattr(crypto, "_get_jwt_secret", lambda: "s" * 48)
    a = crypto.create_access_token("u1", "alice", "user", "s1", is_2fa_verified=True)
    b = crypto.create_access_token("u1", "alice", "user", "s1", is_2fa_verified=True)
    assert a != b
    pa, pb = crypto.decode_token(a), crypto.decode_token(b)
    assert pa["iat"] == pb["iat"]
    assert re.fullmatch(r"[0-9a-f]{32}", pa["jti"]) and pa["jti"] != pb["jti"]


def test_two_sign_ins_in_one_second_both_succeed(h, frozen_second):
    """The double-submitted sign-in: both answer 200 and both sessions exist."""
    from sqlalchemy.orm import Session
    h.add_user("alice", "alice-password-1")
    with Session(h.engine) as s:
        user = s.get(type(h.user("alice")), h.user("alice").id)
        user.requires_2fa_setup = False          # an account without a second factor
        s.commit()
    c = h.client("192.168.9.1")
    first = h.login(c, "alice", "alice-password-1")
    second = h.login(c, "alice", "alice-password-1")
    assert first.status_code == 200 and second.status_code == 200, second.text
    assert first.json()["access_token"] != second.json()["access_token"]
    assert len(h.sessions()) == 2
