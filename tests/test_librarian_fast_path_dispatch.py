# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The librarian's OTHER dispatch path.

`061a82bd` put "the librarian" on the shared pipeline and said so in its commit message and
in two planning documents. It converted one of two paths. The pattern-matching fast path -
the one that answers "how big is this folder", "rename X to Y", "show me the structure"
without ever starting the inner model - kept calling `self.tools[name].run(**args)` directly,
at five sites: `folder_size`, `move_file` twice, `write_file` and `tree`.

That is the failure this file exists to prevent recurring, and the shape of it matters more
than the sites: a conversion was counted by the path that was worked on rather than by
measuring how many paths there were. The same miscount produced "three messengers" (four) and
"the ungated half of the write surface" (a third).

WHAT WAS AND WAS NOT EXPOSED, measured rather than assumed - because the first description of
this gap was too strong in one direction:

  - The per-user file jail DID apply. `LibrarianTool.run` installs it as a contextvar around
    the whole run and all five tools ask `is_safe_path`, which reads it. A foreign tenant path
    resolves to False on the fast path, same as everywhere else.
  - What was missing: policy, the time bound, and every trace of the call. Five tool
    executions emitted no `tool_start`/`tool_end`, so nothing in the timeline recorded that
    the librarian had renamed or written anything. That is a forensic hole, not only a control
    one: afterwards nobody can reconstruct what it did.

The confirmation gate stays off here, and that is a decision rather than an omission: the
inner model loop has no seam to ask a person through, so enabling it on the fast path alone
would make `move_file` prompt or not depending on whether a regex matched the request. Two
paths that gate DIFFERENTLY are worse than two that both do not.
"""
import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vaf.tools.librarian import LibrarianTool

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID


class _SpyCaller:
    """Stands in for the pipeline and records what was handed to it."""

    def __init__(self, result="ok"):
        self.calls = []
        self._result = result

    def execute(self, name, args=None):
        self.calls.append((name, dict(args or {})))
        return self._result


# ── the conversion itself, asserted mechanically ─────────────────────────────

def test_no_raw_dispatch_survives_anywhere_in_the_librarian():
    """THE guard. Not "the fast path uses the caller" - that would pass while a sixth site
    sits in a method nobody thought about, which is exactly what happened. This asks the whole
    file, so a new raw call is a red test on the day it is written."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.getmodule(LibrarianTool))))
    raw = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # self.tools[...].run(...)
        if (isinstance(fn, ast.Attribute) and fn.attr == "run"
                and isinstance(fn.value, ast.Subscript)
                and isinstance(fn.value.value, ast.Attribute)
                and fn.value.value.attr == "tools"):
            raw.append(node.lineno)

    assert raw == [], (
        f"raw tool dispatch is back at line(s) {raw}: no policy, no time bound and no "
        "tool_start/tool_end, so the call leaves no trace in the timeline"
    )


def test_the_caller_is_built_once_and_shared_by_both_paths():
    """Two constructions is how the paths drifted apart in the first place: one was converted
    and the other kept its own."""
    src = inspect.getsource(inspect.getmodule(LibrarianTool))
    assert src.count("ToolCaller(") == 1, (
        "more than one ToolCaller construction in the librarian - the second path will drift "
        "from the first exactly as it did before"
    )
    assert "_make_caller(**kwargs)" in inspect.getsource(LibrarianTool._run_impl)


def test_exactly_one_caller_is_built_per_run():
    """The invariant, stated as itself rather than as a spelling: `_run_impl` builds the caller
    ONCE and both paths get that object.

    Found by a counter-proof rather than by design. Handing the model loop a freshly built
    `self._make_caller()` - no kwargs - loses the identity completely, and every other test
    here stayed green through it, because each one either drives a path with a caller supplied
    by the test or checks a source pattern that the broken version still matches. That is the
    same shape as the original defect: the stage was right and the wiring was not.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(LibrarianTool._run_impl)))
    builds = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "_make_caller"]
    assert len(builds) == 1, (
        f"{len(builds)} callers built in one run. A second one is either identical (waste) or "
        "built from different inputs (an identity that silently differs between the two paths)"
    )
    assert builds[0].keywords and builds[0].keywords[0].arg is None, (
        "the caller is built without forwarding **kwargs, so the run's identity never reaches "
        "the tools - the sub-tools then run unscoped no matter how correct the pipeline is"
    )


# ── the fast path actually routes through it ─────────────────────────────────

def test_the_tree_helper_dispatches_through_the_caller(tmp_path):
    spy = _SpyCaller(result="a\nb\nc")
    LibrarianTool()._show_tree(tmp_path, spy)
    assert [c[0] for c in spy.calls] == ["tree"]


def test_the_write_helper_dispatches_through_the_caller(tmp_path):
    spy = _SpyCaller()
    LibrarianTool()._write_file(tmp_path / "note.txt", "hello", spy)
    assert [c[0] for c in spy.calls] == ["write_file"]
    assert spy.calls[0][1]["content"] == "hello"


def test_a_pattern_matched_task_reaches_the_caller(tmp_path, monkeypatch):
    """Driven through the real entry point rather than the helper, so the wiring from
    `_run_impl` down is covered too."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    spy = _SpyCaller(result="tree output")

    lib = LibrarianTool()
    lib._emit_librarian_state = lambda *a, **k: None
    out = lib._try_direct_execution(f"show me the folder structure of {tmp_path}", spy)

    assert [c[0] for c in spy.calls] == ["tree"], f"the fast path did not dispatch: {out!r}"


