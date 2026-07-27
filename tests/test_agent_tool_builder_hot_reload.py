# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: a tool the agent had just written did not become usable until a restart.

``create_agent_tool`` writes a new custom tool to disk and then calls ``_hot_reload()`` so
the agent can use it in the same session - the dispatcher's comment says as much, "making the
new tool live immediately without a server restart".

It never did. The dispatcher injected ``_agent`` into the call, but the tool only ever READ
``self._agent`` and never assigned it from the arguments, so the class attribute stayed None
for the object's whole life. ``_hot_reload`` opens with "Does nothing if _agent was not
injected (e.g. in tests)" - and it did nothing always, in production too. The sibling
``agent_workflow_builder`` picks the value up correctly, which is what makes this an omission
rather than a design.

Nothing pointed at it: no test covered the reload path, the write itself succeeded, and the
tool did show up after the next restart. The symptom was a delay, and delays get attributed to
everything.

Found while auditing which plumbing injections are actually consumed - the question "does
anyone read this?" turned up a receiver that had simply forgotten to.
"""
import importlib
import inspect

import pytest

from vaf.tools.base import BaseTool


def _builder():
    mod = importlib.import_module("vaf.tools.agent_tool_builder")
    cls = next(o for _, o in inspect.getmembers(mod, inspect.isclass)
               if issubclass(o, BaseTool) and getattr(o, "name", None) == "create_agent_tool")
    return cls()


class _Agent:
    def __init__(self, explode=False):
        self.reloads = 0
        self._explode = explode

    def reload_custom_tools(self):
        self.reloads += 1
        if self._explode:
            raise RuntimeError("registry busy")


def test_the_injected_agent_is_picked_up():
    """THE regression. The dispatcher hands it over; the tool has to take it."""
    tool, agent = _builder(), _Agent()
    tool.run(action="list", _agent=agent)
    assert tool._agent is agent, (
        "the tool ignored the injected agent - _hot_reload() can then never run, and a "
        "newly written tool stays invisible until the next restart"
    )


def test_the_reload_actually_fires():
    """The point of picking it up: the whole feature is that the tool works this turn."""
    tool, agent = _builder(), _Agent()
    tool.run(action="list", _agent=agent)
    tool._hot_reload()
    assert agent.reloads == 1


def test_a_direct_call_does_not_wipe_an_agent_set_earlier():
    """Tests and non-dispatch callers invoke run() without _agent. Overwriting with None
    would re-create the bug for every call after the first."""
    tool, agent = _builder(), _Agent()
    tool.run(action="list", _agent=agent)
    tool.run(action="list")
    assert tool._agent is agent


def test_without_an_agent_it_stays_harmless():
    """The docstring's own promise: does nothing when nothing was injected."""
    tool = _builder()
    tool.run(action="list")
    assert tool._agent is None
    tool._hot_reload()   # must not raise


def test_a_failing_reload_is_not_fatal():
    """The file was written correctly either way; the next restart picks it up. Turning a
    reload hiccup into a tool error would lose the work the agent just did."""
    tool, agent = _builder(), _Agent(explode=True)
    tool.run(action="list", _agent=agent)
    tool._hot_reload()
    assert agent.reloads == 1


def test_the_sibling_builder_still_does_the_same_thing():
    """Both builders receive _agent from the dispatcher. They drifted once; pin that they
    agree, so a fix to one is not left out of the other."""
    src = inspect.getsource(importlib.import_module("vaf.tools.agent_workflow_builder"))
    assert 'kwargs.get("_agent")' in src, (
        "agent_workflow_builder no longer picks up the injected agent - the two builders "
        "have drifted apart again"
    )


@pytest.mark.parametrize("tool_name", ["create_agent_tool", "create_agent_workflow"])
def test_the_dispatcher_still_injects_the_agent_for_both(tool_name):
    """Belt and braces: the pickup is worthless if the injection goes away. The kwargs
    baseline pins this too, but from the other side."""
    import vaf.core.agent as agent_mod

    src = inspect.getsource(agent_mod.Agent.execute_tool)
    assert f'if name == "{tool_name}":' in src
