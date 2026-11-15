# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The empty-answer retry keeps what the turn already did.

MEASURED BEFORE THE FIX, on the real `chat_step`: the first empty-answer retry rebuilt the
history as `history[:snapshot + 1] + [empty round]`, so a turn that had run a tool and then
answered empty sent the retry without that tool exchange, and the model ran the tool again.
An answer the user had already been shown (the pending-task auto-continue) went the same
way. The index was taken at the start of the turn; a compression after a tool result rebuilt
the list, and the slice then cut an assistant `tool_calls` message off its result.

The REAL `chat_step` on the API lane with only `api_backend.chat_completion` faked, and the
retry's four pure helpers under a seeded fuzz.

MUTATIONS, each turns the named test red:
- restore `self.history[:history_snapshot_len + 1] + [round]` at the first retry
  -> test_first_retry_keeps_the_tool_exchange, test_first_retry_keeps_a_kept_answer_and_its_note,
     test_first_retry_after_the_delay_reset;
- keep only `_turn_tool_exchanges(...)` of the turn at the first retry
  -> test_first_retry_keeps_a_kept_answer_and_its_note;
- skip the `drop_ids` filter in `_history_for_first_retry`
  -> test_first_retry_drops_only_what_earlier_retries_added,
     test_an_earlier_retry_message_before_a_fallen_back_start_goes_too,
     test_first_retry_after_the_delay_reset, the retry fuzz (P0, P4);
- apply that filter only after a start index (`history[:2] + filtered(history[2:])`)
  -> test_an_earlier_retry_message_before_a_fallen_back_start_goes_too, the retry fuzz;
- skip the filter on the part before the start in `_history_for_later_retry`
  -> test_an_earlier_retry_message_before_a_fallen_back_start_goes_too, the retry fuzz (P0);
- match the dropped messages by text instead of identity
  -> test_first_retry_drops_only_what_earlier_retries_added (the quoting reply);
- return `fallback + 1` from `_turn_start_index` without the identity search
  -> test_a_compression_inside_the_turn_cuts_no_exchange, the retry fuzz (P1);
- append the nudge in front of the turn's messages instead of after them
  -> the adjacency check in every chat_step test here (`assert_adjacent`);
- cut a result to `max_result_chars` and then append the note (the old, longer form)
  -> test_a_later_retry_cuts_a_result_once, the retry fuzz (P5).
