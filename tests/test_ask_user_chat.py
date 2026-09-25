# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A question with options ends the chat turn, and a tool of anyone's can end a turn the same way.

`ask_user` was a background tool (thinking runs, scheduled automations). In a chat the agent could
only ask in prose, with no options to pick and nothing to stop it from answering its own question.
Now a chat agent asks with `options`: the question is the turn's answer, streamed like any answer
so every surface shows it, and the turn ends there. The person's reply is the chat's next message.

The turn end is a declaration on the tool (`BaseTool.ends_turn` / `turn_closing`), not a name the
loop knows: the loop used to know exactly one turn-ending result, a draft, by its marker. Driven
through the REAL `Agent.chat_step` with only the model replaced, because the turn end lives in the
loop and a source check cannot tell whether it fires.

MUTATION: drop the `_close_turn_if_declared(...)` call in `_chat_post_dispatch` and the first
and the embedder test go red (a second model call answers the question); drop the stream of a
visible closing in `chat_step` and the stream assertion goes red; register ask_user without the
chat kind and the registration test goes red.
"""
import json
from pathlib import Path

import pytest

from vaf.core.platform import Platform
from vaf.tools.ask_user import ASKED_PREFIX, AskUserTool, chat_closing, options_block, question_of
from vaf.tools.base import BaseTool

SESSION = "green123456"


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.current_session_id = SESSION
    a._current_chat_source = "web"
    return a


class _Model:
    """The provider: the main call (the one that carries tools) answers from `script`, one entry
    per round; every side call (router, validation) gets a short plain answer."""

    def __init__(self, *script):
        self.script = list(script)
        self.main_calls = 0

    def __call__(self, messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        self.main_calls += 1
        step = self.script.pop(0) if self.script else "Fertig."
        if isinstance(step, str):
            yield step
            return
        name, args = step
        # Text first, as a real stream has it: a tool-only stream takes the loop's non-stream
        # fallback, which would spend a second main call before the tool call is read.
        yield "Einen Moment."
        yield json.dumps({"tool_calls": [{"index": 0, "id": f"c{self.main_calls}", "type": "function",
                                          "function": {"name": name, "arguments": json.dumps(args)}}]})
        yield json.dumps({"finish_reason": "tool_calls"})


def _turn(agent, *script):
    model = _Model(*script)
    agent.api_backend.chat_completion = model
    streamed = []
    out = agent.chat_step(user_input="Bau mir das Plugin", stream_callback=streamed.append)
    return model, out, "".join(str(s) for s in streamed)


QUESTION = "Welche Variante soll ich bauen?"
OPTIONS = ["Schnell, ohne Tests", "Gruendlich, mit Tests"]


def test_a_question_with_options_ends_the_chat_turn(agent):
    model, out, streamed = _turn(agent, ("ask_user", {"message": QUESTION, "options": OPTIONS}),
                                 "Ich nehme einfach Variante A.")
    closing = chat_closing(QUESTION, OPTIONS)
    assert model.main_calls == 1, "nothing is generated after the question: the model would answer it"
    assert out == closing
    assert agent.history[-1] == {"role": "assistant", "content": closing}
    tool_msg = agent.history[-2]
    assert tool_msg["role"] == "tool" and tool_msg["content"].startswith(ASKED_PREFIX)
    assert question_of(tool_msg["content"]) == {"question": QUESTION, "options": OPTIONS}
    # Streamed like any answer, so the terminal apps, the channels and the web all show it.
    assert streamed.rstrip().endswith(closing)
    assert agent._turn_closing is None, "spent with the turn"


def test_an_error_does_not_end_the_turn(agent):
    too_many = [f"Option {i}" for i in range(12)]
    model, out, _ = _turn(agent, ("ask_user", {"message": QUESTION, "options": too_many}),
                          "Dann frage ich anders.")
    assert model.main_calls == 2, "the model has to see the refusal and go on"
    assert out != chat_closing(QUESTION, too_many)


def test_any_tool_that_declares_it_ends_the_turn(agent):
    """The primitive, for a tool of an embedder: no name the loop knows."""

    class PickColour(BaseTool):
        name = "pick_colour"
        description = "Ask which colour."
        ends_turn = True
        parameters = {"type": "object", "properties": {}}

        def run(self, **kwargs):
            return "Rot oder Blau?"

    agent.tools["pick_colour"] = PickColour()
    agent._active_tools = None
    model, out, streamed = _turn(agent, ("pick_colour", {}), "Ich nehme Rot.")
    assert model.main_calls == 1 and out == "Rot oder Blau?"
    assert streamed.rstrip().endswith("Rot oder Blau?")


def test_a_chat_agent_is_offered_ask_user_and_a_nameless_one_is_not(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.delenv("VAF_THINKING_MODE", raising=False)
    monkeypatch.delenv("VAF_IN_AUTOMATION", raising=False)
    from vaf.core.agent import Agent
    cfg = {"provider": "openai", "api_key_openai": "sk-test"}
    assert "ask_user" in Agent(register_signals=False, run_kind="chat", config_overrides=cfg).tools
    assert "ask_user" not in Agent(register_signals=False, config_overrides=cfg).tools


def test_the_background_lanes_keep_their_delivery_and_get_the_options_as_text(monkeypatch):
    """A thinking run's question is still tracked, never a turn end; its options become text."""
    import types
    import vaf.core.thinking_mode as tm
    sent = {}

    def deliver(scope, message, **kw):
        sent["message"] = message
        return {"id": "r1", "delivered": True}

    monkeypatch.setattr(tm, "deliver_tracked_message", deliver)
    tool = AskUserTool()
    out = tool.run(message=QUESTION, options=OPTIONS, _agent=types.SimpleNamespace(_run_kind="thinking"))
    assert out.startswith("Message delivered")
    assert sent["message"] == chat_closing(QUESTION, OPTIONS)
    assert tool.turn_closing({}, out) is None


