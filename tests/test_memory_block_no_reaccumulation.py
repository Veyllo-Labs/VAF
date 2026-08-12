# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The memory block belongs to the REQUEST, never to the stored history.

`_prepare_messages` ends in a bare `return messages` for API providers, so the
list it hands back holds the very dicts of `self.history`. Merging the memory
block into `prepared_messages[0]["content"]` therefore wrote it into the history,
and the generation loop re-splices once per LLM round-trip - so a single turn
re-appended the same block again and again. An archived context carried one system
message of 145,284 characters with 24 copies of it.

These tests pin the two halves of the fix: the splice builds a fresh first message
(so the history stays clean and the block cannot stack), and the size of the block
still reaches the token estimate, which only ever walks `self.history`.
"""
import re
from pathlib import Path

from vaf.core.agent import Agent as CoreAgent

ROOT = Path(__file__).resolve().parent.parent
AGENT_SRC = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")

BLOCK = "## Memory context (relevant to this query)\n\n[Source 1] (Relevance: 90%)\nthe user likes tea"


class _Dummy:
    """Just enough agent for the two unbound methods under test."""

    def __init__(self, history=None):
        self.history = history if history is not None else []
        self.api_backend = None
        self.TOOLS = None
        self.config = {"n_ctx": 8192}


def _splice(dummy, prepared, block=BLOCK):
    return CoreAgent._splice_memory_block(dummy, prepared, block)


def _estimate(dummy):
    return CoreAgent._estimate_token_usage(dummy)[0]


def test_splice_leaves_history_untouched():
    history = [{"role": "system", "content": "BASE PROMPT"}, {"role": "user", "content": "hi"}]
    dummy = _Dummy(history)

    # The API path passes the history list itself (that is the whole trap).
    spliced = _splice(dummy, history)

    assert history[0]["content"] == "BASE PROMPT"
    assert spliced[0] is not history[0]
    assert BLOCK in spliced[0]["content"]


def test_block_does_not_stack_across_round_trips():
    history = [{"role": "system", "content": "BASE PROMPT"}, {"role": "user", "content": "hi"}]
    dummy = _Dummy(history)

    # Three LLM round-trips inside ONE turn: tool call, tool result, final answer.
    for _ in range(3):
        spliced = _splice(dummy, history)
        assert spliced[0]["content"].count("## Memory context") == 1

    assert "## Memory context" not in history[0]["content"]


def test_block_is_prepended_when_message_zero_is_not_a_system_message():
    history = [{"role": "user", "content": "hi"}]
    dummy = _Dummy(history)

    spliced = _splice(dummy, history)

    assert [m["role"] for m in spliced] == ["user", "system"]
    assert spliced[1]["content"] == BLOCK
    assert history == [{"role": "user", "content": "hi"}]


def test_injected_block_still_counts_towards_the_token_estimate():
    """The estimate walks self.history, which the block deliberately no longer joins."""
    dummy = _Dummy([{"role": "system", "content": "BASE PROMPT"}])
    without = _estimate(dummy)

    _splice(dummy, dummy.history, "x" * 3600)
    with_block = _estimate(dummy)

    assert with_block > without
    assert with_block - without >= 3600 / 3.6


def test_no_in_place_merge_survives_in_the_source():
    """Mutation guard: restoring the in-place rewrite at either splice site fails here."""
    assert "prepared_messages[0][\"content\"] =" not in AGENT_SRC
    # ALL THREE generation lanes go through it: API backends, the local llama
    # server, and llama-cpp-python in-process. The third was missing until the
    # branch-parity fix - it sent the raw history, so the memory block never
    # reached the model on that lane (see tests/test_generation_branch_parity.py).
    assert AGENT_SRC.count("self._splice_memory_block(") == 3


def test_the_empty_response_retry_forwards_the_memory_context():
    """The nuclear retry re-enters chat_step; without this it drops the whole block."""
    retry = re.search(
        r"NUCLEAR OPTION.*?return self\.chat_step\((.*?)\)", AGENT_SRC, re.DOTALL
    )
    assert retry, "the empty-response retry moved - re-point this guard"
    assert "memory_context=memory_context" in retry.group(1)
