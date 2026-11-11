# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The API-error reflex: a provider refusal the model can act on goes back to it once.

A provider failure ended every turn, and the model never learned of it. Some refusals are
about what the model itself produced: a tool call the provider could not parse (Groq's
`tool_use_failed`), or a request its content check refused after a tool step pulled in the
material. Those now go back to the model as a fixed note, never the provider's own text, and
the round is asked again. Rate limits, credentials, quota, server and network errors never do:
a note cannot fix them, and a loop of requests would only spend more. One reflex per class
and turn, none on the round right after a reflex.

The REAL `chat_step` with only `api_backend.chat_completion` faked, in the real SDK shapes:
content strings, `{"tool_calls": ...}` frames and the `[API Error from ...]` sentinel. The
fake refuses a seventh main request, so a broken loop guard fails instead of hanging.

MUTATIONS, each turns the named test red:
- drop the `_classify_failure` step or the never-markers step of `api_error_reflex_class`,
  or return a class for every client error -> test_the_classifier;
- delete the post-loop reflex block -> test_a_malformed_call_is_fed_back_once;
- drop the class set -> test_one_reflex_per_class;
- drop `_round_no == _reflex_next_round` -> test_no_reflex_on_a_reflex_round;
- drop the pop in `_end_turn_without_answer` -> test_a_failing_reflex_round_leaves_nothing;
- make the classifier ignore `_classify_failure` -> test_never_fed_back (429, 500);
- skip the withholding -> test_a_content_refusal_after_a_tool_round_withholds_its_results;
- drop the tool-round precondition -> test_a_content_refusal_without_a_tool_round_ends_the_turn;
- move the reflex block above the shipped retry and drop its `_too_large_retried` condition
  -> test_too_large_takes_the_compression_retry_first; drop that condition alone
  -> test_too_large_with_the_retry_unused_gets_no_reflex; feed only the chunk-time class
  -> test_too_large_reflexes_once_the_retry_does_not_shrink;
- drop the answer-text condition -> test_no_reflex_after_answer_text;
- read the tool round as `_turn_rounds[-2]` again
  -> test_too_large_reflexes_after_a_retry_that_shrank;
- take the reflex note back only when it is the last message
  -> test_a_failing_reflex_note_is_taken_back_behind_a_compaction_note;
