# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""WorkflowSelector._extract_value tests.

Pins the time-extraction gate: a time value (HH:MM) is extracted ONLY for an actual time variable
(identified by var_name or by a HH:MM hint in var_desc), NOT for every variable just because the input
contains a colon. The old `or ":" in user_input` clause filled non-time variables (task_description,
frequency, output_path, ...) with the first time token — e.g. a note "Erinnerung: Arzt um 9:00" gave every
field "09:00". No network, no LLM.
"""
from vaf.workflows.selector import WorkflowSelector


def _sel():
    return WorkflowSelector()


# ── A non-time variable is NOT poisoned by a colon / time token in the input ───

def test_task_description_not_filled_with_time_token():
    s = _sel()
    inp = "Erstelle eine Erinnerung: Arzt anrufen um 9:00"
    # task_description must not become "09:00" just because the sentence has a colon + a time
    assert s._extract_value(inp, "task_description", "What the task does") != "09:00"


def test_frequency_uses_its_own_branch_not_time():
    s = _sel()
    inp = "Erinnere mich täglich um 7:00 ans Wasser trinken"
    # frequency has its own extractor → "daily", never the time token
    assert s._extract_value(inp, "frequency", "How often (daily, weekly, hourly, monthly)") == "daily"


def test_colon_does_not_poison_task_description():
    s = _sel()
    # a colon + a time token in the input must not turn task_description into "09:00"; the time branch is
    # skipped, so it falls through to the proper task-description extractor (which strips the time) and
    # returns the real task text.
    out = s._extract_value("Notiz: Milch kaufen um 9:00", "task_description", "What the task does")
    assert out is not None
    assert "9:00" not in out and "09:00" not in out
    assert "Milch" in out


# ── A real time variable still extracts the time (regression-safe) ─────────────

def test_time_variable_extracts_by_name():
    s = _sel()
    assert s._extract_value("Arzt anrufen um 9:00", "time", "Time to run") == "09:00"


def test_time_variable_normalizes_to_hh_mm():
    s = _sel()
    assert s._extract_value("um 7:05 bitte", "time", "Time to run (HH:MM format, e.g., '21:07')") == "07:05"


def test_time_variable_identified_by_hhmm_description():
    s = _sel()
    # variable not named "time" but its description signals a HH:MM time → still extracts
    assert s._extract_value("starte um 22:46", "schedule", "Run at HH:MM") == "22:46"


def test_german_named_time_variable():
    s = _sel()
    assert s._extract_value("um 6:30", "uhrzeit", "Wann ausführen") == "06:30"


def test_time_variable_without_time_token_returns_none():
    s = _sel()
    assert s._extract_value("irgendwann mal", "time", "Time to run") is None
