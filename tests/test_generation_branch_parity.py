# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""All three generation lanes send PREPARED messages (vaf/core/agent.py).

`chat_step` picks one of three lanes: the API backend, the local llama-server,
and llama-cpp-python in-process. The third one sent `self.history` raw, so on
that lane every pre-send repair was skipped - dangling tool_calls were not
stripped, orphaned `role:tool` messages stayed, images were never converted to
text, the memory block was dropped, and `disable_tools` / `_force_tool_choice`
/ the adaptive temperature were ignored. Three sibling branches with the safety
pass in two is Rule 2's "copies" defect applied to code.

It was reachable, just not here: the lane needs not-py3.13, not macOS and no
force_server, i.e. Linux with Python <= 3.12 in local mode (Ubuntu 24.04 ships
3.12). This machine runs 3.13, so the live app takes the server lane - which is
exactly why this is a test and not a live check.

The assertion is structural (AST over chat_step), because driving the branch
functionally would need a loaded model. It proves the wiring exists, not that
the model behaves - `tests/test_history_dedup.py` and friends cover what
`_prepare_messages` actually does.
"""
import ast
from pathlib import Path

import pytest

SOURCE = Path("vaf/core/agent.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chat_step_node():
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "chat_step":
            return node
    pytest.fail("chat_step not found - the loop was renamed")


def _messages_kwargs(node, *, literals=False):
    """Every `messages=<expr>` of a call inside chat_step, as source.

    Literal lists are one-shot side prompts (e.g. the translation call), not a
    conversation lane, so they are excluded unless asked for.
    """
    out = []
    for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
        for kw in call.keywords:
            if kw.arg == "messages":
                if isinstance(kw.value, (ast.List, ast.ListComp)) and not literals:
                    continue
                out.append(ast.unparse(kw.value))
    return out


def test_no_generation_lane_sends_the_raw_history(chat_step_node):
    raw = [expr for expr in _messages_kwargs(chat_step_node) if expr == "self.history"]
    assert raw == [], (
        "a generation lane sends self.history unprepared again: dangling tool_calls, "
        "orphaned role:tool messages and images would reach the model untouched"
    )


def test_all_three_conversation_lanes_send_prepared_messages(chat_step_node):
    sent = _messages_kwargs(chat_step_node)
    assert len(sent) >= 3, f"expected the three generation lanes, found {sent}"
    assert all("prepared" in expr or "payload" in expr for expr in sent), \
        f"a lane sends an unprepared list: {sent}"


def test_the_library_lane_honours_the_turn_flags(chat_step_node):
    """disable_tools, _force_tool_choice and the adaptive temperature were
    ignored on that lane: it hardcoded tools, tool_choice='auto' and the base
    temperature."""
    # The flags must be read on the library lane specifically.
    lib = SOURCE[SOURCE.index("# Library Logic (llama-cpp-python)"):]
    lib = lib[: lib.index("create_chat_completion") + 400]
    assert "disable_tools" in lib, "the library lane ignores disable_tools again"
    assert "_force_tool_choice" in lib, "the library lane ignores a forced tool choice again"
    assert "current_temp" in lib, "the library lane ignores the adaptive temperature again"
    assert "_splice_memory_block" in lib, "the library lane drops the memory block again"


def test_the_server_lane_still_prepares_per_attempt():
    """The retry loop rebinds self.history between attempts (compression,
    pruning, 400/500 recovery), so a value computed once above the branch would
    make that recovery a no-op. The call must stay INSIDE the loop."""
    start = SOURCE.index("for _attempt in range(15):")
    end = SOURCE.index("# Library Logic (llama-cpp-python)")
    assert "_prepare_messages(self.history)" in SOURCE[start:end], \
        "the server lane stopped re-preparing per attempt"
