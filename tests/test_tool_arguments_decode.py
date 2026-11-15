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

Text nested deeper than the interpreter can decode gets the same refusal. `json.loads` raises
`RecursionError` there, which is not a `ValueError`, so it used to escape the decoder and end
the chat turn, the coder's tool loop or a `ToolCaller.execute` call. The depth limit depends
on the interpreter (about 1000 before 3.12, about 10,000 on 3.13, and lower inside the chat
loop's frames), so the tests use depths that fail everywhere and never pin the number.

MUTATION: restore `except: arguments = {}` in `chat_step` and the chat test goes red (the
tool runs); drop the string branch in `ToolCaller.execute` and the funnel test goes red;
narrow the decoder's catch back to `except ValueError` and every too-deep test and the fuzz
go red with `RecursionError`; make the decoder walk the text quadratically before parsing
and the megabyte cost test goes red. `VAF_DECODE_FUZZ_DRAWS` runs a larger seeded draw count
outside the suite.
"""
import inspect
import json
import os
import random
import time

import pytest
import requests

from test_local_engine_wiring import _local_agent, _Resp, _Stream
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


# ── arguments nested deeper than the interpreter can decode ──────────────────────────

_PREFIX = "Tool Error: the arguments of this 'web_search' call"


def _list_nesting(depth: int) -> str:
    return "[" * depth + "]" * depth


def _object_nesting(depth: int) -> str:
    return '{"a":' * depth + "1" + "}" * depth


def _assert_retryable_refusal(refusal):
    assert refusal.startswith(_PREFIX + " are not valid JSON ("), refusal[:200]
    assert "nothing was run" in refusal
    assert tool_result_is_error(refusal)
    assert not refusal.lower().startswith(("error", "failed", "❌"))


@pytest.mark.parametrize("raw", [
    _list_nesting(10_000), _list_nesting(100_000),
    _object_nesting(10_000), _object_nesting(100_000),
    '{"query":' + _list_nesting(10_000) + "}",
], ids=["list-10000", "list-100000", "object-10000", "object-100000", "inside-query-10000"])
def test_arguments_nested_too_deep_are_refused(raw):
    args, refusal = decode_arguments("web_search", raw)
    assert args == {}
    _assert_retryable_refusal(refusal)


def test_the_funnel_refuses_too_deep_text(monkeypatch):
    """The librarian's and an embedder's lane: `execute` keeps its never-raises contract."""
    monkeypatch.setattr("vaf.core.trust.get_tool_policy", lambda *a, **k: "always")
    spy = _Spy()
    out = ToolCaller({spy.name: spy}).execute(spy.name, _list_nesting(10_000))
    assert out.startswith("Tool Error: the arguments of this 'spy_tool' call are not valid JSON (")
    assert "nothing was run" in out
    assert spy.calls == []


@pytest.mark.parametrize("depth", [9990, 10_000, 100_000])
def test_the_chat_loop_answers_a_too_deep_call_in_its_own_message(monkeypatch, tmp_path, depth):
    """A real `chat_step` against the local stub, the call arriving as a structured delta.

    Depth 9990 decodes at the top level of a 3.13 interpreter and fails inside the loop's
    frames, so its tail (list type or recursion) is not pinned; the deeper two fail on every
    supported version."""
    agent = _local_agent(monkeypatch, tmp_path)
    assert "web_search" in agent.tools, "the stub agent has no web_search to dispatch"
    ran = []
    monkeypatch.setattr(agent.tools["web_search"], "run",
                        lambda **kw: ran.append(kw) or "Ergebnis.", raising=False)
    # The loop pauses two seconds after every error result; nothing here needs the pause.
    monkeypatch.setattr("vaf.core.agent.time.sleep", lambda *_a, **_k: None)
    call = {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                            "function": {"name": "web_search",
                                         "arguments": _list_nesting(depth)}}]}
    seen = []

    def post(url, json=None, **kw):
        if isinstance(json, dict) and json.get("stream") and "messages" in json:
            seen.append(json)
            return _Stream([call] if len(seen) == 1 else [{"content": "Fertig."}])
        return _Resp(500, {})
    monkeypatch.setattr(requests, "post", post)

    agent.chat_step(user_input="Hallo", raw_user_input="Hallo", stream_callback=lambda _t: None)

    assert len(seen) >= 2, "the turn ended before the refusal went back to the model"
    assert ran == [], "the tool ran without the arguments the model meant"
    hist = agent.history
    asked = [i for i, m in enumerate(hist) if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(asked) == 1, [m.get("role") for m in hist]
    assert [tc.get("id") for tc in hist[asked[0]]["tool_calls"]] == ["c1"]
    answer = hist[asked[0] + 1]
    assert answer.get("role") == "tool" and answer.get("tool_call_id") == "c1", answer
    assert len([m for m in hist if m.get("role") == "tool"]) == 1
    assert answer["content"].startswith(_PREFIX), answer["content"][:200]
    if depth >= 10_000:
        assert answer["content"].startswith(_PREFIX + " are not valid JSON ("), answer["content"][:200]


def _seconds(raw) -> float:
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        _args, refusal = decode_arguments("web_search", raw)
        best = min(best, time.perf_counter() - start)
        assert refusal and refusal.startswith(_PREFIX)
    return best


@pytest.mark.parametrize("raw", [
    "[" * 1_000_000, _list_nesting(500_000), '{"a":' * 200_000,
], ids=["open-1mb", "balanced-1mb", "object-1mb"])
def test_a_megabyte_of_nesting_is_refused_fast(raw):
    """The scanner gives up at the depth limit, not at the end of the text: measured 0.5 to
    0.8 ms per megabyte. The bound is generous for a loaded CI runner; a decoder that walked
    the text quadratically first would blow it."""
    assert _seconds(raw) < 1.0


# ── seeded fuzz: never raises, and agrees with a json.loads reference ────────────────

_OPENERS = [("[", "]"), ('{"a":', "}"), ('{"a":[', "]}"), (" [ ", "]")]
_INNERS = ["", "1", '"x"', "null", "{}", "[]", "nul"]
_TAILS = ["", " ", "x", "]"]
_NOISE = '[]{}":,a1 \nu'


def _depth(rng: random.Random) -> int:
    pick = rng.random()
    if pick < 0.4:
        return rng.randint(0, 50)
    if pick < 0.8:
        return int(10 ** rng.uniform(0, 5.3))
    return 9998 + rng.randint(-5, 5)


def _fuzz_input(rng: random.Random):
    pick = rng.random()
    if pick < 0.05:
        return rng.choice([None, {"a": 1}, {}, [1], [], 0, 3.5, -2, True, False])
    if pick < 0.15:
        chars = [rng.choice(_NOISE) for _ in range(rng.randint(0, 200))]
        if chars and rng.random() < 0.3:
            chars.insert(rng.randrange(len(chars)), "\ud800")
        return "".join(chars)
    opener, closer = rng.choice(_OPENERS)
    depth = _depth(rng)
    close = depth if rng.random() < 0.7 else rng.randint(0, depth)
    return (rng.choice(["", " ", "\n\t "]) + opener * depth + rng.choice(_INNERS)
            + closer * close + rng.choice(_TAILS))


def _same_json(left, right) -> bool:
    """Structural equality without recursion: the decoded values can be thousands deep."""
    stack = [(left, right)]
    while stack:
        a, b = stack.pop()
        if type(a) is not type(b):
            return False
        if isinstance(a, dict):
            if a.keys() != b.keys():
                return False
            stack.extend((a[k], b[k]) for k in a)
        elif isinstance(a, list):
            if len(a) != len(b):
                return False
            stack.extend(zip(a, b))
        elif a != b:
            return False
    return True


def _reference(raw):
    """What the decoder must answer: (args, None) or the string "refuse"."""
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return "refuse"
    text = raw.strip()
    if not text:
        return {}, None
    try:
        value = json.loads(text)
    except RecursionError:
        return "refuse-depth"
    except ValueError:
        return "refuse"
    if value is None:
        return {}, None
    if isinstance(value, dict):
        return value, None
    return "refuse"


def _fuzz(draws: int, seed: int = 0xDEC0DE) -> dict:
    rng = random.Random(seed)
    stats = {"draws": 0, "refused": 0, "boundary": 0}
    for i in range(draws):
        raw = _fuzz_input(rng)
        ctx = f"draw {i}, input {str(raw)[:80]!r}... ({len(str(raw))} chars)"
        try:
            got = decode_arguments("web_search", raw)
        except BaseException as exc:  # noqa: BLE001 - the oracle is "never raises"
            raise AssertionError(f"{ctx} raised {type(exc).__name__}: {exc}") from exc
        assert isinstance(got, tuple) and len(got) == 2, ctx
        args, refusal = got
        assert isinstance(args, dict), ctx
        assert refusal is None or (isinstance(refusal, str) and refusal.startswith(_PREFIX)), ctx
        want = _reference(raw)
        stats["draws"] += 1
        if want in ("refuse", "refuse-depth"):
            # The decoder runs a frame deeper than the reference, so where the reference ran
            # out of depth, it did too.
            assert args == {} and refusal is not None, ctx
            stats["refused"] += 1
        elif refusal is not None:
            # The one tolerated disagreement: a value right at the depth limit decoded at the
            # reference's stack depth and ran out one frame further down.
            assert args == {} and "recursion" in refusal, ctx
            stats["boundary"] += 1
        else:
            if isinstance(raw, dict):
                assert args is raw, ctx
            assert _same_json(args, want[0]), ctx
    return stats


def test_decode_fuzz_never_raises_and_agrees_with_json():
    """1,000 seeded draws in about a second; `VAF_DECODE_FUZZ_DRAWS` runs more outside the
    suite. The deep and near-limit depths are drawn on purpose."""
    stats = _fuzz(int(os.environ.get("VAF_DECODE_FUZZ_DRAWS", "1000")))
    assert stats["draws"] > 0 and stats["refused"] > 0, stats
