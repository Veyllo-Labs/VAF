# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The rekey maintenance loop (vaf/memory/rekey.py) + `vaf memory rekey`.

Pinned against its failure modes:
- rows the current key opens are SKIPPED (idempotent re-runs),
- old-key rows are decrypted, re-encrypted, verified, and both memory columns
  (encrypted_content + nonce) replaced together,
- rows neither key opens are counted and NEVER overwritten,
- dry-run writes nothing (no UPDATE, no cache-file unlink, no cache clear),
- an RLS-restricted role aborts loudly instead of rekeying a fraction,
- the CLI exits non-zero on a missing/keyless backup file.
"""
import base64
import secrets
from contextlib import asynccontextmanager

import pytest

from vaf.memory.crypto import FIELD_PREFIX, MemoryCrypto
from vaf.memory.rekey import rekey_store


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar


class _FakeSession:
    def __init__(self, mem_rows, chunk_rows, owner_ok=True):
        self.mem_rows = mem_rows
        self.chunk_rows = chunk_rows
        self.owner_ok = owner_ok
        self.mem_updates = []
        self.chunk_updates = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "pg_roles" in sql:
            return _Result(scalar=self.owner_ok)
        if sql.startswith("UPDATE memories"):
            self.mem_updates.append(params)
            return _Result()
        if sql.startswith("UPDATE chunks"):
            self.chunk_updates.append(params)
            return _Result()
        if "FROM memories" in sql:
            return _Result(rows=self.mem_rows)
        if "FROM chunks" in sql:
            return _Result(rows=self.chunk_rows)
        raise AssertionError(f"unexpected SQL: {sql}")

    async def commit(self):
        self.commits += 1


class _FakeCache:
    cleared = 0

    async def clear_all(self):
        _FakeCache.cleared += 1
        return True


@pytest.fixture
def rig(monkeypatch, tmp_path):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key_old = secrets.token_bytes(32)
    key_cur = secrets.token_bytes(32)
    cur = MemoryCrypto(key=key_cur)
    old = AESGCM(key_old)

    import vaf.memory.crypto as cryptomod
    monkeypatch.setattr(cryptomod, "_crypto_instance", cur)

    from vaf.core.config import Config
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    cache_dir = tmp_path / "user_profile_cache"
    cache_dir.mkdir()
    (cache_dir / "scope1.txt").write_bytes(b"VAFENC1:old")

    def old_field(plaintext: str) -> str:
        nonce = secrets.token_bytes(12)
        ct = old.encrypt(nonce, plaintext.encode(), None)
        return (FIELD_PREFIX + base64.b64encode(nonce).decode()
                + ":" + base64.b64encode(ct).decode())

    ct_cur, nonce_cur = cur.encrypt("schon aktuell")
    nonce_old = secrets.token_bytes(12)
    mem_rows = [
        ("m-old", old.encrypt(nonce_old, b"alte erinnerung", None), nonce_old),
        ("m-cur", ct_cur, nonce_cur),
        ("m-bad", b"garbage-bytes-here", secrets.token_bytes(12)),
    ]
    chunk_rows = [
        ("c-old", old_field("alter chunk")),
        ("c-cur", cryptomod.encrypt_field("aktueller chunk")),
        ("c-bad", FIELD_PREFIX + "AAAA:BBBB"),
    ]
    session = _FakeSession(mem_rows, chunk_rows)

    @asynccontextmanager
    async def fake_owner_db():
        yield session

    import vaf.memory.database as dbmod
    monkeypatch.setattr(dbmod, "get_owner_db", fake_owner_db)

    import vaf.memory.cache as cachemod
    _FakeCache.cleared = 0
    monkeypatch.setattr(cachemod, "get_cache", lambda: _FakeCache())

    return {"session": session, "cur": cur, "key_old": key_old,
            "cache_dir": cache_dir, "tmp": tmp_path}


def _b64(key: bytes) -> str:
    return base64.b64encode(key).decode()


def test_rekey_rewrites_old_skips_current_never_touches_unreadable(rig):
    import asyncio
    report = asyncio.run(rekey_store(_b64(rig["key_old"])))

    assert (report.memories.rekeyed, report.memories.skipped_current,
            report.memories.failed) == (1, 1, 1)
    assert (report.chunks.rekeyed, report.chunks.skipped_current,
            report.chunks.failed) == (1, 1, 1)

    s = rig["session"]
    assert len(s.mem_updates) == 1 and s.mem_updates[0]["i"] == "m-old"
    # both columns replaced together, and the rewrite opens with the CURRENT key
    assert rig["cur"].decrypt(s.mem_updates[0]["c"], s.mem_updates[0]["n"]) == "alte erinnerung"
    assert len(s.chunk_updates) == 1 and s.chunk_updates[0]["i"] == "c-old"
    import vaf.memory.crypto as cryptomod
    assert cryptomod.decrypt_field(s.chunk_updates[0]["t"]) == "alter chunk"
    # unreadable rows were counted, named, and never written
    assert "memory:m-bad" in report.failed_ids and "chunk:c-bad" in report.failed_ids
    # profile cache deleted, redis cleared
    assert report.profile_cache_deleted == 1
    assert not any(rig["cache_dir"].iterdir())
    assert report.caches_cleared and _FakeCache.cleared == 1


def test_dry_run_writes_nothing(rig):
    import asyncio
    report = asyncio.run(rekey_store(_b64(rig["key_old"]), dry_run=True))
    s = rig["session"]
    assert report.memories.rekeyed == 1 and report.chunks.rekeyed == 1  # counted
    assert s.mem_updates == [] and s.chunk_updates == []               # not written
    assert any(rig["cache_dir"].iterdir()), "dry-run must keep the profile cache"
    assert _FakeCache.cleared == 0
    assert report.dry_run and "dry-run" in " ".join(report.lines())


def test_restricted_role_aborts_loudly(rig):
    import asyncio
    rig["session"].owner_ok = False
    with pytest.raises(RuntimeError, match="RLS-restricted"):
        asyncio.run(rekey_store(_b64(rig["key_old"])))
    assert rig["session"].mem_updates == []


def test_invalid_old_key_is_refused(rig):
    import asyncio
    with pytest.raises(RuntimeError, match="Base64"):
        asyncio.run(rekey_store("not-base64!!"))
    with pytest.raises(RuntimeError, match="32"):
        asyncio.run(rekey_store(_b64(b"short")))


def test_cli_exits_nonzero_without_a_usable_backup(tmp_path):
    from typer.testing import CliRunner

    from vaf.cli.cmd.memory import app

    runner = CliRunner()
    res = runner.invoke(app, ["rekey", "--old-key-file", str(tmp_path / "nope.json")])
    assert res.exit_code == 1
    keyless = tmp_path / "keyless.json"
    keyless.write_text("{}", encoding="utf-8")
    res = runner.invoke(app, ["rekey", "--old-key-file", str(keyless)])
    assert res.exit_code == 1