"""
import copy
import json
import os
import random

import pytest
from fake_clock import only_on_thread

import vaf
from vaf.core.agent import (_history_for_first_retry, _history_for_later_retry,
                            _turn_start_index, _turn_tool_exchanges)
from vaf.core.platform import Platform

SESSION = "green123456"
ASK = "Bitte installiere mir die Pakete aus der Liste"
TASK = "python_sandbox"
TOOL = "TOOL"          # a script step: announce, then call the tool
TOOL2 = "TOOL2"        # a script step: announce, then call the tool twice in one round
STOP = "STOP"          # inside a step's chunks: the user presses Stop before the next chunk
FIRST_NUDGE = "You only provided thinking but no final answer for the user."
LATER_NUDGE = "You didn't provide a final answer."


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
        self.on_run = None      # called inside each run, e.g. to press Stop meanwhile

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_run is not None:
            self.on_run()
        return "10/10 installed"


def _tool_call(n, count=1):
    """One streamed round's tool calls: ids `c<n>`, then `c<n>b` for a second call, whose
    arguments differ (loop protection refuses a repeated identical call in one batch)."""
    calls = [(f"c{n}", "{}"), (f"c{n}b", '{"packages": ["numpy"]}')][:count]
    return json.dumps({"tool_calls": [{"index": i, "id": cid, "type": "function",
                                       "function": {"name": TASK, "arguments": args}}
                                      for i, (cid, args) in enumerate(calls)]})


def make_agent(monkeypatch, tmp_path):
    """The real Agent on the API lane. `agent.run(script)` drives one turn: each streamed
    main request takes the next step, a text, a list of chunks (`STOP` among them presses
    Stop), `TOOL` or `TOOL2`. A non-streamed request (the fallback for an empty stream)
    answers nothing, so an empty step stays empty. `agent.press_stop()` presses Stop from
    anywhere, a tool's `on_run` included. Returns (streamed text, return value)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    for var in ("VAF_IN_SUBAGENT_TERMINAL", "VAF_THINKING_MODE", "VAF_IN_AUTOMATION"):
        monkeypatch.delenv(var, raising=False)
    import vaf.core.agent as agent_mod
    from vaf.core.task_queue import TaskQueue
    # The retries' waits, skipped on this test's thread only (chat_step runs here): the
    # `time` module is shared, and another thread's loop must keep its real sleep.
    monkeypatch.setattr(agent_mod.time, "sleep", only_on_thread(lambda s: None))
    a = agent_mod.Agent(register_signals=False, run_kind="chat",
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
    a.chunks = []
    stop = {"now": False}
    monkeypatch.setattr(TaskQueue, "should_stop", lambda self, sid: stop["now"])
    monkeypatch.setattr(TaskQueue, "clear_stop", lambda self, sid: stop.update(now=False))
    a.press_stop = lambda: stop.update(now=True)

    def run(script, user_input=ASK):
        script = list(script)

        def provider(messages=None, **kw):
            if not kw.get("tools"):
                yield "ok"
                return
            if kw.get("stream") is False:
                return
            a.requests.append(copy.deepcopy(messages))
            if len(a.requests) > 12:
                raise AssertionError("runaway turn: more than twelve main requests")
            step = script.pop(0) if script else "Fertig."
            if step in (TOOL, TOOL2):
                yield "Einen Moment. "
                yield _tool_call(len(a.requests), 2 if step == TOOL2 else 1)
                yield json.dumps({"finish_reason": "tool_calls"})
                return
            for chunk in ([step] if isinstance(step, str) else step):
                if chunk == STOP:
                    stop["now"] = True
                    continue
                yield chunk
        a.api_backend.chat_completion = provider
        a.chunks = []
        returned = a.chat_step(user_input=user_input, raw_user_input=user_input,
                               stream_callback=a.chunks.append)
        return "".join(c for c in a.chunks if isinstance(c, str)), returned

    a.run = run
    return a


def assert_adjacent(messages):
    """Rule 4.1: each assistant message with `tool_calls` is followed at once by exactly one
    `role: tool` message per call id, and by nothing else before them."""
    for i, m in enumerate(messages):
        calls = m.get("tool_calls") if m.get("role") == "assistant" else None
        if not calls:
            continue
        ids = [str(c.get("id")) for c in calls]
        follow = messages[i + 1:i + 1 + len(ids)]
        got = [str(f.get("tool_call_id")) for f in follow if f.get("role") == "tool"]
        assert len(got) == len(ids) and sorted(got) == sorted(ids), (i, ids, follow)


def test_the_adjacency_check_catches_a_cut_exchange():
    call = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}, {"id": "c2"}]}
    r1 = {"role": "tool", "tool_call_id": "c1", "content": "a"}
    r2 = {"role": "tool", "tool_call_id": "c2", "content": "b"}
    note = {"role": "system", "content": "note"}
    assert_adjacent([call, r1, r2, note])
    for broken in ([call, r1, note, r2], [call, note, r1, r2], [call, r1], [call, r1, r1]):
        with pytest.raises(AssertionError):
            assert_adjacent(broken)


def _index(messages, pred, what):
    for i, m in enumerate(messages):
        if pred(m):
            return i
    raise AssertionError(f"{what} is not in the request: {messages}")


def _is_call(call_id):
    return lambda m: (m.get("role") == "assistant"
                      and any(str(c.get("id")) == call_id for c in (m.get("tool_calls") or ())))


def _is_result(call_id):
    return lambda m: m.get("role") == "tool" and str(m.get("tool_call_id")) == call_id


def _is_empty_round(m):
    return m.get("role") == "assistant" and m.get("content") == "[Empty]"


def _is_first_nudge(m):
    return m.get("role") == "system" and str(m.get("content") or "").startswith(FIRST_NUDGE)


def _is_later_nudge(m):
    return m.get("role") == "system" and str(m.get("content") or "").startswith(LATER_NUDGE)


@pytest.fixture
def agent(monkeypatch, tmp_path):
    return make_agent(monkeypatch, tmp_path)


# ── AC1: the tool exchange stays ─────────────────────────────────────────────

