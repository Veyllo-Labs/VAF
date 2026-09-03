# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Two seams a stranger can reach without patching the loop, and one answer six
surfaces used to give differently.

The compaction hook: the signal was computed and thrown away - `_compress_history_if_needed`
returned whether it ran, and the one caller in the turn bound the answer to a name nothing
read. Now an application puts back what a summary loses, on both paths that compact, bounded
and forgiving.

Tool visibility: `_excluded_tools` hid a tool from the model's schema and from nothing else.
Measured with one exclusion set: `list_tools`, `search_tools`, the router's own prompt, the
system prompt's tool documentation and the sandbox's tool bridge all still named it. One
function answers now, and every surface reads it.

The methods under test are lifted off the real class, the way the wake-up tests lift
theirs: what is pinned is the seam, not a model.
"""
import time
from pathlib import Path

import pytest

import vaf
from vaf.core.agent import Agent as _Real

ROOT = Path(__file__).resolve().parents[1]


class _Manager:
    """A context manager that compresses on demand and reports what it did."""

    def __init__(self, compress=True):
        self._compress = compress

    def should_compress(self, history):
        return self._compress

    def get_usage_percent(self, history):
        return 0.9

    def compress(self, history, working_memory=None):
        return history[:1] + [{"role": "system", "content": "[summary]"}]

    def estimate_tokens(self, history):
        return 42


class _Agent:
    _compress_history_if_needed = _Real._compress_history_if_needed
    _after_compaction = _Real._after_compaction
    set_compaction_hook = _Real.set_compaction_hook
    visible_tools = _Real.visible_tools
    COMPACTION_HOOK_SECONDS = 1.0

    def __init__(self, *, compress=True, tools=None):
        self.context_manager = _Manager(compress)
        self.history = [{"role": "system", "content": "sys"}] + [
            {"role": "user", "content": f"m{i}"} for i in range(6)]
        self.current_session_id = "s-1"
        self.tools = dict(tools or {})

    def get_token_usage(self):
        return 100, 1000

    def _sync_compression_limit(self, window):
        pass


# ── the compaction hook ────────────────────────────────────────────────────

def test_the_hook_runs_after_a_compaction_and_its_note_lands_in_the_history():
    """MUTATION: do not fire _after_compaction, or drop the returned note."""
    agent = _Agent()
    seen = []

    def hook(info):
        seen.append(info)
        return "TASK BOARD: deploy pending"

    agent.set_compaction_hook(hook)
    assert agent._compress_history_if_needed() is True
    assert seen == [{"before": 7, "after": 2, "tokens": 42, "session_id": "s-1"}]
    assert agent.history[-1] == {"role": "system", "content": "TASK BOARD: deploy pending"}


def test_nothing_fires_when_nothing_was_compacted():
    agent = _Agent(compress=False)
    agent.set_compaction_hook(lambda info: "never")
    assert agent._compress_history_if_needed() is False
    assert all(m.get("content") != "never" for m in agent.history)


@pytest.mark.parametrize("answer", [None, "", "   "])
def test_an_empty_answer_adds_nothing(answer):
    agent = _Agent()
    agent.set_compaction_hook(lambda info: answer)
    agent._compress_history_if_needed()
    assert agent.history[-1]["content"] == "[summary]"


def test_a_slow_hook_is_nothing_to_add_and_a_broken_one_is_swallowed():
    """MUTATION: call the hook unbounded, or let its exception escape.

    A timeout counts as no objection; a raising hook is an observer that must not
    fail a run. Both leave the compacted history exactly as the compaction left it.
    """
    slow = _Agent()
    slow.set_compaction_hook(lambda info: time.sleep(3) or "late")
    started = time.monotonic()
    slow._compress_history_if_needed()
    assert time.monotonic() - started < 2.5, "the hook held the turn"
    assert slow.history[-1]["content"] == "[summary]"

    broken = _Agent()

    def boom(info):
        raise RuntimeError("hook fell over")

    broken.set_compaction_hook(boom)
    broken._compress_history_if_needed()
    assert broken.history[-1]["content"] == "[summary]"


def test_the_hook_fires_on_both_paths_and_the_dead_signal_is_gone():
    """MUTATION: fire from the turn only, or bring `compression_happened` back.

    Both callers of _compress_history_if_needed - the turn and the session load -
    reach the hook because it fires INSIDE the seam; and the variable that used to
    receive the seam's answer and throw it away is deleted, not renamed.
    """
    source = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    seam = source.split("def _compress_history_if_needed", 1)[1].split("\n    def ", 1)[0]
    assert "self._after_compaction(old_count)" in seam
    assert "compression_happened" not in source
    assert source.count("self._compress_history_if_needed()") == 2, "a caller went missing"


def test_the_facade_carries_the_hook_to_the_engine_before_and_after_it_is_built():
    from unittest.mock import MagicMock, patch

    def hook(info):
        return None

    agent = vaf.Agent(config={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    agent.on_compaction(hook)
    with patch("vaf.framework.CoreAgent", return_value=MagicMock()) as built:
        engine = agent.core
    assert built.called
    engine.set_compaction_hook.assert_called_once_with(hook)
    agent.on_compaction(None)
    engine.set_compaction_hook.assert_called_with(None)


# ── one answer for the model's view of the registry ────────────────────────

class _Tool:
    def __init__(self, name, description="a tool"):
        self.name, self.description = name, description
        self.category = "misc"


def test_visible_tools_is_the_registry_minus_the_hidden_set():
    agent = _Agent(tools={"a": _Tool("a"), "hidden": _Tool("hidden")})
    assert set(agent.visible_tools()) == {"a", "hidden"}
    agent._excluded_tools = {"hidden"}
    assert set(agent.visible_tools()) == {"a"}
    assert "hidden" in agent.tools, "hidden is not forbidden: the registry keeps it"


def test_list_tools_and_search_tools_read_the_agents_answer():
    """MUTATION: read the static registry reference when the agent is at hand."""
    from vaf.tools.list_tools import ListToolsTool
    from vaf.tools.search_tools import SearchToolsTool

    registry = {"visible_one": _Tool("visible_one", "sends mail"),
                "hidden_one": _Tool("hidden_one", "sends mail too")}
    agent = _Agent(tools=registry)
    agent._excluded_tools = {"hidden_one"}

    listing = ListToolsTool()
    listing.available_tools = registry
    assert "hidden_one" in listing.run(), "without the agent the static reference is all it has"
    assert "hidden_one" not in listing.run(_agent=agent)
    assert "visible_one" in listing.run(_agent=agent)

    search = SearchToolsTool()
    search.available_tools = registry
    assert "hidden_one" in search.run(query="mail")
    found = search.run(query="mail", _agent=agent)
    assert "visible_one" in found and "hidden_one" not in found
    browse = search.run(query="zzzz", _agent=agent)
    assert "No close matches" in browse and "hidden_one" not in browse, "the fallback browses the registry"


def test_the_system_prompt_documents_only_what_the_model_may_see():
    """MUTATION: document every registered tool.

    The prompt's tool documentation is the model's other list of what it can call;
    a tool hidden from the schema and documented in the prompt is a tool the model
    will call and be refused, in front of the user, for no reason it can see.
    """
    from vaf.core.system_prompt import SystemPromptManager

    shown, hidden = _Tool("shown", "does a thing"), _Tool("hidden", "does another")
    shown.parameters = hidden.parameters = {"type": "object", "properties": {}}
    agent = _Agent(tools={"shown": shown, "hidden": hidden})
    agent._active_tools = None
    agent._excluded_tools = {"hidden"}
    manager = SystemPromptManager(tools=[shown, hidden], agent_instance=agent)

    documented = manager._build_tool_documentation()
    assert "shown" in documented
    assert "hidden" not in documented, "the prompt documents a tool the schema hides"


def test_every_model_facing_surface_reads_visible_tools():
    """Source guards for the four surfaces whose functions start a whole model or a
    whole prompt: the schema, the router prompt, the system prompt's tool
    documentation and the sandbox bridge."""
    agent = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    schema = agent.split("    def TOOLS(self)", 1)[1].split("\n    def ", 1)[0]
    assert "self.visible_tools().keys()" in schema, "the schema fell back to the whole registry"
    assert "for name, tool_instance in self.visible_tools().items():" in agent, "the router prompt"
    assert 'tool_names_list = ", ".join(sorted(self.visible_tools().keys()))' in agent, (
        "the router's allowed-names list names a hidden tool")
    prompt = (ROOT / "vaf" / "core" / "system_prompt.py").read_text(encoding="utf-8")
    assert "self.agent.visible_tools()" in prompt, "the system prompt's tool documentation"
    sandbox = (ROOT / "vaf" / "tools" / "python_sandbox.py").read_text(encoding="utf-8")
    assert "agent.visible_tools()" in sandbox, "the sandbox bridge"
    # and the two discovery tools are handed the agent by the dispatcher
    handed = 'if name in ("list_tools", "search_tools"):'
    assert handed in agent, "the dispatcher does not hand the agent to the discovery tools"
    after = agent.split(handed, 1)[1][:400]
    assert 'tool_args["_agent"] = self' in after


# ── the hook object on the facade ──────────────────────────────────────────

def test_the_pipelines_hook_object_is_public_and_gates_a_bare_caller():
    """The parameter was public and its type was not: `ToolCaller(hooks=...)` took an
    object a stranger could only import from a private path."""
    from vaf.core import tool_dispatch

    assert vaf.ToolCallHooks is tool_dispatch.ToolCallHooks

    class Echo(vaf.BaseTool):
        name = "echo"
        description = "says it back"
        parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

        def run(self, **kwargs):
            return f"echo: {kwargs.get('text', '')}"

    refused = vaf.ToolCallHooks(after_policy=lambda name, tool, args: "gated: not now")
    caller = vaf.ToolCaller({"echo": Echo()}, hooks=refused)
    assert caller.execute("echo", {"text": "hi"}) == "gated: not now"
    plain = vaf.ToolCaller({"echo": Echo()})
    assert plain.execute("echo", {"text": "hi"}) == "echo: hi"
