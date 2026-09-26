# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A long turn keeps its request, and the <user_intent> block names the message being answered.

MEASURED LIVE, one 37-round tool turn ("start a client on a virtual display, take a screenshot,
look at it"): the context crossed the compression limit at round 20, and from then on the
compression ran after every tool result, keeping "system + the recent messages". By round ~34
the person's own request had left that window. What remained was the <user_intent> block - and
that block was one message behind in every turn, because the system prompt was built before
the new intent was written (prompt 14:43:29, intent 14:43:32). The model read the previous
message as "the authoritative current task", re-ran it and called the actual task an accident.
The saved chat then had no steps for the turn at all: the save anchors on the request.

Driven through the REAL Agent.chat_step with only the model replaced.

MUTATION: write the intent after the prompt build again and the first test goes red; let a
non-person turn write it and the second goes red; drop the anchor from ContextManager.compress
and the compression and long-turn tests go red.
"""
import json
import re

import pytest

from vaf.core.context import CONTEXT_RESTORED_PREFIX, ContextManager, turn_anchor_index
from vaf.core.platform import Platform
from vaf.core.session import turn_context_messages_since_last_user
from vaf.tools.base import BaseTool

SESSION = "green123456"


@pytest.fixture
def make_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))

    def _make(**overrides):
        from vaf.core.agent import Agent
        cfg = {"provider": "openai", "api_key_openai": "sk-test", **overrides}
        a = Agent(register_signals=False, run_kind="chat", config_overrides=cfg)
        a.current_session_id = SESSION
        a._current_chat_source = "web"
        a.init_chat()
        a._bind_session_persistence(SESSION)
        return a
    return _make


class _Model:
    """The main call answers `steps` tool rounds, then a final answer; side calls say ok."""

    def __init__(self, steps=0, tool="list_files"):
        self.steps, self.tool, self.main, self.seen = steps, tool, 0, []

    def __call__(self, messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        self.main += 1
        self.seen.append(messages)
        if self.main > self.steps:
            yield "Fertig."
            return
        yield "Ich schaue nach."
        yield json.dumps({"tool_calls": [{"index": 0, "id": f"c{self.main}", "type": "function",
                                          "function": {"name": self.tool,
                                                       "arguments": json.dumps({"n": self.main})}}]})
        yield json.dumps({"finish_reason": "tool_calls"})


def _intent_block(messages):
    head = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
    m = re.search(r"<user_intent>\s*(.*?)\s*</user_intent>", head, re.S)
    return m.group(1) if m else None


def _turn(agent, text, model, **kw):
    agent.api_backend.chat_completion = model
    agent.chat_step(user_input=text, stream_callback=lambda t: None, **kw)
    return model


# ── the <user_intent> block ──────────────────────────────────────────────────────────

def test_the_intent_block_names_the_message_being_answered(make_agent):
    agent = make_agent()
    _turn(agent, "Aufgabe A: zeig mir die Dateien", _Model())
    seen = _turn(agent, "Aufgabe B: etwas ganz anderes", _Model()).seen[0]
    assert _intent_block(seen) == "Aufgabe B: etwas ganz anderes"


def test_a_turn_nobody_typed_does_not_become_the_goal(make_agent):
    """A wake ("background command finished"), a timer, a drain turn: VAF wrote the text."""
    agent = make_agent()
    _turn(agent, "Aufgabe A: bau das Plugin", _Model())
    agent._turn_is_human = False
    seen = _turn(agent, "Background command finished: mvn package (exit 0)", _Model()).seen[0]
    assert _intent_block(seen) == "Aufgabe A: bau das Plugin"
    assert agent.main_persistence.get_user_intent() == "Aufgabe A: bau das Plugin"


def test_the_goal_is_the_persons_own_words(make_agent):
    agent = make_agent()
    enriched = "[SESSION WORKSPACE] All files for this chat are stored in: /tmp/x\n\nStarte den Server"
    seen = _turn(agent, enriched, _Model(), raw_user_input="Starte den Server").seen[0]
    assert _intent_block(seen) == "Starte den Server"


# ── the compression keeps the request ───────────────────────────────────────────────

def _long_turn(request="Starte den Client und mach einen Screenshot", rounds=30):
    history = [{"role": "system", "content": "SYSTEM"},
               {"role": "user", "content": "Vorige Aufgabe"},
               {"role": "assistant", "content": "Erledigt."},
               {"role": "user", "content": request}]
    for i in range(rounds):
        history.append({"role": "assistant", "content": "",
                        "tool_calls": [{"id": f"c{i}", "function": {"name": "host_bash", "arguments": "{}"}}]})
        history.append({"role": "tool", "name": "host_bash", "tool_call_id": f"c{i}", "content": f"output {i}"})
    return history


def test_the_compression_keeps_the_request_of_the_turn():
    cm = ContextManager(max_tokens=128000)
    cm.recent_memory_size = 12
    request = "Starte den Client und mach einen Screenshot"
    out = cm.compress(_long_turn(request))
    assert any(m.get("role") == "user" and m.get("content") == request for m in out)
    anchor = turn_anchor_index(out)
    assert out[anchor]["content"] == request
    assert out[anchor + 1:] == _long_turn(request)[-12:], "the request sits right before the kept steps"
    assert turn_context_messages_since_last_user(out, request), "the save finds the turn's steps"


def test_a_checkpoints_restored_context_is_not_a_request():
    history = [{"role": "system", "content": "S"}, {"role": "user", "content": "Die Anfrage"},
               {"role": "user", "content": f"{CONTEXT_RESTORED_PREFIX}\nsummary"},
               {"role": "assistant", "content": "ok"}]
    assert turn_anchor_index(history) == 1
    assert turn_anchor_index([{"role": "system", "content": "S"}]) is None


class _BigOutput(BaseTool):
    name = "big_output"
    description = "Read a long log."
    parameters = {"type": "object", "properties": {"n": {"type": "integer"}}}

    def run(self, **kwargs):
        return f"log part {kwargs.get('n')}: " + "x" * 3000


def test_a_long_turn_keeps_its_request_to_the_end(make_agent):
    """The live incident, small: an 8000-token budget keeps 12 recent messages, so eight rounds
    of large results (sixteen messages) push the request out unless the compression pins it.
    Eight, not more: ten calls within five seconds trip the runaway brake and end the turn early."""
    agent = make_agent(context_compress_tokens=8000)
    agent.tools["big_output"] = _BigOutput()
    agent._active_tools = None
    request = "Lies das ganze Log und fasse es zusammen"
    model = _turn(agent, request, _Model(steps=8, tool="big_output"))
    last = model.seen[-1]
    assert not any("log part 1:" in str(m.get("content")) for m in last), \
        "the setup is real: the compression ran inside the turn and dropped the first result"
    assert any(m.get("role") == "user" and request in str(m.get("content")) for m in last), \
        "the model still sees the request at the end of the turn"
    assert agent.history[-1] == {"role": "assistant", "content": "Fertig."}, "the turn ran to its answer"
    anchor = turn_anchor_index(agent.history)
    assert anchor is not None and agent.history[anchor]["content"] == request
    assert turn_context_messages_since_last_user(agent.history, request), \
        "the saved chat gets the turn's steps (or their summary)"


def test_the_turn_end_squash_starts_after_the_request(make_agent, monkeypatch):
    """The squash took its start from an index noted before the turn's compressions rebuilt the
    list. With context pressure at the final answer it has to fold exactly the steps after the
    request. MUTATION: start at history_snapshot_len + 1 again - red."""
    agent = make_agent(context_compress_tokens=8000)
    agent.tools["big_output"] = _BigOutput()
    agent._active_tools = None
    request = "Lies das ganze Log und fasse es zusammen"
    model = _Model(steps=8, tool="big_output")
    monkeypatch.setattr(agent.context_manager, "should_compress",
                        lambda history: model.main > model.steps)   # pressure at the final answer only
    _turn(agent, request, model)
    anchor = turn_anchor_index(agent.history)
    assert agent.history[anchor]["content"] == request
    assert str(agent.history[anchor + 1]["content"]).startswith("[Context:")
    assert agent.history[anchor + 2]["role"] == "assistant" and agent.history[-1] is agent.history[anchor + 2]
