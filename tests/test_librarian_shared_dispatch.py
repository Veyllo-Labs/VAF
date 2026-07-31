# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The librarian's own agent loop, running its tools through the shared pipeline.

The librarian is a tool that drives a second model, and that model calls thirteen sub-tools.
Those calls used to bypass everything the dispatcher does: `self.tools[name].run(**args)`,
directly. So the librarian was the one place in VAF where a tool ran with no policy check, no
declared identity and no execution bound - and the identity part was the sharp end, because
SIX of those thirteen are file tools whose per-user jail cannot form without a scope and a
role. They inherited the librarian's own outer jail instead, which is not the same thing.

WHY THIS FILE EXISTS AT ALL: nothing drove that loop. Not one test. The same gap let a
NameError live inside `multi_tool_use.parallel` through a whole refactor, green the entire
time. A conversion of an untested loop is a claim, not a change, so the loop is driven here
with a stubbed model rather than left to be discovered in production.

What is deliberately NOT asserted: the gate. The librarian has no seam to the person who
started it, so `gate_enabled=False` is passed. The point of the conversion is that this is
now one argument instead of a missing capability - `tests/test_tool_caller.py` already covers
what the gate does when a caller does supply an asker.
"""
import json
import pathlib
from unittest.mock import patch

import pytest

from vaf.tools.base import BaseTool
from vaf.tools.librarian import LibrarianTool

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID

# The loop reads Config.LOAD() - a dict - not Config.get, and branches on `provider`: with the
# shipped default ("local") it takes a different path entirely and never reaches the dispatch.
# So the config is supplied whole. An earlier version patched Config.get, which the loop never
# calls, and therefore passed only on a machine whose real config happened to name an API
# provider - green here, red on every runner. Exactly the failure CI caught an hour earlier in
# a different test; the lesson is the same one: run the suite in a throwaway HOME before
# calling it green.
_API_CONFIG = {"provider": "openai", "model": "gpt-4o-mini"}


class _Recorder(BaseTool):
    """Stands in for a librarian sub-tool and reports what the pipeline handed it."""

    description = "probe"
    permission_level = "read"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

    def __init__(self, name, declares=("user_role", "user_scope_id"), fn=None):
        super().__init__()
        self.name = name
        self.identity_kwargs = tuple(declares)
        self.seen = None
        self._fn = fn or (lambda **kw: "tool output")

    def run(self, **kwargs):
        self.seen = dict(kwargs)
        return self._fn(**kwargs)


def _drive(tool, *, user_scope_id=SCOPE, user_role="user", tool_name=None):
    """Run one turn of the librarian's model loop with a scripted tool call.

    The loop streams from the API backend and treats a JSON chunk carrying `tool_calls` as
    the model's decision, so the model is replaced by exactly that: one chunk, one call.
    """
    # The tool is always registered under ITS OWN name; `tool_name` is what the model asks
    # for, so the two can differ deliberately (the unknown-tool case).
    requested = tool_name or tool.name
    chunks = [json.dumps({"tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": requested, "arguments": json.dumps({"path": "notes.md"})}},
    ]})]

    class _Backend:
        def __init__(self, *a, **k):
            self._first = True

        def chat_completion(self, **kwargs):
            # First round hands over the tool call; the second ends the loop with plain text
            # so the librarian stops instead of calling forever.
            if self._first:
                self._first = False
                yield from chunks
            else:
                yield "done"

    librarian = LibrarianTool()
    librarian.tools = {tool.name: tool}
    with patch("vaf.core.api_backend.APIBackendManager", _Backend), \
         patch("vaf.core.config.Config.load", classmethod(lambda cls: _API_CONFIG)):
        return librarian._execute_with_llm(
            "do the thing",
            caller=librarian._make_caller(user_scope_id=user_scope_id, user_role=user_role),
        )


# ── the identity, which is the point ─────────────────────────────────────────

def test_a_sub_tool_receives_the_callers_declared_identity():
    """The sharp end: six of the thirteen are file tools, and their jail cannot form from a
    scope alone - `is_admin_identity` needs the role to recognise a second administrator."""
    tool = _Recorder("read_file")
    _drive(tool)
    assert tool.seen is not None, "the librarian's loop never dispatched the tool"
    assert tool.seen.get("user_scope_id") == SCOPE
    assert tool.seen.get("user_role") == "user"


def test_a_tool_that_declares_nothing_still_receives_nothing():
    tool = _Recorder("list_files", declares=())
    _drive(tool)
    assert "user_scope_id" not in tool.seen
    assert "user_role" not in tool.seen


def test_the_model_cannot_supply_its_own_identity():
    """The librarian's inner model writes the arguments, so an identity in them is the
    model's own claim. Assigned, never defaulted - the same rule as the chat lane."""
    tool = _Recorder("read_file")
    name = "read_file"
    chunk = json.dumps({"tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": name,
        "arguments": json.dumps({"path": "x", "user_scope_id": "ffffffff-0000",
                                 "user_role": "admin"})}}]})

    class _Backend:
        def __init__(self, *a, **k):
            self._first = True

        def chat_completion(self, **kwargs):
            if self._first:
                self._first = False
                yield chunk
            else:
                yield "done"

    librarian = LibrarianTool()
    librarian.tools = {name: tool}
    with patch("vaf.core.api_backend.APIBackendManager", _Backend), \
         patch("vaf.core.config.Config.load", classmethod(lambda cls: _API_CONFIG)):
        librarian._execute_with_llm(
            "go", caller=librarian._make_caller(user_scope_id=SCOPE, user_role="user"))

    assert tool.seen["user_scope_id"] == SCOPE
    assert tool.seen["user_role"] == "user"


