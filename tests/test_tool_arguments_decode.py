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

Arguments nested deeper than `MAX_ARGUMENT_DEPTH` (100) levels are refused as well, as text
and as a dict alike, with a refusal that names the limit. Every walk after the decoder is
Python-recursive (`make_json_serializable` when the request is built, `mask_secret_args` at
the gate, the previews, the tools), and a value about 1000 levels deep used to raise out of
`ToolCaller.execute`, end the librarian's run, end the coder's run at its doom-loop
signature, or come back from the chat loop as "maximum recursion depth exceeded". Text nested
deeper than the interpreter can decode at all makes `json.loads` raise `RecursionError`
(about 1000 levels before 3.12, about 10,000 on 3.13, lower inside the chat loop's frames);
it is over the limit by definition and gets the same depth refusal.

MUTATION: restore `except: arguments = {}` in `chat_step` and the chat test goes red (the
tool runs); drop the string branch in `ToolCaller.execute` and the funnel test goes red;
drop the decoder's `RecursionError` branch and every too-deep test and the fuzz go red with
`RecursionError`; make the decoder walk the text quadratically before parsing and the
megabyte cost test goes red. Remove the depth check and the deep-decoded tests and the depth
fuzz go red; write `>=` and the depth-100 cases are refused; narrow the funnel back to text
and the dict cases raise. `VAF_DECODE_FUZZ_DRAWS` and `VAF_DEPTH_FUZZ_DRAWS` run larger
seeded draw counts outside the suite.
"""
import inspect
import json
import os
import pathlib
import random
import time
import uuid

import pytest
import requests

from fake_clock import only_on_thread
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
    assert "decode_arguments(fn_name, fn_args if fn_args else fn_args_str)" in run


# ── arguments nested deeper than the interpreter can decode ──────────────────────────

_PREFIX = "Tool Error: the arguments of this 'web_search' call"
_DEEP = "Tool Error: the arguments of this '{}' call are nested deeper than 100 levels"


def _list_nesting(depth: int) -> str:
    return "[" * depth + "]" * depth


def _object_nesting(depth: int) -> str:
    return '{"a":' * depth + "1" + "}" * depth


def _assert_retryable_refusal(refusal, tool="web_search"):
    assert refusal.startswith(_DEEP.format(tool)), refusal[:200]
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
    _assert_retryable_refusal(out, spy.name)
    assert spy.calls == []


def test_malformed_shallow_text_keeps_its_own_refusal():
    """The depth branch is split off the parse branch: an unquoted value still names the
    parse error, byte for byte as before."""
    with pytest.raises(ValueError) as parse_error:
        json.loads(MALFORMED)
    args, refusal = decode_arguments("web_search", MALFORMED)
    assert args == {}
    assert refusal == (_PREFIX + f" are not valid JSON ({parse_error.value}), so nothing was "
                       "run. Call it again with a JSON object whose string values are all in "
                       "double quotes.")


@pytest.mark.parametrize("depth", [9990, 10_000, 100_000])
def test_the_chat_loop_answers_a_too_deep_call_in_its_own_message(monkeypatch, tmp_path, depth):
    """A real `chat_step` against the local stub, the call arriving as a structured delta.

    Depth 9990 decodes at the top level of a 3.13 interpreter and may fail inside the loop's
    frames; either way it is over the depth limit, so all three get the depth refusal."""
    _chat_turn_answers_with_the_depth_refusal(monkeypatch, tmp_path, _list_nesting(depth))


def test_the_chat_loop_answers_a_deep_decoded_call(monkeypatch, tmp_path):
    """Text that decodes fine, 3001 levels deep: before the limit it reached the funnel as a
    dict and came back as "Error executing tool: maximum recursion depth exceeded", after a
    two-second pause and an error line on the console."""
    errors = _chat_turn_answers_with_the_depth_refusal(
        monkeypatch, tmp_path, '{"query": ' + _list_nesting(3000) + "}")
    assert not [e for e in errors if "web_search" in str(e)], errors


def _chat_turn_answers_with_the_depth_refusal(monkeypatch, tmp_path, arguments) -> list:
    """Drive one real `chat_step` whose model calls `web_search` with ``arguments``; assert
    the call is answered once, adjacent, with the depth refusal, and the tool never ran.
    Returns what the loop printed through `UI.error`."""
    from vaf.cli.ui import UI
    errors = []
    monkeypatch.setattr(UI, "error", staticmethod(lambda text: errors.append(text)))
    agent = _local_agent(monkeypatch, tmp_path)
    assert "web_search" in agent.tools, "the stub agent has no web_search to dispatch"
    ran = []
    monkeypatch.setattr(agent.tools["web_search"], "run",
                        lambda **kw: ran.append(kw) or "Ergebnis.", raising=False)
    # The loop pauses two seconds after every error result; nothing here needs the pause.
    # `time` is the shared module, so the fake is limited to this thread, the one chat_step
    # runs on.
    monkeypatch.setattr("vaf.core.agent.time.sleep", only_on_thread(lambda _s: None))
    call = {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                            "function": {"name": "web_search", "arguments": arguments}}]}
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
    _assert_retryable_refusal(answer["content"])
    return errors


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


def _true_depth(value) -> int:
    """Container levels, counted with its own stack and without `nesting_depth`: the reference
    must not share the code it checks. Decoded JSON has no sharing and no cycles."""
    deepest, stack = 0, [(value, 1)]
    while stack:
        node, level = stack.pop()
        if isinstance(node, (dict, list, tuple)):
            deepest = max(deepest, level)
            stack.extend((child, level + 1)
                         for child in (node.values() if isinstance(node, dict) else node))
    return deepest


def _reference(raw):
    """What the decoder must answer: (args, None), "refuse" or "refuse-depth"."""
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return ("refuse-depth" if _true_depth(raw) > 100 else (raw, None))
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
        return ("refuse-depth" if _true_depth(value) > 100 else (value, None))
    return "refuse"


def _fuzz(draws: int, seed: int = 0xDEC0DE) -> dict:
    rng = random.Random(seed)
    stats = {"draws": 0, "refused": 0, "too_deep": 0}
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
        if want == "refuse-depth":
            # A value too deep to decode at the reference's stack depth is far over the limit,
            # so the decoder answers it with the depth refusal wherever its own decode stops.
            assert args == {} and refusal is not None, ctx
            assert refusal.startswith(_DEEP.format("web_search")), ctx
            stats["refused"] += 1
            stats["too_deep"] += 1
        elif want == "refuse":
            assert args == {} and refusal is not None, ctx
            stats["refused"] += 1
        else:
            assert refusal is None, ctx
            if isinstance(raw, dict):
                assert args is raw, ctx
            assert _same_json(args, want[0]), ctx
    return stats


def test_decode_fuzz_never_raises_and_agrees_with_json():
    """1,000 seeded draws in about a second; `VAF_DECODE_FUZZ_DRAWS` runs more outside the
    suite. The deep and near-limit depths are drawn on purpose."""
    stats = _fuzz(int(os.environ.get("VAF_DECODE_FUZZ_DRAWS", "1000")))
    assert stats["draws"] > 0 and stats["refused"] > 0 and stats["too_deep"] > 0, stats


# ── arguments that decode, but nest deeper than MAX_ARGUMENT_DEPTH ───────────────────
#
# Deep values are built with loops and deep text with string operations, never with
# recursion or `json.dumps`: on 3.10 and 3.11 the encoder stops at about 1000 levels.

def _deep_list(depth: int) -> list:
    value: list = []
    for _ in range(depth - 1):
        value = [value]
    return value


def _deep_dict(depth: int) -> dict:
    value: dict = {}
    for _ in range(depth - 1):
        value = {"k": value}
    return value


def _deep_text(depth: int, inner: str) -> str:
    """``{"plan": ...}`` as text, ``depth`` levels deep with the arguments object."""
    if inner == "list":
        return '{"plan": ' + _list_nesting(depth - 1) + "}"
    return '{"plan": ' + '{"k": ' * (depth - 2) + "{}" + "}" * (depth - 2) + "}"


def _deep_args(depth: int, inner: str) -> dict:
    return {"plan": (_deep_list if inner == "list" else _deep_dict)(depth - 1)}


def _shared_lists(levels: int) -> list:
    """``levels`` lists that each hold the next one twice: 2**levels paths, depth levels + 1."""
    node: list = []
    for _ in range(levels):
        node = [node, node]
    return node


def _self_containing() -> dict:
    loop: dict = {"plan": "x"}
    loop["again"] = loop
    return loop


@pytest.mark.parametrize("lane", ["dict", "text"])
@pytest.mark.parametrize("inner", ["list", "dict"])
def test_a_dict_one_level_too_deep_is_refused(lane, inner):
    at_limit = _deep_args(100, inner)
    raw = _deep_text(100, inner) if lane == "text" else at_limit
    args, refusal = decode_arguments("web_search", raw)
    assert refusal is None
    if lane == "dict":
        assert args is at_limit
    else:
        assert args == at_limit
    over = _deep_text(101, inner) if lane == "text" else _deep_args(101, inner)
    args, refusal = decode_arguments("web_search", over)
    assert args == {}
    _assert_retryable_refusal(refusal)


def test_a_dict_that_contains_itself_is_refused():
    args, refusal = decode_arguments("web_search", _self_containing())
    assert args == {}
    _assert_retryable_refusal(refusal)


def test_the_set_todos_wrap_is_bounded():
    """The coder wraps a bare list for `set_todos` into ``{"tasks": ...}``, one level deeper
    than what it decoded; 9999 levels made its doom-loop `json.dumps` raise outside any
    `try` and end the whole run. The coder now asks the decoder about the wrapped dict. The
    list is built in Python, so the case is the same on every interpreter version."""
    args, refusal = decode_arguments("set_todos", {"tasks": _deep_list(9998)})
    assert args == {}
    _assert_retryable_refusal(refusal, "set_todos")


def test_the_coder_asks_the_decoder_before_its_doom_loop_signature():
    """No test drives the coder's inner loop, so the order is read from the source: the
    decoder must judge the arguments the loop will use before `json.dumps` walks them."""
    import vaf.tools.coder as coder
    run = inspect.getsource(coder.CodingAgentTool.run)
    asked = run.index("decode_arguments(fn_name, fn_args if fn_args else fn_args_str)")
    signed = run.index('action_sig = f"{fn_name}:{json.dumps(fn_args, sort_keys=True)')
    assert asked < signed


# The coder's tool loop has no driver in the suite (it needs a streaming model, a TUI and a
# task manager at once), so its structure is pinned through the syntax tree: what a
# mutation would have to change to break the behaviour, these tests read.

def _coder_run_tree():
    """`CodingAgentTool.run` as a syntax tree, taken from the parsed module (its source holds
    column-0 text inside strings, so it cannot be dedented and parsed on its own)."""
    import ast

    import vaf.tools.coder as coder
    tree = ast.parse(inspect.getsource(coder))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CodingAgentTool")
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "run")


def _bodies(tree):
    """Every statement list in the tree, so a statement can be found with its neighbours."""
    import ast
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if isinstance(stmts, list) and stmts and isinstance(stmts[0], ast.stmt):
                yield stmts
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                yield handler.body


def _assigns(node, name: str) -> bool:
    import ast
    return isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == name for t in node.targets)


def test_the_coder_answers_a_refused_call_and_skips_it():
    """The refusal block between the decoder and the doom-loop signature: answered as a
    `role: tool` message to the call, then `continue`, with nothing that could short-circuit
    the condition; three identical refusals in a row end the run; any call that gets past
    resets the streak."""
    import ast
    tree = _coder_run_tree()
    asked = [n for n in ast.walk(tree) if _assigns(n, "_args_refusal")]
    signed = [n for n in ast.walk(tree) if _assigns(n, "action_sig")]
    assert len(asked) == 1 and len(signed) == 1
    assert ast.unparse(asked[0].value) == \
        "decode_arguments(fn_name, fn_args if fn_args else fn_args_str)[1]"
    blocks = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and "_args_refusal" in ast.unparse(n.test)
              and not ast.unparse(n.test).startswith("_repeated_refusal(")]
    assert len(blocks) == 1, [ast.unparse(b.test) for b in blocks]
    block = blocks[0]
    assert isinstance(block.test, ast.Name) and block.test.id == "_args_refusal", \
        ast.unparse(block.test)
    assert asked[0].lineno < block.lineno < signed[0].lineno

    answer = block.body[0]
    assert ast.unparse(answer).startswith("_history_at_dispatch.append("), ast.unparse(answer)
    message = answer.value.args[0]
    fields = {ast.literal_eval(k): v for k, v in zip(message.keys, message.values)}
    assert ast.literal_eval(fields["role"]) == "tool"
    assert ast.unparse(fields["tool_call_id"]) == "tc['id']"
    assert ast.unparse(fields["content"]) == "_args_refusal"
    assert isinstance(block.body[-1], ast.Continue)

    stops = [s for s in block.body if isinstance(s, ast.If) and ast.unparse(s.test)
             == "_repeated_refusal(loop, fn_name, fn_args_str, _args_refusal)"]
    assert len(stops) == 1
    assert _assigns(stops[0].body[0], "_loop_stop_report")
    assert isinstance(stops[0].body[-1], ast.Break)

    reset = "loop.refusal_streak = (None, 0)"
    for stmts in _bodies(tree):
        if block in stmts:
            after = stmts[stmts.index(block) + 1]
            assert ast.unparse(after) == reset, ast.unparse(after)
            break
    else:
        raise AssertionError("the refusal block has no statement after it")
    # A call that leaves the batch iteration BEFORE the refusal block was answered without
    # being refused, so it ends a run of refusals too (the identical-content shortcut).
    batch = _batch_loop(tree)
    early = [n for n in ast.walk(batch) if isinstance(n, ast.Continue)
             and n.lineno < block.lineno and _innermost_loop(batch, n) is batch]
    assert early, "no early continue found; the shortcut moved"
    for stmts in _bodies(batch):
        for i, stmt in enumerate(stmts):
            if stmt in early:
                assert i > 0 and ast.unparse(stmts[i - 1]) == reset, ast.unparse(stmts[i - 1])


def _batch_loop(tree):
    import ast
    loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)
             and ast.unparse(n.iter) == "tool_calls" and ast.unparse(n.target) == "tc"
             and any(_assigns(s, "_args_refusal") for s in ast.walk(n))]
    assert len(loops) == 1
    return loops[0]


def _innermost_loop(root, target):
    """The nearest For/While above ``target`` inside ``root`` (``root`` itself counts)."""
    import ast
    parents = {c: p for p in ast.walk(root) for c in ast.iter_child_nodes(p)}
    cur = target
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.For, ast.While)):
            return cur
    return None


def test_three_identical_refusals_in_a_row_end_the_coder_run():
    from vaf.tools.coder import AgenticLoop, _repeated_refusal
    loop = AgenticLoop()
    refusal = _DEEP.format("set_todos")
    raw = _list_nesting(9998)
    assert [_repeated_refusal(loop, "set_todos", raw, refusal) for _ in range(3)] \
        == [False, False, True]
    loop.refusal_streak = (None, 0)             # a call got past the refusal
    assert not _repeated_refusal(loop, "set_todos", raw, refusal)
    assert not _repeated_refusal(loop, "set_todos", raw, refusal)
    assert not _repeated_refusal(loop, "read_file", raw, refusal)   # another call breaks the row
    assert not _repeated_refusal(loop, "set_todos", raw, refusal)
    assert not _repeated_refusal(loop, "set_todos", raw, "Tool Error: something else")
    # The same refusal for different calls is not the same call: three list-shaped calls
    # all get "a JSON list, not an object", and none of them repeats.
    loop.refusal_streak = (None, 0)
    listed = _DEEP.format("set_todos").replace("nested deeper", "a list")
    assert [_repeated_refusal(loop, "set_todos", text, listed)
            for text in ('["a"]', "[1, 2, 3]", "[[]]")] == [False, False, False]
    # Arguments that did not arrive as text are keyed by tool and refusal alone.
    assert [_repeated_refusal(loop, "set_todos", {"tasks": 1}, listed) for _ in range(3)] \
        == [False, False, True]


def test_a_stop_in_the_batch_leaves_through_the_common_end():
    """The same error or the same refused call three times in a row used to `return` from
    inside the batch: no final commit, no summary, no open tasks. Each stop now records its
    report and leaves the batch with `break`, the main loop breaks right after the batch,
    and both final answers lead with the report (`_stopped_outcome`)."""
    import ast
    tree = _coder_run_tree()
    batch = _batch_loop(tree)
    # The one return allowed: CONTENT_ONLY `task_done` answers with the file itself, and that
    # mode never commits (`skip_template`). A nested helper's own return does not leave run.
    nested = {id(r) for f in ast.walk(batch) if isinstance(f, (ast.FunctionDef, ast.Lambda))
              for r in ast.walk(f) if isinstance(r, ast.Return)}
    returns = [ast.unparse(n) for n in ast.walk(batch)
               if isinstance(n, ast.Return) and id(n) not in nested]
    assert returns == ["return content"], returns
    records = []
    for stmts in _bodies(batch):
        for i, stmt in enumerate(stmts):
            if _assigns(stmt, "_loop_stop_report"):
                records.append(stmt)
                assert isinstance(stmts[i + 1], ast.Break), ast.unparse(stmts[i + 1])
                assert _innermost_loop(batch, stmts[i + 1]) is batch
    assert len(records) == 3, len(records)          # the refusal and both write-error stops
    for stmts in _bodies(tree):
        if batch in stmts:
            after = stmts[stmts.index(batch) + 1]
            assert ast.unparse(after) == "if _loop_stop_report:\n    break", ast.unparse(after)
            break
    else:
        raise AssertionError("the batch loop was not found in a statement list")
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.While) and batch in ast.walk(n))
    nested_fns = {id(r) for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.Lambda))
                  and f is not tree for r in ast.walk(f) if isinstance(r, ast.Return)}
    finals = [ast.unparse(n.value) for n in ast.walk(tree) if isinstance(n, ast.Return)
              and n.lineno > main.end_lineno and id(n) not in nested_fns]
    led = [f for f in finals if f.startswith("_stopped_outcome(_loop_stop_report, ")]
    assert len(led) == 2, finals                     # the summary and the no-files answer
    assert sorted(set(finals) - set(led)) == ["content"], finals
    # After the loop, the CONTENT_ONLY answer (a file's text) is not given for a stopped run.
    content_only = [ast.unparse(n.test) for n in ast.walk(tree) if isinstance(n, ast.If)
                    and n.lineno > main.end_lineno and "skip_template" in ast.unparse(n.test)
                    and any(isinstance(r, ast.Return) for r in ast.walk(n))]
    assert content_only == ["skip_template and (not _loop_stop_report)"], content_only


def test_a_stopped_outcome_leads_with_the_stop_and_still_reads_as_failed():
    from vaf.core.context import tool_result_is_error
    from vaf.tools.coder import _stopped_outcome
    from vaf.workflows.engine import _step_failure_reason

    summary = ("[VAF_CODING_AGENT_STATUS: PARTIAL]\n\n### ⚠️ Task Partially Complete\n\n"
               "**📁 Project Directory**: /home/user/demo\n**⚠️ Remaining tasks (1)**:\n- b")
    assert _stopped_outcome("", summary) == summary
    stop = ("### ❌ Repeated Refused Call\n\nThe model sent the same refused `set_todos` call "
            "3 times in a row:\n\nTool Error: x\n\nStopped to prevent an endless loop.")
    out = _stopped_outcome(stop, summary)
    assert out.startswith(stop + "\n\n### ⚠️ Task Partially Complete")
    assert "VAF_CODING_AGENT_STATUS" not in out
    assert "/home/user/demo" in out and "Remaining tasks" in out
    assert tool_result_is_error(out) and tool_result_is_error(out, content_carrying=True)
    assert _step_failure_reason(out).startswith(
        "Tool returned error message: Repeated Refused Call. The model sent the same")


def test_every_answer_of_a_coder_batch_goes_to_the_history_that_owns_it():
    """`set_todos` switches `history` to a new task context. Taken per call, the history a
    later call in the same batch answered into was the new one, where the call has no
    owning assistant message: the request builder drops such an answer, so the model never
    saw it. The reference is taken once, before the batch."""
    import ast
    tree = _coder_run_tree()
    batch = _batch_loop(tree)
    assert not [s for s in ast.walk(batch) if _assigns(s, "_history_at_dispatch")]
    for stmts in _bodies(tree):
        if batch in stmts:
            before = stmts[stmts.index(batch) - 1]
            assert ast.unparse(before) == "_history_at_dispatch = history", ast.unparse(before)
            break
    else:
        raise AssertionError("the batch loop was not found in a statement list")

    # Every answer, the early refusals included (a share path, the coder allowlist, a write
    # before the plan), goes to the batch's history, never to `history`, which set_todos may
    # have switched to a fresh task context by then.
    writes = [n for n in ast.walk(batch) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and n.func.attr in ("append", "extend", "insert")
              and isinstance(n.func.value, ast.Name) and "history" in n.func.value.id]
    answers = [n for n in writes if n.args and isinstance(n.args[0], ast.Dict)
               and any(isinstance(k, ast.Constant) and k.value == "role" for k in n.args[0].keys)]
    assert len(answers) >= 15, len(answers)
    stray = [f"line {n.lineno}: {ast.unparse(n.func)}" for n in writes
             if n.func.value.id != "_history_at_dispatch"]
    assert not stray, stray

    # A failed context switch rolls back to a COPY of the history, so the batch follows it.
    switch = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "switch_to_task_context")
    assert any(isinstance(n, ast.Nonlocal) and "_history_at_dispatch" in n.names
               for n in ast.walk(switch))
    rollback = next(n for n in ast.walk(switch) if isinstance(n, ast.Try)).handlers[0].body
    lines = [ast.unparse(s) for s in rollback]
    assert "sync_legacy_vars()" in lines
    assert lines.index("_history_at_dispatch = history") > lines.index("sync_legacy_vars()")


def _catches_recursion(handler_type) -> bool:
    import ast
    if handler_type is None:
        return True
    names = handler_type.elts if isinstance(handler_type, ast.Tuple) else [handler_type]
    return any(ast.unparse(n) in ("RecursionError", "RuntimeError", "Exception",
                                  "BaseException") for n in names)


def test_every_coder_json_decode_survives_text_too_deep_to_decode():
    """`json.loads` raises RecursionError, not a ValueError, on text nested deeper than the
    interpreter can decode. The display loop that shows a tool call caught only
    `JSONDecodeError`, so the error reached the stream handler, which ends the whole coder
    run. Every decode in the module must be inside a `try` whose own handlers catch it.

    Named exception: the SSE frame (`chunk = json.loads(data_str)`). The provider builds that
    frame, and the model's arguments are a string field inside it, so model output cannot
    nest it."""
    import ast

    import vaf.tools.coder as coder
    tree = ast.parse(inspect.getsource(coder))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    checked, missing = 0, []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "loads" and ast.unparse(node.func.value) in ("json", "_json")):
            continue
        if _assigns(parents.get(node), "chunk"):
            continue
        cur, owner = node, None
        while cur in parents:
            parent = parents[cur]
            if isinstance(parent, ast.Try) and cur in parent.body:
                owner = parent
                break
            cur = parent
        checked += 1
        if owner is None or not any(_catches_recursion(h.type) for h in owner.handlers):
            missing.append(f"line {node.lineno}: {ast.unparse(node)}")
    assert checked >= 8 and not missing, missing


def _funnel(monkeypatch):
    monkeypatch.setattr("vaf.core.trust.get_tool_policy", lambda *a, **k: "always")
    spy = _Spy()
    events: list = []
    return spy, events, ToolCaller({spy.name: spy}, on_event=events.append)


_FUNNEL_CASES = (
    [(inner, depth, lane) for inner in ("list", "dict") for depth in (101, 996, 3000, 9000)
     for lane in ("text", "dict")]
    + [(inner, depth, "dict") for inner in ("list", "dict") for depth in (20_000, 100_000)]
)


@pytest.mark.parametrize("inner,depth,lane", _FUNNEL_CASES,
                         ids=[f"{i}-{d}-{lane}" for i, d, lane in _FUNNEL_CASES])
def test_the_funnel_refuses_deep_decoded_arguments(monkeypatch, inner, depth, lane):
    """`ToolCaller.call` with text or with a dict: no exception, nothing dispatched, no event.
    From about 996 levels on, this raised `RecursionError` out of `ToolRequest`."""
    spy, events, caller = _funnel(monkeypatch)
    raw = _deep_text(depth, inner) if lane == "text" else _deep_args(depth, inner)
    outcome = caller.call(spy.name, raw)
    assert outcome.dispatched is False
    _assert_retryable_refusal(outcome.result, spy.name)
    assert spy.calls == [] and events == []


def test_the_funnel_refuses_a_dict_that_contains_itself(monkeypatch):
    spy, events, caller = _funnel(monkeypatch)
    outcome = caller.call(spy.name, _self_containing())
    assert outcome.dispatched is False
    _assert_retryable_refusal(outcome.result, spy.name)
    assert spy.calls == [] and events == []


class _ArraySpy(_Spy):
    """`tasks` is an array, so the schema repair parses a string holding a JSON array."""
    name = "array_spy"
    parameters = {"type": "object", "properties": {"tasks": {"type": "array"}}}


def test_the_funnel_measures_what_the_schema_repair_made(monkeypatch):
    """A call one level deep whose string field holds a JSON array 500 deep: the repair
    parses it into a list, and the tool used to receive that list (measured through
    `edit_file.edits` and `update_working_memory.tasks`, up to 9999 deep). Depth 500 decodes
    on every supported interpreter, so the case is the same everywhere."""
    monkeypatch.setattr("vaf.core.trust.get_tool_policy", lambda *a, **k: "always")
    spy = _ArraySpy()
    events: list = []
    caller = ToolCaller({spy.name: spy}, on_event=events.append)
    outcome = caller.call(spy.name, {"tasks": _list_nesting(500)})
    _assert_retryable_refusal(outcome.result, spy.name)
    assert spy.calls == []
    assert outcome.dispatched is True       # after tool_start, like a schema error
    assert caller.execute(spy.name, {"tasks": '["a", ["b"]]'}) == "ran"
    assert spy.calls == [{"tasks": ["a", ["b"]]}]


def test_the_funnel_refuses_a_deep_list_of_pairs(monkeypatch):
    """The sandbox tool bridge forwards `args` from model-written code as they are, so a
    list reaches `ToolCaller.call`. 3000 deep it raised `RecursionError`."""
    spy, events, caller = _funnel(monkeypatch)
    for raw in ([["plan", _deep_list(3000)]], [("other", _deep_list(100))]):
        outcome = caller.call(spy.name, raw)
        assert outcome.dispatched is False
        _assert_retryable_refusal(outcome.result, spy.name)
    assert spy.calls == [] and events == []
    # A list of pairs runs as the dict it makes, up to the limit of that dict.
    assert caller.execute(spy.name, [("plan", "x")]) == "ran"
    assert caller.execute(spy.name, [("other", _deep_list(99))]) == "ran"
    assert spy.calls[0] == {"plan": "x"} and len(spy.calls) == 2


def _funnel_outcome(monkeypatch, raw) -> tuple:
    spy, events, caller = _funnel(monkeypatch)
    outcome = caller.call(spy.name, raw)
    return outcome.result, outcome.dispatched, spy.calls, [e.get("type") for e in events]


@pytest.mark.parametrize("lane", ["dict", "text"])
def test_the_funnel_at_the_limit_behaves_as_without_it(monkeypatch, lane):
    """Depth 100 passes, and the call goes on exactly as it did before the check existed:
    here to the schema refusal, because `plan` is a string parameter."""
    import vaf.core.tool_dispatch as tool_dispatch
    raw = (lambda: _deep_text(100, "list")) if lane == "text" else (lambda: _deep_args(100, "list"))
    with_limit = _funnel_outcome(monkeypatch, raw())
    monkeypatch.setattr(tool_dispatch, "MAX_ARGUMENT_DEPTH", 10 ** 9)
    without = _funnel_outcome(monkeypatch, raw())
    assert with_limit == without
    assert with_limit[0].startswith("Tool Error:") and "nested deeper" not in with_limit[0]
    assert with_limit[2] == []


def test_the_librarian_survives_a_deep_decoded_call():
    """The librarian hands the model's text to the funnel. A value about 1000 levels deep
    raised out of `caller.execute`, and the loop's `except Exception` ended the whole run
    with "Error: maximum recursion depth exceeded"."""
    from unittest.mock import patch

    from test_librarian_shared_dispatch import _API_CONFIG, SCOPE, _Recorder
    from vaf.tools.librarian import LibrarianTool

    tool = _Recorder("read_file")
    chunks = [json.dumps({"tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": tool.name, "arguments": '{"path": ' + _list_nesting(3000) + "}"}},
    ]})]

    class _Backend:
        def __init__(self, *a, **k):
            self._first = True

        def chat_completion(self, **kwargs):
            if self._first:
                self._first = False
                yield from chunks
            else:
                yield "done"

    librarian = LibrarianTool()
    librarian.tools = {tool.name: tool}
    with patch("vaf.core.api_backend.APIBackendManager", _Backend), \
         patch("vaf.core.config.Config.load", classmethod(lambda cls: _API_CONFIG)):
        out = librarian._execute_with_llm(
            "do the thing", caller=librarian._make_caller(user_scope_id=SCOPE, user_role="user"))

    assert "maximum recursion depth" not in out, out[:300]
    assert _DEEP.format("read_file") in out, out[:300]
    assert tool.seen is None, "the sub-tool ran"


# ── cost: linear in containers, constant for deep values, nodes not paths when shared ─

def _best_of_three(raw) -> tuple:
    best, got = float("inf"), None
    for _ in range(3):
        start = time.perf_counter()
        got = decode_arguments("web_search", raw)
        best = min(best, time.perf_counter() - start)
    return best, got


def test_the_depth_check_costs_little():
    """Bounds with CI headroom; measured locally in the build report."""
    seconds, (args, refusal) = _best_of_three({"plan": _deep_dict(100_000)})
    assert refusal and args == {} and seconds < 0.05, seconds

    shared = {"plan": _shared_lists(22)}                 # depth 24, 2**22 paths
    seconds, (args, refusal) = _best_of_three(shared)
    assert refusal is None and args is shared and seconds < 0.25, seconds

    wide = '{"plan": [' + ",".join(['["abcd"]'] * 150_000) + "]}"
    assert len(wide) > 1_350_000
    seconds, (args, refusal) = _best_of_three(wide)
    assert refusal is None and len(args["plan"]) == 150_000 and seconds < 2.0, seconds


# ── seeded fuzz over the funnel: never raises, refuses over 100, unchanged up to 100 ─

_TEXT_LEAVES = ["", 1, "x", None, True]
_PY_LEAVES = _TEXT_LEAVES + [pathlib.Path("/p"), uuid.UUID(int=7)]


def _spine_depth(rng: random.Random) -> int:
    pick = rng.random()
    if pick < 0.3:
        return max(1, rng.randint(0, 20))
    if pick < 0.6:
        return rng.randint(95, 105)
    if pick < 0.7:
        return rng.randint(990, 1000)
    return int(10 ** rng.uniform(2, 4.30103))         # 100 .. 20,000, log-uniform


def _tree(rng: random.Random, depth: int, python: bool):
    """A value exactly ``depth`` container levels deep, the arguments dict on top, plus its
    JSON text on the text lane. Both are built bottom-up in one pass: the text as a list of
    prefixes and suffixes around the spine, so a 20,000-level value costs linear time."""
    leaves = _PY_LEAVES if python else _TEXT_LEAVES
    kinds = ("dict", "list", "tuple") if python else ("dict", "list")
    leaf = rng.choice(leaves)
    node = leaf
    heads: list = []
    tails: list = []
    for level in range(depth - 1):
        kind = rng.choice(kinds)
        siblings = [rng.choice(leaves) for _ in range(rng.randint(0, 4))]
        if level and siblings and rng.random() < 0.2:
            siblings[0] = rng.choice([{}, []])      # shallow, never deeper than the spine
        at = rng.randint(0, len(siblings))
        before, after = siblings[:at], siblings[at:]
        if kind == "dict":
            keys = [f"k{i}" for i in range(len(siblings) + 1)]
            node = dict(zip(keys, before + [node] + after))
            if not python:
                head = ", ".join(f'"{k}": {json.dumps(v)}' for k, v in zip(keys, before))
                tail = ", ".join(f'"{k}": {json.dumps(v)}' for k, v in zip(keys[at + 1:], after))
                heads.append("{" + head + (", " if head else "") + f'"{keys[at]}": ')
                tails.append((", " if tail else "") + tail + "}")
        else:
            items = before + [node] + after
            node = items if kind == "list" else tuple(items)
            if not python:
                head = ", ".join(json.dumps(v) for v in before)
                tail = ", ".join(json.dumps(v) for v in after)
                heads.append("[" + head + (", " if head else ""))
                tails.append((", " if tail else "") + tail + "]")
    key = rng.choice(["plan", "other"])     # `other` is outside the schema, so the spy runs
    value = {key: node}
    text = None
    if not python:
        text = (f'{{"{key}": ' + "".join(reversed(heads)) + json.dumps(leaf)
                + "".join(tails) + "}")
    return value, text


def _depth_draw(rng: random.Random):
    """(raw, true depth, the plain value for the depth oracle, a shared value to time with
    its depth, or None)."""
    pick = rng.random()
    if pick < 0.5:
        depth = _spine_depth(rng)
        value, text = _tree(rng, depth, python=False)
        return text, depth, value, None
    if pick < 0.55:
        # A shared DAG: the depth oracle times the big one; the funnel gets either a small
        # DAG at or under the limit (the copy walks after the check follow paths, which is
        # unchanged behaviour and not this check's to bound), or the big one pushed past it.
        levels = rng.randint(14, 22)
        big = _shared_lists(levels)
        if rng.random() < 0.5:
            small = rng.randint(1, 10)
            return {"plan": _shared_lists(small)}, small + 2, None, (big, levels + 1)
        wrap = rng.randint(100 - levels - 1, 100 - levels + 20)
        value = big
        for _ in range(wrap):
            value = [value]
        return {"plan": value}, 1 + wrap + levels + 1, None, (big, levels + 1)
    if pick < 0.57:
        loop = _self_containing()
        return (loop if rng.random() < 0.5 else {"plan": [loop]}), float("inf"), None, None
    depth = _spine_depth(rng)
    value, _text = _tree(rng, depth, python=True)
    return value, depth, value, None


def _depth_fuzz(monkeypatch, draws: int, seed: int = 0xDE97A) -> dict:
    import copy

    import vaf.core.tool_dispatch as tool_dispatch
    from vaf.core.nesting import nesting_depth

    rng = random.Random(seed)
    stats = {"draws": 0, "refused": 0, "passed": 0, "ran": 0, "dags": 0, "cycles": 0}
    for i in range(draws):
        raw, depth, plain, dag = _depth_draw(rng)
        lane = "text" if isinstance(raw, str) else "dict"
        ctx = f"draw {i}, {lane} lane, depth {depth}"
        if depth == float("inf"):
            stats["cycles"] += 1
        if dag is not None:
            shared, shared_depth = dag
            stats["dags"] += 1
            start = time.perf_counter()
            got = nesting_depth(shared, 100)
            took = time.perf_counter() - start
            assert got == shared_depth, ctx
            assert took < 0.5, f"{ctx}: {took:.2f} s for a shared value: paths, not nodes"
        if plain is not None:
            assert nesting_depth(plain, 100) == min(depth, 101), ctx
        if lane == "dict" and depth != float("inf"):
            assert nesting_depth(raw, 100) == min(depth, 101), ctx
        try:
            if depth > 100:
                got = _funnel_outcome(monkeypatch, raw)
            else:
                monkeypatch.setattr(tool_dispatch, "MAX_ARGUMENT_DEPTH", 100)
                got = _funnel_outcome(monkeypatch, copy.deepcopy(raw))
                monkeypatch.setattr(tool_dispatch, "MAX_ARGUMENT_DEPTH", 10 ** 9)
                unchecked = _funnel_outcome(monkeypatch, copy.deepcopy(raw))
                monkeypatch.setattr(tool_dispatch, "MAX_ARGUMENT_DEPTH", 100)
        except BaseException as exc:  # noqa: BLE001 - the oracle is "never raises"
            raise AssertionError(f"{ctx} raised {type(exc).__name__}: {exc}") from exc
        result, dispatched, calls, events = got
        stats["draws"] += 1
        if depth > 100:
            assert result.startswith(_DEEP.format("spy_tool")), f"{ctx}: {result[:200]}"
            assert dispatched is False and calls == [] and events == [], ctx
            stats["refused"] += 1
        else:
            assert got == unchecked, ctx
            stats["passed"] += 1
            stats["ran"] += bool(calls)
    return stats


def test_argument_depth_fuzz(monkeypatch):
    """300 seeded draws over both lanes of `ToolCaller.call` (measured in the build report);
    `VAF_DEPTH_FUZZ_DRAWS` runs more outside the suite. Depths cluster at the limit, around
    the first recursive walk that broke (990-1000) and log-uniform up to 20,000; the dict
    lane adds tuples, Path and UUID leaves, shared DAGs and cycles."""
    stats = _depth_fuzz(monkeypatch, int(os.environ.get("VAF_DEPTH_FUZZ_DRAWS", "300")))
    print(f"depth fuzz, seed 0xDE97A: {stats}")
    assert stats["refused"] > 0 and stats["passed"] > 0 and stats["ran"] > 0, stats
    assert stats["dags"] > 0 and stats["cycles"] > 0, stats
