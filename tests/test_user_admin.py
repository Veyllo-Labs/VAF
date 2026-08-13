# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one place accounts are created, and the rules it enforces for everyone.

Account creation used to live in two routes that had drifted apart: the
bootstrap checked duplicates case-INsensitively and demanded eight characters,
the admin route checked case-SENSITIVELY and accepted anything. So "Alice" and
"alice" could both exist while every lookup that matters compares lowercased,
and an admin could hand out a two-character password. These tests pin the
unified rules and the ordering that keeps a half-finished setup understandable.
"""
import asyncio
import json

import pytest

from vaf.auth import user_admin
from vaf.auth.user_admin import UserAdminError


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDb:
    """Just enough session: rows in a list, queries answered from it.

    The real queries are SQLAlchemy selects; this double answers them by
    inspecting the *intent* the caller declared, which keeps the test about
    the rules rather than about SQL rendering.
    """

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.committed = 0
        self.deleted = []
        self._next = None

    # user_admin asks exactly two questions: "is there an active admin" and
    # "is this username taken". They are told apart by the lower() in the
    # duplicate check - NOT by looking for "role", which appears in every
    # statement because it is one of the selected columns.
    async def execute(self, statement, *_a, **_k):
        if "lower(" in str(statement).lower():
            wanted = (self._next or "").lower()
            return _Result(next((r for r in self.rows
                                 if r.username.lower() == wanted), None))
        return _Result(next((r for r in self.rows
                             if r.role == "admin" and r.is_active), None))

    def add(self, row):
        # The real columns carry defaults that are applied on flush; without
        # them the identity assertions below would compare None to None.
        import uuid as _uuid
        if getattr(row, "user_scope_id", None) is None:
            row.user_scope_id = _uuid.uuid4()
        if getattr(row, "is_active", None) is None:
            row.is_active = True
        self.rows.append(row)

    async def commit(self):
        self.committed += 1

    async def refresh(self, _row):
        return None

    async def delete(self, row):
        self.deleted.append(row)
        if row in self.rows:
            self.rows.remove(row)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No real keyring, no real config, no real home."""
    saved = {}
    monkeypatch.setattr(user_admin, "hash_password", lambda p: f"hash:{p}")

    from vaf.core import config as config_module
    monkeypatch.setattr(config_module.Config, "load", lambda: dict(saved))
    monkeypatch.setattr(config_module.Config, "save", lambda c: saved.update(c))
    monkeypatch.setattr(config_module.Config, "APP_DIR", tmp_path)

    import vaf.cli.gate as gate
    monkeypatch.setattr(gate, "mirror_admin_password_hash", lambda h: saved.__setitem__("_mirror", h))
    monkeypatch.setattr(gate, "is_local_admin_account", lambda **kw: True)
    return saved


def _lookup(db, username):
    """The duplicate check reads a name the fake cannot see in the statement."""
    db._next = username
    return db


def test_duplicate_usernames_are_refused_regardless_of_case():
    db = FakeDb()
    asyncio.run(user_admin.create_local_user(
        _lookup(db, "Alice"), username="Alice", password="longenough1"))
    with pytest.raises(UserAdminError) as e:
        asyncio.run(user_admin.create_local_user(
            _lookup(db, "alice"), username="alice", password="longenough1"))
    assert e.value.code == "username_taken"
    assert e.value.http_status == 409


def test_the_password_rule_also_applies_to_ordinary_users():
    """The admin route had no rule at all - that was the divergence."""
    with pytest.raises(UserAdminError) as e:
        asyncio.run(user_admin.create_local_user(
            _lookup(FakeDb(), "bob"), username="bob", password="short", role="user"))
    assert e.value.code == "password_too_short"


def test_a_username_must_be_long_enough():
    with pytest.raises(UserAdminError) as e:
        asyncio.run(user_admin.create_local_user(
            _lookup(FakeDb(), "a"), username=" a ", password="longenough1"))
    assert e.value.code == "username_invalid"


def test_the_first_admin_requires_2fa_setup():
    """This flag is what walks the owner into 2FA enrollment on first web login."""
    user = asyncio.run(user_admin.create_first_admin(
        _lookup(FakeDb(), "mert"), username="mert", password="longenough1"))
    assert user.role == "admin"
    assert user.requires_2fa_setup is True


def test_a_second_first_admin_is_refused(_isolated):
    db = FakeDb()
    asyncio.run(user_admin.create_first_admin(
        _lookup(db, "mert"), username="mert", password="longenough1"))
    with pytest.raises(UserAdminError) as e:
        asyncio.run(user_admin.create_first_admin(
            _lookup(db, "eve"), username="eve", password="longenough1"))
    assert e.value.code == "admin_exists"
    assert e.value.http_status == 403