- skip rewriting the failed tool's note -> test_a_failed_tools_note_is_withheld_with_its_result.
"""
import copy
import json

import pytest

import vaf
from vaf.core.platform import Platform
from vaf.core.session import TurnStatus

SESSION = "green123456"
ASK = "Bitte installiere mir die Pakete aus der Liste"
TASK = "python_sandbox"
RESULT = "10/10 installed"

GROQ_TOOL_USE_FAILED = (
    "[API Error from openrouter: Error code: 400 - {'error': {'message': 'Provider returned "
    "error', 'code': 400, 'metadata': {'raw': '{\"error\":{\"message\":\"Failed to call a "
    "function. Please adjust your prompt. See \\'failed_generation\\' for more details.\","
    "\"type\":\"invalid_request_error\",\"code\":\"tool_use_failed\",\"failed_generation\":"
    "\"<function=python_sandbox{\\\"x\\\": 1}</function>\"}}', 'provider_name': 'Groq'}}}]")
OPENAI_INVALID_PROMPT = (
    "[API Error from openai: Error code: 400 - {'error': {'message': 'Invalid prompt: your "
    "prompt was flagged as potentially violating our usage policy. Please try again with a "
    "different prompt.', 'type': 'invalid_request_error', 'param': None, 'code': "
    "'invalid_prompt'}}]")
AZURE_CONTENT_FILTER = (
    "[API Error from openai: Error code: 400 - {'error': {'message': \"The response was "
    "filtered due to the prompt triggering Azure OpenAI's content management policy.\", "
    "'type': None, 'param': 'prompt', 'code': 'content_filter', 'status': 400}}]")
VEYLLO_413 = (
    "[API Error from veyllo: Error code: 413 - {'error': {'message': 'The request is too "
    "large.', 'type': 'invalid_request_error', 'code': 'request_too_large', 'param': None}}]")
CONTEXT_LENGTH_400 = (
    "[API Error from deepseek: Error code: 400 - {'error': {'message': \"This model's maximum "
    "context length is 131072 tokens. However, you requested 140000 tokens.\", 'type': "
    "'invalid_request_error'}}]")

RATE_LIMIT = ("[API Error from openai: Error code: 429 - {'error': {'message': 'Rate limit "
              "reached for gpt-4o', 'type': 'requests', 'code': 'rate_limit_exceeded'}}]")
BAD_KEY = ("[API Error from openai: Error code: 401 - {'error': {'message': 'Incorrect API key "
           "provided: sk-ab12***...cd34.', 'type': 'invalid_request_error', 'code': "
           "'invalid_api_key'}}]")
NO_CREDIT = ("[API Error from veyllo: Error code: 402 - {'error': {'message': 'Insufficient "
             "balance', 'code': 'insufficient_quota'}}]")
FORBIDDEN = ("[API Error from openai: Error code: 403 - {'error': {'message': 'Project does not "
             "have access to model gpt-5', 'code': 'model_not_found'}}]")
# A 403 that names a content marker: a refusal of the account, not of the reply.
FORBIDDEN_BY_POLICY = ("[API Error from openai: Error code: 403 - {'error': {'message': 'This "
                       "request was blocked under our usage policy for your organization.', "
                       "'code': 'permission_denied'}}]")
NOT_FOUND = ("[API Error from openai: Error code: 404 - {'error': {'message': 'The model "
             "gpt-9 does not exist', 'code': 'model_not_found'}}]")
SERVER_ERROR = ("[API Error from openai: Error code: 500 - {'error': {'message': 'The server "
                "had an error while processing your request.'}}]")
# A server error that names a content marker: the check itself was down.
CONTENT_FILTER_DOWN = ("[API Error from openai: Error code: 500 - {'error': {'message': 'The "
                       "content_filter service is unavailable.', 'type': 'server_error'}}]")
OVERLOADED = "[API Error from anthropic: Error code: 503 - service unavailable]"
CONNECTION = "[API Error from openai: Connection error.]"
TIMEOUT = "[API Error from openai: Request timed out.]"
UNSUPPORTED_PARAMETER = (
    "[API Error from openai: Error code: 400 - {'error': {'message': \"Unsupported parameter: "
    "'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.\", "
    "'type': 'invalid_request_error', 'param': 'max_tokens', 'code': 'unsupported_parameter'}}]")
# Two engine faults: the request VAF built was wrong, nothing the model can change.
INSUFFICIENT_TOOL_MESSAGES = (
    "[API Error from deepseek: Error code: 400 - {'error': {'message': \"An assistant message "
    "with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'. "
    "(insufficient tool messages following tool_calls message)\", 'type': "
    "'invalid_request_error'}}]")
INVALID_SCHEMA = (
    "[API Error from openai: Error code: 400 - {'error': {'message': \"Invalid schema for "
    "function 'web_search': 'required' is required to be supplied\", 'type': "
    "'invalid_request_error', 'code': 'invalid_function_parameters'}}]")

PROVIDER_TRACES = ("failed_generation", "Groq", "Error code", "invalid_prompt",
                   "content_filter", "request_too_large")


@pytest.mark.parametrize("failure, cls", [
    (GROQ_TOOL_USE_FAILED, "malformed_call"),
    (OPENAI_INVALID_PROMPT, "content_refused"),
    (AZURE_CONTENT_FILTER, "content_refused"),
    (VEYLLO_413, "too_large"),
    (CONTEXT_LENGTH_400, "too_large"),
    (RATE_LIMIT, ""), (BAD_KEY, ""), (NO_CREDIT, ""), (FORBIDDEN, ""),
    (FORBIDDEN_BY_POLICY, ""), (NOT_FOUND, ""), (SERVER_ERROR, ""), (CONTENT_FILTER_DOWN, ""),
    (OVERLOADED, ""), (CONNECTION, ""), (TIMEOUT, ""), (UNSUPPORTED_PARAMETER, ""),
    (INSUFFICIENT_TOOL_MESSAGES, ""), (INVALID_SCHEMA, ""), ("", ""),
])
def test_the_classifier(failure, cls):
    from vaf.core.agent import api_error_reflex_class
    assert api_error_reflex_class(failure) == cls


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
        self.result = RESULT

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _Callback:
    """The web lane's stream callback shape, recording every clear."""

    def __init__(self):
        self.chunks, self.clears = [], []

    def __call__(self, text):
        self.chunks.append(str(text))

    def clear(self, keep=""):
        self.clears.append(keep)
        self.chunks = [keep] if keep else []

    @property
    def text(self):
        return "".join(self.chunks)


