# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The synchronous database probe never borrows the cached main-loop engine.

check_db_connection_sync runs each call on a fresh event loop. An asyncpg
connection is bound to the loop that opened it, so a probe that reached for the
pooled main-thread engine answered True once and False from the second call on
("attached to a different loop"). `vaf repair` probes three times per run and
restarts what does not answer, so a healthy memory database was restarted
under a running VAF (live incident). The probe now makes a NullPool engine of
its own per call and disposes it.
"""
from contextlib import asynccontextmanager

import vaf.memory.database as db_mod


class _FakeEngine:
    instances = []

    def __init__(self, url, **kwargs):
        self.url, self.kwargs, self.disposed = url, kwargs, False
        _FakeEngine.instances.append(self)

    @asynccontextmanager
    async def connect(self):
        class _Conn:
            async def execute(self, _stmt):
                class _Res:
                    @staticmethod
                    def scalar():
                        return 1
                return _Res()
        yield _Conn()

    async def dispose(self):
        self.disposed = True


def test_every_probe_makes_and_disposes_its_own_nullpool_engine(monkeypatch):
    _FakeEngine.instances.clear()
    monkeypatch.setattr(db_mod, "create_async_engine", _FakeEngine)
    monkeypatch.setattr(db_mod, "get_database_url", lambda: "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setattr(db_mod, "_main_engine", None)
    assert db_mod.check_db_connection_sync(timeout_seconds=2) is True
    assert db_mod.check_db_connection_sync(timeout_seconds=2) is True
    assert len(_FakeEngine.instances) == 2
    assert all(e.kwargs.get("poolclass") is db_mod.NullPool and e.disposed for e in _FakeEngine.instances)
    # the cached main-thread engine is never created, let alone bound to a probe loop
    assert db_mod._main_engine is None
