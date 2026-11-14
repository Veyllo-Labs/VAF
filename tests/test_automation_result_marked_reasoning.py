# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An automation's result falls back to the marked-reasoning rule, never to a phrase guess.

The prompt lane delivers `agent._clean_reasoning(stream)`. When that is empty (the run's text was
all marked reasoning, or its last words went to the history only, such as a background-task
acknowledgement), it fell back to `_extract_clean_answer`, which deleted five lazy DOTALL phrase
patterns ("Okay ... let ... me", "First ... I ... need", "I ... should ... use", ...) and every
`<...>` and `[...]` from the newest assistant message, and otherwise delivered the stream with only
its think TAGS removed, which is the reasoning text itself.

MEASURED BEFORE THE FIX: a think-only run delivered its reasoning (English and German, a token it
was told never to repeat included) to the chat, the main messenger and the notifications; the
background-task acknowledgement lost its `[Task: t1]`; a kept answer came back with the earlier
reasoning glued in front; and plain answers lost the text between "it" and "use", a link label,
or everything between `x <` and `y >`.

MUTATIONS, each turns the named test red:
- restore the old body (history branch plus the tag-stripped stream)
  -> test_fallback_never_delivers_marked_reasoning, test_fallback_keeps_answer_text_as_written,
     test_fallback_keeps_its_rule (every seed);
- re-add one phrase pattern -> its row of test_fallback_keeps_answer_text_as_written;
- re-add the `[...]` strip -> [A7-link]; re-add the `<...>` strip -> [A8-lt-gt];
- walk oldest first, drop the empty-skip, or drop the str check -> test_fallback_walk_order;
- fall back to the tag-stripped stream, or hand the fallback a tag-only cleaner
  -> test_run_task_delivers_the_marked_only_fallback[a-*] and [c-*];
- re-add `response_parts` or a phrase literal -> test_no_phrase_guess_left_in_the_lane;
- drop the placeholder skip (or keep only one of the two placeholders)
  -> test_fallback_never_delivers_marked_reasoning[placeholder*];
- call the fallback outside the try that guards the primary step
  -> test_a_failing_cleaner_in_the_fallback_still_shuts_the_agent_down;
- let the first empty retry store `full_response` again (a local engine's reasoning channel,
  unmarked), or store `full_content` alone (the model loses its own thinking on the retry)
  -> test_a_local_reasoning_only_round_then_a_failed_retry_is_not_the_result;
- store `full_response` (or the bare `full_reasoning`) for a round with no content when the
  empty retry is off -> test_a_thinking_run_stores_a_reasoning_only_round_marked;
- write a drifted "[Empty]" at the empty retry, or change `EMPTY_PLACEHOLDER`
  -> test_an_empty_first_round_is_retried_with_the_empty_placeholder;
- write or compare a placeholder as a bare literal in agent.py, automation.py or
  thinking_mode.py -> test_history_placeholders_are_written_and_read_by_name;
- run `_sanitize_response` before the marked strip in `_clean_reasoning`
  -> test_reasoning_with_a_tool_call_tail_never_leaks (every seed).