def test_identity_is_not_stored_on_the_shared_tool_instance():
    """The librarian is registered once per process and serves whoever is routed to it, so a
    per-call identity parked on `self` would leak across users the moment a second worker
    exists. It travels as an argument; the instance must stay clean."""
    librarian = LibrarianTool()
    before = {k for k in vars(librarian)}
    tool = _Recorder("read_file")
    librarian.tools = {"read_file": tool}

    for attr in ("_current_user_scope_id", "_user_scope_id", "_user_role", "_scope"):
        assert not hasattr(librarian, attr), f"identity parked on the shared instance: {attr}"
    assert "user_scope_id" not in before


# ── the two arguments that keep the conversion behaviour-neutral ─────────────

def _configured_with(**drive_kwargs):
    """Capture how the librarian configures its ToolCaller, by driving the real loop.

    Asserting on a ToolCaller this test built itself would only prove ToolCaller works - it
    is the librarian's OWN construction that carries the two arguments keeping the conversion
    behaviour-neutral, and that is what has to be pinned.
    """
    import vaf.core.tool_dispatch as td

    captured = {}
    real = td.ToolCaller

    class _Capturing(real):
        def __init__(self, tools, **kwargs):
            captured.update(kwargs)
            super().__init__(tools, **kwargs)

    with patch.object(td, "ToolCaller", _Capturing):
        _drive(_Recorder("read_file"), **drive_kwargs)
    return captured


def test_the_librarian_switches_truncation_off():
    """Listings and file contents are what the inner model reasons about. The chat lane's
    2000-char cut would silently shorten the answer, and nothing downstream would say so."""
    assert _configured_with().get("max_result_chars", "<absent>") is None


def test_the_librarian_passes_the_identity_it_was_given():
    cfg = _configured_with(user_scope_id=SCOPE, user_role="user")
    assert cfg.get("user_scope_id") == SCOPE
    assert cfg.get("user_role") == "user"


def test_the_gate_is_off_and_that_is_now_one_argument():
    """Not asserted as a preference - asserted as the shape of the decision. Before the
    conversion "should the librarian gate" was not expressible at all; it is a keyword now."""
    assert _configured_with().get("gate_enabled") is False


def test_the_run_entrypoint_hands_the_identity_to_the_loop():
    """The wiring, separately from the stage. Every behavioural test here calls the loop with
    an identity directly, so all of them stay green if the ONE call site stops passing it -
    which is exactly how a correct stage reached through a dropping caller looks."""
    import inspect

    src = inspect.getsource(LibrarianTool._run_impl)
    assert "_execute_with_llm(" in src
    assert "_make_caller(**kwargs)" in src, (
        "_run_impl no longer builds the caller from its own kwargs, so every sub-tool would "
        "run unscoped no matter how correct the loop itself is"
    )
    assert "_try_direct_execution(task, caller)" in src, (
        "the fast path is not being handed the caller - it dispatched raw for a whole release "
        "exactly this way, while the LLM path was converted and looked like the whole lane"
    )
    factory = inspect.getsource(LibrarianTool._make_caller)
    assert 'kwargs.get("user_scope_id")' in factory and 'kwargs.get("user_role")' in factory
    assert 'kwargs.get("user_role")' in inspect.getsource(LibrarianTool._make_caller)


