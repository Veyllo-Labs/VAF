# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Emitting an event, and why the debug mirror is NOT part of it.

The dispatcher used to do two things in one closure: hand each event to the caller's sink,
and mirror it into the sub-agent debug log. That entanglement is invisible while there is
only one caller. It stops being invisible the moment a second one arrives: the workflow
engine does not write ``events.jsonl`` today, and moving the pair wholesale into the shared
path would hand it a debug artifact it never produced - a behaviour change smuggled in
through a refactor, and exactly the kind nobody sees in a diff.

So they are two named things. ``emit_event`` is the dispatch primitive: notify, never veto,
never let a broken observer take the call down. ``with_subagent_debug_mirror`` is an
aspect of the CHAT lane that the chat lane applies to its own sink.

The subtlety worth a test of its own: the mirror fires even when there is NO sink. In the web
app ``_event_sink`` is frequently None while the debug log is still wanted, so "no sink" must
not silently mean "no debug log". Nesting the mirror inside a sink-conditional branch is the
obvious tidy-up and would break that.
"""
import pytest

from vaf.core.tool_dispatch import emit_event, with_subagent_debug_mirror


class _Logger:
    def __init__(self, explode=False):
        self.events = []
        self._explode = explode

    def event(self, kind, payload=None):
        if self._explode:
            raise RuntimeError("debug log unavailable")
        self.events.append((kind, payload))


@pytest.fixture
def logger(monkeypatch):
    lg = _Logger()
    monkeypatch.setattr("vaf.core.subagent_debug.get_subagent_logger_from_env", lambda: lg)
    return lg


@pytest.fixture
def no_logger(monkeypatch):
    monkeypatch.setattr("vaf.core.subagent_debug.get_subagent_logger_from_env", lambda: None)


# ── emit_event: the dispatch primitive ───────────────────────────────────────

def test_the_sink_receives_the_event():
    got = []
    emit_event(got.append, {"type": "tool_start", "tool": "probe"})
    assert got == [{"type": "tool_start", "tool": "probe"}]


@pytest.mark.parametrize("sink", [None, "not callable", 42])
def test_a_missing_or_unusable_sink_is_not_an_error(sink):
    emit_event(sink, {"type": "tool_end"})


def test_a_raising_sink_does_not_break_the_dispatch():
    """Observation is fail-OPEN, the opposite of a gate: a broken observer must not take a
    tool call down, while a broken guard must never degrade to 'allowed'."""
    def _boom(evt):
        raise RuntimeError("consumer exploded")

    emit_event(_boom, {"type": "tool_start"})


def test_the_sinks_return_value_is_ignored():
    """A notification, not a veto. A caller wanting a say gets it before dispatch."""
    assert emit_event(lambda evt: False, {"type": "tool_start"}) is None


def test_emit_event_alone_does_not_touch_the_debug_log(logger):
    """THE separation. This is what a non-chat caller gets - and must keep getting."""
    emit_event(lambda evt: None, {"type": "tool_start", "tool": "probe"})
    assert logger.events == [], "the shared path wrote events.jsonl"


# ── the mirror: an aspect of the chat lane ───────────────────────────────────

def test_the_mirror_forwards_to_the_sink_and_the_log(logger):
    got = []
    with_subagent_debug_mirror(got.append)({"type": "tool_end", "tool": "probe"})
    assert got == [{"type": "tool_end", "tool": "probe"}]
    assert logger.events == [("agent_event", {"type": "tool_end", "tool": "probe"})]


def test_the_mirror_fires_even_without_a_sink(logger):
    """The subtlety: in the web app the sink is often None while the debug log is still
    wanted. Nesting the mirror inside a sink-conditional is the obvious tidy-up, and wrong."""
    with_subagent_debug_mirror(None)({"type": "tool_start", "tool": "probe"})
    assert logger.events == [("agent_event", {"type": "tool_start", "tool": "probe"})]


def test_a_raising_sink_still_reaches_the_log(logger):
    """The debug log is what you read when something went wrong, so a broken consumer must
    not be able to silence it."""
    def _boom(evt):
        raise RuntimeError("consumer exploded")

    with_subagent_debug_mirror(_boom)({"type": "tool_end"})
    assert logger.events == [("agent_event", {"type": "tool_end"})]


def test_a_broken_debug_log_still_lets_the_sink_through(monkeypatch):
    """And the other direction: the live UI must not go quiet because a log file cannot be
    written."""
    monkeypatch.setattr("vaf.core.subagent_debug.get_subagent_logger_from_env",
                        lambda: _Logger(explode=True))
    got = []
    with_subagent_debug_mirror(got.append)({"type": "tool_start"})
    assert got == [{"type": "tool_start"}]


def test_no_debug_logger_configured_is_the_normal_case(no_logger):
    """Outside a sub-agent terminal there is no logger at all; that is not an error path."""
    got = []
    with_subagent_debug_mirror(got.append)({"type": "tool_start"})
    assert got == [{"type": "tool_start"}]


def test_the_chat_dispatcher_applies_the_mirror_itself():
    """Pins WHERE the mirror is applied. If it drifts into the shared path, the engine starts
    writing a debug artifact it never produced - the reason these are two functions."""
    import inspect

    from vaf.core.agent import Agent
    from vaf.core import tool_dispatch

    assert "_with_subagent_debug_mirror" in inspect.getsource(Agent.execute_tool), (
        "the chat lane no longer applies the mirror to its own sink"
    )
    assert "get_subagent_logger_from_env" not in inspect.getsource(tool_dispatch.emit_event), (
        "the shared emit path mirrors to the debug log - every future caller inherits it"
    )
