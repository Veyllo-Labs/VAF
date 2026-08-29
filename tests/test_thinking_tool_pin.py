# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A background thinking run must always own a tool that ENDS it.

The per-turn tool set is picked by an LLM router and then narrowed again by a size cap and
several safety nets. A forced node additionally sets tool_choice="required". If a narrowing
drops `thinking_done`, the model is obliged to emit a tool call and owns nothing that stops
the turn - a deadlock by construction, and a measured one: a run spent 12 tool turns
re-calling a rejecting `ask_user` because `thinking_done` was not in its set.

Two halves are pinned here, matching the two mechanisms in the product: the pure cap helper
(`_apply_tool_cap`) and the after-narrowing backstop (`_ensure_thinking_exit_tools`)."""
import types

from vaf.core.agent import Agent, _THINKING_EXIT_TOOLS, _apply_tool_cap


def _obj(run_kind="thinking", active=None, tools=None):
    """Stand-in carrying the REAL run-kind predicate, like the read-cap tests do.

    Re-implementing `_is_thinking_run` here would keep the test green if the two ever
    disagreed, which is the one thing it exists to catch."""
    ns = types.SimpleNamespace(
        _run_kind=run_kind,
        _active_tools=active,
        tools={name: object() for name in (tools if tools is not None else _THINKING_EXIT_TOOLS)},
    )
    ns._is_thinking_run = types.MethodType(Agent._is_thinking_run, ns)
    ns._ensure_thinking_exit_tools = types.MethodType(Agent._ensure_thinking_exit_tools, ns)
    return ns


# ── the pure cap helper ───────────────────────────────────────────────────────────────────

def test_pinned_tool_survives_the_cap_from_the_last_position():
    """The exact shape that broke: the exit tool sits past the cap in the incoming order.

    Before the fix the cap pinned only the discovery tools and truncated the rest, so a
    merely force-included `thinking_done` at index 19 of 20 was cut. Sorting the set first
    made WHICH tool got cut reproducible; pinning makes it survive."""
    incoming = [f"tool_{i:02d}" for i in range(19)] + ["thinking_done"]
    out = _apply_tool_cap(incoming, 12, {"list_tools", "search_tools", "thinking_done"})
    assert "thinking_done" in out
    assert len(out) == 12


def test_pins_are_never_sacrificed_to_a_tiny_cap():
    incoming = ["a", "b", "c", "thinking_done", "ask_user"]
    out = _apply_tool_cap(incoming, 2, {"thinking_done", "ask_user"})
    assert set(out) == {"thinking_done", "ask_user"}


def test_cap_preserves_order_and_is_a_noop_below_the_limit():
    incoming = ["c", "a", "b"]
    assert _apply_tool_cap(incoming, 12, set()) == ["c", "a", "b"]


def test_cap_survives_a_broken_limit():
    """Fail open, never with a traceback: a bad config value must not empty the tool set."""
    incoming = ["a", "b"]
    assert _apply_tool_cap(incoming, None, set()) == ["a", "b"]


# ── the after-narrowing backstop ──────────────────────────────────────────────────────────

def test_backstop_restores_a_dropped_exit_tool():
    o = _obj(active=["list_tools", "search_tools"])
    o._ensure_thinking_exit_tools()
    assert "thinking_done" in o._active_tools
    assert "ask_user" in o._active_tools


def test_backstop_does_not_duplicate():
    o = _obj(active=["thinking_done", "ask_user"])
    o._ensure_thinking_exit_tools()
    assert o._active_tools.count("thinking_done") == 1


def test_backstop_is_off_outside_a_thinking_run():
    """A normal chat turn keeps its router selection untouched - `thinking_done` is not even
    registered there, and pinning it would be a tool the main agent must never see."""
    o = _obj(run_kind=None, active=["list_tools"])
    o._ensure_thinking_exit_tools()
    assert o._active_tools == ["list_tools"]


def test_backstop_leaves_the_all_tools_case_alone():
    o = _obj(active=None)
    o._ensure_thinking_exit_tools()
    assert o._active_tools is None


def test_backstop_only_pins_registered_tools():
    """`ask_user` is thinking-mode-only; a run whose registry lacks one of the pair must not
    gain a name that does not resolve to a tool."""
    o = _obj(active=["list_tools"], tools=["thinking_done"])
    o._ensure_thinking_exit_tools()
    assert o._active_tools == ["list_tools", "thinking_done"]
