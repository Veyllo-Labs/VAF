# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A workflow step's JSON input is decoded like a tool call's arguments, and odd text fails
one step, never the whole run.

A step without `args` whose resolved input is a JSON object takes that object as the call's
arguments. The input usually arrives through a `{placeholder}` that holds a fetched page or an
API answer, so it is foreign text. The engine used to decode it with a bare `json.loads` that
caught only `JSONDecodeError`: an object about 10,000 levels deep raised `RecursionError`, and
an integer past Python's 4300-digit limit raised `ValueError`, out of
`WorkflowEngine.execute`. The run ended with no result, the later steps never ran, and
`optional`/`on_failure` were never asked. Now the engine uses the funnel's decoder
(`tool_dispatch.decode_arguments`): an object deeper than `MAX_ARGUMENT_DEPTH` fails the step
with the funnel's depth refusal, and text that is not valid JSON is the step's single argument.

A `variables` string is decoded the same way, and every reader of `variables` sees the
coerced mapping (a string used to fail every step at the variable anchoring).
"""
import contextlib
import os
import random
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

import vaf.workflows.engine as engine_module
from vaf.core.tool_dispatch import MAX_ARGUMENT_DEPTH, too_deep_refusal
from vaf.workflows.engine import (StepArgsRefused, StepStatus, WorkflowEngine, WorkflowStep,
                                  _infer_step_args, build_step_args)

SAME = lambda text: text  # noqa: E731


def deep_object(depth: int, inner: str = "{}") -> str:
    """A JSON object `depth` levels deep, built without recursion ({} is one level)."""
    return '{"a":' * (depth - 1) + inner + "}" * (depth - 1)


def deep_list_field(depth: int) -> str:
    """An object whose field `a` is a list `depth` levels deep."""
    return '{"a":' + "[" * depth + "]" * depth + "}"


def long_int(digits: int) -> str:
    return "7" * digits


# Python 3.10.7 and later have an integer digit limit; before it, a long integer decodes.
_NEEDS_DIGIT_LIMIT = pytest.mark.skipif(not hasattr(sys, "set_int_max_str_digits"),
                                        reason="this Python has no integer digit limit")


@pytest.fixture
def int_digit_limit():
    with _digit_limit():
        yield


def test_the_digit_limit_fixture_works_on_a_python_without_the_limit(monkeypatch):
    monkeypatch.delattr(sys, "get_int_max_str_digits", raising=False)
    monkeypatch.delattr(sys, "set_int_max_str_digits", raising=False)
    with _digit_limit():
        pass


@contextlib.contextmanager
def _digit_limit():
    """Python's integer digit limit at its default, 4300, for the length of the block.

    The limit is configurable (`PYTHONINTMAXSTRDIGITS`, `-X int_max_str_digits`, 0 turns it
    off), so a test that expects a long integer to fail decoding sets it rather than
    inheriting whatever the test process was started with. On a Python without the limit
    this does nothing, and the digit cases are skipped (`_NEEDS_DIGIT_LIMIT`); the fuzz's
    reference decodes in the same process, so it agrees either way.
    """
    get = getattr(sys, "get_int_max_str_digits", None)
    put = getattr(sys, "set_int_max_str_digits", None)
    if get is None or put is None:
        yield
        return
    previous = get()
    put(4300)
    try:
        yield
    finally:
        put(previous)


# ── build_step_args: the decision per input text ────────────────────────────────────────────

@pytest.mark.usefixtures("int_digit_limit")
@pytest.mark.parametrize("text,expected", [
    ('{"command": "ls"}', {"command": "ls"}),
    ("{}", {}),
    ("{not json}", {"command": "{not json}"}),
    pytest.param('{"a": ' + long_int(4301) + "}", {"command": '{"a": ' + long_int(4301) + "}"},
                 id="digits-4301", marks=_NEEDS_DIGIT_LIMIT),
    pytest.param('{"a": -' + long_int(5000) + "}",
                 {"command": '{"a": -' + long_int(5000) + "}"},
                 id="digits-minus-5000", marks=_NEEDS_DIGIT_LIMIT),
], ids=["object", "empty", "not-json", "digits-4301", "digits-minus-5000"])
def test_step_input_json_decides_args_text_or_refusal(text, expected):
    assert build_step_args("bash", text, None, SAME) == expected


def test_an_object_exactly_at_the_limit_is_the_args():
    text = deep_object(MAX_ARGUMENT_DEPTH)
    args = build_step_args("bash", text, None, SAME)
    import json
    assert args == json.loads(text)


@pytest.mark.parametrize("text", [
    deep_object(MAX_ARGUMENT_DEPTH + 1),
    deep_object(9998),
    deep_object(9999),
    deep_object(10000),
    deep_object(100000),
    deep_list_field(10000),
], ids=["101", "9998", "9999", "10000", "100000", "list-10000"])
def test_a_too_deep_step_input_is_refused(text):
    with pytest.raises(StepArgsRefused) as exc:
        build_step_args("bash", text, None, SAME)
    assert str(exc.value) == too_deep_refusal("bash")


def test_a_refused_input_is_not_a_value_error():
    """No existing `except ValueError` on the way may swallow the refusal."""
    assert not issubclass(StepArgsRefused, ValueError)


# ── the engine: the step fails, the run goes on ─────────────────────────────────────────────

class _Capture:
    """A contract-complete tool that records its calls and returns a fixed text."""
    description = "records calls"
    permission_level = "read"
    identity_kwargs = ()

    def __init__(self, name, primary, answer="ok"):
        self.name = name
        self.parameters = {"type": "object", "properties": {primary: {"type": "string"}}}
        self.answer = answer
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.answer


def _chain(first_answer, *, optional=False, callback=None):
    search = _Capture("web_search", "query", first_answer)
    bash = _Capture("bash", "command", "BASH_RAN")
    listing = _Capture("list_files", "path", "LIST_RAN")
    engine = WorkflowEngine(tools={"web_search": search, "bash": bash, "list_files": listing},
                            callback=callback or (lambda *a, **k: None))
    steps = [
        WorkflowStep(tool="web_search", input_template="find it", output_name="s1"),
        WorkflowStep(tool="bash", input_template="{s1}", output_name="s2", optional=optional),
        WorkflowStep(tool="list_files", input_template=".", output_name="s3"),
    ]
    return engine, steps, search, bash, listing


@pytest.fixture(params=["funnel", "rollback"])
def lane(request, monkeypatch):
    if request.param == "rollback":
        monkeypatch.setattr(engine_module, "_identity_is_declared", lambda: False)
    return request.param


def test_a_too_deep_step_input_fails_the_step_not_the_run(lane):
    engine, steps, search, bash, listing = _chain(deep_object(10000))
    result = engine.execute(steps, variables={})

    assert result.success is False
    assert steps[1].status == StepStatus.FAILED
    assert steps[1].error == too_deep_refusal("bash")
    assert bash.calls == [], "the step's tool ran on a refused input"
    assert listing.calls == [], "the run went on past a failed required step"


def test_an_optional_too_deep_step_lets_the_run_go_on(lane):
    engine, steps, search, bash, listing = _chain(deep_object(10000), optional=True)
    engine.execute(steps, variables={})

    assert steps[1].status == StepStatus.FAILED
    assert steps[1].error == too_deep_refusal("bash")
    assert bash.calls == []
    assert len(listing.calls) == 1, "an optional refused step stopped the run"


@pytest.mark.parametrize("optional", [False, True], ids=["required", "optional"])
def test_a_refused_step_reports_its_own_attempt(optional):
    """The refused attempt replaces what an earlier pass over the step left (a branch can
    revisit a step, simulated here by a stale result), and the progress callback gets an
    "error" for it before the run goes on or stops."""
    events = []
    engine, steps, search, bash, listing = _chain(
        deep_object(10000), optional=optional,
        callback=lambda event, step, i, total: events.append((event, i)))
    steps[1].result = "STALE RESULT OF AN EARLIER PASS"
    steps[1].duration = -1.0
    engine.execute(steps, variables={})

    assert steps[1].status == StepStatus.FAILED
    assert steps[1].result is None
    assert steps[1].duration >= 0.0
    assert ("error", 2) in events, events
    if optional:
        assert events.index(("error", 2)) < events.index(("start", 3)), events


def test_an_observer_that_fails_on_the_refusal_does_not_stop_the_recovery():
    """A progress callback that raises on "error" must not take optional/on_failure with it."""
    events = []

    def observer(event, step, i, total):
        events.append((event, i))
        if event == "error":
            raise RuntimeError("observer broke")

    engine, steps, search, bash, listing = _chain(deep_object(10000), optional=True,
                                                  callback=observer)
    engine.execute(steps, variables={})

    assert steps[1].status == StepStatus.FAILED
    assert len(listing.calls) == 1, "the optional step's recovery did not run"
    assert ("error", 2) in events and ("start", 3) in events


def test_a_failing_log_handler_does_not_stop_the_recovery_either(monkeypatch):
    """The warning about the observer is best effort: a handler that raises on it must not
    take optional/on_failure with it."""
    def broken_warning(*args, **kwargs):
        raise RuntimeError("log handler broke")

    monkeypatch.setattr(engine_module._log, "warning", broken_warning)

    def observer(event, step, i, total):
        if event == "error":
            raise RuntimeError("observer broke")

    engine, steps, search, bash, listing = _chain(deep_object(10000), optional=True,
                                                  callback=observer)
    engine.execute(steps, variables={})
    assert len(listing.calls) == 1, "a failing log handler stopped the recovery"


@_NEEDS_DIGIT_LIMIT
@pytest.mark.usefixtures("int_digit_limit")
def test_an_integer_past_the_digit_limit_is_passed_on_as_text(lane):
    # 4400 digits: past the decoder's 4300-digit limit, and the whole text stays below the
    # engine's 5000-character input cut, so the tool gets it unchanged.
    text = '{"a": ' + long_int(4400) + "}"
    engine, steps, search, bash, listing = _chain(text)
    result = engine.execute(steps, variables={})

    assert result.success, result.error
    assert [call.get("command") for call in bash.calls] == [text]
    assert len(listing.calls) == 1


# ── variables ───────────────────────────────────────────────────────────────────────────────

def _one_bash_step(variables):
    bash = _Capture("bash", "command", "BASH_RAN")
    engine = WorkflowEngine(tools={"bash": bash}, callback=lambda *a, **k: None)
    steps = [WorkflowStep(tool="bash", input_template="value {x}", output_name="out")]
    return engine.execute(steps, variables=variables), steps, bash


@pytest.mark.parametrize("variables,command", [
    ('{"x": "v"}', "value v"),
    ({"x": "v"}, "value v"),
    ("[1]", "value {x}"),
    ('{"x": ' + "[" * MAX_ARGUMENT_DEPTH + "]" * MAX_ARGUMENT_DEPTH + "}", "value {x}"),
    (deep_object(10000), "value {x}"),
], ids=["text", "dict", "list-text", "101-levels", "10000-levels"])
def test_variables_text_is_coerced_once_for_every_reader(variables, command):
    result, steps, bash = _one_bash_step(variables)

    assert result.success, result.error
    assert steps[0].status == StepStatus.SUCCESS, steps[0].error
    assert [call.get("command") for call in bash.calls] == [command]


def test_a_variables_dict_is_used_as_it_is():
    variables = {"x": "v", "nested": {"k": [1, 2]}}
    snapshot = repr(variables)
    result, steps, bash = _one_bash_step(variables)

    assert [call.get("command") for call in bash.calls] == ["value v"]
    assert repr(variables) == snapshot


# ── the planner ─────────────────────────────────────────────────────────────────────────────

def _planner_tools():
    from vaf.workflows.tool_overlay import workflow_primitives
    return {"bash": workflow_primitives()["bash"]}


def test_the_planner_refuses_a_literal_deep_input():
    from vaf.tools.agent_workflow_builder import _preflight_steps
    steps = [{"tool": "bash", "input": deep_object(MAX_ARGUMENT_DEPTH + 1), "output": "a"},
             {"tool": "bash", "input": "ls", "output": "b"}]
    refusal = _preflight_steps(steps, _planner_tools())

    assert refusal is not None
    assert ("step 1 (bash): its input is a JSON object nested deeper than "
            f"{MAX_ARGUMENT_DEPTH} levels") in refusal
    assert "step 2" not in refusal


class _TextTool:
    """A tool whose one required parameter is the step's text (`input`), with no other check."""
    name = "echo"
    description = "echoes its input"
    permission_level = "read"
    identity_kwargs = ()
    parameters = {"type": "object", "properties": {"input": {"type": "string"}},
                  "required": ["input"]}

    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return "ECHOED"


