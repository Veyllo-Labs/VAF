# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Nothing that comes and goes may sit at the front of the system prompt.

Providers cache on the leading tokens of a request and bill what they serve from
that cache at a fraction of the input price. A block that appears at position 0
on one turn and is gone on the next therefore does not cost its own size, it
costs the WHOLE request, every time it flips.

Measured on a live account before this guard existed: the orchestrator module's
MISSION STATUS block was the first thing in the prompt, and the provider reported
a nought per cent cache hit on every thirteen-thousand-token chat request across
three turns, while a sibling lane whose prompt barely moved reported ninety-seven
per cent on the same account in the same minute. The mechanism was never the
problem; the assembly was.

The module is not exotic either. Thirty-eight keywords activate it, among them
words as ordinary as "alle", "plan", "review" and "analyse", and it decays two
turns later, so an ordinary conversation switches it on and off repeatedly.

This pins the PROPERTY rather than that one block: whatever a turn switches on,
it must not land in the first stretch of the prompt.
"""
import pytest

from vaf.core.system_prompt import SystemPromptManager

# OpenAI caches in 128-token blocks and reports nothing below 1024 tokens, so a
# head that stays identical for less than that is worth exactly zero. Characters
# rather than tokens, at the conventional four-to-one, because this file must not
# depend on a tokenizer to state a structural rule.
MIN_STABLE_CHARS = 1024 * 4


def _prompt(modules):
    builder = SystemPromptManager(tools={}, model_name="gpt-4o-mini",
                                  agent_instance=None, username="admin")
    builder.active_modules = dict(modules)
    return builder.build_prompt(username="admin", session_id="green123456")


def _first_difference(a: str, b: str):
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return None if a == b else min(len(a), len(b))


@pytest.mark.parametrize("module", ["orchestrator", "coding", "research", "workflow"])
def test_switching_a_module_leaves_the_head_of_the_prompt_alone(module):
    """MUTATION: append the MISSION STATUS block first again, as it was, and the
    orchestrator case reports a first difference at character 0."""
    without = _prompt({})
    with_it = _prompt({module: 3})
    assert without != with_it, f"module {module} contributed nothing, so this proves nothing"

    at = _first_difference(without, with_it)
    assert at is not None and at >= MIN_STABLE_CHARS, (
        f"switching `{module}` changes the prompt at character {at}, inside the first "
        f"{MIN_STABLE_CHARS} characters. A provider caches on the leading tokens, so a "
        f"block that flips this early costs the whole request rather than its own size.")


def test_the_orchestrator_block_is_still_delivered():
    """Moved, not dropped. It is still in the system message, where its authority
    comes from, and its text is unchanged."""
    with_it = _prompt({"orchestrator": 3})
    assert "MISSION STATUS" in with_it
    assert "PLAN LOADED" in with_it
    assert "MISSION STATUS" in with_it[-1500:], "the block is no longer at the end"