# ── what the conversion buys: the call becomes visible ───────────────────────

def test_a_fast_path_call_now_emits_start_and_end_events():
    """The forensic half. Before this change these five executions produced no events at all,
    so a rename done by the librarian left no record anywhere."""
    seen = []

    class _Tool:
        name = "tree"
        description = "x"
        parameters = {"type": "object", "properties": {}}
        permission_level = "read"
        identity_kwargs = ()

        def run(self, **kw):
            return "output"

    from vaf.core.tool_dispatch import ToolCaller

    caller = ToolCaller({"tree": _Tool()}, max_result_chars=None, gate_enabled=False,
                        on_event=lambda ev, *a, **k: seen.append(ev.get("type")))
    LibrarianTool()._show_tree(Path("/tmp"), caller)

    assert "tool_start" in seen and "tool_end" in seen, (
        f"the fast path still runs invisibly: {seen}"
    )


# ── the argument that keeps a truncated tree from looking complete ───────────

def test_truncation_is_off_for_this_lane():
    """`_show_tree` wraps the result in a code fence. A cut lands INSIDE it, and the
    pipeline's truncation notice then reads as one more line of the tree rather than as a
    system message - overlooked and ticked off as seen at once."""
    caller = LibrarianTool()._make_caller(user_scope_id=SCOPE, user_role="user")
    assert caller.max_result_chars is None


def test_the_gate_stays_off_deliberately():
    """Pinned so that turning it on is a decision someone makes on purpose. `move_file` is the
    one tool of the thirteen in RISKY_TOOLS, so this is the line that would change."""
    caller = LibrarianTool()._make_caller(user_scope_id=SCOPE, user_role="user")
    assert caller.gate_enabled is False


def test_the_identity_reaches_the_caller_from_the_run_kwargs():
    caller = LibrarianTool()._make_caller(user_scope_id=SCOPE, user_role="admin")
    assert caller.user_scope_id == SCOPE
    assert caller.user_role == "admin"


def test_the_identity_falls_back_to_the_subagent_environment(monkeypatch):
    """The librarian also runs in a separate terminal, where identity arrives as env."""
    monkeypatch.setenv("VAF_USER_SCOPE_ID", SCOPE)
    monkeypatch.setenv("VAF_USER_ROLE", "user")
    caller = LibrarianTool()._make_caller()
    assert caller.user_scope_id == SCOPE
    assert caller.user_role == "user"


# ── the jail half, which was NOT broken and must not be broken now ───────────

def test_the_jail_still_answers_on_the_fast_path(tmp_path, monkeypatch):
    """The correction to the original description, kept as a test so the record stays honest:
    the file jail did apply here before this change and still does. It comes from the
    contextvar `run()` installs, not from the caller."""
    from vaf.tools.filesystem import (compute_user_jail, is_safe_path,
                                      reset_librarian_scope, set_librarian_scope)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    foreign = str(tmp_path / "Documents" / "VAF_Projects" / "ffffffff" / "notes.txt")

    assert is_safe_path(foreign)[0] is True, "precondition: allowed with no jail installed"
    token = set_librarian_scope(compute_user_jail(SCOPE, None))
    try:
        assert is_safe_path(foreign)[0] is False
    finally:
        reset_librarian_scope(token)


def test_the_caller_is_never_parked_on_the_shared_instance():
    """`agent.tools` is built once per process and serves every user, so an identity left on
    the instance outlives the turn that set it. Threading it through five call levels is the
    deliberately inconvenient alternative."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(LibrarianTool)))
    parked = [n.lineno for n in ast.walk(tree)
              if isinstance(n, ast.Attribute) and n.attr == "_caller"
              and isinstance(n.value, ast.Name) and n.value.id == "self"]
    assert parked == []
