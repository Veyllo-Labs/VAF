# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A malformed tool-call list a model wrote as text never ends the turn.

`extract_wire_json_tool_calls` recovers a bare ``{"tool_calls": [...]}`` object a model leaked
into its reply. Its item loop assumed every item was an object, so a reply holding
``{"tool_calls": [1]}`` raised `AttributeError` out of `Agent.chat_step`; a number as the name
was dispatched under its repr where no name list is passed (the coder), and arguments ``0`` or
``false`` collapsed to ``"{}"`` and ran the tool with nothing. A malformed item now refuses the
whole leak like a foreign name does, and the text stays in the reply as written; arguments that
are not an object are passed on as JSON text, so `decode_arguments` answers them by type.

Mutations that turn these tests red (each run and restored):
- drop the ``isinstance(c, dict)`` guard: the int/str/list chat_step shapes raise, the refuse
  table and the fuzz raise;
- drop the ``isinstance(fn, dict)`` guard: the function chat_step shapes raise; reverting it to
  ``c.get("function") or {}`` keeps the falsy refuse rows green and the truthy ones red;
- revert the name to ``str(fn.get("name") or "").strip()``: the non-string name test and the
  fuzz oracle;
- revert the arguments to ``str(args or "{}")``: the JSON-text test (``0`` gives ``"{}"``,
  ``true`` gives ``"True"``) and the chat_step tool-message test ("missing required field");
- revert the id to ``str(c.get("id") or "").strip()``: the synthetic id test;
- make the span scan quadratic: the 128 KB cost test.