def test_policy_now_applies_where_it_did_not_before():
    """The other half of the conversion: an admin-only tool used to run here regardless."""
    tool = _Recorder("read_file")
    tool.admin_only = True
    result = _drive(tool, user_role="user")
    assert tool.seen is None, "an admin-only tool ran for a non-admin inside the librarian"
    assert isinstance(result, str)


def test_an_unknown_tool_does_not_raise():
    tool = _Recorder("read_file")
    result = _drive(tool, tool_name="no_such_tool")
    assert tool.seen is None
    assert isinstance(result, str)


# ── the seam itself ──────────────────────────────────────────────────────────

def test_the_loop_dispatches_through_the_shared_pipeline():
    """A source guard, because the whole conversion is that this call site exists. Reverting
    to `self.tools[name].run(**args)` would restore a lane with no policy and no identity, and
    every behavioural test above would still need the loop to be driven to notice."""
    import inspect

    src = inspect.getsource(LibrarianTool._execute_with_llm)
    assert "caller.execute(" in src
    assert "self.tools[fn_name].run(" not in src
    # The construction moved into _make_caller so BOTH paths share one; assert it there.
    assert "ToolCaller(" in inspect.getsource(LibrarianTool._make_caller)
    assert "ToolCaller(" not in src, "a second construction is back - that is how the two paths drifted"


# ── the jail widening, measured rather than asserted in a comment ────────────

FILE_TOOLS_WITH_BOTH_KEYS = (
    # Grew from six to seven on 2026-07-31: document_viewer gained user_role together with
    # its `file_access` declaration, which refuses to be declared without the identity that
    # resolves it. The count is the point - a tool dropping out of this set would lose its
    # jail without failing anything else.
    # And from seven to EIGHT the same day: cloud_storage declared both keys in cloud step
    # A. It is the one member that does NOT declare `file_access` - its boundary is
    # applied narrowly in `_action_save`, because `read`/`show_in_viewer` hand the tool's
    # OWN temp download to LibrarianTool._read_file, which asks is_safe_path and would
    # refuse a temp path inside a tenant jail.
    "cloud_storage", "document_viewer", "find_files", "folder_size", "list_files",
    "read_file", "tree", "write_file",
)


def test_the_file_tools_among_the_thirteen_carry_both_jail_keys():
    """The count is load-bearing for the paragraph below, so it is measured, not written
    down. A tool losing a key would silently drop out of the jail without failing anything."""
    import importlib
    import inspect
    import pkgutil
    import re

    import vaf.tools

    src = pathlib.Path(inspect.getfile(LibrarianTool)).read_bytes().decode()
    block = re.search(r"self\.tools\s*=\s*\{(.*?)\n\s*\}", src, re.S)
    registered = set(re.findall(r'"([a-z_]+)":', block.group(1)))

    both = set()
    for mod in pkgutil.iter_modules(vaf.tools.__path__):
        try:
            module = importlib.import_module("vaf.tools." + mod.name)
        except Exception:
            continue
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, BaseTool) and getattr(cls, "name", None) in registered:
                if {"user_role", "user_scope_id"} <= set(getattr(cls, "identity_kwargs", ()) or ()):
                    both.add(cls.name)
    assert both == set(FILE_TOOLS_WITH_BOTH_KEYS), (
        "the set of librarian tools that can form a per-user jail changed: " + repr(sorted(both))
    )


def test_reading_reaches_exactly_one_visible_skill_further_than_writing():
    """THE security-relevant consequence of the conversion, held by a test instead of a
    comment.

    Before, one outer write-mode jail governed all thirteen. Now each declaring tool installs
    its own, and read mode deliberately reaches further - by the folders of the skills this
    user may see. It has to be forced here: on a machine where the scope sees no skills the
    delta is EMPTY, so a casual re-measurement would show zero and read as a refutation. What
    is pinned is the mechanism, not one machine's data.
    """
    from vaf.tools.filesystem import compute_user_jail

    skill = pathlib.Path("/tmp/vaf-test-skill-folder")
    with patch("vaf.tools.filesystem._visible_skill_roots", return_value=[skill]):
        write = compute_user_jail(SCOPE, "user", mode="write")
        read = compute_user_jail(SCOPE, "user", mode="read")

    extra = {str(r) for r in read["allowed_roots"]} - {str(r) for r in write["allowed_roots"]}
    assert extra == {str(skill)}, (
        "read mode no longer reaches exactly the visible skills further than write mode; "
        "the librarian conversion's stated widening is wrong in one direction or the other"
    )
    assert not ({str(r) for r in write["allowed_roots"]}
                - {str(r) for r in read["allowed_roots"]}), "writing gained a root reading lacks"