def test_weak_model_shapes_of_options_are_understood():
    from vaf.tools.ask_user import normalize_options
    assert normalize_options('["A", "B"]') == ["A", "B"]
    assert normalize_options("A\nB\n\nA") == ["A", "B"]
    assert normalize_options([{"label": "A"}, {"text": "B"}, "  C  "]) == ["A", "B", "C"]
    assert normalize_options(None) == []


# ── the web chat's half of the contract ──────────────────────────────────────

WEB = Path(__file__).resolve().parents[1] / "web" / "components" / "chat" / "askContract.ts"


def test_the_web_chat_reads_the_same_contract():
    src = WEB.read_text(encoding="utf-8")
    assert f"export const ASKED_PREFIX = '{ASKED_PREFIX}';" in src
    # The numbered list the web cuts off is built the same way on both sides.
    assert options_block(["x", "y"]) == "1. x\n2. y"
    assert "`${i + 1}. ${o}`" in src and ".join('\\n')" in src


_TS = Path(__file__).resolve().parents[1] / "web" / "node_modules" / "typescript"


def _without_options(answer, asks):
    """The web chat's REAL `withoutOptions`, transpiled with the app's own TypeScript and run
    in node: the cut is the web's code, so a copy of it here would test the copy."""
    import json
    import shutil
    import subprocess
    if shutil.which("node") is None or not _TS.is_dir():
        pytest.skip("node or web/node_modules/typescript is not installed")
    script = (
        "const ts = require(process.argv[1]);"
        "const fs = require('fs');"
        "const src = fs.readFileSync(process.argv[2], 'utf8');"
        "const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS,"
        " target: ts.ScriptTarget.ES2019}}).outputText;"
        "const m = {exports: {}}; new Function('module', 'exports', js)(m, m.exports);"
        "const [answer, asks] = JSON.parse(process.argv[3]);"
        "process.stdout.write(JSON.stringify(m.exports.withoutOptions(answer, asks)));"
    )
    out = subprocess.run(["node", "-e", script, str(_TS), str(WEB), json.dumps([answer, asks])],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_the_web_cuts_the_list_only_where_the_tool_put_it():
    """MUTATION: search for the bare list again (`out.lastIndexOf(block)`) and the sentence
    case loses "1. Ja" out of its middle."""
    ask = {"question": QUESTION, "options": OPTIONS}
    assert _without_options(chat_closing(QUESTION, OPTIONS), [ask]) == QUESTION
    two = {"question": "Und die Farbe?", "options": ["Rot", "Blau"]}
    both = chat_closing(QUESTION, OPTIONS) + "\n\n" + chat_closing(two["question"], two["options"])
    assert _without_options(both, [ask, two]) == QUESTION + "\n\nUnd die Farbe?"
    # The option's words inside a sentence, and a list without its question: every word stays.
    one = {"question": "Passt das?", "options": ["Ja"]}
    sentence = "Schritt 1. Ja, das passt."
    assert _without_options(sentence, [one]) == sentence
    loose = "Hier die Wahl:\n\n" + options_block(OPTIONS)
    assert _without_options(loose, [ask]) == loose


def test_a_later_mention_in_a_sentence_does_not_hide_the_list():
    """The same question and option again, later, inside a sentence: the newest match is not
    whole lines, and the list before it still goes. MUTATION: take only `lastIndexOf` again
    and the answer keeps its list."""
    one = {"question": "Passt das?", "options": ["Ja"]}
    tail = "\n\nIch frage nochmal: Passt das?\n\n1. Jawohl, schreib es."
    answer = chat_closing(one["question"], one["options"]) + tail
    assert _without_options(answer, [one]) == "Passt das?" + tail


# ── ask_user rides along on every chat turn, and no rider pushes out the task's tools ──

def test_the_riders_never_push_out_what_the_task_needs():
    """MUTATION: build the set as one sorted list again (`sorted(tools_set)`) and the research
    turn loses web_search to update_working_memory, because "w" sorts last."""
    from vaf.core.agent import _apply_tool_cap, _task_tools_first
    riders = ["update_intent", "update_working_memory", "memory_search", "memory_save",
              "memory_update", "update_user_identity", "set_timer", "ask_user",
              "send_telegram", "send_whatsapp", "send_to_user"]
    task = ["web_search", "write_file", "coding_agent"]
    tools = set(task + riders + ["list_tools", "search_tools"])
    out = _apply_tool_cap(_task_tools_first(tools, task), 12, {"list_tools", "search_tools"})
    assert set(task) <= set(out)
    assert "ask_user" in out, "the question tool is the first rider"
    assert out == _apply_tool_cap(_task_tools_first(set(tools), list(reversed(task))), 12,
                                  {"list_tools", "search_tools"}), "reproducible"


def test_the_chat_turn_builds_its_set_task_first():
    import inspect
    from vaf.core.agent import Agent
    src = inspect.getsource(Agent.chat_step)
    assert "selected_tools = _task_tools_first(tools_set, _task_tools)" in src
    assert '"set_timer", "ask_user"):' in src


def test_a_wake_turn_does_not_answer_the_question():
    """A timer, a finished command or a sent draft arrive as user-role rows with a kind; they
    must leave the buttons open for the person. MUTATION: drop `&& !m.kind` - red."""
    page = (Path(__file__).resolve().parents[1] / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "find(m => m.role === 'user' && !m.kind)" in page
