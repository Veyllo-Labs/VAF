# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The re-embed maintenance loop (vaf/memory/reembed.py) and its startup hook.

Pinned against its failure modes:
- rows stamped with another model are re-embedded (passage side), re-stamped,
  and written with an explicit vector CAST; rows already on the target are
  never touched,
- undecryptable rows are stamped 'unreadable', their vectors NEVER overwritten,
  and they cannot wedge the pending count,
- a killed run resumes at the stamps with no double work,
- dry-run writes nothing,
- an RLS-restricted role aborts loudly instead of migrating a fraction,
- the startup hook's decision matrix: custom model untouched, fresh/finished
  stores flip the config, a DEFAULTS-flip ahead of the store pins the legacy
  model first, foreign stamps abort, an unreachable DB releases the latch,
- the embedding cache key separates models (same text, two models, two keys),
- migration v4 backfills only unstamped vector-carrying rows,
- the attachment query lane passes the query prefix.
"""
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from vaf.memory.crypto import MemoryCrypto, encrypt_field

TARGET = "intfloat/multilingual-e5-small"
LEGACY = "all-MiniLM-L6-v2"


class _Result:
    def __init__(self, rows=None, scalar=None, scalars=None):
        self._rows = rows or []
        self._scalar = scalar
        self._scalars = scalars or []

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar

    def scalars(self):
        outer = self

        class _S:
            def all(self):
                return outer._scalars

        return _S()


class _FakeStore:
    """Stateful stand-in for the two tables, honoring the keyset queries the
    job actually issues, so pagination/resume semantics are exercised for real."""

    def __init__(self, mem_rows, chunk_rows, owner_ok=True, fail_after_updates=None):
        # rows: id -> dict(stamp=..., payload=...); ids sort as strings
        self.mem = mem_rows
        self.chunks = chunk_rows
        self.owner_ok = owner_ok
        self.updates = []          # (table, id, has_vector)
        self.commits = 0
        self.fail_after_updates = fail_after_updates

    def _pending(self, table, params):
        rows = self.mem if table == "memories" else self.chunks
        return [
            (rid, r) for rid, r in sorted(rows.items())
            if r["stamp"] != params.get("t")
            and ("unreadable" not in params or r["stamp"] != params["unreadable"])
        ]

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        params = params or {}
        if "pg_roles" in sql:
            return _Result(scalar=self.owner_ok)
        if sql.startswith("SELECT count(*)"):
            table = "memories" if "FROM memories" in sql else "chunks"
            return _Result(scalar=len(self._pending(table, params)))
        if sql.startswith("SELECT DISTINCT embedding_model"):
            rows = self.mem if "FROM memories" in sql else self.chunks
            return _Result(scalars=sorted({r["stamp"] for r in rows.values() if r["stamp"]}))
        if sql.startswith("UPDATE"):
            table = "memories" if "UPDATE memories" in sql else "chunks"
            rows = self.mem if table == "memories" else self.chunks
            rows[params["i"]]["stamp"] = params["s"]
            self.updates.append((table, params["i"], "e" in params))
            if self.fail_after_updates is not None \
                    and len(self.updates) >= self.fail_after_updates:
                raise RuntimeError("simulated crash mid-run")
            return _Result()
        if sql.startswith("SELECT id,"):
            table = "memories" if "FROM memories" in sql else "chunks"
            pending = self._pending(table, params)
            if "last" in params:
                pending = [(rid, r) for rid, r in pending if rid > params["last"]]
            pending = pending[: params["lim"]]
            if table == "memories":
                return _Result(rows=[
                    (rid, r["ct"], r["nonce"], r.get("meta")) for rid, r in pending])
            return _Result(rows=[(rid, r["text"]) for rid, r in pending])
        raise AssertionError(f"unexpected SQL: {sql}")

    async def commit(self):
        self.commits += 1


class _FakeService:
    def __init__(self, model_name=None):
        self.model_name = model_name
        self.calls = []

    def get_dimension(self):
        return 3

    def embed_sync(self, text, *, prefix=None):
        self.calls.append(prefix)
        return [0.1, 0.2, 0.3]


class _FakeCache:
    cleared = 0

    async def clear_all(self):
        _FakeCache.cleared += 1
        return True


def _mem_row(crypto, content, stamp, tags=None):
    ct, nonce = crypto.encrypt(content)
    import json
    return {"stamp": stamp, "ct": ct, "nonce": nonce,
            "meta": json.dumps({"tags": tags or []})}


@pytest.fixture
def rig(monkeypatch, tmp_path):
    crypto = MemoryCrypto(key=secrets.token_bytes(32))
    import vaf.memory.crypto as cryptomod
    monkeypatch.setattr(cryptomod, "_crypto_instance", crypto)

    import vaf.memory.embeddings as embmod
    monkeypatch.setattr(embmod, "EmbeddingService", _FakeService)

    import vaf.memory.cache as cachemod
    monkeypatch.setattr(cachemod, "get_cache", lambda: _FakeCache())
    _FakeCache.cleared = 0

    def build(mem_rows, chunk_rows, **kw):
        store = _FakeStore(mem_rows, chunk_rows, **kw)

        @asynccontextmanager
        async def fake_owner_db():
            yield store

        import vaf.memory.database as dbmod
        monkeypatch.setattr(dbmod, "get_owner_db", fake_owner_db)
        return store

    return crypto, build


def test_reembed_stamps_updates_and_spares_target_rows(rig, tmp_path):
    crypto, build = rig
    store = build(
        {
            "m1": _mem_row(crypto, "alte notiz", LEGACY, ["tag1"]),
            "m2": _mem_row(crypto, "schon migriert", TARGET),
            "m3": {"stamp": LEGACY, "ct": b"garbage", "nonce": b"x" * 12, "meta": None},
        },
        {
            "c1": {"stamp": LEGACY, "text": encrypt_field("alter chunk")},
            "c2": {"stamp": TARGET, "text": encrypt_field("neuer chunk")},
        },
    )
    from vaf.memory.reembed import reembed_store
    import asyncio
    report = asyncio.run(reembed_store(TARGET, status_file=tmp_path / "s.json"))

    assert report.memories.reembedded == 1
    assert report.chunks.reembedded == 1
    assert report.memories.unreadable == 1
    assert report.pending_after == 0
    # target-stamped rows never touched
    touched = {rid for _, rid, _ in store.updates}
    assert "m2" not in touched and "c2" not in touched
    # the unreadable row was stamped WITHOUT a vector write
    unreadable_updates = [u for u in store.updates if u[1] == "m3"]
    assert unreadable_updates == [("memories", "m3", False)]
    assert store.mem["m3"]["stamp"] == "unreadable"
    # re-embedded rows carry the target stamp and a vector write
    assert store.mem["m1"]["stamp"] == TARGET
    assert ("chunks", "c1", True) in store.updates
    # complete run clears the caches (old-model vectors are garbage)
    assert _FakeCache.cleared == 1


def test_reembed_uses_passage_prefix(rig, tmp_path):
    crypto, build = rig
    build({"m1": _mem_row(crypto, "inhalt", LEGACY)}, {})
    import vaf.memory.embeddings as embmod
    seen = []

    class _Recorder(_FakeService):
        def embed_sync(self, text, *, prefix=None):
            seen.append(prefix)
            return super().embed_sync(text, prefix=prefix)

    import unittest.mock as mock
    with mock.patch.object(embmod, "EmbeddingService", _Recorder):
        from vaf.memory.reembed import reembed_store
        import asyncio
        asyncio.run(reembed_store(TARGET))
    assert seen and set(seen) == {"passage"}


def test_reembed_resume_after_crash_no_double_work(rig):
    crypto, build = rig
    rows = {f"m{i}": _mem_row(crypto, f"notiz {i}", LEGACY) for i in range(6)}
    store = build(dict(rows), {}, fail_after_updates=3)

    from vaf.memory.reembed import reembed_store
    import asyncio
    with pytest.raises(Exception):
        asyncio.run(reembed_store(TARGET))
    first_run = list(store.updates)
    assert len(first_run) == 3

    # second run over the SAME store state: only the not-yet-stamped rows
    store.fail_after_updates = None
    report = asyncio.run(reembed_store(TARGET))
    second_run = store.updates[len(first_run):]
    assert report.pending_after == 0
    all_ids = [rid for _, rid, _ in store.updates]
    assert sorted(all_ids) == sorted(set(all_ids)), "a row was re-embedded twice"
    assert len(all_ids) == 6


def test_reembed_dry_run_writes_nothing(rig):
    crypto, build = rig
    store = build({"m1": _mem_row(crypto, "notiz", LEGACY)},
                  {"c1": {"stamp": LEGACY, "text": encrypt_field("chunk")}})
    from vaf.memory.reembed import reembed_store
    import asyncio
    report = asyncio.run(reembed_store(TARGET, dry_run=True))
    assert store.updates == []
    assert store.commits == 0
    assert report.dry_run is True
    assert report.memories.reembedded == 1  # counted as would-re-embed
    assert _FakeCache.cleared == 0


def test_reembed_rls_restricted_role_aborts(rig):
    crypto, build = rig
    store = build({"m1": _mem_row(crypto, "notiz", LEGACY)}, {}, owner_ok=False)
    from vaf.memory.reembed import reembed_store
    import asyncio
    with pytest.raises(RuntimeError, match="RLS-restricted"):
        asyncio.run(reembed_store(TARGET))
    assert store.updates == []


# ── startup hook decision matrix ─────────────────────────────────────────────

class _HookRig:
    def __init__(self, monkeypatch, config_model, pending, foreign=()):
        import vaf.memory.reembed as re_mod
        self.mod = re_mod
        self.config = {"memory_embedding_model": config_model}
        self.flips = []
        self.spawns = []

        from vaf.core.config import Config
        monkeypatch.setattr(Config, "get", classmethod(
            lambda cls, key, default=None: self.config.get(key, default)))
        monkeypatch.setattr(Config, "set", classmethod(
            lambda cls, key, value: self.config.__setitem__(key, value)))
        monkeypatch.setattr(re_mod, "_count_pending_standalone", lambda: pending if not isinstance(pending, dict) else dict(pending, foreign_stamps=list(foreign)))
        monkeypatch.setattr(re_mod, "_flip_to_target", lambda: self.flips.append(True))
        monkeypatch.setattr(re_mod, "_spawn_worker", lambda: self.spawns.append(True) or None)
        import vaf.memory.embeddings as embmod
        monkeypatch.setattr(embmod, "reset_embedding_service", lambda: None)


def test_hook_custom_model_untouched(monkeypatch):
    rig = _HookRig(monkeypatch, "some/custom-model", {"memories": 5, "chunks": 5})
    rig.mod._hook_worker()
    assert rig.flips == [] and rig.spawns == []


def test_hook_fresh_store_flips_config(monkeypatch):
    rig = _HookRig(monkeypatch, LEGACY, {"memories": 0, "chunks": 0})
    rig.mod._hook_worker()
    assert rig.flips == [True] and rig.spawns == []


def test_hook_migrated_store_and_target_config_is_noop(monkeypatch):
    rig = _HookRig(monkeypatch, TARGET, {"memories": 0, "chunks": 0})
    rig.mod._hook_worker()
    assert rig.flips == [] and rig.spawns == []


def test_hook_defaults_flip_ahead_of_store_pins_legacy(monkeypatch):
    rig = _HookRig(monkeypatch, TARGET, {"memories": 3, "chunks": 0})
    rig.mod._hook_worker()
    assert rig.config["memory_embedding_model"] == LEGACY, \
        "queries would run in a mixed vector space"
    assert rig.spawns  # and the migration was started


def test_hook_pending_rows_spawn_worker(monkeypatch):
    rig = _HookRig(monkeypatch, LEGACY, {"memories": 3, "chunks": 2})
    rig.mod._hook_worker()
    assert rig.spawns and rig.flips == []


def test_hook_foreign_stamps_abort(monkeypatch):
    rig = _HookRig(monkeypatch, LEGACY, {"memories": 3, "chunks": 0},
                   foreign=("their/model",))
    rig.mod._hook_worker()
    assert rig.flips == [] and rig.spawns == []


def test_hook_unreachable_db_releases_latch(monkeypatch):
    import vaf.memory.reembed as re_mod
    rig = _HookRig(monkeypatch, LEGACY, None)
    re_mod._hook_done = True
    rig.mod._hook_worker()
    assert re_mod._hook_done is False  # next call retries


# ── slice-1 guards ───────────────────────────────────────────────────────────

def test_embedding_cache_key_separates_models():
    from vaf.memory.embeddings import EmbeddingService
    a = EmbeddingService(model_name=LEGACY)
    b = EmbeddingService(model_name=TARGET)
    assert a._get_cache_key("gleicher text") != b._get_cache_key("gleicher text")


def test_memory_summary_text_is_content_head_plus_tags():
    from vaf.memory.rag import _memory_summary_text
    out = _memory_summary_text("x" * 300, ["a", "b"])
    assert out == "x" * 256 + " a b"


def test_v4_backfill_guards():
    """v4 must add the column itself (ordered migrations run before the
    generic reconcile) and only stamp unstamped rows that carry a vector."""
    import inspect
    from vaf.memory import db_migrations
    src = inspect.getsource(db_migrations._v4_stamp_embedding_model)
    assert "ADD COLUMN IF NOT EXISTS embedding_model" in src
    assert "embedding_model IS NULL" in src
    assert "embedding IS NOT NULL" in src
    assert (4, db_migrations._v4_stamp_embedding_model) in db_migrations.DB_MIGRATIONS


def test_every_start_lane_runs_the_embedding_model_reconcile():
    """Modeled on the at-rest parity guard: a hook wired into some start lanes
    is the defect this repository has shipped before (a cleanup once ran for
    the terminal and never for the web app), and it is invisible from inside
    any single lane. Textual on purpose - what goes wrong is a missing call
    site, not a broken call.

    MUTATION: delete the ensure_embedding_model_current() call from any lane
    and this goes red, naming the lane.
    """
    root = Path(__file__).resolve().parent.parent
    lanes = {
        "modern / classic CLI (vaf run)": root / "vaf/cli/cmd/run.py",
        "the default TUI app lane": root / "vaf/cli/tui_app/agent_bridge.py",
        "web and tray startup": root / "vaf/core/web_server.py",
    }
    needle = "reembed import ensure_embedding_model_current"
    missing = [name for name, path in lanes.items()
               if needle not in path.read_text(encoding="utf-8")]
    assert not missing, \
        f"these start lanes never reconcile the embedding model: {missing}"

    # The classic CLI lane lives in the same file as the modern one; make sure
    # BOTH call sites exist there (the modern block and the classic block).
    assert (root / "vaf/cli/cmd/run.py").read_text(encoding="utf-8").count(needle) >= 2


def test_attachment_query_lane_passes_query_prefix():
    """The hierarchical attachment lane compares its query vector against
    passage-prefixed rows; without the query prefix, E5 retrieval degrades
    silently (the gap this round closed)."""
    src = Path("vaf/memory/attachment_rag.py").read_text(encoding="utf-8")
    assert 'embed(q, prefix="query")' in src
