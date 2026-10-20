# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Password work: equal for every name, and never on the event loop.

Measured before:
- an unknown name answered before any Argon2 work (median 3.5 ms against 28.6 ms for a known
  one), so the account names could be listed by timing the answer;
- Argon2 ran on the event loop: 40 concurrent wrong logins delayed an unrelated request by
  about a second (idle: 1 ms), freezing every chat stream and socket of the instance.
"""
import ast
import threading
from pathlib import Path

import pytest

from auth_harness import Harness

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


@pytest.fixture
def argon2_threads(monkeypatch):
    """The thread every Argon2 call runs on, and how many verifications ran."""
    from vaf.auth import crypto
    real = crypto._ph
    seen = {"verify": [], "hash": []}

    class _Recording:
        def verify(self, hashed, password):
            seen["verify"].append(threading.current_thread().name)
            return real.verify(hashed, password)

        def hash(self, password):
            seen["hash"].append(threading.current_thread().name)
            return real.hash(password)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(crypto, "_ph", _Recording())
    return seen


def test_unknown_names_cost_one_hash(h, argon2_threads):
    """MUTATION: skip the verification for an unknown name - red."""
    h.add_user("alice", "alice-password-1")
    c = h.client("192.168.10.1")
    argon2_threads["verify"].clear()
    argon2_threads["hash"].clear()
    unknown = h.login(c, "mallory", "some-password")
    assert len(argon2_threads["verify"]) == 1
    # Not even the first unknown name pays for a hash on top (the dummy is a fixed hash).
    assert argon2_threads["hash"] == []
    argon2_threads["verify"].clear()
    known = h.login(h.client("192.168.10.2"), "alice", "some-password")
    assert len(argon2_threads["verify"]) == 1
    assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())


def test_argon2_never_runs_on_the_loop(h, argon2_threads, monkeypatch):
    """Login, the password change and the first admin's creation all hash on the Argon2 pool.
    MUTATION: call verify_password directly in the login route - red."""
    from vaf.auth import account_seal, user_admin
    from vaf.core.config import Config
    saved = {}
    monkeypatch.setattr(account_seal, "admin_established", lambda: False)
    monkeypatch.setattr(account_seal, "mark_admin_established", lambda: None)
    monkeypatch.setattr(Config, "load", staticmethod(lambda *a, **k: dict(saved)))
    monkeypatch.setattr(Config, "save", staticmethod(lambda c, *a, **k: saved.update(c)))
    monkeypatch.setattr(user_admin, "_prepare_user_home", lambda *a, **k: None)

    local = h.client(None)
    argon2_threads["verify"].clear()
    argon2_threads["hash"].clear()
    r = local.post("/api/auth/bootstrap", json={"username": "owner", "password": "owner-password-1"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    assert h.login(local, "owner", "owner-password-1").status_code == 200
    r = local.post("/api/auth/change-password", headers={"Authorization": f"Bearer {token}"},
                   json={"current_password": "owner-password-1", "new_password": "owner-password-2"})
    assert r.status_code == 200, r.text
    names = argon2_threads["verify"] + argon2_threads["hash"]
    assert len(argon2_threads["hash"]) == 2 and len(argon2_threads["verify"]) >= 3
    assert all(n.startswith("vaf-argon2") for n in names), names


def _sync_password_calls_in_async_defs(source: str) -> list:
    hits = []

    def walk(node, in_async):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.Lambda)):
                walk(child, False)
                continue
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child, True)
                continue
            if in_async and isinstance(child, ast.Call):
                fn = child.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if name in ("verify_password", "hash_password"):
                    hits.append((child.lineno, name))
            walk(child, in_async)

    walk(ast.parse(source), False)
    return hits


def test_no_sync_password_work_in_async_routes():
    """The structural half: no blocking Argon2 call inside an async def of the routes or the
    auth layer (the terminal's sync functions stay sync). MUTATION: put one back - red."""
    offenders = []
    for path in sorted([*(ROOT / "vaf" / "api").glob("*.py"), *(ROOT / "vaf" / "auth").glob("*.py")]):
        for line, name in _sync_password_calls_in_async_defs(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT).as_posix()}:{line} {name}(")
    assert not offenders, offenders
    # The guard's floor: it sees the shape it forbids.
    assert _sync_password_calls_in_async_defs("async def f():\n    verify_password(a, b)\n")
    assert not _sync_password_calls_in_async_defs("def f():\n    verify_password(a, b)\n")


def test_the_dummy_hash_costs_what_a_real_one_does():
    """The fixed dummy must carry _ph's parameters, or an unknown name answers faster again.
    MUTATION: change a parameter of _ph or of the dummy - red."""
    from vaf.auth import crypto
    assert crypto._ph.check_needs_rehash(crypto._DUMMY_HASH) is False
    assert crypto._verify_or_dummy(None, "anything") is False