"""
import ast
import random
import re
from pathlib import Path

import pytest

from vaf.core.agent import Agent
from vaf.core.automation import AutomationManager

SCOPE = "ab12cd34-0000-4000-8000-0000000000c3"
AUTOMATION_PY = Path(__file__).resolve().parents[1] / "vaf" / "core" / "automation.py"

_CLEAN = Agent.__new__(Agent)._clean_reasoning


def _fallback(history):
    return AutomationManager.__new__(AutomationManager)._extract_clean_answer(history, _CLEAN)


def S():
    return {"role": "system", "content": "You are VAF."}


def U(text="Fasse meine Termine fuer morgen kurz zusammen."):
    return {"role": "user", "content": text}


def A(content, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def T(content="2 events tomorrow", call_id="call_1"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


_CALL = [{"id": "call_1", "type": "function",
          "function": {"name": "calendar", "arguments": "{}"}}]

R_EN = ("Okay, let me check the calendar for tomorrow. The user wants a short summary in "
        "German.")
R_DE = ("Ich muss zuerst den Kalender fuer morgen lesen und dann kurz auf Deutsch antworten, "
        "der Nutzer will nur die Uhrzeiten.")
R_TOKEN = "The token in the config is ab12cd34; never repeat it. Reply briefly."
ANSWER_DE = "Morgen um 9 Uhr hast du den Zahnarzttermin in der Praxis Dr. Weber."
ACK = ("I started coding_agent in the background [Task: t1]. I will report when it is "
       "done.")


def _think(text):
    return f"<think>{text}</think>"


# ── AC1: the fallback rows ───────────────────────────────────────────────────

FALLBACK_ROWS = [
    pytest.param([S(), U(), A(_think(R_EN))], "", id="F1-en"),
    pytest.param([S(), U(), A(_think(R_DE))], "", id="F2-de"),
    pytest.param([S(), U(), A(_think(R_TOKEN))], "", id="F3-token"),
    pytest.param([S(), U(), A(_think(R_EN)), A(_think(R_DE)), A(_think(R_TOKEN))], "",
                 id="F4-three-attempts"),
    pytest.param([S(), U(), A(_think(R_EN), _CALL), T(), A(_think(R_DE))], "",
                 id="F5-marked-tool-round"),
    pytest.param([S(), U(), A(_think("Kalender lesen.") + "\n\n" + ANSWER_DE),
                  A(_think("Noch einmal den Kalender pruefen, ob alles stimmt."))],
                 ANSWER_DE, id="F6-kept-answer-then-think"),
    pytest.param([S(), U(), A("", _CALL), T(), A(None, _CALL), T()], "", id="F7-tool-only"),
    pytest.param([S(), U(), A(_think("Plan: read calendar."))], "", id="F8-short"),
    pytest.param([S(), U(), A(_think(R_EN)), A(ACK)], ACK, id="ack-unstreamed"),
    # The engine's placeholders for a round without text are status, not an answer.
    pytest.param([S(), U(), A("Thinking...", _CALL), T()], "", id="placeholder-thinking"),
    pytest.param([S(), U(), A("[Empty]"),
                  {"role": "system", "content": "Provide a clear, direct answer now."}], "",
                 id="placeholder-empty"),
    pytest.param([S(), U(), A("Erledigt, der Bericht liegt im Ordner."), A("Thinking...", _CALL),
                  T(), A("[Empty]")], "Erledigt, der Bericht liegt im Ordner.",
                 id="placeholders-over-an-answer"),
]


@pytest.mark.parametrize("history, expected", FALLBACK_ROWS)
def test_fallback_never_delivers_marked_reasoning(history, expected):
    """Marked reasoning is never the result, in any language and however short; an answer the
    history holds is returned whole, brackets included."""
    out = _fallback(history)
    assert out == expected
    for leaked in (R_EN, R_DE, R_TOKEN, "Kalender lesen", "Plan: read", "ab12cd34"):
        assert leaked not in out


# ── AC2: answer text is kept as written ──────────────────────────────────────

ANSWER_ROWS = [
    pytest.param("Okay, here is your summary. Let me know if you want the full list of all "
                 "twelve open invoices.", id="A1-okay-let-me"),
    pytest.param("Tomorrow it should be sunny; use sunscreen if you go out to the lake with "
                 "Alice.", id="A2-it-should-use"),
    pytest.param("First, the backup finished. I need nothing else from you; all 4 disks are "
                 "healthy and synced.", id="A3-first-i-need"),
    pytest.param("Wait times at the office: the user portal reports 20 minutes, the phone line "
                 "about 45.", id="A4-wait-the-user"),
    pytest.param("Let me check list: your inbox has 3 new messages, 2 from Bob and 1 from the "
                 "bank about fees.", id="A5-let-me-check"),
    pytest.param("Ich habe die Termine geprüft. Morgen um 9 Uhr ist der Zahnarzt, um 14 Uhr "
                 "das Team-Meeting.", id="A6-german-control"),
    pytest.param("The report is ready: [Quarterly numbers](https://example.com/q3) shows revenue "
                 "up 4 percent [1].", id="A7-link"),
    pytest.param("Filter the rows with x < 5 and y > 3, then sort them by date and send the "
                 "result to Bob.", id="A8-lt-gt"),
    pytest.param("In the morning the bus is late. You should take the car; use the garage key "
                 "today.", id="A9-you-should-use"),
]


@pytest.mark.parametrize("answer", ANSWER_ROWS)
def test_fallback_keeps_answer_text_as_written(answer):
    """Plain text is the answer: no phrase, bracket or angle-bracket guess cuts it."""
    assert _fallback([S(), U(), A(_think(R_EN), _CALL), T(), A(answer)]) == answer.strip()


# ── AC3: the walk ────────────────────────────────────────────────────────────

def test_fallback_walk_order():
    """Newest non-empty wins, an assistant message the cleaner empties is passed over, content
    that is not a str is skipped without raising, and no other role is ever the result."""
    assert _fallback([S(), U(), A("Old answer."), A("New answer.")]) == "New answer."
    assert _fallback([S(), U(), A("The answer."), A(_think(R_EN))]) == "The answer."
    multimodal = [{"type": "text", "text": "Listed content."}]
    assert _fallback([S(), U(), A("The answer."), A(multimodal), A(None)]) == "The answer."
    assert _fallback([S(), U(), A(multimodal)]) == ""
    assert _fallback([S(), U("user text"), T("tool text")]) == ""
    assert _fallback([]) == ""


# ── AC4: the real run_task prompt lane ───────────────────────────────────────

def _drive(monkeypatch, tmp_path, chunks, turn_history, *, cleaner=None, shutdowns=None):
    """The real `AutomationManager.run_task` prompt lane with a stub agent whose reply cleaner
    is the real `Agent._clean_reasoning` (or `cleaner`); returns (run_task's value, the pushed
    deliveries). `shutdowns` collects one entry per `agent.shutdown()`."""
    import vaf.core.agent as agent_mod
    from vaf.core import automation
    from vaf.core.lock_manager import LockManager

    class _Agent:
        tools = {}
        _current_username = "alice"
        _tool_authorizer = None
        _clean_reasoning = cleaner or Agent._clean_reasoning
        _sanitize_response = Agent._sanitize_response

        def __init__(self, *a, **k):
            self.history = []

        def load_model(self):
            pass

        def init_chat(self):
            self.history = [S()]

        def chat_step(self, prompt, stream_callback=None, **k):
            self.history.append(U(prompt))
            self.history.extend(turn_history)
            for chunk in chunks:
                if stream_callback:
                    stream_callback(chunk)

        def shutdown(self):
            if shutdowns is not None:
                shutdowns.append(1)

    pushed = []
    monkeypatch.setattr(agent_mod, "Agent", _Agent)
    monkeypatch.setattr(automation, "bind_identity", lambda *a, **k: None)
    monkeypatch.setattr(automation, "resolve_scope_identity", lambda *a, **k: None)
    monkeypatch.setattr(automation, "_push_result_to_web_ui",
                        lambda *a, **k: pushed.append((a[1], a[2])))
    monkeypatch.setattr("vaf.memory.rag.turn_memory_context", lambda *a, **k: "")
    monkeypatch.setattr(LockManager, "acquire", lambda lock_id: True)
    monkeypatch.setattr(LockManager, "release", lambda lock_id: None)
    manager = automation.AutomationManager(storage_dir=str(tmp_path / "tasks"))
    monkeypatch.setattr(manager, "_sync_workspace_automation_state", lambda *a, **k: None)
    task = automation.AutomationTask(name="summary", prompt="Summarise tomorrow.",
                                     user_scope_id=SCOPE)
    out = manager.run_task(task, new_terminal=False)
    return out, pushed


PLAIN = "I will send the report to Alice at 9:00 tomorrow, as you asked."
RUN_ROWS = [
    pytest.param(["<think>", R_EN, "</think>\n\n"], [A(_think(R_EN))],
                 "", ("success", "Completed"), id="a-think-only"),
    pytest.param(["<think>", R_DE, "</think>"], [A(_think(R_DE)), A(ACK)],
                 ACK, ("success", ACK), id="b-unstreamed-ack"),
    pytest.param(["<think>Kalender lesen.</think>\n\n", ANSWER_DE,
                  "<think>", "Noch einmal pruefen.", "</think>"],
                 [A(_think("Kalender lesen.") + "\n\n" + ANSWER_DE),
                  A(_think("Noch einmal pruefen."))],
                 ANSWER_DE, ("success", ANSWER_DE), id="c-kept-answer-then-think"),
    pytest.param([PLAIN], [A(PLAIN)], PLAIN, ("success", PLAIN), id="d-plain-stream"),
]


@pytest.mark.parametrize("chunks, turn_history, returned, delivered", RUN_ROWS)
def test_run_task_delivers_the_marked_only_fallback(monkeypatch, tmp_path, chunks,
                                                    turn_history, returned, delivered):
    out, pushed = _drive(monkeypatch, tmp_path, chunks, turn_history)
    assert out == returned
    assert pushed == [delivered]
    for leaked in (R_EN, R_DE, "Kalender lesen", "Noch einmal"):
        assert leaked not in out and leaked not in pushed[0][1]


def test_a_failing_cleaner_in_the_fallback_still_shuts_the_agent_down(monkeypatch, tmp_path):
    """The fallback calls the same cleaner as the primary step, and like the primary step a
    failure there gives no result rather than ending the run in the outer handler, which
    skipped `agent.shutdown()` and turned a finished run into an error."""
    def boom(self, text):
        raise RuntimeError("cleaner failed")

    shutdowns = []
    out, pushed = _drive(monkeypatch, tmp_path, [PLAIN], [A(PLAIN)], cleaner=boom,
                         shutdowns=shutdowns)
    assert shutdowns, "the agent was never shut down"
    assert out == ""
    assert pushed == [("success", "Completed")]


SECRET = "SECRETREASONING the token in the config is ab12cd34, never repeat it"


def _local_agent_class(monkeypatch, tmp_path, run_kind, seen):
    """A real `Agent` on the local server lane (no model: the HTTP calls are `_serve`'s),
    built the way `run_task` builds one; `seen["agent"]` is the instance."""
    import vaf.core.agent as agent_mod
    from vaf.core.platform import Platform

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))

    class _Local(agent_mod.Agent):
        def __init__(self, *a, **k):
            super().__init__(register_signals=False, run_kind=run_kind,
                             config_overrides={"provider": "local"})
            self.api_backend = None
            self.use_server = True
            monkeypatch.setattr(self, "_try_workflow", lambda *args, **kw: None)
            monkeypatch.setattr(self, "_route_tools", lambda text: ["web_search"])
            monkeypatch.setattr(self, "get_token_usage", lambda **kw: (1000, 32768))
            monkeypatch.setattr(self, "manage_context", lambda *args, **kw: None)
            monkeypatch.setattr(self, "analyze_intent", lambda *args, **kw: 0.7)
            seen["agent"] = self

        def load_model(self):
            pass

        def shutdown(self):
            pass

    return _Local


def _serve(monkeypatch, rounds):
    """The local server: the n-th streamed request gets `rounds[n]` as its deltas, every
    request after them HTTP 500. Returns the list the request payloads are recorded in."""
    import requests
    from test_local_engine_wiring import _Resp, _Stream
    payloads = []

    def post(url, json=None, **kw):
        if isinstance(json, dict) and json.get("stream") and "messages" in json:
            payloads.append(json)
            if len(payloads) <= len(rounds):
                return _Stream(rounds[len(payloads) - 1])
        return _Resp(500, {})

    monkeypatch.setattr(requests, "post", post)
    return payloads


def test_a_thinking_run_stores_a_reasoning_only_round_marked(monkeypatch, tmp_path):
    """A background thinking run has the empty retry off, so a round that streamed only a local
    engine's reasoning channel reaches the final history append with no content. That append
    stored `full_response`, the reasoning without its marks, where every later reader of the
    history takes it for an answer."""
    seen = {}
    agent = _local_agent_class(monkeypatch, tmp_path, "thinking", seen)()
    agent.init_chat()
    payloads = _serve(monkeypatch, [[{"reasoning_content": SECRET}]])
    agent.chat_step(user_input="Was steht an?", raw_user_input="Was steht an?",
                    stream_callback=lambda c: None)
    assert payloads, "the turn never reached the local server"
    stored = [m.get("content") for m in agent.history if m.get("role") == "assistant"]
    assert SECRET not in stored, stored
    assert f"<think>{SECRET}</think>" in stored, stored
    assert _fallback(agent.history) == ""


def test_an_empty_first_round_is_retried_with_the_empty_placeholder(monkeypatch, tmp_path):
    """The writer side of the placeholder the fallback skips: a first round with neither
    reasoning nor content is kept for the retry as `EMPTY_PLACEHOLDER`, the exact text the
    fallback passes over."""
    from vaf.core.completion import EMPTY_PLACEHOLDER
    seen = {}
    agent = _local_agent_class(monkeypatch, tmp_path, "automation", seen)()
    agent.init_chat()
    payloads = _serve(monkeypatch, [[{"content": ""}]])
    agent.chat_step(user_input="Was steht an?", raw_user_input="Was steht an?",
                    stream_callback=lambda c: None)
    assert len(payloads) >= 2, "the empty retry never asked the model again"
    retry_sent = [m.get("content") for m in payloads[1]["messages"] if m.get("role") == "assistant"]
    assert retry_sent == [EMPTY_PLACEHOLDER] == ["[Empty]"], retry_sent
    assert _fallback(agent.history) == ""


def test_a_local_reasoning_only_round_then_a_failed_retry_is_not_the_result(monkeypatch,
                                                                            tmp_path):
    """The real `chat_step` on a local engine, driven by the real `run_task`: the first round
    streams only the reasoning channel, the empty retry's request fails (HTTP 500), so nothing
    unmarked reached the stream and the fallback reads the history. The empty retry stored that
    reasoning as `full_response`, which holds a local engine's reasoning channel WITHOUT its
    marks, so the fallback delivered it as a successful result. Stored marked, it is still sent
    back to the model on the retry and never delivered."""
    import vaf.core.agent as agent_mod
    from vaf.core import automation
    from vaf.core.lock_manager import LockManager

    seen = {}
    _Local = _local_agent_class(monkeypatch, tmp_path, "automation", seen)
    payloads = _serve(monkeypatch, [[{"reasoning_content": SECRET}]])

    pushed = []
    monkeypatch.setattr(agent_mod, "Agent", _Local)
    monkeypatch.setattr(automation, "bind_identity", lambda *a, **k: None)
    monkeypatch.setattr(automation, "resolve_scope_identity", lambda *a, **k: None)
    monkeypatch.setattr(automation, "_push_result_to_web_ui",
                        lambda *a, **k: pushed.append((a[1], a[2])))
    monkeypatch.setattr("vaf.memory.rag.turn_memory_context", lambda *a, **k: "")
    monkeypatch.setattr(LockManager, "acquire", lambda lock_id: True)
    monkeypatch.setattr(LockManager, "release", lambda lock_id: None)
    manager = automation.AutomationManager(storage_dir=str(tmp_path / "tasks"))
    monkeypatch.setattr(manager, "_sync_workspace_automation_state", lambda *a, **k: None)
    task = automation.AutomationTask(name="summary", prompt="Summarise tomorrow.",
                                     user_scope_id=SCOPE)
    out = manager.run_task(task, new_terminal=False)

    assert SECRET not in out and SECRET not in str(pushed), (out, pushed)
    assert out == ""
    assert pushed == [("success", "Completed")]
    history = [m.get("content") for m in seen["agent"].history if m.get("role") == "assistant"]
    assert SECRET not in history, "the reasoning channel is in the history without its marks"
    # The retry still shows the model its own thinking, marked as such.
    assert len(payloads) >= 2, "the empty retry never asked the model again"
    retry_sent = [m.get("content") for m in payloads[1]["messages"] if m.get("role") == "assistant"]
    assert f"<think>{SECRET}</think>" in retry_sent, retry_sent


# ── AC5: no guess left in the source ─────────────────────────────────────────

def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in automation.py")


def _docstring_nodes(fn):
    ids = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


_GUESS = re.compile(r"think>|redacted_reasoning|Okay|First|Wait|should|Let.*me")


def test_no_phrase_guess_left_in_the_lane():
    source = AUTOMATION_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = []
    for name in ("_extract_clean_answer", "run_task"):
        fn = _function(tree, name)
        skip = _docstring_nodes(fn)
        for node in ast.walk(fn):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in skip and _GUESS.search(node.value)):
                found.append((name, node.lineno, node.value[:60]))
    assert found == [], found
    for gone in ("response_parts", "thinking_patterns"):
        assert gone not in source, gone


def _status_call_args(tree):
    """ids of the string constants passed to a `UI.event(...)` status line: the same words on
    screen, never written into the history."""
    ids = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "event"):
            for arg in node.args:
                if isinstance(arg, ast.Constant):
                    ids.add(id(arg))
    return ids


def test_history_placeholders_are_written_and_read_by_name():
    """The engine writes its placeholders and the readers skip them through the constants in
    `vaf/core/completion.py`; a hand-written copy in a writer or a reader drifts silently (a
    renamed placeholder would then be delivered as an automation's result)."""
    from vaf.core.completion import HISTORY_PLACEHOLDERS
    core = AUTOMATION_PY.parent
    found = []
    for name in ("agent.py", "automation.py", "thinking_mode.py"):
        tree = ast.parse((core / name).read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree) | _status_call_args(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in skip and node.value.strip() in HISTORY_PLACEHOLDERS):
                found.append((name, node.lineno, node.value))
    assert found == [], found


# ── AC6: the fuzz ────────────────────────────────────────────────────────────

_TOKENS = ["Okay ", "okay, ", "let ", "me ", "Let me check ", "First ", "first, ", "I ", "need ",
           "Wait ", "wait, ", "the ", "user ", "should ", "use ", "I should use ", "it ", "is ",
           "Ich ", "prüfe ", "Termine ", "Morgen ", "um ", "9 ", "Uhr. ", "Gerne, ",
           "äöüß ", "[1] ", "[Bericht](https://example.com/r) ", "x < 5 ",
           "y > 3 ", "thinking ", "reasoning ", "tool_call ", "Alice ", "Bob ", "done. ", "\n",
           "\n\n", " ", "- item\n", "**bold** "]


def _draw(rng, least=0):
    return "".join(rng.choice(_TOKENS) for _ in range(rng.randint(least, 40)))


def fallback_mismatches(seed, draws):
    """The three oracles over `draws` seeded draws; returns the mismatches (oracle, history)."""
    rng = random.Random(seed)
    bad = []
    for _ in range(draws):
        t, r, b, p = _draw(rng), _draw(rng, 1), _draw(rng), _draw(rng)
        rows = [
            (1, [S(), U(), A(t)], t.strip()),
            (2, [S(), U(), A(p, _CALL), T(), A(t + _think(r) + b)], (t + b).strip() or p.strip()),
            (3, [S(), U(), A(_think(r) + "\n\n"), A(_think(p + r))], ""),
        ]
        for oracle, history, expected in rows:
            if _fallback(history) != expected:
                bad.append((oracle, history))
    return bad


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_fallback_keeps_its_rule(seed):
    bad = fallback_mismatches(seed, 500)
    assert bad == [], f"{len(bad)} mismatches, first: {bad[:2]}"


# Reasoning that quotes stray closers and ends tool-call-shaped: the leaked-JSON patterns cut
# to the end of the text, so run before the marked strip they cut the block's own closer.
_RSN_TOKENS = ["RSN ", "the token is ab12cd34 ", "I will call ", "</thinking> ", "</Think> ",
               "</THINKING> ", "</think> ", '{"name": "calendar_read", "arguments": {"d": 1}} ',
               '[{"tool_calls": [', '", "name": "x", "type": "function"', '"}, "type": "function"',
               ', "name": "x"', "}]}", "\n", " "]
_ANS_TOKENS = ["Morgen ", "um ", "9 ", "Uhr. ", "Alice ", "done. ", "\n", " ", "Gerne, "]


def json_tail_mismatches(seed, draws):
    """`<think>` + reasoning + `</think>` + answer, cleaned, is the answer; returns the misses."""
    rng = random.Random(seed)
    bad = []
    for _ in range(draws):
        r = "".join(rng.choice(_RSN_TOKENS) for _ in range(rng.randint(1, 25)))
        b = "".join(rng.choice(_ANS_TOKENS) for _ in range(rng.randint(0, 15)))
        text = "<think>" + r + "</think>" + b
        if _CLEAN(text) != b.strip() or _fallback([S(), U(), A(text)]) != b.strip():
            bad.append(text)
    return bad


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_reasoning_with_a_tool_call_tail_never_leaks(seed):
    bad = json_tail_mismatches(seed, 500)
    assert bad == [], f"{len(bad)} mismatches, first: {bad[:2]}"
