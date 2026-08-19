# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Hybrid retrieval candidate depth (rag.py search()).

With RRF fusion enabled, the vector lane must feed the fusion the SAME
candidate depth as the lexical lane (max(k*4, 20)). It fed only k while the
lexical lane fed 20: a correct chunk at vector rank 6-15 never even reached
the fusion, which concentrated its damage on models with a narrow similarity
band (measured 2026-08-19: multilingual-e5-small hit@1 12/26 -> 18/26 on the
golden set once the depth was symmetric).

MUTATION: change the vector fetch back to .limit(k) and the hybrid test goes
red; drop the hybrid gate and the non-hybrid test goes red.
"""
import asyncio
from uuid import uuid4

import pytest


class _Result:
    def all(self):
        return []


class _CaptureSession:
    def __init__(self):
        self.limits = []

    async def execute(self, stmt, params=None):
        clause = getattr(stmt, "_limit_clause", None)
        value = getattr(clause, "value", None)
        self.limits.append(value)
        return _Result()


class _FakeEmbeddings:
    model_name = "intfloat/multilingual-e5-small"

    async def embed(self, text, *, prefix=None):
        return [0.1] * 384


class _Dummy:
    pass


def _run_search(monkeypatch, hybrid: bool, k: int = 5):
    from vaf.core.config import Config
    from vaf.memory.rag import RagPipeline

    cfg = {
        "memory_hybrid_enabled": hybrid,
        "debug_logs_enabled": False,
    }
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, key, default=None: cfg.get(key, default)))

    db = _CaptureSession()
    pipeline = RagPipeline(
        db, crypto=_Dummy(), embedding_service=_FakeEmbeddings(), chunker=_Dummy())
    result = asyncio.run(pipeline.search(
        "wer besitzt das patent", k=k, user_scope_id=uuid4()))
    assert result == []
    return db.limits


def test_hybrid_vector_lane_feeds_fusion_at_lexical_depth(monkeypatch):
    limits = _run_search(monkeypatch, hybrid=True, k=5)
    assert limits, "the vector select never ran"
    assert limits[0] == 20, \
        f"vector lane fed {limits[0]} candidates into RRF; the lexical lane feeds 20"


def test_non_hybrid_vector_lane_stays_at_k(monkeypatch):
    limits = _run_search(monkeypatch, hybrid=False, k=5)
    assert limits and limits[0] == 5
