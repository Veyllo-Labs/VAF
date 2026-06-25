# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: tool-call pairing must never emit an orphaned role:"tool".

Pins the fix for the DeepSeek 400 "Messages with role 'tool' must be a response
to a preceding message with 'tool_calls'". Root cause was a session whose history
got an early turn DUPLICATED (verbatim-duplicate tool_call_ids); the old cleanup
in Agent._prepare_messages used a single global "first response index per id" map,
which stripped the 2nd assistant's tool_calls while keeping the 2nd tool result
-> orphaned role:"tool" -> 400. The new cleanup does greedy nearest-following-
UNCLAIMED pairing, so duplicate ids each pair correctly.

Pure list transform — no Agent init / network: we bind the unbound method to a
minimal stub (provider="local" avoids any config/disk access).
"""
from vaf.core.agent import Agent


class _Stub:
    """Minimal `self` for Agent._prepare_messages (pure pairing path).

    provider="local" skips the per-provider config/disk read; filename avoids the
    Gemma-detection default; _consolidate_system_messages is a no-op passthrough
    (it runs AFTER the tool-pairing we are testing)."""
    _thinking_reply_context = None
    provider = "local"
    filename = "local"
    model_display_name = ""

    def _consolidate_system_messages(self, messages):
        return messages


def _prepare(messages):
    return Agent._prepare_messages(_Stub(), messages)


def _orphan_count(msgs):
    """A role:"tool" is an orphan unless the immediately preceding message is an
    assistant whose tool_calls contain its tool_call_id (the API's requirement)."""
    bad = 0
    for i, m in enumerate(msgs):
        if m.get("role") == "tool":
            prev = msgs[i - 1] if i > 0 else None
            ok = bool(
                prev
                and prev.get("role") == "assistant"
                and prev.get("tool_calls")
                and any(tc.get("id") == m.get("tool_call_id") for tc in prev["tool_calls"])
            )
            if not ok:
                bad += 1
    return bad


def test_duplicate_tool_call_ids_produce_no_orphan():
    """The reported bug shape: the same id pair appears twice. BOTH pairs must
    survive intact, with zero orphaned tool results."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "X", "function": {"name": "t"}}]},
        {"role": "tool", "content": "r1", "tool_call_id": "X"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "X", "function": {"name": "t"}}]},
        {"role": "tool", "content": "r2", "tool_call_id": "X"},
    ]
    out = _prepare(msgs)
    assert _orphan_count(out) == 0
    # both assistant tool_calls retained, both results retained
    assert sum(1 for m in out if m.get("role") == "tool") == 2
    assert sum(1 for m in out if m.get("role") == "assistant" and m.get("tool_calls")) == 2


def test_clean_unique_ids_unchanged():
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {"name": "t"}}]},
        {"role": "tool", "content": "r", "tool_call_id": "a"},
        {"role": "assistant", "content": "answer"},
    ]
    out = _prepare(msgs)
    assert _orphan_count(out) == 0
    assert sum(1 for m in out if m.get("role") == "tool") == 1
    assert out[1].get("tool_calls")  # the (only) call kept


def test_dangling_tool_call_is_stripped():
    """An assistant tool_call with no following result must lose its tool_calls
    (not become an unanswered tool_use)."""
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "z", "function": {"name": "t"}}]},
        {"role": "assistant", "content": "text"},
    ]
    out = _prepare(msgs)
    assert "tool_calls" not in out[0]


def test_orphan_tool_result_without_preceding_call_is_dropped():
    """A role:"tool" whose call is absent must be removed, not passed through."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "tool", "content": "stray", "tool_call_id": "ghost"},
        {"role": "assistant", "content": "hi"},
    ]
    out = _prepare(msgs)
    assert _orphan_count(out) == 0
    assert all(m.get("role") != "tool" for m in out)
