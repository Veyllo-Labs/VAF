# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The sign-in doors in process, the way an attacker meets them.

The real `vaf.api.auth_routes` router (and the user admin routes) behind the real
`AuthMiddleware`, `IPValidationMiddleware` and `RateLimitMiddleware`, registered in the
order network mode registers them (vaf/core/web_server.py). The accounts and sessions are
`LocalUser` / `UserSession` on an in-memory SQLite behind a thin async wrapper, so the
seal's mapper hook and every query run as they do against PostgreSQL. A fixed JWT secret
and seal key; nothing reaches the keyring or a real database.

A "LAN client" is what the backend sees behind the integrated proxy: the peer 127.0.0.1
with `X-Forwarded-For: <address>`. `client(ip)` sends that header on every request.

Not a test module: tests import it (`import auth_harness`), like ftp_stub.
"""
from __future__ import annotations

import contextlib
from typing import Any, Dict, List, Optional, Tuple

import pyotp
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from vaf.auth.models import LocalUser, UserSession


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kw):
    return "JSON"


JWT_SECRET = "harness-jwt-secret-" + "x" * 32
PROXY_PEER = ("127.0.0.1", 40000)


class AsyncSessionShim:
    """The AsyncSession surface the auth code uses, over a sync Session."""

    def __init__(self, session: Session):
        self._s = session

    async def execute(self, *a, **k):
        return self._s.execute(*a, **k)

    async def commit(self):
        self._s.commit()

    async def rollback(self):
        self._s.rollback()

    async def refresh(self, obj, *a, **k):
        self._s.refresh(obj)

    async def delete(self, obj):
        self._s.delete(obj)

    async def flush(self):
        self._s.flush()

    async def get(self, *a, **k):
        return self._s.get(*a, **k)

    def add(self, obj):
        self._s.add(obj)

    async def close(self):
        self._s.close()


class Harness:
    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        LocalUser.__table__.create(self.engine)
        UserSession.__table__.create(self.engine)
        self.events: List[Tuple[str, Dict[str, Any]]] = []
        engine = self.engine

        @contextlib.asynccontextmanager
        async def _db(*_a, **_k):
            with Session(engine, expire_on_commit=False, autoflush=False) as s:
                shim = AsyncSessionShim(s)
                try:
                    yield shim
                    s.commit()
                except Exception:
                    s.rollback()
                    raise

        import vaf.api.auth_routes as auth_routes
        import vaf.api.user_routes as user_routes
        import vaf.auth.database as database
        from vaf.auth import account_seal, crypto, permissions, rate_limit
        import vaf.core.security_events as security_events

        monkeypatch.setattr(database, "get_auth_db", _db)
        monkeypatch.setattr(auth_routes, "get_auth_db", _db)
        monkeypatch.setattr(user_routes, "get_auth_db", _db)
        monkeypatch.setattr(crypto, "_get_jwt_secret", lambda: JWT_SECRET)
        monkeypatch.setattr(account_seal, "_key_cache", b"k" * 32)
        monkeypatch.setattr(account_seal, "_enforced_cache", True)
        # The terminal door's hash copy lives in the keyring; never touched from a test.
        import vaf.cli.gate as gate
        monkeypatch.setattr(gate, "mirror_admin_password_hash", lambda *_a, **_k: None)
        monkeypatch.setattr(security_events, "log_security_event",
                            lambda kind, **f: self.events.append((kind, f)))
        for tracker in self._trackers(rate_limit):
            monkeypatch.setattr(tracker, "_attempts", {})
            for name in ("_horizon", "_announced", "_closed_until", "_closures"):
                if hasattr(tracker, name):
                    monkeypatch.setattr(tracker, name, {})
        from vaf.auth import second_factor
        monkeypatch.setattr(second_factor, "_last_step", {})
        permissions.invalidate_permissions_cache()
        with contextlib.suppress(Exception):
            from vaf.auth import sessions
            sessions.forget_cached_sessions()

    @staticmethod
    def _trackers(rate_limit):
        return [v for v in vars(rate_limit).values() if isinstance(v, rate_limit._AttemptTracker)]

    # ── accounts ────────────────────────────────────────────────────────────

    def add_user(self, username: str, password: str, *, role: str = "user", totp: bool = False,
                 active: bool = True) -> Tuple[str, Optional[str]]:
        """An account; with `totp` a second factor that is set up. Returns (id, totp secret)."""
        from vaf.auth.crypto import encrypt_totp_secret, hash_password
        secret = pyotp.random_base32() if totp else None
        with Session(self.engine) as s:
            user = LocalUser(username=username, password_hash=hash_password(password), role=role,
                             is_active=active, requires_2fa_setup=not totp)
            if secret:
                ct, nonce = encrypt_totp_secret(secret)
                user.totp_secret, user.totp_nonce = ct, nonce
            s.add(user)
            s.commit()
            return str(user.id), secret

    def user(self, username: str) -> LocalUser:
        s = Session(self.engine, expire_on_commit=False)
        from sqlalchemy import func, select
        return s.execute(select(LocalUser).where(func.lower(LocalUser.username)
                                                 == username.lower())).scalar_one()

    def sessions(self) -> List[UserSession]:
        from sqlalchemy import select
        with Session(self.engine, expire_on_commit=False) as s:
            return list(s.execute(select(UserSession)).scalars().all())

    # ── the app ─────────────────────────────────────────────────────────────

    def app(self, extra_routes=()):
        from fastapi import FastAPI, Request

        from vaf.api.auth_routes import router as auth_router
        from vaf.api.user_routes import router as user_router
        from vaf.auth.middleware import AuthMiddleware, IPValidationMiddleware
        from vaf.auth.rate_limit import RateLimitMiddleware

        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(user_router)

        async def _probe(request: Request):
            return {"user": getattr(request.state, "user", None)}

        app.add_api_route("/api/probe", _probe, methods=["GET"])
        for path, endpoint, methods in extra_routes:
            app.add_api_route(path, endpoint, methods=methods)
        # web_server.py order: the LAST added is outermost -> Auth, IP check, rate limit.
        app.add_middleware(RateLimitMiddleware)
        app.add_middleware(IPValidationMiddleware)
        app.add_middleware(AuthMiddleware)
        return app

    def client(self, ip: Optional[str] = "192.168.1.50", app=None):
        """A client at `ip` behind the integrated proxy; None is the machine itself."""
        from starlette.testclient import TestClient
        c = TestClient(app or self.app(), client=PROXY_PEER)
        if ip:
            c.headers["X-Forwarded-For"] = ip
        return c

    # ── flows ───────────────────────────────────────────────────────────────

    @staticmethod
    def login(c, username: str, password: str):
        return c.post("/api/auth/login", json={"username": username, "password": password})

    @staticmethod
    def verify(c, temp_token: str, code: str):
        return c.post("/api/auth/verify-2fa", json={"temp_token": temp_token, "code": code})

    def sign_in(self, c, username: str, password: str, secret: Optional[str]) -> str:
        """A completed sign-in; returns the access token."""
        r = self.login(c, username, password)
        assert r.status_code == 200, r.text
        data = r.json()
        if not data.get("requires_2fa"):
            return data["access_token"]
        r = self.verify(c, data["temp_token"], pyotp.TOTP(secret).now())
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def kinds(self, kind: str) -> List[Dict[str, Any]]:
        return [f for k, f in self.events if k == kind]


def code_at(secret: str, steps: int = 0) -> str:
    """The code of the step `steps` away from now."""
    import time
    totp = pyotp.TOTP(secret)
    return totp.at(int(time.time()) + steps * totp.interval)


def wrong_code(secret: str) -> str:
    """A six-digit code that is none of the three a verifier accepts now."""
    valid = {code_at(secret, s) for s in (-1, 0, 1)}
    for n in range(1_000_000):
        candidate = f"{(n * 7919) % 1_000_000:06d}"
        if candidate not in valid:
            return candidate
    raise AssertionError("unreachable")
