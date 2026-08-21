# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Whare Wananga delivery-gate split (tool-friction audit, Fix 3).

The strict quality gate silenced 18 of 67 live records COMPLETELY - including
document_writer, whose stored pitfalls held exactly the knowledge that would
have prevented the incident. The gate is now split: the proactive A-track
(schema injection) stays strictly gated, the reactive B-track (a call just
failed) may deliver gate-failing records too, clearly tagged UNVERIFIED. Every
gate reject is enqueued for re-training instead of rotting silently.

Hermetic: the store dir and the retrain queue both derive from Platform.vaf_dir,
pointed at a pytest tmp dir; the delivery lru_cache is keyed by file mtime, but
same-second writes can collide, so each test uses its own tool name.
"""
import pytest

import vaf.core.platform as platform_mod
from vaf.whare_wananga import delivery, retrain, store


@pytest.fixture(autouse=True)
def ww_home(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_mod.Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    delivery._load_classified.cache_clear()
    return tmp_path


def _save(tool, *, status="confirmed", challenge=True, mode="probe",
          pitfalls=("Field 'x' is required.",), procedure=("Pass x as a string.",)):
    rec = store.new_record(tool, tool_schema_hash="h1")
    rec["status"] = status
    rec["challenge_passed"] = challenge
    rec["learn_mode"] = mode
    rec["tuatea"]["pitfalls"] = [{"text": p, "source": "whare_wananga", "seen": 1} for p in pitfalls]
    rec["tuarua"]["procedure"] = list(procedure)
    store.save(rec)
    return rec


# ── A-track stays strictly gated ─────────────────────────────────────────────

def test_a_track_delivers_verified_record():
    _save("t_ok")
    assert "Field 'x' is required." in (delivery.tool_pitfalls("t_ok") or "")


def test_a_track_never_delivers_declare_record():
    _save("t_declare", challenge=False, mode="declare")
    assert delivery.tool_pitfalls("t_declare") is None


def test_a_track_never_delivers_stale_record():
    _save("t_stale", status="stale")
    assert delivery.tool_pitfalls("t_stale") is None


# ── B-track: relaxed, tagged ─────────────────────────────────────────────────

def test_b_track_default_stays_gated():
    _save("t_declare2", challenge=False, mode="declare")
    assert delivery.tool_knowhow("t_declare2") is None


def test_b_track_allow_unverified_delivers_declare_tagged():
    _save("t_declare3", challenge=False, mode="declare")
    kh = delivery.tool_knowhow("t_declare3", allow_unverified=True)
    assert kh is not None
    assert "UNVERIFIED" in kh and "declare" in kh
    assert "Field 'x' is required." in kh


def test_b_track_allow_unverified_delivers_stale_and_draft():
    _save("t_stale2", status="stale")
    _save("t_draft", status="draft", challenge=False)
    assert "UNVERIFIED" in (delivery.tool_knowhow("t_stale2", allow_unverified=True) or "")
    assert "UNVERIFIED" in (delivery.tool_knowhow("t_draft", allow_unverified=True) or "")


def test_b_track_verified_record_is_untagged():
    _save("t_ok2")
    kh = delivery.tool_knowhow("t_ok2", allow_unverified=True)
    assert kh is not None and "UNVERIFIED" not in kh


def test_vacuous_pitfalls_still_filtered_in_relaxed_mode():
    _save("t_vac", challenge=False, mode="declare",
          pitfalls=("No probe attempts were provided.",), procedure=())
    assert delivery.tool_knowhow("t_vac", allow_unverified=True) is None


def test_known_pitfall_hit_relaxed():
    _save("t_match", challenge=False, mode="declare",
          pitfalls=("Error: missing required field 'filename' must be provided.",))
    err = "Tool Error: 'filename' is a required field and must be provided"
    assert delivery.known_pitfall_hit("t_match", err) is False           # strict: gated out
    assert delivery.known_pitfall_hit("t_match", err, allow_unverified=True) is True


# ── The live miss: dispatcher wrapper + argument-contract wording ────────────

def test_known_pitfall_hit_sees_through_the_dispatcher_wrapper():
    """The live pair (2026-08-21): read_file failed and the stored pitfall
    stated exactly that trap in other words - but eleven of the error's sixteen
    tokens were VAF's OWN dispatcher wrapping (the invalid-arguments prefix and
    the retry hint), so token overlap scored 0.31 and the hit was logged as a
    novel surprise while the record's first pitfall described it."""
    _save("t_wrap", pitfalls=(
        "Using 'file_path' as the argument name instead of 'path' causes a "
        "validation error: 'path is a required property'",
    ))
    err = ("Tool Error: invalid arguments for 't_wrap': missing required field: "
           "'path'. Retry the SAME call and include it.")
    assert delivery.known_pitfall_hit("t_wrap", err) is True


def test_arg_contract_branch_needs_the_required_signal():
    """The new branch must not turn every quoted token into a match: an error
    naming an argument WITHOUT the required/missing signal stays with the
    overlap rule (here: too little overlap, no match)."""
    _save("t_quoted", pitfalls=(
        "The 'path' argument is required and must name an existing file.",))
    err = "Tool Error: something entirely else broke around 'path' handling today"
    assert delivery.known_pitfall_hit("t_quoted", err) is False


def test_relearn_gate_accepts_stale(monkeypatch):
    """A stale record keeps learning from live surprises. The loop used to
    refuse it: a stale tool is already excluded from proactive injection, and
    this gate then refused it the lesson too - so the same error repeated until
    a retrain nobody had triggered (measured live: the retrain queue sat
    undrained for 41 days while stale read_file relearned nothing)."""
    from types import SimpleNamespace

    from vaf.whare_wananga import runtime as ww_runtime

    spawned = []
    monkeypatch.setattr(ww_runtime, "threading", SimpleNamespace(
        Thread=lambda *a, **k: SimpleNamespace(start=lambda: spawned.append(k.get("name")))))
    monkeypatch.setattr(ww_runtime, "_is_learnable_error", lambda e: True)
    for status, expect in (("stale", True), ("confirmed", True), ("draft", False)):
        spawned.clear()
        rec = store.new_record(f"t_rl_{status}", tool_schema_hash="h1")
        rec["status"] = status
        store.save(rec)
        ww_runtime.maybe_relearn(None, f"t_rl_{status}", {}, "boom")
        assert bool(spawned) is expect, f"status={status}"


# ── Gate rejects feed the re-training queue ──────────────────────────────────

def test_gate_reject_enqueues_for_retraining():
    _save("t_rot", status="stale")
    delivery.tool_knowhow("t_rot")  # strict path, returns None - but must enqueue
    names = {e["tool"] for e in retrain.pending(include_declare=True, all_entries=True)}
    assert "t_rot" in names


def test_verified_record_is_not_enqueued():
    _save("t_fine")
    delivery.tool_knowhow("t_fine")
    names = {e["tool"] for e in retrain.pending(include_declare=True, all_entries=True)}
    assert "t_fine" not in names