def test_the_planner_passes_text_that_is_not_json():
    from vaf.tools.agent_workflow_builder import _preflight_steps
    steps = [{"tool": "echo", "input": "{not json}", "output": "a"}]

    assert _preflight_steps(steps, {"echo": _TextTool()}) is None


# An object 101 levels deep whose string field is a placeholder: a value with a newline,
# substituted inside the quotes, breaks the JSON, and the engine passes the text on.
_DYNAMIC_DEEP = ('{"first": {}, "value": "{value}", "nested": '
                 + "[" * MAX_ARGUMENT_DEPTH + "]" * MAX_ARGUMENT_DEPTH + "}")


def test_the_planner_leaves_input_with_placeholders_to_the_engine():
    from vaf.tools.agent_workflow_builder import _preflight_steps
    steps = [{"tool": "echo", "input": _DYNAMIC_DEEP, "output": "a"}]

    assert _preflight_steps(steps, {"echo": _TextTool()}) is None

    echo = _TextTool()
    engine = WorkflowEngine(tools={"echo": echo}, callback=lambda *a, **k: None)
    result = engine.execute(
        [WorkflowStep(tool="echo", input_template=_DYNAMIC_DEEP, output_name="a")],
        variables={"value": "line one\nline two"})
    assert result.success, result.error
    assert echo.calls and "line one\nline two" in echo.calls[0]["input"]