def test_the_admin_identity_is_written_to_the_config(_isolated):
    user = asyncio.run(user_admin.create_first_admin(
        _lookup(FakeDb(), "mert"), username="mert", password="longenough1"))
    assert _isolated["local_admin_username"] == "mert"
    assert _isolated["local_admin_scope_id"] == str(user.user_scope_id)


def test_an_unwritable_config_takes_the_account_back(monkeypatch, _isolated):
    """A machine with an admin whose identity was never recorded resolves every
    local caller to the legacy fallback scope - worse than having no account."""
    from vaf.core import config as config_module
    monkeypatch.setattr(config_module.Config, "save",
                        lambda c: (_ for _ in ()).throw(OSError("read-only")))
    db = FakeDb()
    with pytest.raises(UserAdminError) as e:
        asyncio.run(user_admin.create_first_admin(
            _lookup(db, "mert"), username="mert", password="longenough1"))
    assert e.value.code == "identity_not_written"
    assert db.deleted, "the account row must be removed again"
    assert asyncio.run(user_admin.active_admin(db)) is None


def test_a_failing_keyring_mirror_does_not_cost_the_account(monkeypatch):
    """The mirror is best-effort by design: a successful login writes it again."""
    import vaf.cli.gate as gate
    monkeypatch.setattr(gate, "mirror_admin_password_hash",
                        lambda h: (_ for _ in ()).throw(RuntimeError("no ring")))
    user = asyncio.run(user_admin.create_local_user(
        _lookup(FakeDb(), "mert"), username="mert", password="longenough1"))
    assert user.username == "mert"


def test_the_mirror_runs_after_the_row_exists(monkeypatch, _isolated):
    """Order matters: mirroring first could leave the terminal door holding a
    password for an account that was never created."""
    seen = []
    import vaf.cli.gate as gate
    monkeypatch.setattr(gate, "mirror_admin_password_hash",
                        lambda h: seen.append(("mirror", h)))
    db = FakeDb()
    original_commit = db.commit

    async def spy_commit():
        seen.append(("commit", None))
        await original_commit()
    db.commit = spy_commit

    asyncio.run(user_admin.create_local_user(
        _lookup(db, "mert"), username="mert", password="longenough1"))
    assert [s[0] for s in seen] == ["commit", "mirror"]


def test_the_agent_gets_the_name_it_was_given(_isolated, tmp_path):
    asyncio.run(user_admin.create_first_admin(
        _lookup(FakeDb(), "mert"), username="mert", password="longenough1",
        agent_name="Jarvis"))
    identity = json.loads((tmp_path / "users" / "mert" / "identity.json").read_text())
    assert identity["name"] == "Jarvis"


def test_without_a_name_the_workspace_still_exists(_isolated, tmp_path):
    """Bootstrap never created this directory at all - it made a different one
    under the platform data dir that nothing reads."""
    asyncio.run(user_admin.create_first_admin(
        _lookup(FakeDb(), "mert"), username="mert", password="longenough1"))
    identity_file = tmp_path / "users" / "mert" / "identity.json"
    assert identity_file.exists(), "the agent workspace must be prepared"
    assert json.loads(identity_file.read_text())["name"].startswith("Nobel")


def test_the_suggested_agent_name_has_the_familiar_shape():
    name = user_admin.suggest_agent_name()
    assert name.startswith("Nobel") and name[5:].lstrip("0123456789")


# --------------------------------------------------------------- route guards

def _source(path):
    from pathlib import Path
    return Path(path).read_text(encoding="utf-8")


def test_both_routes_create_accounts_through_the_shared_layer():
    """Mutation guard for the whole point of this module.

    A behavioural test cannot see a copy that was pasted back into a route, so
    the source is checked directly: both handlers must call in, and neither may
    grow its own hashing or duplicate check again.
    """
    auth = _source("vaf/api/auth_routes.py")
    users = _source("vaf/api/user_routes.py")

    assert "create_first_admin(" in auth
    assert "create_local_user(" in users
    for name, text in (("auth_routes", auth), ("user_routes", users)):
        assert "hash_password(body.password" not in text, f"{name} hashes on its own again"
        assert "func.lower(LocalUser.username) == username.lower()" not in text, (
            f"{name} grew its own duplicate check again")


def test_the_first_admin_always_reaches_the_terminal_door(monkeypatch, _isolated):
    """The owner's hash must be mirrored even when the account is not "admin".

    `create_local_user` asks `is_local_admin_account`, which reads the config -
    and while the FIRST admin is being created that config still names the
    default owner. An account called anything else failed that test, so the
    door kept believing no account existed while one had just been made.
    """
    mirrored = []
    import vaf.cli.gate as gate
    monkeypatch.setattr(gate, "is_local_admin_account", lambda **kw: False)
    monkeypatch.setattr(gate, "mirror_admin_password_hash", lambda h: mirrored.append(h))

    asyncio.run(user_admin.create_first_admin(
        _lookup(FakeDb(), "mert"), username="mert", password="longenough1"))
    assert mirrored == ["hash:longenough1"]
