# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A stop that arrives while the model is still streaming ends the round before any tool.

MEASURED BEFORE THE FIX, on the real `chat_step` with no model (STOP = Stop pressed before
the next chunk):

| Script of the first round | Before the fix |
|---|---|
| API: text, complete call frame, STOP, finish frame | the tool ran; 2 requests; the second request's answer returned |
| API: call 1 frame, STOP, call 2 frame | call 1 ran; 2 requests |
| API: STOP inside the arguments | a `Tool Error` result for the cut-off arguments was committed; 2 requests |
| API: frame with an id but no name, STOP, the name frame | returned `[Error] API returned tool calls without a function name`, a failed turn |
| API: STOP before the first chunk | a non-streamed request was sent; its text streamed after the stop notice and returned |
| API: a call written as text (also inside `<think>`), STOP | the recovered call ran; 2 requests |
| API: an ungrounded claim, STOP | the correction round ran; a `CORRECTION NEEDED` note was committed after the stop |
| API: a call refused by loop protection, STOP | the blocked-batch regenerate sent a second request |
| local server and library lane: text, call delta, STOP | the tool ran; another request followed |
| API: call frame, STOP, the stream ends | the tool did not run, but its call was left unanswered before the marker (Rule 4.1) |

The three stream polls consume the stop, but the only exit that acted on it came after the
guards, the tool parsing and the tool section; the API lane's non-streamed fallback and its
call conversion did not read the flag; and the stop before a tool committed the marker after
a `tool_calls` message whose calls had no result.

The real `chat_step` on the API lane (`make_agent`), on the local-server lane (a stubbed
`requests.post` line stream) and on the library lane (a stubbed `create_chat_completion`).

MUTATIONS, each turns the named test red. On the API lane two changes keep a cut stream's
calls from running, the exit after the stream and the guard around the call conversion, so
the API rows of the first mutation need the second one as well:
- move the stop exit back below `_final_answer_probe`
  -> test_a_stop_after_a_call_on_every_lane[server], [library],
     test_a_stopped_text_call_does_not_run[xml], [in-think],
     test_no_guard_judges_a_stopped_round[grounding], the fuzz (all seeds: the text call);
- that, and drop the `if not _generation_stopped:` around the API lane's call conversion
  -> also test_a_stop_after_a_streamed_call_runs_nothing,
     test_a_stop_inside_a_batch_or_its_arguments[second-call], [mid-arguments],
     test_no_guard_judges_a_stopped_round[refused-call];
- drop only that conversion guard
  -> test_a_stop_before_the_name_is_no_backend_failure, the fuzz (seeds 5 and 2026);
- drop `and not _generation_stopped` from the non-streamed fallback condition
  -> test_a_stop_on_the_first_chunk_asks_nothing_again, the fuzz (all seeds);
- place the stop exit just before `# 1. Handle Tool Calls` (after the guards)
  -> test_no_guard_judges_a_stopped_round[grounding];
- place it just before the tool section and drop the conversion guard
  -> test_no_guard_judges_a_stopped_round[refused-call] (the blocked-batch regenerate);
- drop the answering loop in the stop before a tool (marker only)
  -> test_a_stop_before_the_first_tool_answers_the_call,
     test_a_stop_between_tools_answers_the_rest_and_keeps_the_notes, the fuzz (all seeds);
- answer only the current call there
  -> test_a_stop_between_tools_answers_the_rest_and_keeps_the_notes, the fuzz (all seeds);
- flush the deferred notes before the not-run results
  -> test_a_stop_between_tools_answers_the_rest_and_keeps_the_notes (adjacency).