def test_first_retry_keeps_the_tool_exchange(agent):
    """Tool round, empty round, answer: the retry request holds the call and its result
    before the empty round and the nudge, and the tool ran once."""
    _, returned = agent.run([TOOL, "", "Die Pakete sind installiert."])
    assert returned == "Die Pakete sind installiert."
    assert len(agent.requests) == 3, len(agent.requests)
    retry = agent.requests[2]
    call = _index(retry, _is_call("c1"), "the assistant tool_calls message")
    result = _index(retry, _is_result("c1"), "the tool result")
    empty = _index(retry, _is_empty_round, "the empty round")
    nudge = _index(retry, _is_first_nudge, "the first-retry nudge")
    assert call < result < empty < nudge, (call, result, empty, nudge)
    assert retry[result]["content"] == agent.requests[1][
        _index(agent.requests[1], _is_result("c1"), "the result")]["content"]
    assert len(agent.tools[TASK].calls) == 1
    for request in agent.requests:
        assert_adjacent(request)
    assert_adjacent(agent.history)


# ── AC2: a shown answer and its note stay ────────────────────────────────────

def test_first_retry_keeps_a_kept_answer_and_its_note(agent, monkeypatch):
    """A validated answer, the pending-task auto-continue note, then an empty round: the
    retry still holds the answer and the note, in that order, before the empty round."""
    steps = iter([(0, "Pakete installieren", 0, 1)])
    monkeypatch.setattr(agent.main_persistence, "_current_step", lambda tasks: next(steps, None))
    monkeypatch.setattr(agent, "_reply_needs_user", lambda text: False)
    first = "Hier ist die Liste der zehn Pakete: numpy, pandas und acht weitere."
    second = "Alle zehn Pakete sind jetzt installiert."
    _, returned = agent.run([first, "", second])
    assert returned == first + "\n\n" + second
    assert len(agent.requests) == 3, len(agent.requests)
    retry = agent.requests[2]
    kept = _index(retry, lambda m: m.get("role") == "assistant" and m.get("content") == first,
                  "the kept answer")
    note = _index(retry, lambda m: m.get("role") == "system"
                  and str(m.get("content") or "").startswith("[System: You"),
                  "the auto-continue note")
    empty = _index(retry, _is_empty_round, "the empty round")
    nudge = _index(retry, _is_first_nudge, "the first-retry nudge")
    assert kept < note < empty < nudge, (kept, note, empty, nudge)
    for request in agent.requests:
        assert_adjacent(request)
    assert_adjacent(agent.history)


# ── AC3: what an earlier retry added goes, nothing else ──────────────────────

def test_first_retry_drops_only_what_earlier_retries_added():
    base = {"role": "user", "content": ASK}
    call = {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": TASK, "arguments": "{}"}}]}
    result = {"role": "tool", "tool_call_id": "c1", "content": "10/10 installed"}
    old_round = {"role": "assistant", "content": "[Empty]"}
    old_nudge = {"role": "system", "content": FIRST_NUDGE + " Provide a clear answer now."}
    later_nudge = {"role": "system", "content": LATER_NUDGE + " Please provide a response."}
    # The model's own words, quoting the nudge: equal text, not an object a retry added.
    quoting = {"role": "assistant", "content": old_nudge["content"]}
    history = [{"role": "system", "content": "sys"}, base, call, result, old_round, old_nudge,
               quoting, later_nudge]
    new_round = {"role": "assistant", "content": "[Empty]"}
    new_nudge = {"role": "system", "content": FIRST_NUDGE}
    out = _history_for_first_retry(history, {id(old_round), id(old_nudge), id(later_nudge)},
                                   new_round, new_nudge)
    assert [id(m) for m in out] == [id(m) for m in (history[0], base, call, result, quoting,
                                                    new_round, new_nudge)]


def test_an_earlier_retry_message_before_a_fallen_back_start_goes_too():
    """A turn started without input on a history with no user message, the base dropped by
    the emergency purge: the start falls back to an index, which lies after an earlier
    retry's nudge (the fuzz shape `[sys, nudge, call, result]`, start 2). Both retries drop
    that nudge all the same: it is this turn's own message, wherever it sits."""
    sys_msg = {"role": "system", "content": "sys"}
    old_nudge = {"role": "system", "content": LATER_NUDGE}
    call = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}
    result = {"role": "tool", "tool_call_id": "c1", "content": "10/10 installed"}
    history = [sys_msg, old_nudge, call, result]
    start = _turn_start_index(history, {"role": "system", "content": "the dropped base"}, 1)
    assert start == 2
    new_round = {"role": "assistant", "content": "[Empty]"}
    new_nudge = {"role": "system", "content": FIRST_NUDGE}
    first = _history_for_first_retry(history, {id(old_nudge)}, new_round, new_nudge)
    assert [id(m) for m in first] == [id(m) for m in (sys_msg, call, result, new_round,
                                                      new_nudge)]
    later = _history_for_later_retry(history, start, {id(old_nudge)})
    assert [id(m) for m in later] == [id(m) for m in (sys_msg, call, result)]


