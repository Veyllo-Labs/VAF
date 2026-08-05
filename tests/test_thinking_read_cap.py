# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Thinking-mode read-tool cap: a background run must not spin on memory_search / list_* (the redundant
block only catches EXACT-arg duplicates). The cap blocks the Nth call by NAME within one step. It is
gated by the agent's own run kind - NOT the process environment, which is shared across threads, so a
background pass used to impose this cap on a concurrent human's turn. Tested directly on the method via
a bare object (no model load needed): the stand-in carries the run kind the real thinking agent is
constructed with."""
import types

from vaf.core.agent import Agent
from vaf.core.config import Config

_cap = Agent._thinking_read_cap_step


def _obj(run_kind="thinking"):
    """The stand-in declares its run kind, like the real thinking agent does.

    It carries the REAL predicate rather than a re-implementation: the cap asks
    `self._is_thinking_run()`, and a stand-in that answered that question its own way
    would keep passing if the two ever disagreed.
    """
    ns = types.SimpleNamespace(_run_kind=run_kind)
    ns._is_thinking_run = types.MethodType(Agent._is_thinking_run, ns)
    return ns


def test_blocks_third_read_call():
    o = _obj()
    assert _cap(o, "memory_search") is None        # 1
    assert _cap(o, "memory_search") is None        # 2
    blocked = _cap(o, "memory_search")             # 3 -> blocked (default cap 3)
    assert blocked and "memory_search" in blocked


def test_per_tool_counter_is_independent():
    o = _obj()
    assert _cap(o, "memory_search") is None
    assert _cap(o, "list_automation_notes") is None   # different tool, own counter
    assert _cap(o, "memory_search") is None
    assert _cap(o, "list_automation_notes") is None


def test_web_search_is_capped():
    o = _obj()
    assert _cap(o, "web_search") is None        # 1
    assert _cap(o, "web_search") is None        # 2
    assert _cap(o, "web_search") is not None     # 3 -> blocked (web_search spin was the 15:38 failure)


def test_non_read_tool_never_blocked():
    o = _obj()
    for _ in range(6):
        assert _cap(o, "ask_user") is None         # a decisive/progress tool is never capped


def test_off_outside_thinking_mode():
    # A chat turn must never be capped - and before this, a concurrent background
    # pass imposed the cap on exactly such a turn through the shared environment.
    o = _obj(run_kind="chat")
    for _ in range(6):
        assert _cap(o, "memory_search") is None


def test_disabled_via_config(monkeypatch):
    monkeypatch.setattr(Config, "get",
                        lambda k, d=None: False if k == "thinking_read_cap_enabled" else d)
    o = _obj()
    for _ in range(6):
        assert _cap(o, "memory_search") is None


def test_custom_cap(monkeypatch):
    monkeypatch.setattr(Config, "get",
                        lambda k, d=None: 2 if k == "thinking_read_cap_per_tool" else d)
    o = _obj()
    assert _cap(o, "memory_search") is None     # 1
    assert _cap(o, "memory_search") is not None  # 2 -> blocked at cap 2


def test_forced_node_blocks_gather_on_first_call():
    """On a forced-resolution node (_thinking_force_progress), gather tools are blocked from call #1 so a
    forced tool_choice='required' can only be satisfied by a decisive tool."""
    o = _obj()
    o._thinking_force_progress = True
    blocked = _cap(o, "web_search")
    assert blocked is not None and "Gathering is disabled" in blocked
    assert _cap(o, "memory_search") is not None
    # a decisive/progress tool is still allowed even on a forced node
    assert _cap(o, "ask_user") is None
    assert _cap(o, "delete_automation_note") is None
