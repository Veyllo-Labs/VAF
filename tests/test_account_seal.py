# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The account seal (vaf/auth/account_seal.py): a row changed in the database alone grants
no login and no authority.

Whoever could write `local_users` could set their own password hash and
`requires_2fa_setup`, sign in and enrol a fresh second factor. The writes are exercised
for real: LocalUser on an in-memory SQLite (JSONB rendered as JSON), so the mapper hook
that seals every ORM write runs exactly as it does against PostgreSQL. A "tamper" is a raw
SQL UPDATE, which is what a database-only attacker can do.
"""
import contextlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from vaf.auth import account_seal
from vaf.auth.models import LocalUser, UserSession


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture
def sealed(monkeypatch):
    """A fixed seal key, and the seal enforced (this installation was adopted)."""
    monkeypatch.setattr(account_seal, "_key_cache", b"k" * 32)
    monkeypatch.setattr(account_seal, "_enforced_cache", True)
    yield
    account_seal.reset_caches()


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    LocalUser.__table__.create(eng)
    UserSession.__table__.create(eng)
    return eng


def _add(engine, **kw):
    with Session(engine) as s:
        user = LocalUser(username=kw.pop("username", "alice"), password_hash=kw.pop("password_hash", "h1"),
                         role=kw.pop("role", "user"), **kw)
        s.add(user)
        s.commit()
        return user.id


def _load(engine, user_id):
    s = Session(engine)
    return s, s.get(LocalUser, user_id)


def _raw(engine, sql, **params):
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def test_a_written_row_is_sealed_and_verifies_after_reload(sealed, engine):
    """The hook seals the INSERT with the defaults the INSERT itself applies (id, scope,
    is_active, permissions). MUTATION: seal before materializing the defaults - red."""
    uid = _add(engine)
    s, user = _load(engine, uid)
    with s:
        assert user.seal and user.seal.startswith("v1:")
        assert user.user_scope_id is not None and user.is_active is True
        assert account_seal.is_sealed(user)


@pytest.mark.parametrize("column,value", [
    ("password_hash", "'attacker-hash'"),
    ("role", "'admin'"),
    ("requires_2fa_setup", "1"),
    ("is_active", "0"),
    ("permissions", "'{}'"),
    ("username", "'mallory'"),
    ("user_scope_id", "'00000000-0000-0000-0000-000000000001'"),
])
def test_a_row_changed_in_the_database_alone_is_refused(sealed, engine, column, value):
    """Each sealed column, changed by raw SQL: the row no longer verifies.
    MUTATION: drop the column from SEALED_COLUMNS - red for that one."""
    uid = _add(engine, permissions={"tools": ["read_file"]}, requires_2fa_setup=False)
    _raw(engine, f"UPDATE local_users SET {column} = {value}")
    s, user = _load(engine, uid)
    with s:
        assert not account_seal.is_sealed(user)


def test_a_seal_nulled_in_the_database_is_a_broken_seal(sealed, engine):
    uid = _add(engine)
    _raw(engine, "UPDATE local_users SET seal = NULL")
    s, user = _load(engine, uid)
    with s:
        assert not account_seal.is_sealed(user)


def test_a_totp_secret_copied_from_another_row_breaks_the_seal(sealed, engine):
    """The TOTP ciphertext carries no row binding of its own; the seal does."""
    a = _add(engine, username="alice", totp_secret=b"secret-a", totp_nonce=b"nonce-a")
    b = _add(engine, username="bob")
    _raw(engine, "UPDATE local_users SET totp_secret = :s, totp_nonce = :n WHERE id = :b",
         s=b"secret-a", n=b"nonce-a", b=str(b).replace("-", ""))
    s, bob = _load(engine, b)
    with s:
        assert bob.totp_secret == b"secret-a"
        assert not account_seal.is_sealed(bob)
    s2, alice = _load(engine, a)
    with s2:
        assert account_seal.is_sealed(alice)


def test_a_tampered_row_is_frozen_and_a_good_one_re_seals(sealed, engine):
    """A harmless write (a login time, an admin's edit) must not launder a row changed
    outside VAF. MUTATION: skip the old-seal check in the update hook - red."""
    good = _add(engine, username="alice")
    s, user = _load(engine, good)
    with s:
        user.role = "admin"
        s.commit()
        assert account_seal.is_sealed(user)

    bad = _add(engine, username="bob")
    _raw(engine, "UPDATE local_users SET role = 'admin' WHERE username = 'bob'")
    s, user = _load(engine, bad)
    with s:
        from datetime import datetime
        user.last_login = datetime.utcnow()
        with pytest.raises(account_seal.AccountSealBroken):
            s.commit()


def test_only_a_trusted_rewrite_re_establishes_a_row(sealed, engine):
    bad = _add(engine, username="bob")
    _raw(engine, "UPDATE local_users SET role = 'admin'")
    s, user = _load(engine, bad)
    with s, account_seal.trusted_rewrite():
        user.role = "user"
        s.commit()
        assert account_seal.is_sealed(user)


def test_before_adoption_nothing_is_enforced(monkeypatch, engine):
    """An installation that has not sealed its rows yet keeps working: the seal is only
    enforced once the keyring records the adoption."""
    monkeypatch.setattr(account_seal, "_key_cache", b"k" * 32)
    monkeypatch.setattr(account_seal, "_enforced_cache", False)
    monkeypatch.setattr(account_seal, "enforced", lambda: False)
    uid = _add(engine)
    _raw(engine, "UPDATE local_users SET seal = NULL, role = 'admin'")
    s, user = _load(engine, uid)
    with s:
        assert account_seal.is_sealed(user)
        user.role = "user"
        s.commit()                      # no freeze before adoption
    account_seal.reset_caches()


def test_adoption_seals_every_row_once_and_records_it_outside_the_database(monkeypatch):
    """MUTATION: record the adoption before sealing, or not at all - red."""
    rows = [SimpleNamespace(id=uuid.uuid4(), username="alice", password_hash="h", role="admin",
                            totp_secret=None, totp_nonce=None, requires_2fa_setup=False,
                            is_active=True, user_scope_id=uuid.uuid4(), permissions={}, seal=None),
            SimpleNamespace(id=uuid.uuid4(), username="bob", password_hash="h2", role="user",
                            totp_secret=None, totp_nonce=None, requires_2fa_setup=True,
                            is_active=True, user_scope_id=uuid.uuid4(), permissions={}, seal=None)]
    ring = {}
    monkeypatch.setattr(account_seal, "_key_cache", b"k" * 32)
    monkeypatch.setattr(account_seal, "_enforced_cache", False)
    import vaf.core.data_keyring as data_keyring
    monkeypatch.setattr(data_keyring, "peek_data_secret", lambda name: ring.get(name, ""))
    monkeypatch.setattr(data_keyring, "set_data_secret", lambda name, value: ring.__setitem__(name, value))

    class _Db:
        async def execute(self, _q):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

        async def commit(self):
            pass

    @contextlib.asynccontextmanager
    async def _get_db():
        yield _Db()

    import vaf.auth.database as database
    monkeypatch.setattr(database, "get_auth_db", _get_db)
    import asyncio
    assert asyncio.run(account_seal.adopt_once()) == 2
    assert ring == {"account_seal_adopted": "1", "admin_established": "1"}
    assert all(account_seal.is_sealed(r) for r in rows)
    assert asyncio.run(account_seal.adopt_once()) == 0      # once
    account_seal.reset_caches()


# ── the readers ────────────────────────────────────────────────────────────────

def _broken_user(password="correct horse battery staple"):
    from vaf.auth.crypto import hash_password
    return SimpleNamespace(id=uuid.uuid4(), username="alice", password_hash=hash_password(password),
                           role="admin", totp_secret=None, totp_nonce=None, requires_2fa_setup=False,
                           is_active=True, user_scope_id=uuid.uuid4(), permissions={}, seal="v1:forged",
                           last_login=None)


def test_a_login_on_a_row_changed_outside_vaf_is_refused_after_the_password(sealed, monkeypatch):
    """The attacker's own hash passes the password check; the seal does not, and neither the
    terminal door's hash copy nor a token is handed out. MUTATION: drop the check - red."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    import vaf.api.auth_routes as auth_routes
    import vaf.cli.gate as gate
    user = _broken_user()
    mirrored = []
    monkeypatch.setattr(gate, "mirror_admin_password_hash", lambda h: mirrored.append(h))

    class _Db:
        async def execute(self, *_a, **_k):
            # first(): the login's lookup reads the id and the hash; the row serves as both.
            return SimpleNamespace(scalar_one_or_none=lambda: user, first=lambda: user)

        async def commit(self):
            pass

    @contextlib.asynccontextmanager
    async def _get_db(*_a, **_k):
        yield _Db()

    monkeypatch.setattr(auth_routes, "get_auth_db", _get_db)
    app = FastAPI()
    app.include_router(auth_routes.router)
    c = TestClient(app, raise_server_exceptions=False)
    wrong = c.post("/api/auth/login", json={"username": "alice", "password": "nope"})
    assert wrong.status_code == 401                     # a stranger learns nothing
    r = c.post("/api/auth/login", json={"username": "alice", "password": "correct horse battery staple"})
    assert r.status_code == 403 and "changed outside VAF" in r.json()["detail"]
    assert mirrored == [] and "temp_token" not in r.text and "access_token" not in r.text


def test_a_token_whose_account_was_changed_outside_vaf_no_longer_stands(sealed, monkeypatch):
    """MUTATION: drop the check in account_standing_async - red."""
    import asyncio

    import vaf.auth.database as database
    from vaf.auth import permissions
    user = _broken_user()

    class _Db:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(scalar_one_or_none=lambda: user)

    @contextlib.asynccontextmanager
    async def _get_db(*_a, **_k):
        yield _Db()

    monkeypatch.setattr(database, "get_auth_db", _get_db)
    permissions.invalidate_permissions_cache()
    standing = asyncio.run(permissions.account_standing_async(str(user.user_scope_id)))
    assert standing == ("tampered", "")
    assert not permissions.token_still_stands(standing, "admin")
    permissions.invalidate_permissions_cache()


def test_permissions_of_a_row_changed_outside_vaf_grant_nothing(sealed, monkeypatch):
    """An empty tool list means unrestricted; set in the database alone it must not.
    MUTATION: drop the check in _fetch_permissions_row - red (None, unrestricted)."""
    import vaf.auth.database as database
    from vaf.auth import permissions
    user = _broken_user()
    user.role = "user"

    class _Db:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(scalar_one_or_none=lambda: user)

    @contextlib.asynccontextmanager
    async def _get_db(*_a, **_k):
        yield _Db()

    monkeypatch.setattr(database, "get_auth_db", _get_db)
    permissions.invalidate_permissions_cache()
    assert permissions.resolve_allowed_tools(str(user.user_scope_id)) == frozenset()
    permissions.invalidate_permissions_cache()


def test_an_installation_that_had_an_admin_is_never_a_fresh_install_again(monkeypatch):
    """Deleting or demoting every admin row reopened the web setup to the next caller.
    MUTATION: drop the established check in create_first_admin or needs-setup - red."""
    import asyncio

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    import vaf.api.auth_routes as auth_routes
    from vaf.auth import user_admin
    monkeypatch.setattr(account_seal, "admin_established", lambda: True)

    class _Db:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    with pytest.raises(user_admin.UserAdminError) as err:
        asyncio.run(user_admin.create_first_admin(_Db(), username="mallory", password="x" * 12))
    assert err.value.code == "admin_established" and err.value.http_status == 403

    @contextlib.asynccontextmanager
    async def _get_db(*_a, **_k):
        yield _Db()

    monkeypatch.setattr(auth_routes, "get_auth_db", _get_db)
    app = FastAPI()
    app.include_router(auth_routes.router)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/api/auth/needs-setup").json() == {"needs_setup": False}
    assert c.post("/api/auth/test-veyllo-key", json={"api_key": "k"}).status_code == 403


def test_the_auth_init_adds_the_seal_column_before_it_seals(monkeypatch):
    """In the tray the web server runs off the main thread, where get_engine skips the schema
    reconcile: measured live, the seal column was missing after a start, the adoption failed and
    every query on the model would have. init_auth_db reconciles the columns itself, first.
    MUTATION: drop that reconcile - red."""
    import asyncio

    import vaf.auth.account_seal as seal_mod
    import vaf.auth.database as auth_db
    import vaf.memory.database as mem_db
    order = []

    class _Conn:
        async def run_sync(self, _fn):
            order.append("create_all")

    class _Engine:
        def begin(self):
            @contextlib.asynccontextmanager
            async def _ctx():
                yield _Conn()
            return _ctx()

        async def dispose(self):
            pass

    async def _engine():
        return _Engine()

    async def _reconcile(_conn):
        order.append("columns")
        return {}

    async def _adopt():
        order.append("adopt")
        return 0

    monkeypatch.setattr(auth_db, "get_engine", _engine)
    monkeypatch.setattr(mem_db, "get_owner_engine", _engine)
    monkeypatch.setattr(mem_db, "_reconcile_columns", _reconcile)
    monkeypatch.setattr(seal_mod, "adopt_once", _adopt)
    asyncio.run(auth_db.init_auth_db())
    assert order == ["create_all", "columns", "adopt"]


def test_a_keyring_that_cannot_be_read_is_not_taken_as_adopted(monkeypatch):
    """Review finding: adopt_once asked enforced(), which counted a failed keyring read as
    adopted and remembered it, so a passing failure at the first start skipped the adoption
    and left every unsealed row refused for the life of the process.
    MUTATION: ask enforced() again in adopt_once, or remember the failure in enforced() - red."""
    import asyncio

    import vaf.core.data_keyring as data_keyring
    monkeypatch.setattr(account_seal, "_enforced_cache", False)
    answers = [RuntimeError("keyring locked"), ""]

    def _peek(_name):
        a = answers.pop(0) if answers else ""
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(data_keyring, "peek_data_secret", _peek)
    with pytest.raises(RuntimeError):
        asyncio.run(account_seal.adopt_once())
    assert account_seal._enforced_cache is False

    answers[:] = [RuntimeError("keyring locked"), ""]
    assert account_seal.enforced() is True          # this call fails closed ...
    assert account_seal.enforced() is False         # ... and the next one reads again
    account_seal.reset_caches()

