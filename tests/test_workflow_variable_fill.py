# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Filling a matched workflow template's variables, deterministically.

THE DEFECT, seen live in a timer wake turn: the default-fill loop removed
elements FROM THE LIST IT WAS ITERATING, and because the variable set is a
`set`, WHICH variable got skipped depended on hash order. The skip was
invisibly healed by a `defaults` re-check inside the regex-repair stage -
which made a third fill loop downstream DEAD code (nothing with a default
could ever reach it), sitting under a message that promised "using defaults"
at a point where no default could exist. Two lines then narrated the same
missing variable.

Now: defaults fill first (over a copy), the per-variable regex repair runs
only for variables WITHOUT a default, the caller keeps the single verdict
line, and the whole thing is a module function so these tests can reach it.
"""
import pytest

from vaf.core.agent import _resolve_template_variables


TEMPLATE = {
    "variables": {
        "task_description": "What to do",
        "time": "Time to run (HH:MM format, e.g., '21:07')",
        "frequency": "How often (daily, weekly, hourly, monthly)",
    },
    "defaults": {"frequency": "daily"},
}


def test_defaults_fill_without_consulting_the_extractor(monkeypatch):
    """The headline. With EVERY missing variable defaulted, the repair stage
    has nothing to do - under the old mutate-while-iterating loop, the
    hash-order victim leaked through to the extractor instead."""
    import vaf.workflows.selector as selector_mod

    consulted = []
    monkeypatch.setattr(selector_mod.WorkflowSelector, "_extract_value",
                        lambda self, text, name, desc: consulted.append(name))

    template = {
        "variables": {"alpha": "", "beta": "", "gamma": ""},
        "defaults": {"alpha": "a", "beta": "b", "gamma": "c"},
    }
    variables = {}
    missing = _resolve_template_variables(template, variables, "anything")
    assert missing == []
    assert variables == {"alpha": "a", "beta": "b", "gamma": "c"}
    assert consulted == [], (
        f"defaulted variables reached the extractor: {consulted} - "
        f"the mutate-while-iterating skip is back")


def test_the_regex_repair_fills_what_defaults_cannot():
    """A time the LLM extraction missed, present in the raw input as HH:MM -
    the per-variable regex catches it (real extractor, no stub)."""
    variables = {"task_description": "wetterbericht"}
    missing = _resolve_template_variables(TEMPLATE, variables, "jeden tag um 21:07")
    assert missing == []
    assert variables["time"] == "21:07"
    assert variables["frequency"] == "daily"        # the default


def test_an_unfillable_variable_comes_back_missing():
    """The live case: '30s-Timer' carries no HH:MM, so `time` must come back
    missing - the caller falls back to the agent, which is what saved that
    turn from a mismatched workflow."""
    variables = {"task_description": "pip install", "frequency": "daily"}
    missing = _resolve_template_variables(
        TEMPLATE, variables, "setze 30s-Timer fuer #5/10")
    assert missing == ["time"]


def test_extracted_values_are_never_overwritten_by_defaults():
    variables = {"frequency": "weekly", "task_description": "x", "time": "09:00"}
    _resolve_template_variables(TEMPLATE, variables, "irrelevant")
    assert variables["frequency"] == "weekly"


def test_one_verdict_line_and_no_false_promise():
    """The caller keeps a SINGLE "Missing inputs" emission, and the sentence
    that promised defaults where none could exist is gone."""
    from pathlib import Path

    import vaf.core.agent as agent_mod

    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    assert src.count('f"Missing inputs:') == 1, (
        "the same fact is narrated twice again")
    assert "using defaults or falling back" not in src
    assert "falling back to the agent" in src, (
        "the verdict no longer names its consequence")
