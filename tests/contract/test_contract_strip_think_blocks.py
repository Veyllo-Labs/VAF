# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: `vaf.strip_think_blocks(text, *, greedy=False)` (docs/EMBEDDING.md, "Streaming"
and "What is and isn't stable").

The rule VAF itself uses to take a model's marked reasoning out of its text before the text
is stored, sent or read aloud. An application that keeps or forwards a stream calls it so its
copy follows the same rule as VAF's own lanes.
"""
import random

import pytest

import vaf


@pytest.mark.parametrize("text, expected", [
    ("<think>plan</think>Answer.", "Answer."),
    ("<THINK>plan</THINK>Answer.", "Answer."),
    ("<Thinking>plan</thinking>Answer.", "Answer."),
    ("<redacted_reasoning>plan</redacted_reasoning>Answer.", "Answer."),
    ("Answer. <think>a plan the model never closed", "Answer."),
    ("Answer. <redacted_reasoning>a plan the model never closed", "Answer."),
    ("plan</think>Answer.", "planAnswer."),
    ("plan</redacted_reasoning>Answer.", "planAnswer."),
    ("  Answer with no reasoning.  ", "Answer with no reasoning."),
], ids=["think", "upper", "thinking", "redacted", "unclosed", "redacted-unclosed",
        "stray-closer", "stray-redacted-closer", "plain-is-stripped"])
def test_the_marks_and_the_rule(text, expected):
    assert vaf.strip_think_blocks(text) == expected


def test_a_block_closes_at_its_first_closer_unless_greedy():
    """Greedy is one block from the first opener to the last closer: what a turn's reply
    needs when a model quotes a closer in the middle of its reasoning."""
    text = "<think>a</think>B</think>C"
    assert vaf.strip_think_blocks(text) == "BC"
    assert vaf.strip_think_blocks(text, greedy=True) == "C"


def test_the_named_boundaries():
    """A redacted block the model never closed cuts to the end, a think closer after it
    included; only a think opener before it pairs with that closer, greedy. Crossing blocks
    keep the text between them."""
    assert vaf.strip_think_blocks("<redacted_reasoning>plan</think>Answer.") == ""
    assert vaf.strip_think_blocks("<redacted_reasoning>plan</think>Answer.", greedy=True) == ""
    text = "<think>a</think><redacted_reasoning>b</think>Answer"
    assert vaf.strip_think_blocks(text, greedy=True) == "Answer"
    assert vaf.strip_think_blocks(text) == ""
    assert vaf.strip_think_blocks("<redacted_reasoning><think></redacted_reasoning>Q</think>") == "Q"


def test_reasoning_only_becomes_empty():
    assert vaf.strip_think_blocks("<think>only reasoning</think>") == ""
    assert vaf.strip_think_blocks("<think>only reasoning, never closed") == ""


@pytest.mark.parametrize("value", [None, ""])
def test_a_falsy_input_comes_back_as_it_is(value):
    assert vaf.strip_think_blocks(value) is value


def test_it_never_raises_on_a_string():
    rng = random.Random(7)
    alphabet = ["<think>", "</think>", "<thinking>", "</THINK>", "<redacted_reasoning>",
                "</redacted_reasoning>", "<", ">", "</", "a", " ", "\n", "ok"]
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        for greedy in (False, True):
            assert isinstance(vaf.strip_think_blocks(text, greedy=greedy), str)


def test_it_is_the_engine_function_itself():
    import vaf.core.completion as completion  # re-export source named in EMBEDDING.md

    assert vaf.strip_think_blocks is completion.strip_think_blocks
