# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A background message must carry enough context for the main agent to take it over.

The background run asks; hours later the person answers on a messenger, and from that moment the MAIN
agent owns the topic. It can only work from what the request carries - it never saw the run.

Live incident 2026-08-30, the whole reason this file exists. The record read:

    question        = "Hey Alice, kurze Rückfrage: Sollen wir heute mit dem Commit weitermachen - ja oder nein?"
    details         = None
    followups       = 1

The user answered "Hey sry was ?" and got back a question about WHICH MESSAGE they meant. The handover
itself was fine - the prompt log shows `[REPLY_CTX] lane=plain req=ab12cd34 len=919` with the question
quoted in it. The payload was empty: a follow-up had overwritten the substantive question with its own
terse reminder, and the follow-up rung never asked for `details`, so nothing anywhere said what the
subject had been."""
from pathlib import Path

import pytest

import vaf.core.thinking_mode as tm
import vaf.core.thinking_requests as tr
from vaf.core.agent import Agent
from vaf.core.platform import Platform


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))


# ── 1. a reminder must not erase the subject it is chasing ────────────────────────────────

def test_the_original_question_survives_a_follow_up(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "u-fu"
    original = ("Hey Alice, du hast den Commit fuer die Thinking-Runde noch offen - soll ich die "
                "Aenderungen zusammenfassen und dir einen Vorschlag schicken?")
    e = tr.add_request(scope, original, run_seq=1, details="12 Commits, davon 4 ungepusht.")
    assert e["original_question"] is None          # never chased yet

    tr.bump_followup(scope, e["id"], new_question="Sollen wir heute weitermachen - ja oder nein?",
                     run_seq=2)
    got = tr.get_request(scope, e["id"])
    assert got["question"] == "Sollen wir heute weitermachen - ja oder nein?"
    assert got["original_question"] == original, "the subject was overwritten by its own reminder"


def test_a_second_follow_up_does_not_overwrite_the_subject(monkeypatch, tmp_path):
    """Only the FIRST follow-up may set it - otherwise the second reminder becomes the 'original'."""
    _isolate(monkeypatch, tmp_path)
    scope = "u-fu2"
    original = "Soll ich dir den Wetterbericht automatisch um 7 schicken?"
    e = tr.add_request(scope, original, run_seq=1)
    tr.bump_followup(scope, e["id"], new_question="Reminder 1", run_seq=2)
    tr.bump_followup(scope, e["id"], new_question="Reminder 2", run_seq=3)
    got = tr.get_request(scope, e["id"])
    assert got["original_question"] == original
    assert got["followups"] == 2


def test_a_follow_up_enriches_but_never_blanks_the_context(monkeypatch, tmp_path):
    """A reminder that carries substance fills a gap; one that carries none must not erase what the
    original knew - which is the only reason the original's details are worth anything later."""
    _isolate(monkeypatch, tmp_path)
    scope = "u-fu3"
    e = tr.add_request(scope, "Frage", run_seq=1, details="Der Ursprungskontext.",
                       proposed_action="create automation: wetter 07:00")

    tr.bump_followup(scope, e["id"], new_question="Reminder", run_seq=2)   # carries nothing
    got = tr.get_request(scope, e["id"])
    assert got["details"] == "Der Ursprungskontext."
    assert got["proposed_action"] == "create automation: wetter 07:00"

    tr.bump_followup(scope, e["id"], new_question="Reminder 2", run_seq=3,
                     details="Praeziser: ab morgen, 07:00, per Telegram.")
    assert tr.get_request(scope, e["id"])["details"] == "Praeziser: ab morgen, 07:00, per Telegram."


# ── 2. the handover note must name the subject, not only the reminder ─────────────────────

def _note(q, subject="", facts=" You do not have the specifics on hand."):
    return Agent._build_reply_pickup_note(q, "", "", False, facts, subject)


def test_the_note_names_the_subject_of_a_reminder():
    note = _note("Sollen wir heute weitermachen - ja oder nein?",
                 subject="Der Commit fuer die Thinking-Runde ist noch offen - soll ich zusammenfassen?")
    assert "REMINDER" in note
    assert "Thinking-Runde" in note, "the main agent cannot take over a subject it is not told"
    assert "do not ask them which message you mean" in note


def test_a_question_that_was_never_chased_reads_as_before():
    """No subject, no extra sentence - a first-time question is self-contained."""
    note = _note("Soll ich dir den Wetterbericht um 7 schicken?")
    assert "REMINDER" not in note
    assert "Soll ich dir den Wetterbericht um 7 schicken?" in note


def test_an_identical_subject_is_not_repeated():
    q = "Soll ich das einrichten?"
    assert "REMINDER" not in _note(q, subject=q)


def test_the_subject_reaches_the_handoff_lane_too():
    """An automation handoff carries a bundle, and it can be chased just the same."""
    note = Agent._build_reply_pickup_note(
        "Kurz nachgehakt - ja oder nein?", "", "DIGEST", True, "", "Die Rechnung vom 14."
    )
    assert "Die Rechnung vom 14." in note and "DIGEST" in note


# ── 3. the rung that writes the reminder must ask for the context ─────────────────────────

def test_both_follow_up_lanes_demand_the_context():
    for reconfirm in (False, True):
        p = tm._build_followup_prompt("Soll ich das einrichten?", reconfirm=reconfirm)
        assert "details=" in p, f"reconfirm={reconfirm}: the reminder may carry no subject"
        assert "proposed_action=" in p
        assert "the main agent then takes over" in p


def test_the_context_rule_names_the_failure_it_prevents():
    assert "'what?' rather than yes or no" in tm._FOLLOWUP_CONTEXT_RULE


# ── 4. the admin panel had the same confusion ─────────────────────────────────────────────

def test_the_dashboard_can_show_the_subject():
    from vaf.api.thinking_routes import _REQUEST_FIELDS
    assert "original_question" in _REQUEST_FIELDS, \
        "the panel shows the terse reminder as if it were the question"
    for leaked in ("user_reply", "main_reply", "details"):
        assert leaked not in _REQUEST_FIELDS, "replies and details stay in the chat surfaces"


# ── 5. the doc describes the record ───────────────────────────────────────────────────────

def test_the_design_doc_records_the_field():
    doc = (Path(__file__).resolve().parent.parent / "docs" / "agents" / "Thinking-Mode.md") \
        .read_text(encoding="utf-8")
    assert "original_question" in doc, "the data-file table no longer describes the record"