def _tool_call(n):
    return json.dumps({"tool_calls": [{"index": 0, "id": f"c{n}", "type": "function",
                                       "function": {"name": TASK, "arguments": "{}"}}]})


@pytest.fixture
def agent(monkeypatch, tmp_path):
    """The real Agent on the API lane. `run(script)` drives one turn: each main request takes
    the next step, a text (streamed as one chunk), a list of chunks, or TOOL (a round that
    calls the tool). Every main request's messages are recorded."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    for var in ("VAF_IN_SUBAGENT_TERMINAL", "VAF_THINKING_MODE", "VAF_IN_AUTOMATION"):
        monkeypatch.delenv(var, raising=False)
    from vaf.core import agent as agent_module
    from vaf.core.agent import Agent
    # A too-large refusal lowers the chat's compression limit for the whole process; this
    # test's session id must not carry it into another test.
    monkeypatch.setattr(agent_module, "_session_compress_caps", {})
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    monkeypatch.setattr(a, "_try_workflow", lambda *x, **k: None)
    monkeypatch.setattr(a, "_route_tools", lambda text: [TASK])
    monkeypatch.setattr(a, "get_token_usage", lambda **k: (1000, 128000))
    monkeypatch.setattr(a, "analyze_intent", lambda *x, **k: 0.7)
    monkeypatch.setattr(a, "_detect_ungrounded_result_claim", lambda text, res: (False, None))
    a.tools[TASK] = _Install()
    a.current_session_id = SESSION
    a.requests = []
    a.seed = []

    def run(script, callback=None):
        script = list(script)

        def provider(messages=None, **kw):
            if not kw.get("tools"):
                yield "ok"
                return
            # The engine's per-turn block (`_append_turn_block`) rides last; it is not history.
            sent = [m for m in messages if not str(m.get("content") or "").startswith("<turn>")]
            if a.seed and not a.requests:
                # Seeded at the first request: the turn reloads the chat, a seed set before it
                # is gone.
                at = next(i for i, m in enumerate(a.history) if m.get("content") == ASK)
                a.history[at:at] = a.seed
            a.requests.append({"messages": copy.deepcopy(sent), "stream": kw.get("stream")})
            if len(a.requests) > 6:
                raise AssertionError("runaway turn: more than six main requests")
            step = script.pop(0) if script else "Fertig."
            if step == "TOOL":
                yield "Einen Moment. "
                yield _tool_call(len(a.requests))
                yield json.dumps({"finish_reason": "tool_calls"})
                return
            yield from ([step] if isinstance(step, str) else step)
        a.api_backend.chat_completion = provider
        cb = callback if callback is not None else _Callback()
        returned = a.chat_step(user_input=ASK, raw_user_input=ASK, stream_callback=cb)
        return cb, returned

    a.run = run
    return a


def _text(messages) -> str:
    return json.dumps(messages, ensure_ascii=False, default=str)


def _notes(agent):
    from vaf.core.agent import API_ERROR_REFLEX_NOTES
    return [m for m in agent.history if m.get("content") in API_ERROR_REFLEX_NOTES.values()]


def test_a_malformed_call_is_fed_back_once(agent):
    from vaf.core.agent import API_ERROR_REFLEX_NOTES
    cb, returned = agent.run([GROQ_TOOL_USE_FAILED, "Die Pakete sind installiert."])
    assert len(agent.requests) == 2
    second = agent.requests[1]["messages"]
    assert second[-1] == {"role": "system", "content": API_ERROR_REFLEX_NOTES["malformed_call"]}
    assert not any(t in _text(second) for t in PROVIDER_TRACES)
    assert agent.last_turn == TurnStatus() and len(agent.last_turn.rounds) == 2
    assert "[API Error" not in cb.text and cb.clears == []
    assert returned == "Die Pakete sind installiert."


def test_a_failing_reflex_round_leaves_nothing(agent):
    """Always refused: one reflex, then the turn fails as before, and neither the note nor
    the provider's text stays in the history."""
    cb, returned = agent.run([GROQ_TOOL_USE_FAILED] * 6)
    assert len(agent.requests) == 2
    assert agent.last_turn.failed is True and returned == GROQ_TOOL_USE_FAILED
    assert cb.text.count(GROQ_TOOL_USE_FAILED) == 1
    assert _notes(agent) == []
    assert not any("Groq" in str(m.get("content")) for m in agent.history)
    assert agent.history[-1]["content"] == ASK


