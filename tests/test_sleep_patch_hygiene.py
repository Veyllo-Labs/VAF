# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A test's fake `time.sleep` reaches the test's own thread, never another one.

`monkeypatch.setattr(bp.time, "sleep", f)` does not patch `bp`: `bp.time` IS the shared
`time` module, so the fake is installed for every thread in the process. A background loop
an earlier test left running looks `time.sleep` up on every turn and gets the fake too.
Seen as a full-suite failure: two requests logged at 117840 s and 332100 s of fake time
where 0 s and 41 s were expected, because the web server's connection-map cleanup thread
(`while True: time.sleep(30)`) had picked up a fake clock's sleep and moved it 30 s per
turn, 3928 and 11070 turns. It passed alone and failed in some full runs, depending on
whether the loop's real 30 s wake fell inside the patch window.

Three harms, each measured with one foreign `while True: time.sleep(0.01)` alive while the
14 files that patched `time.sleep` process-wide ran:

1. spin: 309,772,696 loop turns in 67.6 s where a real sleeper makes about 6,800, and the
   files took 67.6 s and 97 s instead of 41.3 s, because the spinning loop holds the GIL;
2. a recording fake takes the foreign thread's values into the list the test asserts on;
3. a raising fake ends the test early: two foreign sleeps filled the tray poll test's list,
   so it raised its stop exception on its own first sleep and never reached the poll
   (`('local_engine', 'vllm', 'llama') in []`), and the foreign thread died of that
   exception, which was not its own.

A ban would be wrong: a test needs a fast sleep on its own thread. So every process-wide
replacement goes through `fake_clock.only_on_thread(fake)`, or a clock's
`sleep_in_test_thread`, which run the fake on one thread and the real sleep on every other.
The guard below finds each replacement by its AST and accepts only those two forms, with no
allowlist: every site has an honest form, and an exception would reopen the class. When the
code under test sleeps on a worker thread the test starts, pass that thread:
`only_on_thread(f, thread=t)`.

