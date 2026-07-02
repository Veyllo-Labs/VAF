# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Startup-race regression tests: the auth DB init and /api/auth/needs-setup must tolerate
PostgreSQL still booting (the Docker stack starts in a thread parallel to the web server).

Pre-fix behavior on a fresh Windows (Rancher/WSL2) install: init_auth_db lost the race and gave
up after one attempt, needs-setup then 500'd on the missing local_users table, and the login page
treated the error as "no setup needed" - the user saw a login form with no account to log in to.

Hermetic: a tiny FastAPI app with only the auth router, get_auth_db monkeypatched. No database,
no Docker, no web_server import.
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.testclient import TestClient

import vaf.api.auth_routes as auth_routes
import vaf.auth.database as auth_db
from vaf.api.auth_routes import _is_db_not_ready_error


# --- _is_db_not_ready_error classification -------------------------------------------------

def test_connection_errors_are_not_ready():
    assert _is_db_not_ready_error(ConnectionRefusedError("refused"))  # OSError subclass
    assert _is_db_not_ready_error(Exception("connection was refused"))
    assert _is_db_not_ready_error(Exception("the database system is starting up"))
    assert _is_db_not_ready_error(Exception("the database system is in recovery mode"))
    assert _is_db_not_ready_error(Exception("FATAL: sorry, too many clients already"))


def test_missing_auth_tables_are_not_ready():
    """The exact race window: Postgres is up but the background init has not created the
    tables yet. asyncpg surfaces this as UndefinedTableError / 'relation ... does not exist'."""
    assert _is_db_not_ready_error(Exception('relation "local_users" does not exist'))
    assert _is_db_not_ready_error(Exception("asyncpg.exceptions.UndefinedTableError: ..."))


def test_real_errors_are_not_classified_not_ready():
    assert not _is_db_not_ready_error(Exception("syntax error at or near SELECT"))
    assert not _is_db_not_ready_error(Exception("permission denied for table local_users"))


def test_permanent_misconfig_is_not_classified_not_ready():
    """Broken DSN/credentials must fail loudly (500), not look like a slow boot the login
    page then polls against forever with a 'starting the database' spinner."""
    import socket

    assert not _is_db_not_ready_error(socket.gaierror(-2, "Name or service not known"))
    assert not _is_db_not_ready_error(Exception('role "vaf" does not exist'))
    assert not _is_db_not_ready_error(Exception('database "vaf_memory" does not exist'))
    assert not _is_db_not_ready_error(Exception('password authentication failed for user "vaf"'))
    assert not _is_db_not_ready_error(Exception('no pg_hba.conf entry for host "10.0.0.5"'))


# --- /api/auth/needs-setup ------------------------------------------------------------------

def _client(monkeypatch, db_ctx) -> TestClient:
    monkeypatch.setattr(auth_routes, "get_auth_db", db_ctx)
    app = FastAPI()
    app.include_router(auth_routes.router)  # router already carries the /api/auth prefix
    return TestClient(app, raise_server_exceptions=False)


def _failing_db(exc: Exception):
    @asynccontextmanager
    async def ctx(*a, **k):
        raise exc
        yield  # pragma: no cover - makes this a generator

    return ctx


class _NoAdminResult:
    def scalar_one_or_none(self):
        return None


def _fresh_db():
    class _Db:
        async def execute(self, *_a, **_k):
            return _NoAdminResult()

    @asynccontextmanager
    async def ctx(*a, **k):
        yield _Db()

    return ctx


def test_needs_setup_returns_503_while_db_boots(monkeypatch):
    """Connection refused (Postgres container still starting) -> 503, NOT 500. The login page
    keeps polling on 503; on 500 it silently fell back to the login form."""
    # Skip the endpoint's internal retry pauses; the retry loop itself is exercised.
    async def _no_sleep(_secs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    c = _client(monkeypatch, _failing_db(ConnectionRefusedError("connect call failed")))
    r = c.get("/api/auth/needs-setup")
    assert r.status_code == 503


def test_needs_setup_returns_503_while_tables_missing(monkeypatch):
    async def _no_sleep(_secs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    c = _client(monkeypatch, _failing_db(Exception('relation "local_users" does not exist')))
    r = c.get("/api/auth/needs-setup")
    assert r.status_code == 503


def test_needs_setup_real_error_still_500s(monkeypatch):
    """Non-boot errors must NOT be masked as 503 - they should surface loudly."""
    c = _client(monkeypatch, _failing_db(Exception("permission denied for table local_users")))
    r = c.get("/api/auth/needs-setup")
    assert r.status_code == 500


def test_needs_setup_true_on_fresh_db(monkeypatch):
    c = _client(monkeypatch, _fresh_db())
    r = c.get("/api/auth/needs-setup")
    assert r.status_code == 200
    assert r.json() == {"needs_setup": True}


# --- init_auth_db_with_retry ----------------------------------------------------------------

class _FastSleepAsyncio:
    """Stand-in for the asyncio module inside vaf.auth.database: near-instant sleeps so the
    retry tests run fast while keeping the real retry/branching logic."""

    @staticmethod
    async def sleep(_secs):
        await asyncio.sleep(0)


def test_retry_succeeds_once_db_becomes_ready(monkeypatch):
    calls = {"n": 0}

    async def _flaky_init():
        calls["n"] += 1
        if calls["n"] < 4:
            raise ConnectionRefusedError("the database system is starting up")

    monkeypatch.setattr(auth_db, "init_auth_db", _flaky_init)
    monkeypatch.setattr(auth_db, "asyncio", _FastSleepAsyncio)
    assert asyncio.run(auth_db.init_auth_db_with_retry(max_wait_seconds=30)) is True
    assert calls["n"] == 4


def test_retry_continues_past_budget_and_still_heals(monkeypatch):
    """The budget only escalates logging - a DB that appears AFTER it must still heal the
    install without a process restart (the frontend polls needs-setup unbounded too)."""
    calls = {"n": 0}

    async def _very_late_db():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionRefusedError("connect call failed")

    monkeypatch.setattr(auth_db, "init_auth_db", _very_late_db)
    monkeypatch.setattr(auth_db, "asyncio", _FastSleepAsyncio)
    # Budget of 0 is exhausted before the first retry - the loop must keep going anyway.
    assert asyncio.run(auth_db.init_auth_db_with_retry(max_wait_seconds=0.0)) is True
    assert calls["n"] == 3