def test_one_reflex_per_class(agent):
    """A second malformed call later in the same turn ends it."""
    _, returned = agent.run([GROQ_TOOL_USE_FAILED, "TOOL", GROQ_TOOL_USE_FAILED, "Fertig."])
    assert len(agent.requests) == 3
    assert agent.last_turn.failed is True and returned == GROQ_TOOL_USE_FAILED


def test_no_reflex_on_a_reflex_round(agent):
    """The round right after a reflex is never answered by another, of any class."""
    _, returned = agent.run(["TOOL", OPENAI_INVALID_PROMPT, GROQ_TOOL_USE_FAILED, "Fertig."])
    assert len(agent.requests) == 3
    assert agent.last_turn.failed is True and returned == GROQ_TOOL_USE_FAILED


@pytest.mark.parametrize("failure", [RATE_LIMIT, BAD_KEY, NO_CREDIT, FORBIDDEN, SERVER_ERROR,
                                     OVERLOADED, CONNECTION, CONTENT_FILTER_DOWN])
def test_never_fed_back(agent, failure):
    cb, returned = agent.run([failure, "Fertig."])
    assert len(agent.requests) == 1
    assert agent.last_turn.failed is True and returned == failure
    assert cb.text.count(failure) == 1
    # The history is what it was before (the system prompt) plus the person's message.
    assert [(m["role"], m["content"]) for m in agent.history[1:]] == [("user", ASK)]


def _assert_adjacent(messages):
    """Every assistant tool call is answered by tool messages before any other role."""
    for i, m in enumerate(messages):
        ids = [tc["id"] for tc in (m.get("tool_calls") or [])]
        if m.get("role") != "assistant" or not ids:
            continue
        answered = []
        for follow in messages[i + 1:]:
            if follow.get("role") != "tool":
                break
            answered.append(follow.get("tool_call_id"))
        assert sorted(answered) == sorted(ids), messages


