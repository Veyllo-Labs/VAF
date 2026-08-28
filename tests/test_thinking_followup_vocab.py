# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Thinking-run improvements: the VAF vocabulary book (varied multilingual nudge) + follow-up tracking
(re-ask the open question instead of a new topic, then rest).
"""
import json
import pathlib

import pytest

from vaf.core import vocab
from vaf.core import thinking_requests as tr
from vaf.core.platform import Platform


@pytest.fixture(autouse=True)
def _isolate_vaf_dir(monkeypatch, tmp_path):
    """Keep request records out of the REAL ~/.vaf store (this file previously
    polluted it with u-* scope dirs that then surfaced in the admin dashboard)."""
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))


# ── Vocabulary book ───────────────────────────────────────────────────────────

def test_vocab_pick_exact_and_format():
    out = vocab.pick("nudge", "de", name="Alice")
    assert out and "Alice" in out


def test_vocab_pick_region_normalizes_to_base():
    assert "Alice" in vocab.pick("nudge", "de-DE", name="Alice")
    assert "Alice" in vocab.pick("nudge", "pt_BR", name="Alice")


def test_vocab_pick_unknown_lang_falls_back_to_en():
    out = vocab.pick("nudge", "qq", name="Alice")  # not in the book -> English
    en_formatted = [p.format(name="Alice") for p in vocab._load("nudge")["en"]]  # type: ignore[attr-defined]
    assert out and "Alice" in out and out in en_formatted


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


# ── The confirmation lexicon, which is matched rather than spoken ─────────────
#
# `speaker_confirm.parse_reply` consumes a reply as yes or no by matching the
# confirm_yes / confirm_no word lists LEADING-ONLY. A language missing from one
# of the two lists is not a cosmetic gap: the user answers in their own language
# and the answer is silently treated as an ordinary chat turn, so the pending
# question stays open. An entry that still holds the English words is worse,
# because `available_languages` then reports the language as covered.
#
# Both defects were present at once and are the reason these are tests: three UI
# languages had no affirmation list, one had no negation list, and two carried
# the English negations verbatim under their own language key.

_VOCAB_DATA = pathlib.Path(__file__).resolve().parents[1] / "vaf" / "core" / "vocab" / "data"
_UI_LOCALES = ("de", "en", "tr", "zh", "ja", "ko", "th")

# A phrasing that legitimately coincides with the English one. Pinned as a pair
# so a NEW collision fails even in a file that already has an accepted one.
_SAME_AS_ENGLISH_BY_DESIGN = {("voice_greeting_anon.json", "de")}


def _vocab_files():
    return sorted(_VOCAB_DATA.glob("*.json"))


def test_every_ui_language_can_answer_yes_and_no():
    missing = []
    for key in ("confirm_yes", "confirm_no"):
        have = set(vocab.available_languages(key))
        for lang in _UI_LOCALES:
            if lang not in have:
                missing.append(f"{key}: {lang}")
    assert not missing, (
        "a UI language with no entry here cannot answer the agent's own\n"
        "confirmation question in that language:\n" + "\n".join(missing)
    )


# Languages that can answer only one of the two, measured. Every one of these
# is a language the voice stack speaks but the UI does not ship, which is why
# they are recorded rather than fixed here: filling them is translation work of
# its own and each entry has to be checked against the leading-only matcher
# before it is added. The set may only SHRINK. Adding a language to it, or
# shipping a UI locale that lands in it, is the defect this pins.
_ONE_SIDED_LANGUAGES = {"bg", "es", "fr", "it", "sr", "uk", "ar", "hr", "hu", "sk", "sv", "vi"}


def test_yes_and_no_cover_the_same_languages():
    yes = set(vocab.available_languages("confirm_yes"))
    no = set(vocab.available_languages("confirm_no"))
    one_sided = yes ^ no
    spread = one_sided - _ONE_SIDED_LANGUAGES
    assert not spread, (
        "confirm_yes and confirm_no must cover the same languages; an asymmetry\n"
        "means one of the two answers is silently unrecognised.\n"
        f"newly one-sided: {sorted(spread)}"
    )
    assert not (one_sided & set(_UI_LOCALES)), (
        "a shipped UI language may never be one-sided:\n"
        f"{sorted(one_sided & set(_UI_LOCALES))}"
    )
    healed = _ONE_SIDED_LANGUAGES - one_sided
    assert not healed, (
        "these languages now cover both answers, so remove them from\n"
        f"_ONE_SIDED_LANGUAGES: {sorted(healed)}"
    )


def test_no_phrasing_is_the_untranslated_english_one():
    offenders = []
    for path in _vocab_files():
        try:
            data = json.loads(path.read_bytes().decode("utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        english = data.get("en")
        if not isinstance(english, list):
            continue
        for lang, items in data.items():
            if lang == "en" or not isinstance(items, list):
                continue
            if items == english and (path.name, lang) not in _SAME_AS_ENGLISH_BY_DESIGN:
                offenders.append(f"{path.name}: {lang} is the English list verbatim")
    assert not offenders, (
        "a language key holding the English phrasings reports as covered while\n"
        "matching nothing a speaker of that language would write:\n" + "\n".join(offenders)
    )
