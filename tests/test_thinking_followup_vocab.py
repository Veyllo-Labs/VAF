# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Thinking-run improvements: the VAF vocabulary book (varied multilingual nudge) + follow-up tracking
(re-ask the open question instead of a new topic, then rest).
"""
import pathlib

from vaf.core import vocab
from vaf.core import thinking_requests as tr


# ── Vocabulary book ───────────────────────────────────────────────────────────

def test_vocab_pick_exact_and_format():
    out = vocab.pick("nudge", "de", name="Mert")
    assert out and "Mert" in out


def test_vocab_pick_region_normalizes_to_base():
    assert "Mert" in vocab.pick("nudge", "de-DE", name="Mert")
    assert "Mert" in vocab.pick("nudge", "pt_BR", name="Mert")


def test_vocab_pick_unknown_lang_falls_back_to_en():
    out = vocab.pick("nudge", "qq", name="Mert")  # not in the book -> English
    en_formatted = [p.format(name="Mert") for p in vocab._load("nudge")["en"]]  # type: ignore[attr-defined]
    assert out and "Mert" in out and out in en_formatted


def test_vocab_unknown_key_is_safe():
    assert vocab.pick("no_such_key", "en", name="X") == ""


def test_vocab_missing_placeholder_returns_raw_not_crash():
    out = vocab.pick("nudge", "en")  # no name -> raw phrasing with literal {name}
    assert out and "{name}" in out


def test_vocab_rotates_so_consecutive_picks_vary():
    picks = {vocab.pick("nudge", "de", scope="rot", name="M") for _ in range(15)}
    assert len(picks) >= 3  # the book has 6 phrasings; rotation must surface several


def test_vocab_resolve_language_defaults_to_en():
    # No username / no preferred_language / no config default -> 'en'
    assert vocab.resolve_user_language(user_scope_id="x", username=None) == "en"


def test_vocab_data_files_well_formed():
    import json
    data = json.loads((pathlib.Path(vocab.__file__).parent / "data" / "nudge.json").read_text("utf-8"))
    assert "en" in data and "de" in data
    for lang, items in data.items():
        assert isinstance(items, list) and items
        assert all("{name}" in s for s in items), f"{lang} missing {{name}}"


# ── Follow-up request tracking ────────────────────────────────────────────────

def test_request_starts_with_zero_followups():
    r = tr.add_request("u-a", question="Q?", run_seq=1)
    assert r["followups"] == 0 and r["status"] == "asked"


def test_open_proactive_request_is_free_and_recent():
    s = "u-open"
    free = tr.add_request(s, question="automate tests?", run_seq=10)
    tr.add_request(s, question="from a note", run_seq=10, source_note_id="n1")  # not free
    op = tr.get_open_proactive_request(s, current_run_seq=11, within_runs=6)
    assert op and op["id"] == free["id"]  # note-sourced is ignored
    # outside the recency window -> not returned
    assert tr.get_open_proactive_request(s, current_run_seq=999, within_runs=6) is None


def test_bump_followup_increments_and_refreshes():
    s = "u-bump"
    r = tr.add_request(s, question="Q1?", run_seq=5)
    u = tr.bump_followup(s, r["id"], new_question="Q2 yes/no?", run_seq=6)
    assert u["followups"] == 1 and u["question"] == "Q2 yes/no?" and u["run_seq"] == 6 and u["status"] == "asked"
    u2 = tr.bump_followup(s, r["id"], run_seq=7)
    assert u2["followups"] == 2
    assert tr.bump_followup(s, "no-such-id") is None


def test_answered_request_is_no_longer_open():
    s = "u-ans"
    r = tr.add_request(s, question="Q?", run_seq=1)
    tr.update_request_status(s, r["id"], "declined")
    op = tr.get_open_proactive_request(s, current_run_seq=2, within_runs=6)
    assert op is None or op["id"] != r["id"]