def test_first_retry_after_the_delay_reset(agent):
    """The API lane resets the retry count after its third empty answer (a delayed retry), so
    a fourth empty answer is a FIRST retry again: its request carries the tool exchange and
    one empty round with one nudge, none of the earlier retries' messages."""
    _, returned = agent.run([TOOL, "", "", "", "", "Die Pakete sind installiert."])
    assert returned == "Die Pakete sind installiert."
    assert len(agent.requests) == 6, len(agent.requests)
    retry = agent.requests[5]
    assert sum(1 for m in retry if _is_empty_round(m)) == 1, retry
    assert sum(1 for m in retry if _is_first_nudge(m)) == 1, retry
    assert not any(_is_later_nudge(m) for m in retry), retry
    assert _index(retry, _is_result("c1"), "the tool result") < _index(
        retry, _is_empty_round, "the empty round")
    assert len(agent.tools[TASK].calls) == 1
    for request in agent.requests:
        assert_adjacent(request)
    assert_adjacent(agent.history)


# ── AC4: a compression inside the turn cuts no exchange ─────────────────────

def test_a_compression_inside_the_turn_cuts_no_exchange(agent, monkeypatch):
    """The compression after a tool result drops three older messages in front of the
    request. The index taken at the start of the turn then pointed three messages too far:
    the first retry's slice cut the assistant `tool_calls` message off its result, and a
    later retry's fresh start kept the first retry's empty round in front of the model."""
    agent.init_chat()
    agent.history.extend([{"role": "user", "content": "Wie spaet ist es?"},
                          {"role": "assistant", "content": "Es ist zehn Uhr."},
                          {"role": "user", "content": "Und das Wetter?"},
                          {"role": "assistant", "content": "Sonnig."}])
    done = {"cut": False}

    def manage_context(*a, **k):
        h = agent.history
        if not done["cut"] and h and h[-1].get("role") == "tool":
            done["cut"] = True
            del h[1:4]
    monkeypatch.setattr(agent, "manage_context", manage_context)
    _, returned = agent.run([TOOL, "", "", "Die Pakete sind installiert."])
    assert done["cut"], "the compression never ran after the tool result"
    assert returned == "Die Pakete sind installiert."
    assert len(agent.requests) == 4, len(agent.requests)
    for retry in agent.requests[2:]:
        call = _index(retry, _is_call("c1"), "the assistant tool_calls message")
        assert _is_result("c1")(retry[call + 1]), retry
    # The second retry starts fresh after the request: the exchange, its own nudge, nothing
    # of the first retry.
    later = agent.requests[3]
    assert not any(_is_empty_round(m) or _is_first_nudge(m) for m in later), later
    assert any(_is_later_nudge(m) for m in later), later
    assert len(agent.tools[TASK].calls) == 1
    for request in agent.requests:
        assert_adjacent(request)
    assert_adjacent(agent.history)


# ── later retries: the exchange scan ────────────────────────────────────────

def test_a_later_retry_cuts_a_result_once():
    long = {"role": "tool", "tool_call_id": "c1", "content": "x" * 6000}
    call = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}
    once = _turn_tool_exchanges([call, {"role": "assistant", "content": "hm"}, long,
                                 {"role": "system", "content": "note"}])
    assert [m.get("role") for m in once] == ["assistant", "tool"]
    assert once[0] is call
    assert len(once[1]["content"]) == 2500
    assert once[1]["content"].endswith("[truncated for snapshot, original len: 6000]")
    assert long["content"] == "x" * 6000, "the original result was changed in place"
    twice = _turn_tool_exchanges(once)
    assert [id(m) for m in twice] == [id(m) for m in once]