def test_writing_does_not_widen():
    """The other half, stated separately because it is the one that would be a real
    escalation: a shared skill may be READ, never overwritten."""
    from vaf.tools.filesystem import compute_user_jail

    skill = pathlib.Path("/tmp/vaf-test-skill-folder")
    with patch("vaf.tools.filesystem._visible_skill_roots", return_value=[skill]):
        write = compute_user_jail(SCOPE, "user", mode="write")
    assert str(skill) not in {str(r) for r in write["allowed_roots"]}


def test_a_session_id_reaches_the_pipeline():
    """Wiring only. Named separately from what it enables, because the two are different
    claims and an earlier version of this test asserted the wiring under a name that promised
    the effect."""
    cfg = _configured_with()
    assert "session_id" in cfg, "no session id reaches the pipeline; the channel check is dead here"


def test_only_the_generic_sentinel_can_fire_without_a_source():
    """The precise semantics, both directions, because the generous reading is wrong.

    `evaluate_tool_policy` intersects a tool's `channel_restrictions` with
    `{"channel"} | {source}`. The librarian passes no source - it is only reachable off the
    injected agent - so a NAMED restriction cannot match, and someone adding `("telegram",)`
    to a librarian tool would be wrong to assume it applies. The generic sentinel does match.
    """
    from vaf.core.tool_dispatch import ToolCaller

    def _blocked(restrictions):
        tool = _Recorder("probe", declares=())
        tool.channel_restrictions = restrictions
        with patch("vaf.core.config.Config.get",
                   side_effect=lambda k, d=None: False if k == "channel_tools_unrestricted" else d):
            ToolCaller({"probe": tool}, session_id="telegram_9001").execute("probe", {})
        return tool.seen is None

    assert _blocked(("channel",)) is True, "the generic sentinel no longer fires without a source"
    assert _blocked(("telegram",)) is False, (
        "a named restriction now fires without a source - either a source is being passed "
        "after all, or the policy's intersection changed; either way this comment is stale"
    )


def test_the_stage_is_wired_but_currently_decides_nothing_here():
    """Honesty about reach: none of the thirteen carries a channel restriction, and the two
    tools that carry the generic sentinel are not among them. If that changes, the comment in
    librarian.py claiming the stage is idle stops being true."""
    import importlib
    import inspect
    import pkgutil
    import re

    import vaf.tools

    src = pathlib.Path(inspect.getfile(LibrarianTool)).read_bytes().decode()
    block = re.search(r"self\.tools\s*=\s*\{(.*?)\n\s*\}", src, re.S)
    registered = set(re.findall(r'"([a-z_]+)":', block.group(1)))

    with_restrictions = {}
    for mod in pkgutil.iter_modules(vaf.tools.__path__):
        try:
            module = importlib.import_module("vaf.tools." + mod.name)
        except Exception:
            continue
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, BaseTool) and getattr(cls, "name", None) in registered:
                restr = tuple(getattr(cls, "channel_restrictions", ()) or ())
                if restr:
                    with_restrictions[cls.name] = restr
    assert with_restrictions == {}, (
        "a librarian tool gained channel_restrictions: " + repr(with_restrictions)
        + " - check whether it is the generic sentinel (fires) or a named source (does NOT "
          "fire here, because no source is passed), and update the comment in librarian.py"
    )


def test_the_session_comes_from_the_contextvar_not_from_the_instance():
    """It must be per-call. Parking it on the shared tool instance would be the same
    cross-user leak the identity avoids."""
    import inspect

    src = inspect.getsource(LibrarianTool._make_caller)
    assert "get_current_session_id()" in src
    assert "self._session_id" not in src
    # Asked of the AST, not of the text: the factory's DOCSTRING names `self._caller` in order
    # to warn against it, so a substring check reports the warning as the offence. (It did.)
    import ast, textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(LibrarianTool)))
    parked = [n for n in ast.walk(tree)
              if isinstance(n, ast.Attribute) and n.attr == "_caller"
              and isinstance(n.value, ast.Name) and n.value.id == "self"]
    assert not parked, (
        "the caller was parked on the shared tool instance; agent.tools is built once per "
        "process and serves every user, so it would outlive the turn that set it"
    )
