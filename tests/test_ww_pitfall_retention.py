# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Pitfall retention: never silently forget a learned lesson.

Two loss lanes existed. The runtime append capped with `pits[:10]` - keep the
FIRST ten - so on a full list the one entry guaranteed to be dropped was the
lesson just learned from a live failure. And a training run started every
basket empty, wiping the runtime-learned pitfalls along with the re-derivable
trained ones; a fresh probe run cannot re-derive a live incident (real
arguments, real environment), so that knowledge was simply gone. The policy
now: evict by REPLACEABILITY (trained entries are re-derived by the next run,
runtime entries are the last resort), always admit the newest lesson, log
every eviction, and carry runtime lessons over every retrain and every distil
pass.
"""
import re
from pathlib import Path

from vaf.whare_wananga import runner, runtime, store

ROOT = Path(__file__).resolve().parents[1]


def _p(text, source="whare_wananga"):
    return {"text": text, "source": source, "seen": 1}


# ── eviction: replaceability order, newest always admitted ──────────────────

def test_evict_drops_trained_before_runtime():
    pits = [_p(f"trained {i}") for i in range(9)] + [_p("live lesson old", "runtime")]
    pits.append(_p("live lesson NEW", "runtime"))          # 11 entries, cap 10
    out = runtime._evict_over_cap("t", pits, cap=10)
    texts = [p["text"] for p in out]
    assert len(out) == 10
    assert "trained 0" not in texts, "the oldest re-derivable trained entry is the victim"
    assert "live lesson NEW" in texts and "live lesson old" in texts


def test_evict_all_runtime_drops_oldest_as_last_resort():
    pits = [_p(f"live {i}", "runtime") for i in range(11)]
    out = runtime._evict_over_cap("t", pits, cap=10)
    texts = [p["text"] for p in out]
    assert len(out) == 10
    assert "live 0" not in texts, "with only runtime entries, the oldest goes"
    assert "live 10" in texts, "the newest lesson is always admitted"


def test_evict_under_cap_is_a_no_op():
    pits = [_p("a"), _p("b", "runtime")]
    assert runtime._evict_over_cap("t", list(pits), cap=10) == pits


# ── harvest: only runtime, non-vacuous, newest win the carry slots ──────────

def test_harvest_takes_runtime_only_and_newest_first():
    rec = {"tuatea": {"pitfalls": (
        [_p("trained a"), _p("No probe attempts were provided.", "runtime")]
        + [_p(f"live {i}", "runtime") for i in range(7)]
    )}}
    got = runner._harvest_runtime_pitfalls(rec, cap=5)
    texts = [p["text"] for p in got]
    assert texts == [f"live {i}" for i in range(2, 7)], (
        "trained and vacuous entries stay out; the newest five runtime lessons carry"
    )


def test_harvest_of_a_fresh_record_is_empty():
    assert runner._harvest_runtime_pitfalls(store.new_record("t_x")) == []


# ── carry: dedup against the fresh distillate, fresh stays first ────────────

def test_carry_dedups_and_keeps_fresh_first():
    fresh = [_p("The 'path' argument is required and must name a file.")]
    preserved = [
        _p("path is a required argument and must name a file", "runtime"),  # dup of fresh
        _p("Reading a directory returns 'Error: Not a file.'", "runtime"),
    ]
    out = runner._carry_runtime_pitfalls("t", list(fresh), preserved)
    texts = [p["text"] for p in out]
    assert texts[0] == fresh[0]["text"], "fresh distillate leads (current contract, top-3 injection)"
    assert "Reading a directory returns 'Error: Not a file.'" in texts
    assert len(out) == 2, "the duplicate runtime lesson folded into the fresh one"


def test_carry_without_preserved_returns_fresh_unchanged():
    fresh = [_p("x")]
    assert runner._carry_runtime_pitfalls("t", fresh, []) is fresh


# ── the shared dedup rule has ONE home ──────────────────────────────────────

def test_runtime_is_dup_delegates_to_the_store_rule():
    existing = ["The 'path' argument is required."]
    assert runtime._is_dup("path is a required argument", existing) \
        == store.is_duplicate_pitfall("path is a required argument", existing)
    assert store.is_duplicate_pitfall("", existing) is True
    assert store.is_duplicate_pitfall("something entirely unrelated here", existing) is False


# ── the WIRING: harvest before the wipe, carry after every overwrite ────────

def test_train_wires_harvest_before_wipe_and_carry_after_overwrite():
    """Source-pinned because train_tool is a several-hundred-line closure with
    an LLM in the middle - there is no seam to drive the full run in CI. The
    ORDER is the contract: a harvest after the wipe reads an empty list, and a
    carry missing from the distil overwrite is undone by the next distil pass."""
    src = (ROOT / "vaf" / "whare_wananga" / "runner.py").read_text(encoding="utf-8")
    harvest_at = src.index("preserved_runtime = _harvest_runtime_pitfalls(rec)")
    wipe_at = src.index('rec["tuatea"] = {"pitfalls": []}')
    assert harvest_at < wipe_at, "harvest must read the record BEFORE the baskets are wiped"

    overwrite_at = src.index('for p in d["tuatea"]["pitfalls"][:10]')
    carry_at = src.index("_carry_runtime_pitfalls(")
    carry_call_at = src.index("_carry_runtime_pitfalls(\n", overwrite_at) \
        if "_carry_runtime_pitfalls(\n" in src[overwrite_at:] else src.index(
            "_carry_runtime_pitfalls(", overwrite_at)
    assert carry_call_at > overwrite_at, (
        "the carry must run AFTER the distil overwrite, inside _distil - a carry "
        "done once at the start is wiped again by the second distil pass"
    )
    assert carry_at  # the helper exists at module level (unit-tested above)