def test_the_turn_start_is_found_by_identity_then_by_the_request():
    sys_msg = {"role": "system", "content": "sys"}
    req = {"role": "user", "content": ASK}
    note = {"role": "system", "content": "onboarding"}
    turn = {"role": "assistant", "content": "x"}
    # The base is the onboarding note after the request: the turn starts after the note.
    assert _turn_start_index([sys_msg, req, note, turn], note, 2) == 3
    # The note is gone (a compression dropped it): after the request.
    assert _turn_start_index([sys_msg, req, turn], note, 2) == 2
    # A copy is not the base.
    assert _turn_start_index([sys_msg, req, dict(note), turn], note, 0) == 2
    # Nothing to find: the index the turn started with, bounded by the history.
    assert _turn_start_index([sys_msg, turn], note, 5) == 2


# ── the retry state machine, fuzzed ─────────────────────────────────────────

def _calls_of(m):
    if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
        return [str(c.get("id")) for c in m["tool_calls"]]
    return []


def _complete_exchanges(history):
    """{id(assistant): [tool messages]} for every assistant `tool_calls` message followed at
    once by one result per call id."""
    out = {}
    for i, m in enumerate(history):
        ids = _calls_of(m)
        if not ids:
            continue
        follow = history[i + 1:i + 1 + len(ids)]
        if (len(follow) == len(ids) and all(f.get("role") == "tool" for f in follow)
                and sorted(str(f.get("tool_call_id")) for f in follow) == sorted(ids)):
            out[id(m)] = follow
    return out


def _broken_calls(history):
    return {id(m) for m in history if _calls_of(m)} - set(_complete_exchanges(history))


def _check(before, after, *, later, drop, pre_turn, request, start=None, start_known=True):
    # P0: nothing an earlier retry of this turn appended survives, wherever it sat.
    assert not any(id(m) in drop for m in after), "P0 earlier retry message"
    if later:
        # P1: the part before the start is the same objects, minus the retries' messages;
        # where the start can be known (the base is there, or the request), it is exactly
        # what came before the turn: no turn message in it, no pre-turn message after it.
        head = [m for m in before[:start] if id(m) not in drop]
        assert [id(m) for m in after[:len(head)]] == [id(m) for m in head], "P1 prefix"
        if start_known:
            assert {id(m) for m in head} == {id(m) for m in before if id(m) in pre_turn}, "P1 turn"
    else:
        # P4: the whole history minus what the retries added, in order.
        assert [id(m) for m in after] == [id(m) for m in before if id(m) not in drop], "P4 turn"
    # P2: every complete exchange stays complete, same objects in the same order; a later
    # retry may hand a truncated copy of a result.
    for aid, results in _complete_exchanges(before).items():
        pos = next((i for i, m in enumerate(after) if id(m) == aid), None)
        assert pos is not None, "P2 call lost"
        got = after[pos + 1:pos + 1 + len(results)]
        assert len(got) == len(results), "P2 results lost"
        for r, g in zip(results, got):
            if later and g is not r:
                assert g.get("tool_call_id") == r.get("tool_call_id"), "P2 result order"
                assert g["content"].startswith(str(r["content"])[:100]), "P2 truncated copy"
            else:
                assert g is r, "P2 result object"
    # P3: no call cut off from its results that was not already cut off.
    assert _broken_calls(after) <= _broken_calls(before), "P3 adjacency"
    # P6: the request is always there.
    assert request is None or any(m is request for m in after), "P6 request"


def _tool_round(rng, counter):
    n = rng.randint(1, 3)
    ids = []
    for _ in range(n):
        counter[0] += 1
        ids.append(f"c{counter[0]}")
    msgs = [{"role": "assistant", "content": rng.choice([None, "", "Einen Moment."]),
             "tool_calls": [{"id": i, "type": "function",
                             "function": {"name": TASK, "arguments": "{}"}} for i in ids]}]
    for i in ids:
        msgs.append({"role": "tool", "tool_call_id": i,
                     "content": "r" * rng.choice([0, 10, 2400, 2500, 2501, 6000])})
    if rng.random() < 0.4:
        msgs.append({"role": "system", "content": "[System: deferred error note]"})
    return msgs