@pytest.mark.parametrize("template,dynamic", [
    (deep_object(MAX_ARGUMENT_DEPTH + 1), False),
    ('{"a": [1, 2], "b": {"c": "d"}}', False),
    ("plain text", False),
    ("{}", False),
    (_DYNAMIC_DEEP, True),
    ("value {x}", True),
    ("{step1.field}", True),
    ("{name|fallback}", True),
    ('{"a|b": 1}', True),
    ('{"x": 1.5}', True),
], ids=["deep-literal", "flat-literal", "text", "empty", "placeholder-in-json", "simple",
        "nested", "default", "json-with-bar", "json-with-dot"])
def test_template_may_substitute_follows_the_resolver(template, dynamic):
    from vaf.workflows.engine import template_may_substitute
    assert template_may_substitute(template) is dynamic
    # The claim is the resolver's own: what it calls literal, resolving leaves unchanged.
    if not dynamic:
        engine = WorkflowEngine(tools={}, callback=lambda *a, **k: None)
        assert engine._resolve_template(template, {"x": "X", "a": "A"}, {}) == template


# ── the public refusal ──────────────────────────────────────────────────────────────────────

def test_too_deep_refusal_is_public():
    import vaf.core.tool_dispatch as td
    assert not hasattr(td, "_too_deep")
    assert td.too_deep_refusal("bash") == (
        "Tool Error: the arguments of this 'bash' call are nested deeper than "
        f"{MAX_ARGUMENT_DEPTH} levels, so nothing was run. Call it again with flatter arguments.")


