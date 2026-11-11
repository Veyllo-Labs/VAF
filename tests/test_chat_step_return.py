# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What `chat_step` returns, and what a tool-only round asks of the provider.

MEASURED BEFORE THE FIX: every normal turn returned "..." while it streamed the answer. The
post-loop check read `clean_content`, which was set to "" at the start and never assigned, so
the answer return and everything after it were unreachable. `vaf prompt --output-format json`
printed `"output": "..."` and an agent room never received a plain-text answer.

And a round that streamed nothing but tool-call frames paid for a second, non-streamed
request: the fallback for an empty stream ran before the streamed calls were collected, and
its JSON frames reached the person as text (5 of 282 rounds in a real backend log).

The REAL `chat_step` with only `api_backend.chat_completion` faked.

MUTATIONS, each turns the named test red:
- restore `if not clean_content:` after the loop (returns "...")
  -> test_a_plain_turn_returns_its_answer and the three after it;
- drop `and not tool_call_accumulator` from the non-stream fallback
  -> test_a_tool_only_round_makes_no_second_request;
- delete the kept-answers return after the loop -> test_the_return_is_the_kept_answers.
"""
import copy
import inspect
import json

import pytest

import vaf
from vaf.core.platform import Platform
from vaf.core.session import TurnStatus

SESSION = "green123456"
ASK = "Bitte installiere mir die Pakete aus der Liste"
TASK = "python_sandbox"


class _Install(vaf.BaseTool):
    """A stand-in for VAF's own python_sandbox."""
    __module__ = "vaf.tools.python_sandbox"
    name = TASK
    description = "Install Python packages."
    permission_level = "write"
    side_effect_class = "reversible"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return "10/10 installed"


def _tool_call(n):
    return json.dumps({"tool_calls": [{"index": 0, "id": f"c{n}", "type": "function",
                                       "function": {"name": TASK, "arguments": "{}"}}]})


@pytest.fixture
def agent(monkeypatch, tmp_path):
    """The real Agent on the API lane. `run(script)` drives one turn: each streamed main
    request takes the next step, a text, a list of chunks, or TOOL (announce, then call the
    tool). A non-streamed request answers with the round's tool call, as the real provider
    does, without taking a step."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    for var in ("VAF_IN_SUBAGENT_TERMINAL", "VAF_THINKING_MODE", "VAF_IN_AUTOMATION"):
        monkeypatch.delenv(var, raising=False)
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    monkeypatch.setattr(a, "_try_workflow", lambda *x, **k: None)
    monkeypatch.setattr(a, "_route_tools", lambda text: [TASK])
    monkeypatch.setattr(a, "get_token_usage", lambda **k: (1000, 128000))
    monkeypatch.setattr(a, "analyze_intent", lambda *x, **k: 0.7)
    monkeypatch.setattr(a, "_detect_ungrounded_result_claim", lambda text, res: (False, None))
    a.tools[TASK] = _Install()
    a.current_session_id = SESSION
    a._bind_session_persistence(SESSION)
    a.requests = []

    def run(script):
        script = list(script)

        def provider(messages=None, **kw):
            if not kw.get("tools"):
                yield "ok"
                return
            a.requests.append({"messages": copy.deepcopy(messages), "stream": kw.get("stream")})
            if len(a.requests) > 6:
                raise AssertionError("runaway turn: more than six main requests")
            if kw.get("stream") is False:
                yield _tool_call(len(a.requests))
                return
            step = script.pop(0) if script else "Fertig."
            if step == "TOOL":
                yield "Einen Moment. "
                yield _tool_call(len(a.requests))
                yield json.dumps({"finish_reason": "tool_calls"})
                return
            yield from ([step] if isinstance(step, str) else step)
        a.api_backend.chat_completion = provider
        streamed = []
        returned = a.chat_step(user_input=ASK, raw_user_input=ASK, stream_callback=streamed.append)
        return "".join(streamed), returned

    a.run = run
    return a


def test_a_plain_turn_returns_its_answer(agent):
    streamed, returned = agent.run(["<think>Es ist spaet.</think>Es ist spaet genug."])
    assert "Es ist spaet genug." in streamed
    assert returned == "Es ist spaet genug."
    assert agent.last_turn == TurnStatus()


def test_a_tool_round_returns_the_final_answer(agent):
    _, returned = agent.run(["TOOL", "Die Pakete sind installiert."])
    assert returned == "Die Pakete sind installiert."
    assert len(agent.tools[TASK].calls) == 1


def test_an_auto_continue_turn_returns_every_answer(agent, monkeypatch):
    """A validated answer, the pending-task auto-continue, a tool round and a second answer:
    the reply is both answers."""
    steps = iter([(0, "Pakete installieren", 0, 1)])
    monkeypatch.setattr(agent.main_persistence, "_current_step", lambda tasks: next(steps, None))
    monkeypatch.setattr(agent, "_reply_needs_user", lambda text: False)
    first = "Hier ist die Liste der zehn Pakete: numpy, pandas und acht weitere."
    second = "Alle zehn Pakete sind jetzt installiert."
    _, returned = agent.run([first, "TOOL", second])
    assert returned == first + "\n\n" + second


def test_a_two_character_answer_is_an_answer(agent):
    """The unreachable tail held a stricter emptiness check (< 3) than the loop (>= 2): made
    reachable as it was, "Ja" would have been rolled back and asked again. ("Ok" is one of
    the loop's own filler words, so it never counted as an answer.)"""
    _, returned = agent.run(["Ja"])
    assert returned == "Ja" and len(agent.requests) == 1


def test_a_tool_only_round_makes_no_second_request(agent):
    streamed, returned = agent.run([[_tool_call(1), json.dumps({"finish_reason": "tool_calls"})],
                                    "Fertig."])
    assert [r["stream"] for r in agent.requests] == [True, True]
    assert '{"tool_calls"' not in streamed
    assert len(agent.tools[TASK].calls) == 1
    assert returned == "Fertig."


def test_the_return_is_the_kept_answers():
    """The post-loop return is the turn's joined answers, the same expression the failure
    closure returns for a turn that answered, and nothing follows the closing fallbacks."""
    from vaf.core.agent import Agent
    src = inspect.getsource(Agent.chat_step)
    tail = src[src.rindex("chat_step_break_reached"):]
    assert ("if kept_turn_answers:\n            return _join_turn_answers(kept_turn_answers[:-1], "
            "kept_turn_answers[-1])") in tail
    assert tail.rstrip().endswith('return "..."')
    assert "clean_content" not in src
