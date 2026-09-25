# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A tool call whose arguments are not a JSON object is refused, never run without them.

Live incident. The model called `update_working_memory` with a string value it had not quoted
(`{"plan": "...", "notes": Neue Aufgabe: ...}`). The chat loop's `json.loads` failed, its bare
`except` substituted `{}`, and the tool, all of whose parameters are optional, ran as a no-op and
answered "Working Memory updated." The plan the call carried was dropped; the next turn read
the stale plan and took the chat to be about an earlier task. Measured over the stored
sessions: 2 of 560 calls, both this shape. The librarian had the same fallback, the coder a
variant that could even keep the PREVIOUS call's arguments.

`tool_dispatch.decode_arguments` is the one decoder: the chat loop and the coder ask it, and
the funnel (`ToolCaller.execute`) applies it to arguments handed over as text, which is how
the librarian and an embedder's own loop pass them. The refusal starts with "Tool Error:",
which every surface reads as a failure, and NOT with "error", which would make the chat loop
tell the model never to try again: this is the one failure a retry fixes.

MUTATION: restore `except: arguments = {}` in `chat_step` and the chat test goes red (the
tool runs); drop the string branch in `ToolCaller.execute` and the funnel test goes red.
"""
import inspect
import json

import pytest

from vaf.core.context import tool_result_is_error
from vaf.core.platform import Platform
from vaf.core.tool_dispatch import ToolCaller, decode_arguments
from vaf.tools.base import BaseTool

MALFORMED = '{"plan": "Testmail senden", "notes": Neue Aufgabe: reine Testmail.}'


@pytest.mark.parametrize("raw,expected", [
    (None, {}), ("", {}), ("  ", {}), ("null", {}), ({"a": 1}, {"a": 1}),
    ('{"plan": "x"}', {"plan": "x"}),
])
def test_arguments_that_are_an_object_or_nothing_decode(raw, expected):
    assert decode_arguments("tool", raw) == (expected, None)


@pytest.mark.parametrize("raw", [MALFORMED, "[1, 2]", '"just text"', "42", 7])
def test_anything_else_is_refused_with_a_retryable_error(raw):
    args, refusal = decode_arguments("update_working_memory", raw)
    assert args == {}
    assert refusal.startswith("Tool Error: the arguments of this 'update_working_memory' call")
    assert "nothing was run" in refusal
    assert tool_result_is_error(refusal)
    assert not refusal.lower().startswith(("error", "failed", "❌"))


class _Spy(BaseTool):
    name = "spy_tool"
    description = "spy"
    permission_level = "read"
    parameters = {"type": "object", "properties": {"plan": {"type": "string"}}}

    def __init__(self):
        super().__init__()
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return "ran"


def test_the_funnel_refuses_malformed_text_and_runs_good_text(monkeypatch):
    monkeypatch.setattr("vaf.core.trust.get_tool_policy", lambda *a, **k: "always")
    spy = _Spy()
    caller = ToolCaller({spy.name: spy})
    out = caller.execute(spy.name, MALFORMED)
    assert out.startswith("Tool Error:") and spy.calls == []
    assert caller.execute(spy.name, '{"plan": "x"}') == "ran"
    assert spy.calls == [{"plan": "x"}]


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.current_session_id = "green123456"
    a._current_chat_source = "web"
    return a


class _Model:
    """Round one sends the incident's call as the provider delivered it; round two answers."""

    def __init__(self):
        self.main_calls = 0

    def __call__(self, messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        self.main_calls += 1
        if self.main_calls > 1:
            yield "Erledigt."
            return
        yield "Einen Moment."
        yield json.dumps({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                          "function": {"name": "update_working_memory",
                                                       "arguments": MALFORMED}}]})
        yield json.dumps({"finish_reason": "tool_calls"})


def test_the_chat_loop_answers_the_call_and_does_not_run_it(agent):
    ran = []
    agent.tools["update_working_memory"].run = lambda **kw: ran.append(kw) or "✅ Working Memory updated."
    agent.api_backend.chat_completion = _Model()
    agent.chat_step(user_input="Schick mir eine Testmail", stream_callback=lambda t: None)
    assert ran == [], "the tool ran without the arguments the model meant"
    answers = [m for m in agent.history if m.get("role") == "tool" and m.get("tool_call_id") == "c1"]
    assert len(answers) == 1, "the call is answered exactly once (tool-call adjacency)"
    assert answers[0]["content"].startswith("Tool Error: the arguments of this 'update_working_memory'")
    assert not any("DO NOT retry" in str(m.get("content")) for m in agent.history), \
        "a malformed call is the one failure a retry fixes"


def test_the_librarian_and_the_coder_ask_the_same_decoder():
    import vaf.tools.coder as coder
    import vaf.tools.librarian as librarian
    lib = inspect.getsource(librarian)
    assert "caller.execute(fn_name, tc['function']['arguments'])" in lib
    assert "fn_args = {}" not in lib
    run = inspect.getsource(coder.CodingAgentTool.run)
    assert "decode_arguments(fn_name, fn_args_str)" in run
