# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Whare Wananga re-training queue (blue378604 audit, Fix 3).

Gate-failing records (stale/draft/declare/interrupted) used to rot silently.
The queue persists them for re-training with an attempt cap and cooldown; the
queue file lives OUTSIDE the record store dir (store.list_tools globs *.json
there and must never mistake the queue for a tool record).
"""
import time

import pytest

import vaf.core.platform as platform_mod
from vaf.whare_wananga import retrain, store


@pytest.fixture(autouse=True)
def ww_home(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_mod.Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    return tmp_path


def _save(tool, *, status="confirmed", challenge=True, mode="probe"):
    rec = store.new_record(tool, tool_schema_hash="h1")
    rec["status"] = status
    rec["challenge_passed"] = challenge
    rec["learn_mode"] = mode
    store.save(rec)
    return rec


# ── classification ───────────────────────────────────────────────────────────

def test_classify_all_states():
    assert retrain.classify(None) == "missing"
    assert retrain.classify(_save("a", status="stale")) == "stale"
    assert retrain.classify(_save("b", status="draft", challenge=False)) == "draft"
    assert retrain.classify(_save("c", status="learning", challenge=False)) == "interrupted"
    assert retrain.classify(_save("d", challenge=False, mode="declare")) == "declare"
    assert retrain.classify(_save("e", challenge=False)) == "challenge_failed"
    assert retrain.classify(_save("f")) == "verified"


# ── queue mechanics ──────────────────────────────────────────────────────────

def test_enqueue_dedups():
    _save("t1", status="stale")
    assert retrain.enqueue("t1", "stale") is True
    assert retrain.enqueue("t1", "stale") is False
    assert len(retrain.pending(all_entries=True, include_declare=True)) == 1


def test_queue_file_outside_store_dir(ww_home):
    _save("t2", status="stale")
    retrain.enqueue("t2", "stale")
    assert (ww_home / "whare_wananga_retrain.json").exists()
    assert "whare_wananga_retrain" not in store.list_tools(), (
        "queue file leaked into store.list_tools - it must live outside the store dir"
    )


def test_pending_excludes_declare_by_default():
    _save("t3", challenge=False, mode="declare")
    retrain.enqueue("t3", "declare")
    assert retrain.pending() == []
    assert [e["tool"] for e in retrain.pending(include_declare=True)] == ["t3"]


def test_pending_prunes_verified_and_missing():
    _save("t4", status="stale")
    retrain.enqueue("t4", "stale")
    _save("t4")  # becomes verified
    retrain.enqueue("t5", "stale")  # record never existed -> missing
    assert retrain.pending(all_entries=True, include_declare=True) == []


def test_attempt_cap_and_cooldown():
    _save("t6", status="stale")
    retrain.enqueue("t6", "stale")
    retrain.mark_attempt("t6")
    # cooldown active after an attempt
    assert retrain.pending() == []
    # age the attempt past the cooldown -> drainable again
    q = retrain._load_queue()
    q["t6"]["last_attempt_at"] = time.time() - retrain.COOLDOWN_SECONDS - 1
    retrain._save_queue(q)
    assert [e["tool"] for e in retrain.pending()] == ["t6"]
    # cap: attempts >= MAX_ATTEMPTS is never drainable
    q = retrain._load_queue()
    q["t6"]["attempts"] = retrain.MAX_ATTEMPTS
    retrain._save_queue(q)
    assert retrain.pending() == []


def test_scan_store_seeds_gate_failures_only():
    _save("rot1", status="stale")
    _save("rot2", status="draft", challenge=False)
    _save("fine")
    added = retrain.scan_store()
    names = {e["tool"] for e in retrain.pending(all_entries=True, include_declare=True)}
    assert added == 2
    assert names == {"rot1", "rot2"}


def test_invalidate_stale_enqueues():
    rec = _save("t7")
    class _T:
        name = "t7"
        description = "changed description"
        parameters = {"type": "object", "properties": {}}
    # hash mismatch flips to stale AND enqueues
    changed = store.invalidate_stale({"t7": _T()})
    assert changed == ["t7"]
    names = {e["tool"] for e in retrain.pending(all_entries=True, include_declare=True)}
    assert "t7" in names


# ── drain ────────────────────────────────────────────────────────────────────

def test_drain_one_trains_and_removes_when_verified(monkeypatch):
    _save("t8", status="stale")
    retrain.enqueue("t8", "stale")

    import vaf.whare_wananga.jobs as jobs
    calls = []

    def fake_start(agent, tool, **kw):
        calls.append(tool)
        _save(tool)  # training "succeeds": record becomes verified
        return {"tool": tool, "state": "done"}

    monkeypatch.setattr(jobs, "start_training", fake_start)
    monkeypatch.setattr(jobs, "is_running", lambda tool: False)
    monkeypatch.setattr(jobs, "get_status", lambda tool: {"tool": tool, "state": "done"})

    class _Agent:
        tools = {"t8": object()}

    out = retrain.drain_one(_Agent())
    assert calls == ["t8"]
    assert out is not None
    assert retrain.pending(all_entries=True, include_declare=True) == []


def test_drain_one_skips_unregistered_tools(monkeypatch):
    _save("t9", status="stale")
    retrain.enqueue("t9", "stale")

    class _Agent:
        tools = {}

    assert retrain.drain_one(_Agent()) is None
    # entry stays queued (tool may appear again after a restart)
    assert [e["tool"] for e in retrain.pending(all_entries=True, include_declare=True)] == ["t9"]
