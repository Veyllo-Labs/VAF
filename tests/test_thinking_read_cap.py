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


def _obj(run_kind="thinking", node=""):
    """The stand-in declares its run kind and its ladder node, like the real thinking agent does.

    It carries the REAL predicates rather than re-implementations: the cap asks
    `self._is_thinking_run()` and `self._forcing_this_generation()`, and a stand-in that
    answered either question its own way would keep passing if the two ever disagreed.
    """
    ns = types.SimpleNamespace(_run_kind=run_kind, _thinking_node=node)
    ns._is_thinking_run = types.MethodType(Agent._is_thinking_run, ns)
    ns._forcing_this_generation = types.MethodType(Agent._forcing_this_generation, ns)
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
    """On a forced-resolution node, gather tools are blocked from call #1 so a forced
    tool_choice='required' can only be satisfied by a decisive tool.

    Sets the two REAL attributes the force is derived from, not a stored twin of the answer:
    that twin existed, was never reset, and kept the block alive for the whole step."""
    o = _obj(node="forced_item")
    o._force_tool_choice = "required"
    o._force_tool_choice_used = False
    blocked = _cap(o, "web_search")
    assert blocked is not None and "Gathering is disabled" in blocked
    assert _cap(o, "memory_search") is not None
    # a decisive/progress tool is still allowed even on a forced node
    assert _cap(o, "ask_user") is None
    assert _cap(o, "delete_automation_note") is None


def test_force_progress_is_derived_not_stored():
    """The block covers ONLY the generation that is actually forced.

    `_force_tool_choice_used` flips to True after the first generation. The stored twin never
    followed, so gather stayed blocked for the rest of the step - which is how a node that had
    no open item kept telling the model to resolve one, with listing blocked so it could never
    find out there was none."""
    o = _obj(node="forced_item")
    o._force_tool_choice = "required"
    o._force_tool_choice_used = False
    assert _cap(o, "web_search") is not None          # forced generation -> blocked outright
    o._force_tool_choice_used = True                  # the force is spent
    assert _cap(o, "web_search") is None              # gather is allowed again (still counted)


def test_getto_node_nudge_is_answerable_on_that_node():
    """The get-to-know node has no open item, so the housekeeping text is unsatisfiable there.

    The nudge must name what this node CAN do (ask a question / finish) and must not demand a
    note id, which is unobtainable because listing notes is exactly what is blocked."""
    o = _obj(node="getto")
    o._force_tool_choice = "required"
    o._force_tool_choice_used = False
    blocked = _cap(o, "list_automation_notes")
    assert blocked is not None
    assert "ask_user(message=" in blocked
    # The two unsatisfiable demands must be gone as INSTRUCTIONS. The nudge may still name them
    # to cancel them explicitly, which is why this asserts on the imperative forms.
    assert "you must resolve the open item" not in blocked
    assert "delete_automation_note(note_id=" not in blocked
    assert blocked.startswith("[BLOCKED]")   # tool_result_is_error() keys on this lead