def retry_mismatches(seed, draws):
    rng = random.Random(seed)
    bad = []
    for _ in range(draws):
        try:
            _one_retry_turn(rng)
        except AssertionError as exc:
            bad.append(str(exc))
    return bad


def _one_retry_turn(rng):
    counter = [0]
    sys_msg = {"role": "system", "content": "sys"}
    history = [sys_msg]
    if rng.random() < 0.2:
        # A turn started without input on a history that holds no user message: the base is
        # the previous last message, and no request is there to fall back on.
        for k in range(rng.randint(0, 6)):
            history.append({"role": "assistant" if k % 2 == 0 else "system", "content": f"alt {k}"})
        request = None
        base = history[-1]
    else:
        for k in range(rng.randint(0, 6)):
            history.append({"role": "user" if k % 2 == 0 else "assistant", "content": f"alt {k}"})
        request = {"role": "user", "content": ASK}
        history.append(request)
        base = request
        if rng.random() < 0.3:
            base = {"role": "system", "content": "onboarding note"}
            history.append(base)
    # The objects themselves are held, so an id stays theirs after a compression drops them.
    pre_turn_objects = list(history)
    pre_turn = {id(m) for m in pre_turn_objects}
    fallback = len(history) - 1
    added = []          # what the retries appended, as the turn-local list keeps it
    count = 0
    for _ in range(rng.randint(1, 12)):
        ev = rng.random()
        if ev < 0.3:
            history.extend(_tool_round(rng, counter))
        elif ev < 0.42:
            history.extend([{"role": "assistant", "content": "Antwort eins steht."},
                            {"role": "system", "content": "[System: You still have pending tasks]"}])
        elif ev < 0.52:
            anchor = history.index(request) if request is not None else 0
            if anchor > 1 and rng.random() < 0.6:
                k = rng.randint(1, anchor - 1)
                del history[1:1 + k]
            else:
                # The emergency purge: system, the request when it lies before the tail, the
                # last six.
                tail_start = max(1, len(history) - 6)
                anchor_msg = [request] if 1 <= anchor < tail_start else []
                history[:] = [sys_msg] + anchor_msg + history[tail_start:]
        elif ev < 0.6:
            count = 0                                   # the API delay path's reset
        elif count == 0:
            drop = {id(m) for m in added}
            r = {"role": "assistant", "content": "[Empty]"}
            n = {"role": "system", "content": FIRST_NUDGE}
            out = _history_for_first_retry(history, drop, r, n)
            _check(history, out[:-2], later=False, drop=drop, pre_turn=pre_turn,
                   request=request)
            assert out[-2] is r and out[-1] is n, "P4 pair"
            history = out
            added.extend((r, n))
            count = 1
        else:
            start = _turn_start_index(history, base, fallback)
            known = any(m is base for m in history) or request is not None
            drop = {id(m) for m in added}
            out = _history_for_later_retry(history, start, drop)
            _check(history, out, later=True, drop=drop, pre_turn=pre_turn, request=request,
                   start=start, start_known=known)
            # P5: nothing but exchanges after the start, every result within the cut, and a
            # second pass changes nothing.
            head = len(out) - len(_turn_tool_exchanges(history[start:]))
            for m in out[head:]:
                assert m.get("role") == "tool" or _calls_of(m), "P5 text after the base"
                if m.get("role") == "tool":
                    assert len(str(m.get("content"))) <= 2500, "P5 result length"
            again = _history_for_later_retry(out, head, drop)
            assert [id(m) for m in again] == [id(m) for m in out], "P5 idempotent"
            nudge = {"role": "system", "content": LATER_NUDGE}
            history = out + [nudge]
            added.append(nudge)
            count += 1
        assert len(history) < 120


@pytest.mark.parametrize("seed", [3, 17, 2026])
def test_the_retry_helpers_keep_their_invariants(seed):
    """3 seeds x 300 turns here; `VAF_EMPTY_RETRY_FUZZ_DRAWS` runs more outside the suite."""
    draws = int(os.environ.get("VAF_EMPTY_RETRY_FUZZ_DRAWS", "300"))
    bad = retry_mismatches(seed, draws)
    assert not bad, f"{len(bad)} mismatches, first: {bad[:3]}"