`VAF_RECOVERY_FUZZ_DRAWS` runs a larger seeded draw count outside the suite.
"""

import json
import os
import random
import time

import pytest
import requests

from test_local_engine_wiring import _local_agent, _Resp, _Stream
from vaf.core.tool_call_recovery import (_wire_json_span, extract_wire_json_tool_calls,
                                         extract_xml_tool_call, extract_xml_tool_calls,
                                         strip_tool_call_markup)
from vaf.core.tool_dispatch import decode_arguments

NAMES = ["web_search"]
PREFIX = "Ich schaue nach. "
VALID = {"id": "call_abc", "type": "function",
         "function": {"name": "web_search", "arguments": '{"query": "wetter"}'}}


def _leak(items) -> str:
    return PREFIX + json.dumps({"tool_calls": items})


# ── through the real chat_step ──────────────────────────────────────────────────────

def _chat(monkeypatch, tmp_path, reply):
    """One real chat turn: the model writes `reply` first and "Fertig." on every later request.
    Returns the agent, the streamed text and the arguments web_search actually ran with."""
    agent = _local_agent(monkeypatch, tmp_path)
    assert "web_search" in agent.tools, "the stub agent has no web_search to dispatch"
    ran = []
    monkeypatch.setattr(agent.tools["web_search"], "run",
                        lambda **kw: ran.append(kw) or "Ergebnis.", raising=False)
    requests_seen, chunks = [], []

    def post(url, json=None, **kw):
        if isinstance(json, dict) and json.get("stream") and "messages" in json:
            requests_seen.append(json)
            return _Stream([{"content": reply if len(requests_seen) == 1 else "Fertig."}])
        return _Resp(500, {})
    monkeypatch.setattr(requests, "post", post)
    agent.chat_step(user_input="Hallo", raw_user_input="Hallo", stream_callback=chunks.append)
    assert requests_seen, "the turn never reached the local server"
    return agent, "".join(c for c in chunks if isinstance(c, str)), ran


_CRASH_SHAPES = {
    "int": [1],
    "str": ["x"],
    "list": [[1]],
    "function_str": [{"function": "web_search"}],
    "function_list": [{"function": [1]}],
    "valid_then_int": [VALID, 1],
}


@pytest.mark.parametrize("items", list(_CRASH_SHAPES.values()), ids=list(_CRASH_SHAPES))
def test_malformed_item_does_not_crash_the_turn(monkeypatch, tmp_path, items):
    reply = _leak(items)
    leaked_json = reply[len(PREFIX):]
    agent, shown, ran = _chat(monkeypatch, tmp_path, reply)
    assert ran == [], "a tool ran for a refused leak"
    assert not [m for m in agent.history if m.get("role") == "tool"], "a tool message was added"
    assert not [m for m in agent.history if m.get("role") == "assistant" and m.get("tool_calls")], \
        "an assistant tool_calls message was added for a refused leak"
    assert leaked_json in shown, "the streamed reply lost the text the model wrote"
    last = [m for m in agent.history if m.get("role") == "assistant"][-1]
    assert leaked_json in str(last.get("content")), "the stored reply lost the leaked JSON"


def test_non_object_arguments_answered_in_their_own_tool_message(monkeypatch, tmp_path):
    reply = PREFIX + json.dumps({"tool_calls": [
        {"function": {"name": "web_search", "arguments": 0}}]})
    agent, _shown, ran = _chat(monkeypatch, tmp_path, reply)
    assert ran == [], "the tool ran although its arguments were not an object"
    hist = agent.history
    asked = [i for i, m in enumerate(hist) if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(asked) == 1, hist
    call = hist[asked[0]]["tool_calls"]
    assert len(call) == 1
    answers = [m for m in hist[asked[0] + 1:] if m.get("role") == "tool"]
    assert len(answers) == 1, "not exactly one tool message for the one call"
    assert hist[asked[0] + 1] is answers[0], "the tool message does not follow its call"
    assert answers[0]["tool_call_id"] == call[0]["id"]
    assert answers[0]["content"].startswith(
        "Tool Error: the arguments of this 'web_search' call are a JSON int"), answers[0]["content"]


# ── the function itself ─────────────────────────────────────────────────────────────

_REFUSE_ROWS = {
    # the item is not an object, falsy or not
    "item_int": [1], "item_zero": [0], "item_str": ["x"], "item_empty_str": [""],
    "item_list": [[1]], "item_empty_list": [[]], "item_null": [None], "item_true": [True],
    "item_false": [False], "item_float": [2.5],
    # the function is missing or not an object
    "function_missing": [{"id": "a"}], "function_null": [{"function": None}],
    "function_str": [{"function": "web_search"}], "function_empty_str": [{"function": ""}],
    "function_list": [{"function": [1]}], "function_empty_list": [{"function": []}],
    "function_int": [{"function": 7}], "function_zero": [{"function": 0}],
    "function_true": [{"function": True}],
    # the name is not a string, or blank
    "name_missing": [{"function": {"arguments": "{}"}}],
    "name_null": [{"function": {"name": None}}], "name_empty": [{"function": {"name": ""}}],
    "name_blank": [{"function": {"name": "   "}}], "name_int": [{"function": {"name": 7}}],
    "name_dict": [{"function": {"name": {"a": 1}}}], "name_list": [{"function": {"name": ["x"]}}],
    "name_true": [{"function": {"name": True}}],
    # a malformed item anywhere refuses the valid ones around it
    "valid_then_bad": [VALID, 1], "bad_then_valid": [{"function": "x"}, VALID],
}


@pytest.mark.parametrize("names", [None, NAMES], ids=["no_names", "names"])
@pytest.mark.parametrize("items", list(_REFUSE_ROWS.values()), ids=list(_REFUSE_ROWS))
def test_every_refuse_row_returns_empty(items, names):
    assert extract_wire_json_tool_calls(_leak(items), names) == []


@pytest.mark.parametrize("text", [
    PREFIX + '{"tool_calls": {"function": {"name": "web_search"}}}',
    PREFIX + '{"tool_calls": []}', PREFIX + '{"tool_calls": "x"}', PREFIX + '{"tool_calls": null}',
    PREFIX + '{"tool_calls": [1, }', PREFIX + '{"tool_calls": [', "", None,
], ids=["not_list", "empty", "str", "null", "invalid", "truncated", "empty_text", "none"])
@pytest.mark.parametrize("names", [None, NAMES], ids=["no_names", "names"])
def test_unusable_wrapper_returns_empty(text, names):
    assert extract_wire_json_tool_calls(text, names) == []


def test_foreign_name_still_refuses_the_whole_leak():
    other = {"function": {"name": "rm_everything", "arguments": "{}"}}
    assert extract_wire_json_tool_calls(_leak([VALID, other]), NAMES) == []


@pytest.mark.parametrize("name", [7, {"a": 1}, ["x"], True], ids=["int", "dict", "list", "true"])
def test_non_string_name_refused_without_names(name):
    """No name list is how the coder calls it: a number used to run under the name "7"."""
    assert extract_wire_json_tool_calls(_leak([{"function": {"name": name}}]), None) == []


def test_string_name_is_stripped():
    out = extract_wire_json_tool_calls(_leak([{"function": {"name": " web_search "}}]), NAMES)
    assert [c["function"]["name"] for c in out] == ["web_search"]


@pytest.mark.parametrize("value, text", [
    (0, "0"), (False, "false"), (True, "true"), ([1], "[1]"), (2.5, "2.5"), ([], "[]"),
    (-1, "-1"), ({"query": "x"}, '{"query": "x"}'),
    ('{"query": "x"}', '{"query": "x"}'), ("", "{}"), (None, "{}"), ("oops", "oops"),
], ids=["zero", "false", "true", "list", "float", "empty_list", "negative", "dict",
        "json_str", "empty_str", "null", "bad_str"])
def test_arguments_become_json_text(value, text):
    out = extract_wire_json_tool_calls(
        _leak([{"function": {"name": "web_search", "arguments": value}}]), NAMES)
    assert [c["function"]["arguments"] for c in out] == [text]


def test_missing_arguments_are_an_empty_object():
    out = extract_wire_json_tool_calls(_leak([{"function": {"name": "web_search"}}]), NAMES)
    assert [c["function"]["arguments"] for c in out] == ["{}"]


@pytest.mark.parametrize("leaked", [7, {"a": 1}, ["x"], True, "", "   ", None],
                         ids=["int", "dict", "list", "true", "empty", "blank", "null"])
def test_non_string_id_gets_a_synthetic_one(leaked):
    item = {"id": leaked, "function": {"name": "web_search", "arguments": "{}"}}
    out = extract_wire_json_tool_calls(_leak([item]), NAMES)
    assert len(out) == 1
    assert out[0]["id"].startswith("call_synth_"), out[0]["id"]


def test_string_id_is_preserved_stripped():
    item = {"id": "  call_42 ", "type": "nonsense", "function": {"name": "web_search"}}
    out = extract_wire_json_tool_calls(_leak([item]), NAMES)
    assert out == [{"id": "call_42", "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"}}]


# ── seeded fuzz with a reference model ──────────────────────────────────────────────

_ATOMS = ["0", "1", "-1", "2.5", "true", "false", "null", '""', '"x"', '"web_search"', '" "',
          '"{}"', '"[1]"', '"{\\"q\\": 1}"', "Infinity"]
_KEYS = ["function", "name", "arguments", "id", "type", "tool_calls", "x"]
_PREFIXES = ["", PREFIX, "{ ", '"', '<invoke name="x">', "}"]


def _value(rng: random.Random, depth: int = 0) -> str:
    roll = rng.random()
    if depth < 5 and roll < 0.15:
        return "[" + ", ".join(_value(rng, depth + 1) for _ in range(rng.randint(0, 3))) + "]"
    if depth < 5 and roll < 0.3:
        keys = rng.sample(_KEYS, rng.randint(0, 3))
        return "{" + ", ".join(f'"{k}": {_value(rng, depth + 1)}' for k in keys) + "}"
    return rng.choice(_ATOMS)


def _name(rng: random.Random) -> str:
    return rng.choice(['"web_search"', '" web_search "', '"web_search"', '"other"', '""',
                       "7", "true", '{"a": 1}', '["x"]', "null", _value(rng, 3)])


def _item(rng: random.Random) -> str:
    shape = rng.random()
    if shape < 0.15:
        return _value(rng, 1)
    if shape < 0.3:
        return '{"function": ' + _value(rng, 2) + "}"
    fn = []
    if rng.random() < 0.9:
        fn.append('"name": ' + _name(rng))
    if rng.random() < 0.8:
        fn.append('"arguments": ' + _value(rng, 2))
    parts = ['"function": {' + ", ".join(fn) + "}"]
    if rng.random() < 0.5:
        parts.append('"id": ' + rng.choice(['"call_1"', '" call_2 "', '""', "7", "null",
                                            _value(rng, 3)]))
    if rng.random() < 0.3:
        parts.append('"type": ' + _value(rng, 3))
    rng.shuffle(parts)
    return "{" + ", ".join(parts) + "}"


def _draw(rng: random.Random) -> str:
    if rng.random() < 0.1:
        calls = _value(rng, 1)
    else:
        calls = "[" + ", ".join(_item(rng) for _ in range(rng.randint(1, 4))) + "]"
    text = '{"tool_calls": ' + calls + "}"
    if rng.random() < 0.1:
        text = text[:rng.randint(0, len(text))]
    if rng.random() < 0.1 and "}" in text:
        cut = rng.choice([i for i, ch in enumerate(text) if ch == "}"])
        text = text[:cut] + text[cut + 1:]
    if rng.random() < 0.1:
        text = "{" + text
    return rng.choice(_PREFIXES) + text


def _reference(text, names):
    """The Design table, applied to the object the scanner hands to json.loads: None for a
    refusal, else (name, leaked arguments, expected id or None for a synthetic one) per call."""
    span = _wire_json_span(text)
    if not span:
        return None
    try:
        obj = json.loads(text[span[0]:span[1]])
    except Exception:
        return None
    calls = obj.get("tool_calls") if isinstance(obj, dict) else None
    if not isinstance(calls, list) or not calls:
        return None
    ref = []
    for c in calls:
        if not isinstance(c, dict) or not isinstance(c.get("function"), dict):
            return None
        name = c["function"].get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        if names is not None and name.strip() not in names:
            return None
        cid = c.get("id")
        ref.append((name.strip(), c["function"].get("arguments"),
                    cid.strip() if isinstance(cid, str) and cid.strip() else None))
    return ref


def _check_calls(calls) -> None:
    assert isinstance(calls, list)
    for c in calls:
        assert isinstance(c, dict) and c["type"] == "function"
        assert isinstance(c["id"], str) and c["id"]
        assert isinstance(c["function"]["name"], str) and c["function"]["name"]
        assert isinstance(c["function"]["arguments"], str)


def _fuzz(draws: int, seed: int = 0x5EED) -> int:
    """Runs `draws` seeded inputs through every recovery function; returns the accepted count."""
    rng = random.Random(seed)
    accepted = 0
    for n in range(draws):
        text = _draw(rng)
        ctx = f"draw {n}: {text!r}"
        for names in (None, NAMES):
            _check_calls(extract_xml_tool_calls(text, names))
            one = extract_xml_tool_call(text, names)
            if one is not None:
                _check_calls([one])
            out = extract_wire_json_tool_calls(text, names)
            _check_calls(out)
            ref = _reference(text, names)
            if ref is None:
                assert out == [], ctx
                continue
            accepted += 1
            assert len(out) == len(ref), ctx
            for got, (name, leaked_args, cid) in zip(out, ref):
                assert got["function"]["name"] == name, ctx
                if cid is None:
                    assert got["id"].startswith("call_synth_"), ctx
                else:
                    assert got["id"] == cid, ctx
                assert decode_arguments(name, got["function"]["arguments"]) == \
                    decode_arguments(name, leaked_args), ctx
        assert isinstance(strip_tool_call_markup(text), str), ctx
    return accepted


def test_recovery_fuzz_never_raises_and_types_hold():
    """3,000 seeded draws (well under a second); `VAF_RECOVERY_FUZZ_DRAWS` runs more outside
    the suite."""
    assert _fuzz(int(os.environ.get("VAF_RECOVERY_FUZZ_DRAWS", "3000"))) > 0, \
        "no draw was ever accepted, so the reference rules were never compared"


# ── cost at 128 KB ──────────────────────────────────────────────────────────────────

_SIZE = 128 * 1024


def _shapes():
    item = json.dumps(VALID)
    count = _SIZE // (len(item) + 2)
    yield "valid_items", '{"tool_calls": [' + ", ".join([item] * count) + "]}"
    yield "int_items", '{"tool_calls": [' + ",".join(["1"] * (_SIZE // 2)) + "]}"
    yield "unbalanced", '{"tool_calls": ' + "{" * _SIZE
    deep = _SIZE // 2
    yield "list_nesting", '{"tool_calls": ' + "[" * deep + "]" * deep + "}"
    deep = _SIZE // 7
    yield "object_nesting", '{"tool_calls": [' + '{"a": ' * deep + "1" + "}" * deep + "]}"
    yield "escaped_string", ('{"tool_calls": [{"function": {"name": "web_search", "arguments": "'
                             + '\\"' * (_SIZE // 2) + '"}}]}')


@pytest.mark.parametrize("label, text", list(_shapes()), ids=[s[0] for s in _shapes()])
def test_wire_lane_cost_at_128k(label, text):
    assert len(text) >= _SIZE - 128
    for fn in (lambda: extract_wire_json_tool_calls(text, NAMES), lambda: strip_tool_call_markup(text)):
        start = time.perf_counter()
        fn()
        took = time.perf_counter() - start
        assert took < 2.0, f"{label}: {took:.2f} s for 128 KB, the scan is no longer linear"
