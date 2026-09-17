# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The seamless compression of a long tool output (ContextManager.process_tool_output,
CONTEXT_MANAGEMENT.md): head and tail never overlap. An output a few lines over the raw
limit used to show its middle lines twice around a "0 lines hidden" seam, because the tail
was cut from the end without regard to where the head stopped.

MUTATION: cut the tail as `lines[-tail_lines:]` again and the first test goes red."""
from vaf.core.context import ContextManager


def _lines(n):
    return "\n".join(f"line {i:03d}" for i in range(n))


def test_the_head_and_the_tail_never_overlap():
    cm = ContextManager(max_tokens=32000)    # head 40, tail 30, pruned from 60 lines on
    out = cm.process_tool_output("read_file", _lines(65))
    for i in range(65):
        assert out.count(f"line {i:03d}") == 1, i
    assert "[... 0 lines hidden ...]" in out


def test_the_hidden_count_is_what_lies_between_head_and_tail():
    cm = ContextManager(max_tokens=32000)
    out = cm.process_tool_output("read_file", _lines(100))
    assert "line 039" in out and "line 040" not in out and "line 069" not in out and "line 070" in out
    assert "[... 30 lines hidden ...]" in out