def test_a_content_refusal_after_a_tool_round_withholds_its_results(agent):
    from vaf.core.agent import API_ERROR_REFLEX_NOTES, REFLEX_WITHHELD_RESULT
    cb, returned = agent.run(["TOOL", OPENAI_INVALID_PROMPT, "Das kann ich nicht verarbeiten."])
    assert len(agent.requests) == 3
    assert RESULT in _text(agent.requests[1]["messages"])
    third = agent.requests[2]["messages"]
    assert RESULT not in _text(third)
    tool_msgs = [m for m in third if m.get("role") == "tool"]
    assert [(m["tool_call_id"], m["content"]) for m in tool_msgs] == [("c1", REFLEX_WITHHELD_RESULT)]
    assert third[-1]["content"] == API_ERROR_REFLEX_NOTES["content_refused"]
    _assert_adjacent(third)
    assert not any(t in _text(third) for t in PROVIDER_TRACES)
    assert agent.last_turn == TurnStatus() and returned == "Das kann ich nicht verarbeiten."
    assert len(agent.tools[TASK].calls) == 1, "the tool ran once and was not run again"


def test_a_content_refusal_without_a_tool_round_ends_the_turn(agent):
    _, returned = agent.run([AZURE_CONTENT_FILTER, "Fertig."])
    assert len(agent.requests) == 1
    assert agent.last_turn.failed is True and returned == AZURE_CONTENT_FILTER


@pytest.fixture
def compress(agent, monkeypatch):
    """The forced compression, faked: `shrinks` decides whether it drops the old exchange
    seeded at the first request. Every forced call is counted."""
    state = {"shrinks": True, "calls": 0, "tail_note": False}
    agent.seed = [{"role": "user", "content": "Alte Frage"},
                  {"role": "assistant", "content": "Alte Antwort"}]

    def fake(force=False, **kw):
        if not force:
            return False          # the turn-start check: below the budget
        state["calls"] += 1
        if state["tail_note"]:
            # A compaction that leaves a note of its own last, after the reflex note.
            agent.history.append({"role": "system", "content": "[Earlier messages summarised.]"})
        old = [m for m in agent.history if m.get("content") in ("Alte Frage", "Alte Antwort")]
        if not (state["shrinks"] and old):
            return False
        agent.history = [m for m in agent.history if m not in old]
        return True
    monkeypatch.setattr(agent, "_compress_history_if_needed", fake)
    return state


def test_too_large_takes_the_compression_retry_first(agent, compress):
    """The shipped retry answers the first refusal; the reflex never does."""
    agent.run(["TOOL", VEYLLO_413, "Fertig."])
    assert len(agent.requests) == 3 and compress["calls"] == 1
    assert "Alte Frage" in _text(agent.requests[1]["messages"])
    third = agent.requests[2]["messages"]
    assert RESULT in _text(third) and "Alte Frage" not in _text(third)
    assert _notes(agent) == [] and agent.last_turn == TurnStatus()


def test_too_large_reflexes_once_the_retry_does_not_shrink(agent, compress):
    from vaf.core.agent import API_ERROR_REFLEX_NOTES, REFLEX_WITHHELD_RESULT
    compress["shrinks"] = False
    agent.run(["TOOL", VEYLLO_413, "Ich antworte mit dem, was ich habe."])
    assert len(agent.requests) == 3
    third = agent.requests[2]["messages"]
    assert third[-1]["content"] == API_ERROR_REFLEX_NOTES["too_large"]
    assert [m["content"] for m in third if m.get("role") == "tool"] == [REFLEX_WITHHELD_RESULT]
    _assert_adjacent(third)
    assert agent.last_turn == TurnStatus()


def test_too_large_reflex_is_used_once(agent, compress):
    compress["shrinks"] = False
    _, returned = agent.run(["TOOL", VEYLLO_413, VEYLLO_413, "Fertig."])
    assert len(agent.requests) == 3
    assert agent.last_turn.failed is True and returned == VEYLLO_413
    assert _notes(agent) == []


def test_too_large_with_the_retry_unused_gets_no_reflex(agent, compress):
    """A too-large refusal the shipped retry did not take (thinking streamed first) is not
    fed back either: the compression retry goes first, always."""
    agent.run(["TOOL", ["<think>Ich pruefe.</think>", VEYLLO_413], "Fertig."])
    assert len(agent.requests) == 2 and agent.last_turn.failed is True


