# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The coder's idle watchdog counts a long tool call as work, not as a silent model.

AgenticLoop.should_continue ends a run past two idle minutes. Only the model's reply reset
the clock, so in a live run a five-minute browser_agent call was followed at once by "Model
stopped responding (idle for 5 min)" and a PARTIAL run with three tasks never started."""
import inspect
import time

from vaf.tools import coder


def test_two_idle_minutes_end_the_loop_and_activity_resets_the_clock():
    loop = coder.AgenticLoop()
    loop.last_activity = time.time() - 334            # the measured browser_agent run
    go, reason = loop.should_continue()
    assert go is False and "stopped responding" in reason
    loop.record_activity()
    assert loop.should_continue() == (True, "")


def test_every_tool_result_counts_as_activity():
    """MUTATION: drop loop.record_activity() from the result append - red."""
    src = inspect.getsource(coder.CodingAgentTool.run)
    start = src.index('Adding tool result to history: fn_name=')
    block = src[start:src.index("loop.guard_seq += 1", start)]
    assert "loop.record_activity()" in block
