# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The learn ledger: durable batch progress that makes resume idempotent.

The RAG ingest has no dedup (every ingest mints a fresh UUID), so a re-run
without a ledger duplicates every stored section. Pinned here: one file per
(document, user), atomic writes, write-after-commit ordering surfaced as API
(`record_batch`), the resume cut line (`section_count`), boundary coercion on
load, per-user separation, and the retention sweep. The batch-aware ingest
params (`section_offset`, `update_root`) are pinned at the bottom - defaults
must stay byte-identical for the existing single-call consumers.
"""
import asyncio
import os
import time

import pytest

from vaf.core.learn_ledger import (
    LEDGER_RETENTION_DAYS,
    LearnBatch,
    LearnLedger,
    file_sha256,
    sweep_stale_ledgers,
)


@pytest.fixture(autouse=True)
def _isolated_vaf_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("vaf.core.platform.Platform.vaf_dir",
                        staticmethod(lambda: str(tmp_path / ".vaf")))
    return tmp_path


def _ledger(**kw):
    base = dict(doc_tag="doc-tora", source_path="/x/tora.pdf",
                content_sha256="abc123", user_scope_id="deadbeef-0000",
                session_id="s1", total_pages=1000, batch_pages=10,
                total_batches=100)
    base.update(kw)
    return LearnLedger(**base)


def test_record_batch_after_commit_advances_the_cut_line():
    led = _ledger()
    led.record_batch(LearnBatch(index=0, first_page=1, pages=10, section_start=0, sections=11))
    led.record_batch(LearnBatch(index=1, first_page=11, pages=10, section_start=11, sections=9))
    assert led.section_count == 20
    assert led.done_batch_indices() == {0, 1}

    # Reload from disk: the record is durable and coerced
    again = LearnLedger.load("doc-tora", "deadbeef-0000")
    assert again is not None
    assert again.section_count == 20
    assert again.done_batch_indices() == {0, 1}
    assert again.batches[1].first_page == 11


def test_ledgers_are_separated_per_user():
    """Two tenants learning the same title must never share a resume state."""
    a = _ledger(user_scope_id="deadbeef-0000")
    b = _ledger(user_scope_id="cafe1234-0000", section_count=5)
    a.save()
    b.save()
    assert LearnLedger.path_for("doc-tora", "deadbeef-0000") != \
        LearnLedger.path_for("doc-tora", "cafe1234-0000")
    assert LearnLedger.load("doc-tora", "deadbeef-0000").section_count == 0
    assert LearnLedger.load("doc-tora", "cafe1234-0000").section_count == 5


def test_write_is_atomic_tmp_rename():
    led = _ledger()
    led.save()
    target = LearnLedger.path_for("doc-tora", "deadbeef-0000")
    assert target.exists()
    assert not target.with_suffix(".json.tmp").exists(), "tmp file left behind"


def test_load_survives_garbage_and_absence():
    assert LearnLedger.load("doc-none", "deadbeef-0000") is None
    p = LearnLedger.path_for("doc-broken", "deadbeef-0000")
    p.write_text("{not json", encoding="utf-8")
    assert LearnLedger.load("doc-broken", "deadbeef-0000") is None


def test_completed_job_unlinks_its_ledger():
    led = _ledger()
    led.save()
    led.delete()
    assert LearnLedger.load("doc-tora", "deadbeef-0000") is None


def test_sweep_removes_only_stale_ledgers():
    fresh = _ledger(doc_tag="doc-fresh")
    fresh.save()
    stale = _ledger(doc_tag="doc-stale", status="stopped")
    stale.save()
    old = time.time() - (LEDGER_RETENTION_DAYS + 1) * 86400
    p = LearnLedger.path_for("doc-stale", "deadbeef-0000")
    os.utime(p, (old, old))
    removed = sweep_stale_ledgers()
    assert removed == 1
    assert LearnLedger.load("doc-fresh", "deadbeef-0000") is not None
    assert LearnLedger.load("doc-stale", "deadbeef-0000") is None


def test_file_sha256_streams(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"a" * (2 * 1024 * 1024 + 17))
    import hashlib
    assert file_sha256(f) == hashlib.sha256(b"a" * (2 * 1024 * 1024 + 17)).hexdigest()


# ---------------------------------------------------------------------------
# Batch-aware ingest params (section_offset / update_root)
# ---------------------------------------------------------------------------

def test_section_offset_keeps_indices_globally_monotonic(monkeypatch):
    """Per-batch indices restarting at 0 would make crash orphans
    indistinguishable from batch 1 - the offset is the resume cut line."""
    from vaf.tools import learn_document as ld

    stored = []

    class _FakePipeline:
        def __init__(self, db):
            pass

        async def ingest(self, content, metadata=None, user_scope_id=None, auto_connect=True):
            stored.append(metadata)

    monkeypatch.setattr("vaf.memory.rag.RagPipeline", _FakePipeline)
    monkeypatch.setattr(ld, "_contextualize_section_llm",
                        lambda text, title, doc, fn: "ctx")
    monkeypatch.setattr(ld, "_summarize_doc_from_contexts",
                        lambda contexts, title, fn: ("sum", []))

    md = "## A\n" + ("wort " * 200) + "\n\n## B\n" + ("wort " * 200)
    res = asyncio.run(ld.ingest_document_knowledge(
        db=None, content_markdown=md, doc_title="T", doc_tag="doc-t",
        source="learn_document", mem_type="document", generate_fn=lambda p: "x",
        user_scope_id=None, section_offset=37, update_root=False,
    ))
    assert res["created"] == 2
    assert [m["section_index"] for m in stored] == [37, 38]
    # update_root=False: no document_index row was written
    assert all(m.get("type") != "document_index" for m in stored)


def test_defaults_preserve_single_call_behavior(monkeypatch):
    """learn_attached_knowledge and the sync lane call without the new params -
    indices start at 0 and the root upsert still runs."""
    from vaf.tools import learn_document as ld

    stored = []

    class _FakePipeline:
        def __init__(self, db):
            pass

        async def ingest(self, content, metadata=None, user_scope_id=None, auto_connect=True):
            stored.append(metadata)

    class _FakeResult:
        def scalar_one_or_none(self):
            return None

    class _FakeDb:
        async def execute(self, *a, **kw):
            return _FakeResult()

    monkeypatch.setattr("vaf.memory.rag.RagPipeline", _FakePipeline)
    monkeypatch.setattr(ld, "_contextualize_section_llm",
                        lambda text, title, doc, fn: "ctx")
    monkeypatch.setattr(ld, "_summarize_doc_from_contexts",
                        lambda contexts, title, fn: ("sum", []))

    md = "## A\n" + ("wort " * 200) + "\n\n## B\n" + ("wort " * 200)
    res = asyncio.run(ld.ingest_document_knowledge(
        db=_FakeDb(), content_markdown=md, doc_title="T", doc_tag="doc-t",
        source="learn_document", mem_type="document", generate_fn=lambda p: "x",
        user_scope_id=None,
    ))
    assert res["created"] == 2
    assert [m["section_index"] for m in stored if "section_index" in m] == [0, 1]
    assert any(m.get("type") == "document_index" for m in stored), \
        "the default path lost its root upsert"
