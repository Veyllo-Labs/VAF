# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Tests for the coder's tool_calls -> tool adjacency normalizer.

Regression cover for the DeepSeek/OpenAI 400 "insufficient tool messages
following tool_calls message": a system message (a doom-loop nudge) wedged
between an assistant's tool_calls and its tool responses crashed the
coding_agent subprocess. The normalizer must repair this and never corrupt a
valid history.
"""
from vaf.tools.coder import _normalize_tool_adjacency as normalize


def _roles(msgs):
    return [m["role"] for m in msgs]


def _assert_contract(msgs):
    """Every assistant with tool_calls is immediately followed by exactly one
    role='tool' per tool_call_id, in order, with nothing wedged in between."""
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            for k, tcid in enumerate(ids):
                nxt = msgs[i + 1 + k]
                assert nxt["role"] == "tool", f"expected tool after assistant, got {nxt['role']}"
                assert nxt["tool_call_id"] == tcid, f"tool order mismatch: {nxt['tool_call_id']} != {tcid}"
            i += 1 + len(ids)
            continue
        i += 1


def test_repairs_the_exact_crash_sequence():
    # assistant(tool_calls) -> system(wedge) -> tool  (the logged loop-12 crash)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "codesearch"}}]},
        {"role": "system", "content": "DOOM LOOP"},
        {"role": "tool", "tool_call_id": "call_1", "name": "codesearch", "content": "res"},
    ]
    out = normalize(msgs)
    assert _roles(out) == ["system", "user", "assistant", "tool", "system"]
    _assert_contract(out)


def test_multi_toolcall_wedge_between_responses():
    # assistant([A,B]) -> tool(A) -> system(wedge) -> tool(B): the per-site fix
    # alone cannot repair this; the normalizer must relocate the system to the end.
    msgs = [
        {"role": "assistant", "tool_calls": [
            {"id": "A", "function": {"name": "x"}},
            {"id": "B", "function": {"name": "y"}},
        ]},
        {"role": "tool", "tool_call_id": "A", "content": "ra"},
        {"role": "system", "content": "wedge"},
        {"role": "tool", "tool_call_id": "B", "content": "rb"},
    ]
    out = normalize(msgs)
    assert _roles(out) == ["assistant", "tool", "tool", "system"]
    assert out[1]["tool_call_id"] == "A" and out[2]["tool_call_id"] == "B"
    _assert_contract(out)


def test_idempotent_on_valid_history():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "t"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "r"},
        {"role": "assistant", "content": "done"},
        {"role": "system", "content": "[TODO STATUS]"},
    ]
    once = normalize(msgs)
    twice = normalize(once)
    assert once == msgs  # already valid -> unchanged
    assert twice == once  # idempotent
    _assert_contract(once)


def test_reasoning_content_preserved():
    msgs = [
        {"role": "assistant", "content": "", "reasoning_content": "thinking",
         "tool_calls": [{"id": "1", "function": {"name": "t"}}]},
        {"role": "system", "content": "wedge"},
        {"role": "tool", "tool_call_id": "1", "content": "r"},
    ]
    out = normalize(msgs)
    asst = out[0]
    assert asst["role"] == "assistant"
    assert asst.get("reasoning_content") == "thinking"
    _assert_contract(out)


def test_deepseek_id_reuse_across_turns():
    # DeepSeek reuses positional ids (call_0/call_1) every turn. FIFO pairing must
    # bind each response to the assistant that owns it, in order.
    msgs = [
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "cs"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "first (46)"},
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "cs"}}]},
        {"role": "system", "content": "wedge"},
        {"role": "tool", "tool_call_id": "call_1", "content": "second (618)"},
    ]
    out = normalize(msgs)
    # first assistant -> first response; second assistant -> second response
    assert out[0]["role"] == "assistant" and out[1]["content"] == "first (46)"
    assert out[2]["role"] == "assistant" and out[3]["content"] == "second (618)"
    assert out[4]["role"] == "system"
    _assert_contract(out)


def test_dangling_toolcall_dropped():
    # A tool_call with no response anywhere -> the tool_call is dropped so we never
    # emit a dangling tool_calls message.
    msgs = [
        {"role": "assistant", "content": "hi", "tool_calls": [{"id": "1", "function": {"name": "t"}}]},
        {"role": "system", "content": "next"},
    ]
    out = normalize(msgs)
    assert out[0]["role"] == "assistant"
    assert "tool_calls" not in out[0]  # demoted to a plain assistant
    assert out[0]["content"] == "hi"
    assert out[1]["role"] == "system"


def test_partial_dangling_keeps_answered_call():
    # assistant([A,B]) but only A answered -> keep A (+its tool), drop B.
    msgs = [
        {"role": "assistant", "tool_calls": [
            {"id": "A", "function": {"name": "x"}},
            {"id": "B", "function": {"name": "y"}},
        ]},
        {"role": "tool", "tool_call_id": "A", "content": "ra"},
    ]
    out = normalize(msgs)
    assert out[0]["role"] == "assistant"
    assert [tc["id"] for tc in out[0]["tool_calls"]] == ["A"]
    assert out[1]["role"] == "tool" and out[1]["tool_call_id"] == "A"
    _assert_contract(out)


def test_orphan_tool_message_dropped():
    # A tool response whose tool_call has no assistant -> dropped (no owner block).
    msgs = [
        {"role": "user", "content": "u"},
        {"role": "tool", "tool_call_id": "ghost", "content": "orphan"},
        {"role": "assistant", "content": "ok"},
    ]
    out = normalize(msgs)
    assert _roles(out) == ["user", "assistant"]