# ── fuzz: the "{...}" branch of build_step_args against a reference ─────────────────────────

_FUZZ_SEED = 0xF372E
_LEAVES = ['""', "1", "-0.5", '"x\\u00e9"', "null", "true"]


def _depth_of(value, limit):
    """The test's own iterative depth counter (independent of `nesting_depth`)."""
    if not isinstance(value, (dict, list)):
        return 0
    deepest, stack = 0, [(value, 1)]
    while stack:
        node, level = stack.pop()
        deepest = max(deepest, level)
        if deepest > limit:
            return deepest
        for child in (node.values() if isinstance(node, dict) else node):
            if isinstance(child, (dict, list)):
                stack.append((child, level + 1))
    return deepest


def _draw_text(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.30:
        depth = rng.randint(1, 20)
    elif roll < 0.60:
        depth = rng.randint(95, 105)
    elif roll < 0.75:
        depth = rng.randint(9990, 10010)
    else:
        depth = int(round(10 ** rng.uniform(2, 4.69897)))  # log-uniform 100..50,000

    # Long integers in a quarter of the shallow draws only: a draw holding one past the digit
    # limit is text, which would otherwise hide the 100/101 boundary in most draws.
    long_ints = depth <= 200 and rng.random() < 0.25

    def leaf():
        if long_ints and rng.random() < 0.05:
            return rng.choice(["-", ""]) + "9" * rng.randint(4290, 4310)
        return rng.choice(_LEAVES)

    opens, closes = [], []
    for level in range(1, depth + 1):
        is_dict = level % 2 == 1
        siblings = [leaf() for _ in range(rng.randint(0, 3))]
        if is_dict:
            items = [f'"s{i}": {v}' for i, v in enumerate(siblings)]
        else:
            items = list(siblings)
        last = level == depth
        if last:
            opens.append(("{" if is_dict else "[") + ", ".join(items) + ("}" if is_dict else "]"))
        else:
            prefix = ", ".join(items) + (", " if items else "")
            opens.append(("{" if is_dict else "[") + prefix + ('"c": ' if is_dict else ""))
            closes.append("}" if is_dict else "]")
    text = "".join(opens) + "".join(reversed(closes))
    if rng.random() < 0.20:
        at = rng.randrange(len(text) + 1)
        if rng.random() < 0.5 and at < len(text):
            text = text[:at] + text[at + 1:]
        else:
            text = text[:at] + rng.choice('{}[],:"x1 ') + text[at:]
    return text


def _reference(text: str):
    """("args", dict) | ("text", dict) | ("refused", None), decided without the engine."""
    import json
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return "text", _infer_step_args("bash", text)
    try:
        value = json.loads(text)
    except RecursionError:
        return "refused", None
    except ValueError:
        return "text", _infer_step_args("bash", text)
    if _depth_of(value, MAX_ARGUMENT_DEPTH) > MAX_ARGUMENT_DEPTH:
        return "refused", None
    return "args", value


def _actual(text: str):
    try:
        return "ok", build_step_args("bash", text, None, SAME)
    except StepArgsRefused as exc:
        assert str(exc) == too_deep_refusal("bash")
        return "refused", None


@pytest.mark.usefixtures("int_digit_limit")
def test_step_input_fuzz():
    draws = int(os.environ.get("VAF_STEP_ARGS_FUZZ_DRAWS", "300"))
    rng = random.Random(_FUZZ_SEED)
    mismatches = []
    for n in range(draws):
        text = _draw_text(rng)
        kind, expected = _reference(text)
        got_kind, got = _actual(text)
        if kind == "refused":
            ok = got_kind == "refused"
        else:
            ok = got_kind == "ok" and got == expected
        if not ok:
            mismatches.append((n, len(text), kind, got_kind, text[:80]))
    assert not mismatches, f"{len(mismatches)} of {draws} draws differ: {mismatches[:5]}"