def test_no_reflex_after_answer_text(agent):
    """Text the person already read is never followed by a second answer, and the stream is
    never cleared by the reflex."""
    cb, _ = agent.run([["Ich schaue nach. ", GROQ_TOOL_USE_FAILED], "Fertig."])
    assert len(agent.requests) == 1 and agent.last_turn.failed is True
    assert cb.text.startswith("Ich schaue nach. ") and GROQ_TOOL_USE_FAILED in cb.text
    assert cb.clears == []


def test_thinking_before_the_refusal_stays_visible(agent):
    cb, returned = agent.run([["<think>Ich rufe das Werkzeug.</think>", GROQ_TOOL_USE_FAILED],
                              "Fertig."])
    assert len(agent.requests) == 2 and agent.last_turn == TurnStatus()
    assert "Ich rufe das Werkzeug." in cb.text and "[API Error" not in cb.text
    assert cb.clears == [] and returned == "Fertig."


def test_too_large_reflexes_after_a_retry_that_shrank(agent, compress):
    """The compression retry opens a round of its own. When that round is refused too, the
    tool step before the first refusal is still the one to withhold."""
    from vaf.core.agent import API_ERROR_REFLEX_NOTES, REFLEX_WITHHELD_RESULT
    _, returned = agent.run(["TOOL", VEYLLO_413, VEYLLO_413, "Ich antworte mit dem, was ich habe."])
    assert len(agent.requests) == 4 and compress["calls"] == 1
    third, fourth = agent.requests[2]["messages"], agent.requests[3]["messages"]
    assert RESULT in _text(third) and "Alte Frage" not in _text(third)
    assert fourth[-1]["content"] == API_ERROR_REFLEX_NOTES["too_large"]
    assert [(m["tool_call_id"], m["content"]) for m in fourth if m.get("role") == "tool"] == [
        ("c1", REFLEX_WITHHELD_RESULT)]
    _assert_adjacent(fourth)
    assert agent.last_turn == TurnStatus() and returned == "Ich antworte mit dem, was ich habe."


def test_a_failing_reflex_note_is_taken_back_behind_a_compaction_note(agent, compress):
    """The refusal of the reflex round compresses the chat for the next turn, and the
    compaction may leave a note after the reflex note; the reflex note goes all the same."""
    compress["shrinks"], compress["tail_note"] = False, True
    _, returned = agent.run(["TOOL", VEYLLO_413, VEYLLO_413, "Fertig."])
    assert len(agent.requests) == 3
    assert agent.last_turn.failed is True and returned == VEYLLO_413
    assert agent.history[-1]["content"] == "[Earlier messages summarised.]"
    assert _notes(agent) == []


def test_a_failed_tools_note_is_withheld_with_its_result(agent):
    """A tool whose result is an error gets a system note that quotes the result whole. A
    content refusal withholds that copy too, or the next request carries the same material."""
    from vaf.core.agent import REFLEX_WITHHELD_RESULT, _tool_error_note
    flagged = "Error: the fetched page says something the content check refuses"
    agent.tools[TASK].result = flagged
    _, returned = agent.run(["TOOL", OPENAI_INVALID_PROMPT, "Das kann ich nicht verarbeiten."])
    assert len(agent.requests) == 3
    second, third = agent.requests[1]["messages"], agent.requests[2]["messages"]
    assert _tool_error_note(TASK, flagged) in [m.get("content") for m in second]
    assert "the content check refuses" not in _text(third)
    assert _tool_error_note(TASK, REFLEX_WITHHELD_RESULT) in [m.get("content") for m in third]
    _assert_adjacent(third)
    assert agent.last_turn == TurnStatus() and returned == "Das kann ich nicht verarbeiten."
