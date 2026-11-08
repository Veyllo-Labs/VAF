# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The account directory remembers a store it could not reach (vaf/auth/permissions.py).

The directory is asked on hot paths: every send to a Veyllo App device asks whether its
account still stands. On Windows a connect to a closed local port is refused only after
about two seconds, so a directory that asked the store again on every call made the
app-link test files take 20 to 150 minutes on the Windows CI. These tests fake the store
and the clock; they need no database and run the same on every platform."""
import contextlib

import pytest

from vaf.auth import permissions


class _Store:
    def __init__(self):
        self.calls = 0
        self.down = True

    @contextlib.asynccontextmanager
    async def session(self):
        self.calls += 1
        if self.down:
            raise ConnectionRefusedError("store down")
        yield _Session()


class _Session:
    async def execute(self, _query):
        return _Result()


class _Result:
    def scalars(self):
        return self

    def all(self):
        return []


@pytest.fixture
def store(monkeypatch):
    import vaf.auth.database as auth_db
    fake = _Store()
    monkeypatch.setattr(auth_db, "get_auth_db", fake.session)
    clock = {"t": 1000.0}
    monkeypatch.setattr(permissions.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(permissions, "_store_url", lambda: "postgresql://test-store")
    monkeypatch.setattr(permissions, "_store_down", {})
    fake.clock = clock
    return fake


def test_an_unreachable_store_is_not_asked_again_for_a_while(store):
    assert permissions.list_accounts() == []
    assert store.calls == 1
    # MUTATION: drop the down check in list_accounts and every call asks the store again.
    for _ in range(5):
        assert permissions.list_accounts() == []
    assert store.calls == 1
    store.clock["t"] += permissions._STORE_DOWN_S + 0.1
    assert permissions.list_accounts() == []
    assert store.calls == 2


def test_a_store_that_is_back_is_asked_every_time(store):
    permissions.list_accounts()
    store.down = False
    store.clock["t"] += permissions._STORE_DOWN_S + 0.1
    assert permissions.list_accounts() == []
    assert permissions.list_accounts() == []
    assert store.calls == 3
    assert permissions._store_down == {}


def test_the_outage_is_kept_per_database(store, monkeypatch):
    permissions.list_accounts()
    monkeypatch.setattr(permissions, "_store_url", lambda: "sqlite:///another-store")
    permissions.list_accounts()
    assert store.calls == 2


class _Row:
    def scalar_one_or_none(self):
        return None


def test_the_permission_row_remembers_the_outage_too(store, monkeypatch):
    """Every tool call reads the account's permission row; an unreachable store still
    raises (the callers decide what that means), but without a new connect each time."""
    for _ in range(3):
        with pytest.raises(Exception):
            permissions._fetch_permissions_row("ab12cd34-0000-4000-8000-000000000001")
    assert store.calls == 1
    store.down = False
    monkeypatch.setattr(_Session, "execute", lambda self, q: _async(_Row()))
    store.clock["t"] += permissions._STORE_DOWN_S + 0.1
    assert permissions._fetch_permissions_row("ab12cd34-0000-4000-8000-000000000001") \
        is permissions._NO_ACCOUNT
    assert store.calls == 2 and permissions._store_down == {}


async def _async(value):
    return value


def test_an_invalidation_forgets_the_outage(store):
    """The admin save that invalidates the caches reached the store; a test or a route that
    invalidates must not be answered from an outage another caller remembered.
    MUTATION: drop the clear in invalidate_permissions_cache - red."""
    permissions.list_accounts()
    assert permissions._store_down
    permissions.invalidate_permissions_cache()
    assert permissions._store_down == {}