"""
import copy
import json
import os
import random

import pytest
import requests

from test_empty_retry_history import ASK, STOP, TASK, _Install, assert_adjacent, make_agent
from test_user_stop_return import FIRST, _auto_continue_once
from vaf.core.agent import STOPPED_CALL_RESULT, _final_answer_probe, _tool_error_note_parts
from vaf.core.task_queue import TaskQueue

MARK = "[Generation stopped by user]"
FIN = json.dumps({"finish_reason": "tool_calls"})
NONSTREAM = "NONSTREAM-ANSWER"


def _frame(*calls):
    """One API stream frame carrying complete calls: (id, arguments) each."""
    return json.dumps({"tool_calls": [{"index": i, "id": cid, "type": "function",
                                       "function": {"name": TASK, "arguments": args}}
                                      for i, (cid, args) in enumerate(calls)]})


def _drive(agent, rounds):
    """One turn on the API lane: each streamed main request takes the next round (a list of
    chunks; `STOP` presses Stop before the next one), then `"Fertig."`. A non-streamed
    request is counted in `agent.nonstream` and answers `NONSTREAM`, so a fallback after a
    stop shows. Returns (streamed text, return value)."""
    rounds = list(rounds)
    agent.nonstream = 0

    def provider(messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        if kw.get("stream") is False:
            agent.nonstream += 1
            yield NONSTREAM
            return
        agent.requests.append(copy.deepcopy(messages))
        if len(agent.requests) > 6:
            raise AssertionError("runaway turn: more than six main requests")
        for chunk in (rounds.pop(0) if rounds else ["Fertig."]):
            if chunk == STOP:
                agent.press_stop()
                continue
            yield chunk
    agent.api_backend.chat_completion = provider
    agent.chunks = []
    out = agent.chat_step(user_input=ASK, raw_user_input=ASK, stream_callback=agent.chunks.append)
    return "".join(c for c in agent.chunks if isinstance(c, str)), out


def _stop_pending():
    return TaskQueue().should_stop("any")


def _turn_messages(history):
    """What the turn added after its request."""
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if m.get("role") == "user" and m.get("content") == ASK:
            return history[i + 1:]
    raise AssertionError(f"the request is not in the history: {history}")


def _no_calls_committed(agent):
    added = _turn_messages(agent.history)
    assert not any(m.get("tool_calls") for m in added), added
    assert not any(m.get("role") == "tool" for m in added), added


@pytest.fixture
def agent(monkeypatch, tmp_path):
    return make_agent(monkeypatch, tmp_path)


# ── AC1: the API lane ───────────────────────────────────────────────────────

def test_a_stop_after_a_streamed_call_runs_nothing(agent):
    streamed, out = _drive(agent, [["Einen Moment. ", _frame(("c1", "{}")), STOP, FIN]])
    assert len(agent.tools[TASK].calls) == 0, "the stopped call ran"
    assert len(agent.requests) == 1, "the turn asked the model again after the stop"
    assert out == "Einen Moment."
    assert _turn_messages(agent.history) == [{"role": "assistant", "content": "Einen Moment."}]
    assert streamed == f"Einen Moment. \n\n{MARK}\n\n", streamed
    assert not _stop_pending()
    assert_adjacent(agent.history)


# ── AC2: the local-server and the library lane ──────────────────────────────

_LANE_ROUND = [{"content": "x "},
               {"tool_calls": [{"index": 0, "id": "c1",
                                "function": {"name": TASK, "arguments": "{}"}}]},
               STOP, {"content": ""}]


class _ServerStream:
    """A llama-server SSE response: one `data:` line per delta, then the finish and DONE."""
    status_code = 200
    text = ""

    def __init__(self, items, agent):
        self.items = items
        self.agent = agent

    def iter_lines(self):
        for it in self.items:
            if it == STOP:
                self.agent.press_stop()
                continue
            yield b"data: " + json.dumps({"choices": [{"delta": it}]}).encode()
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
        yield b"data: [DONE]"

    def close(self):
        pass


def _on_server(agent, monkeypatch, rounds):
    agent.api_backend = None
    agent.use_server = True
    monkeypatch.setattr(agent, "manage_context", lambda *a, **k: None)

    def post(url, json=None, **kw):
        if isinstance(json, dict) and json.get("stream") and "messages" in json:
            if json.get("tools"):
                agent.requests.append(copy.deepcopy(json["messages"]))
            if len(agent.requests) > 6:
                raise AssertionError("runaway turn")
            return _ServerStream(rounds.pop(0) if rounds else [{"content": "Fertig."}], agent)
        raise requests.exceptions.ConnectionError("not stubbed")
    monkeypatch.setattr(requests, "post", post)


def _on_library(agent, monkeypatch, rounds):
    agent.api_backend = None
    agent.use_server = False
    monkeypatch.setattr(agent, "manage_context", lambda *a, **k: None)

    class _Llm:
        def create_chat_completion(self, **kw):
            if kw.get("tools"):
                agent.requests.append(copy.deepcopy(kw["messages"]))
            if len(agent.requests) > 6:
                raise AssertionError("runaway turn")
            items = rounds.pop(0) if rounds else [{"content": "Fertig."}]

            def gen():
                for it in items:
                    if it == STOP:
                        agent.press_stop()
                        continue
                    yield {"choices": [{"delta": it}]}
            return gen()
    agent.llm = _Llm()


@pytest.mark.parametrize("lane", ["server", "library"])
def test_a_stop_after_a_call_on_every_lane(agent, monkeypatch, lane):
    (_on_server if lane == "server" else _on_library)(agent, monkeypatch, [list(_LANE_ROUND)])
    chunks = []
    out = agent.chat_step(user_input=ASK, raw_user_input=ASK, stream_callback=chunks.append)
    assert len(agent.tools[TASK].calls) == 0, "the stopped call ran"
    assert len(agent.requests) == 1, "the turn asked the model again after the stop"
    assert out == "x"
    _no_calls_committed(agent)
    assert "".join(c for c in chunks if isinstance(c, str)) == f"x \n\n{MARK}\n"
    assert not _stop_pending()


# ── AC3: a stop inside a batch or inside the arguments ──────────────────────

def _call2():
    return json.dumps({"tool_calls": [{"index": 1, "id": "c1b", "type": "function",
                                       "function": {"name": TASK,
                                                    "arguments": '{"packages": ["numpy"]}'}}]})


@pytest.mark.parametrize("round_", [
    pytest.param(["x ", _frame(("c1", "{}")), STOP, _call2(), FIN], id="second-call"),
    pytest.param(["x ", _frame(("c1", '{"packa')), STOP,
                  json.dumps({"tool_calls": [{"index": 0, "function":
                                              {"arguments": 'ges": ["numpy"]}'}}]}), FIN],
                 id="mid-arguments"),
])
def test_a_stop_inside_a_batch_or_its_arguments(agent, round_):
    _, out = _drive(agent, [round_])
    assert len(agent.tools[TASK].calls) == 0, "a call of the stopped round ran"
    assert len(agent.requests) == 1
    _no_calls_committed(agent)
    assert out == "x"


# ── AC4: a stop before the call's name ──────────────────────────────────────

def test_a_stop_before_the_name_is_no_backend_failure(agent):
    no_name = json.dumps({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                          "function": {"arguments": ""}}]})
    name = json.dumps({"tool_calls": [{"index": 0, "function": {"name": TASK, "arguments": "{}"}}]})
    streamed, out = _drive(agent, [["x ", no_name, STOP, name, FIN]])
    assert out == "x", out
    assert agent.last_turn.failed is False
    assert "[Error]" not in streamed, streamed
    assert len(agent.requests) == 1
    _no_calls_committed(agent)


# ── AC5: a stop on the first chunk ──────────────────────────────────────────

def test_a_stop_on_the_first_chunk_asks_nothing_again(agent):
    streamed, out = _drive(agent, [[STOP, "Hallo"]])
    assert agent.nonstream == 0, "a non-streamed request followed the stop"
    assert out == MARK
    assert len(agent.requests) == 1
    assert _turn_messages(agent.history) == [], "something of the stopped round was committed"
    assert streamed == f"\n\n{MARK}\n\n", streamed


# ── AC6: a call written as text ─────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    pytest.param('<tool_call>{"name": "%s", "arguments": {}}</tool_call>' % TASK, id="xml"),
    pytest.param('<think><tool_call>{"name": "%s", "arguments": {}}</tool_call></think>' % TASK,
                 id="in-think"),
])
def test_a_stopped_text_call_does_not_run(agent, text):
    _, out = _drive(agent, [[text, STOP, " mehr"]])
    assert len(agent.tools[TASK].calls) == 0, "the text-written call ran"
    assert len(agent.requests) == 1
    assert out == (agent._clean_reasoning(text) or MARK)
    assert_adjacent(agent.history)


# ── AC7: no guard judges a stopped round ────────────────────────────────────

@pytest.mark.parametrize("guard", ["grounding", "refused-call"])
def test_no_guard_judges_a_stopped_round(agent, monkeypatch, guard):
    if guard == "grounding":
        judged = []

        def detect(text, results):
            judged.append(text)
            return True, "installed"
        monkeypatch.setattr(agent, "_detect_ungrounded_result_claim", detect)
        _, out = _drive(agent, [["Alle Pakete sind installiert.", STOP, " und"]])
        assert judged == [], "the grounding guard judged the stopped text"
        assert len(agent.requests) == 1, "a correction round followed the stop"
        assert out == "Alle Pakete sind installiert."
        assert not any(m.get("role") == "system" and "CORRECTION NEEDED" in str(m.get("content"))
                       for m in agent.history)
        return
    import vaf.core.agent as agent_mod
    monkeypatch.setattr(agent_mod, "_find_redundant_read_call", lambda h, fn, fa: True)
    _, out = _drive(agent, [["", _frame(("c1", "{}")), STOP, FIN]])
    assert len(agent.requests) == 1, "the blocked-batch regenerate asked again after the stop"
    assert len(agent.tools[TASK].calls) == 0
    assert out == MARK
    _no_calls_committed(agent)


# ── AC8: the stream ended, the stop is seen before the first tool ───────────

def test_a_stop_before_the_first_tool_answers_the_call(agent):
    _, out = _drive(agent, [["Einen Moment. ", _frame(("c1", "{}")), STOP]])
    assert len(agent.tools[TASK].calls) == 0
    assert len(agent.requests) == 1
    assert out == MARK
    added = _turn_messages(agent.history)
    assert [c["id"] for c in added[0]["tool_calls"]] == ["c1"], added
    assert added[1:] == [
        {"role": "tool", "tool_call_id": "c1", "name": TASK, "content": STOPPED_CALL_RESULT},
        {"role": "assistant", "content": MARK}], added
    assert_adjacent(agent.history)
    assert not _stop_pending()


# ── AC9: the stop is seen between two tools ─────────────────────────────────

class _Fails(_Install):
    __module__ = "vaf.tools.python_sandbox"

    def run(self, **kwargs):
        super().run(**kwargs)
        return "Error: the package index could not be reached"


def test_a_stop_between_tools_answers_the_rest_and_keeps_the_notes(agent, monkeypatch):
    """An answer, the auto-continue, a round of three calls; the first fails and presses
    Stop in its run. The second and third never run and are answered as not run, the first
    keeps its real result, and its error note follows all three results."""
    _auto_continue_once(agent, monkeypatch)
    tool = agent.tools[TASK] = _Fails()
    tool.on_run = agent.press_stop
    batch = _frame(("c2", "{}"), ("c2b", '{"packages": ["numpy"]}'),
                   ("c2c", '{"packages": ["pandas"]}'))
    _, out = _drive(agent, [[FIRST], ["Einen Moment. ", batch, FIN]])
    assert len(tool.calls) == 1, "a later call ran after the stop"
    assert len(agent.requests) == 2, "the turn went on after the stop"
    assert out == f"{FIRST}\n\n{MARK}"
    call = next(i for i, m in enumerate(agent.history)
                if [c.get("id") for c in (m.get("tool_calls") or ())] == ["c2", "c2b", "c2c"])
    tail = agent.history[call + 1:]
    assert [(m.get("role"), m.get("tool_call_id")) for m in tail[:3]] == [
        ("tool", "c2"), ("tool", "c2b"), ("tool", "c2c")], tail
    assert "could not be reached" in tail[0]["content"], "call 1 lost its real result"
    assert [m["content"] for m in tail[1:3]] == [STOPPED_CALL_RESULT] * 2
    head, _ = _tool_error_note_parts(TASK)
    assert tail[3]["role"] == "system" and tail[3]["content"].startswith(head), tail
    assert tail[4:] == [{"role": "assistant", "content": MARK}], tail
    assert_adjacent(agent.history)


# ── AC10: without a stop, nothing changes ───────────────────────────────────

def test_without_a_stop_the_tool_runs(agent):
    _, out = _drive(agent, [["Einen Moment. ", _frame(("c1", "{}")), FIN]])
    assert len(agent.tools[TASK].calls) == 1
    assert len(agent.requests) == 2
    assert out == "Fertig."
    assert_adjacent(agent.history)


# ── AC13: where the stop lands, fuzzed ──────────────────────────────────────

_WORDS = ["Pakete", "werden", "jetzt", "installiert", "numpy", "pandas", "Liste"]


def _text_chunk(rng):
    r = rng.random()
    if r < 0.6:
        return " ".join(rng.sample(_WORDS, rng.randint(1, 3))) + " "
    if r < 0.85:
        return f"<think>{rng.choice(_WORDS)}</think>"
    return f"<think>{rng.choice(_WORDS)} "


def _call_frames(rng, cid, index, args):
    mode = rng.randrange(3)
    if mode == 0:       # everything in one frame
        return [json.dumps({"tool_calls": [{"index": index, "id": cid, "type": "function",
                                            "function": {"name": TASK, "arguments": args}}]})]
    if mode == 1:       # name first, then the arguments in pieces
        cut = rng.randint(1, len(args) - 1)
        pieces = [args] if rng.random() < 0.5 else [args[:cut], args[cut:]]
        return [json.dumps({"tool_calls": [{"index": index, "id": cid, "type": "function",
                                            "function": {"name": TASK, "arguments": ""}}]})] + [
            json.dumps({"tool_calls": [{"index": index, "function": {"arguments": p}}]})
            for p in pieces]
    # id first, then the name
    return [json.dumps({"tool_calls": [{"index": index, "id": cid, "type": "function",
                                        "function": {"arguments": ""}}]}),
            json.dumps({"tool_calls": [{"index": index,
                                        "function": {"name": TASK, "arguments": args}}]})]


def _one_draw(agent, rng, draw, start):
    agent.history[:] = copy.deepcopy(start)
    tool = agent.tools[TASK]
    tool.calls = []
    tool.on_run = None
    agent._tool_exec_times = []
    agent.requests = []
    TaskQueue().clear_stop("any")

    texts = [_text_chunk(rng) for _ in range(rng.randint(0, 3))]
    n_calls = rng.randint(0, 3)
    if not texts and not n_calls:
        texts = [_text_chunk(rng)]
    ids = [f"d{draw}c{j}" for j in range(n_calls)]
    if not n_calls and rng.random() < 0.5:
        # A call written as text (the recovery parsers run it when no call was streamed),
        # placed before any unclosed `<think>` so it stays outside the reasoning. Its id is
        # the engine's own, so only its place is checked.
        text_call = ('<tool_call>{"name": "%s", "arguments": {"packages": ["p%dt"]}}</tool_call>'
                     % (TASK, draw))
        open_at = next((i for i, t in enumerate(texts) if t.startswith("<think>")
                        and "</think>" not in t), len(texts))
        texts.insert(rng.randint(0, open_at), text_call)
        ids = [None]
    items = list(texts)
    for j in range(n_calls):
        items += _call_frames(rng, ids[j], j, json.dumps({"packages": [f"p{draw}x{j}"]}))
    if n_calls and rng.random() < 0.6:
        items.append(FIN)
    stops = ([("none",), ("after",)] + [("before", k) for k in range(len(items))]
             + [("run", j) for j in range(len(ids))])
    where = rng.choice(stops)
    round_ = list(items)
    if where[0] == "before":
        round_.insert(where[1], STOP)
    elif where[0] == "after":
        round_.append(STOP)
    elif where[0] == "run":
        runs = {"n": 0}

        def on_run(j=where[1]):
            if runs["n"] == j:
                agent.press_stop()
            runs["n"] += 1
        tool.on_run = on_run

    streamed, out = _drive(agent, [round_])
    case = f"draw {draw}: stop={where} items={round_}"
    try:
        return _check_draw(agent, where, items, texts, ids, streamed, out)
    except AssertionError as exc:
        raise AssertionError(f"{case}: {exc}") from None


def _ids_match(calls, ids):
    """The committed call ids when they are the expected ones (None stands for an id the
    engine gave a text-written call), else an empty list."""
    got = [str(c.get("id")) for c in (calls or ())]
    if len(got) != len(ids) or any(i is not None and i != g for i, g in zip(ids, got)):
        return []
    return got


def _check_draw(agent, where, items, texts, ids, streamed, out):
    runs = len(agent.tools[TASK].calls)
    added = _turn_messages(agent.history)
    text_all = "".join(texts)
    answered = len(_final_answer_probe(text_all)) >= 2
    # I5, always
    assert agent.nonstream == 0, "I5 non-streamed request"
    assert_adjacent(agent.history)
    for request in agent.requests:
        assert_adjacent(request)
    tool_msgs = [m for m in added if m.get("role") == "tool"]
    if where[0] == "before":
        # I1: the stop is seen inside the stream
        k = where[1]
        before = "".join(i for i in items[:k] if i in texts)
        assert runs == 0, "I1 tool ran"
        assert len(agent.requests) == 1, "I1 request after the stop"
        assert added == ([{"role": "assistant", "content": before.strip()}]
                         if before.strip() else []), f"I1 committed {added}"
        assert out == (agent._clean_reasoning(before) or MARK), f"I1 return {out!r}"
        assert streamed == f"{before}\n\n{MARK}\n\n", f"I1 stream {streamed!r}"
        assert not _stop_pending(), "I5 stop left standing"
        if any(i.startswith("<tool_call>") for i in items[:k]):
            return "I1-text-call"
        return "I1-calls" if any(i not in texts for i in items[:k]) else "I1"
    if where[0] == "after" and ids:
        # I2: the stream ended, the stop is seen before the first tool
        assert runs == 0, "I2 tool ran"
        assert len(agent.requests) == 1, "I2 request after the stop"
        called = _ids_match(added[0].get("tool_calls"), ids)
        assert called, f"I2 calls {added}"
        assert [(m["tool_call_id"], m["content"]) for m in added[1:-1]] == [
            (cid, STOPPED_CALL_RESULT) for cid in called], f"I2 answers {added}"
        assert added[-1] == {"role": "assistant", "content": MARK}, f"I2 marker {added}"
        assert out == MARK, f"I2 return {out!r}"
        assert not _stop_pending(), "I5 stop left standing"
        return "I2"
    if where[0] == "after":
        # I2 without calls: the turn ends as on main. With an answer it ends on it and the
        # stop stays standing for the host; without one the empty retry's loop top sees it.
        assert runs == 0 and len(agent.requests) == 1, "I2 text-only request after the stop"
        if answered:
            assert out != MARK, f"I2 text-only return {out!r}"
            assert _stop_pending(), "I2 text-only: the stop was consumed"
        else:
            assert out == MARK, f"I2 text-only return {out!r}"
            assert not _stop_pending(), "I5 stop left standing"
        return "I2-text"
    if where[0] == "run":
        # I3: the stop is pressed inside the run of tool j
        j = where[1]
        assert runs == j + 1, f"I3 runs {runs}"
        assert len(agent.requests) == 1, "I3 request after the stop"
        called = _ids_match(added[0].get("tool_calls"), ids)
        assert called and [m["tool_call_id"] for m in tool_msgs] == called, f"I3 answers {added}"
        assert all(m["content"] != STOPPED_CALL_RESULT for m in tool_msgs[:j + 1]), "I3 ran"
        assert all(m["content"] == STOPPED_CALL_RESULT for m in tool_msgs[j + 1:]), "I3 rest"
        assert added[-1] == {"role": "assistant", "content": MARK}, f"I3 marker {added}"
        assert out == MARK, f"I3 return {out!r}"
        assert not _stop_pending(), "I5 stop left standing"
        return "I3-last" if j == len(ids) - 1 else "I3"
    # I4: no stop
    assert runs == len(ids), f"I4 runs {runs}"
    assert len(agent.requests) == (2 if ids or not answered else 1), \
        f"I4 requests {len(agent.requests)}"
    if ids:
        assert out == "Fertig.", f"I4 return {out!r}"
    assert not _stop_pending(), "I5 stop left standing"
    return "I4"


def stop_mismatches(agent, seed, draws):
    """(mismatches, {case kind: draws}) for `draws` rounds drawn from `seed`."""
    rng = random.Random(seed)
    start = copy.deepcopy(agent.history)
    bad, seen = [], {}
    for draw in range(draws):
        try:
            kind = _one_draw(agent, rng, draw, start)
            seen[kind] = seen.get(kind, 0) + 1
        except AssertionError as exc:
            bad.append(str(exc))
    TaskQueue().clear_stop("any")
    return bad, seen


@pytest.mark.parametrize("seed", [5, 23, 2026])
def test_stop_anywhere_keeps_the_invariants(agent, seed):
    """3 seeds x 40 draws here; `VAF_STOP_FUZZ_DRAWS` runs more per seed outside the suite.
    Every seed reaches each stop place that carries calls, so none goes untested quietly."""
    draws = int(os.environ.get("VAF_STOP_FUZZ_DRAWS", "40"))
    bad, seen = stop_mismatches(agent, seed, draws)
    assert not bad, f"{len(bad)} mismatches of {draws}, first: {bad[:3]}"
    assert {"I1-calls", "I1-text-call", "I2", "I3", "I3-last", "I4"} <= set(seen), seen