Named boundary: module-local seams (`monkeypatch.setattr(rl, "_sleep", f)`, a module's
`time` attribute replaced by a namespace of its own) and patches of `time.monotonic` or
`asyncio.sleep` are not covered. The first two do not reach other modules; a foreign READ
of a fake clock does not move it; `asyncio.sleep` is awaited on an event loop, and the one
loop thread that outlives its test waits in `select`, not in a sleep.
"""
import ast
import threading
import time
from pathlib import Path

import pytest

from conftest import _product_loop
from fake_clock import only_on_thread

_TESTS = Path(__file__).resolve().parent
_ACCEPTED_CALL = "only_on_thread"
_ACCEPTED_ATTR = "sleep_in_test_thread"


def _is_sleep_path(node) -> bool:
    """A string naming the shared sleep: "time.sleep" or "<module>.time.sleep"."""
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and (node.value == "time.sleep" or node.value.endswith(".time.sleep")))


def _last_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _wrapped(value) -> bool:
    if isinstance(value, ast.Call) and _last_name(value.func) == _ACCEPTED_CALL:
        return True
    return isinstance(value, ast.Attribute) and value.attr == _ACCEPTED_ATTR


def _keyword(call, name):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _arg(call, index, name):
    if len(call.args) > index:
        return call.args[index]
    return _keyword(call, name)


def unwrapped_sleep_patches(source: str) -> list:
    """Line numbers of every process-wide `time.sleep` replacement in `source` whose
    replacement is not `only_on_thread(...)` or `<clock>.sleep_in_test_thread`."""
    tree = ast.parse(source)
    aliases = {"time"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(a.asname for a in node.names if a.name == "time" and a.asname)

    def is_time(node) -> bool:
        if isinstance(node, ast.Name):
            return node.id in aliases
        return isinstance(node, ast.Attribute) and node.attr == "time"

    def is_sleep_name(node) -> bool:
        return isinstance(node, ast.Constant) and node.value == "sleep"

    found = []

    def check(node, value):
        if value is None or not _wrapped(value):
            found.append(node.lineno)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = _last_name(node.func)
            args = node.args
            if func == "setattr":
                if len(args) >= 2 and is_time(args[0]) and is_sleep_name(args[1]):
                    check(node, _arg(node, 2, "value"))
                elif args and _is_sleep_path(args[0]):
                    check(node, _arg(node, 1, "name"))
            elif func == "patch" and args and _is_sleep_path(args[0]):
                check(node, _arg(node, 1, "new"))
            elif (func == "object" and _last_name(getattr(node.func, "value", None)) == "patch"
                  and len(args) >= 2 and is_time(args[0]) and is_sleep_name(args[1])):
                check(node, _arg(node, 2, "new"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (isinstance(target, ast.Attribute) and target.attr == "sleep"
                        and is_time(target.value)):
                    check(node, node.value)
    return sorted(found)


def test_no_process_wide_sleep_fake_reaches_other_threads():
    offenders = []
    for path in sorted(_TESTS.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for line in unwrapped_sleep_patches(source):
            offenders.append(f"{path.relative_to(_TESTS.parent).as_posix()}:{line}")
    assert not offenders, (
        "a fake time.sleep installed for the whole process reaches every thread, including "
        "loops other tests left running; wrap it as only_on_thread(fake) from fake_clock "
        "(thread=t for a worker the test starts): " + ", ".join(offenders))


_SPELLINGS = {
    "monkeypatch_module": 'monkeypatch.setattr(time, "sleep", {v})',
    "monkeypatch_module_attr": 'monkeypatch.setattr(bp.time, "sleep", {v})',
    "monkeypatch_value_kw": 'monkeypatch.setattr(time, "sleep", value={v})',
    "builtin_setattr": 'setattr(time, "sleep", {v})',
    "monkeypatch_string": 'monkeypatch.setattr("time.sleep", {v})',
    "monkeypatch_dotted_string": 'monkeypatch.setattr("vaf.core.backend.time.sleep", {v})',
    "patch": 'with patch("time.sleep", {v}):\n    pass',
    "mock_patch_new_kw": 'with mock.patch("time.sleep", new={v}):\n    pass',
    "patch_object": 'patch.object(time, "sleep", {v})',
    "mock_patch_object_new_kw": 'mock.patch.object(bp.time, "sleep", new={v})',
    "import_alias": 'import time as _t\nmonkeypatch.setattr(_t, "sleep", {v})',
    "assignment": "time.sleep = {v}",
    "assignment_module_attr": "bp.time.sleep = {v}",
}


@pytest.mark.parametrize("spelling", sorted(_SPELLINGS))
def test_every_spelling_is_caught_bare_and_passes_wrapped(spelling):
    template = _SPELLINGS[spelling]
    line = template.count("\n", 0, template.index("{v}")) + 1
    assert unwrapped_sleep_patches(template.format(v="lambda s: None")) == [line]
    assert unwrapped_sleep_patches(template.format(v="only_on_thread(lambda s: None)")) == []
    assert unwrapped_sleep_patches(template.format(v="fake_clock.only_on_thread(f)")) == []
    assert unwrapped_sleep_patches(template.format(v="clock.sleep_in_test_thread")) == []


@pytest.mark.parametrize("source", [
    'patch("time.sleep")',
    'mock.patch.object(time, "sleep")',
    'monkeypatch.setattr("vaf.core.backend.time.sleep", clock.sleep)',
])
def test_a_patch_without_a_wrapped_replacement_is_caught(source):
    assert unwrapped_sleep_patches(source) == [1]


@pytest.mark.parametrize("source", [
    'monkeypatch.setattr(rl, "_sleep", lambda s: None)',
    'monkeypatch.setattr(hr, "time", SimpleNamespace(time=_time.time, sleep=f))',
    'monkeypatch.setattr(asyncio, "sleep", f)',
    'monkeypatch.setattr("asyncio.sleep", f)',
    'monkeypatch.setattr(time, "monotonic", f)',
    'monkeypatch.setattr("time.monotonic", f)',
    'x = time.sleep',
])
def test_module_local_seams_and_other_clocks_are_not_flagged(source):
    assert unwrapped_sleep_patches(source) == []


def test_only_on_thread_fakes_on_the_caller_and_sleeps_for_real_elsewhere():
    calls = []
    sleep = only_on_thread(calls.append)
    sleep(7)
    assert calls == [7]

    took = {}

    def other():
        start = time.monotonic()
        sleep(0.05)
        took["s"] = time.monotonic() - start

    t = threading.Thread(target=other)
    t.start()
    t.join(5)
    assert calls == [7], "another thread reached the fake"
    assert took["s"] >= 0.05, "another thread did not really sleep"


def test_only_on_thread_can_name_a_thread_before_it_starts():
    calls = []

    def other():
        sleep(3)

    t = threading.Thread(target=other)
    sleep = only_on_thread(calls.append, thread=t)
    t.start()
    t.join(5)
    assert calls == [3]
    start = time.monotonic()
    sleep(0.05)
    assert calls == [3], "the caller reached a fake meant for another thread"
    assert time.monotonic() - start >= 0.05


def test_a_raising_fake_raises_on_its_owner_thread_only():
    class _Stop(Exception):
        pass

    def fake(s):
        raise _Stop()

    sleep = only_on_thread(fake)
    with pytest.raises(_Stop):
        sleep(1)
    errors = []

    def other():
        try:
            sleep(0.01)
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=other)
    t.start()
    t.join(5)
    assert errors == []


def test_the_connection_map_starts_no_cleanup_thread_in_a_test(monkeypatch):
    """The session stub in conftest keeps the tracker's endless cleanup loop out of the
    suite; a fresh tracker is built here so the start path runs whatever ran before."""
    from vaf.network import connection_tracker as ct

    monkeypatch.setattr(ct.ConnectionTracker, "_instance", None)
    ct.get_tracker()
    alive = [_product_loop(t) for t in threading.enumerate()
             if t.is_alive() and "connection_tracker" in _product_loop(t)]
    assert alive == []
