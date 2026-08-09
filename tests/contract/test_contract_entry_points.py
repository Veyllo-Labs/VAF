# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: the `vaf.tools` entry-point group (docs/EMBEDDING.md, "Shipping
tools as a pip package (entry points)").

The documented promise: a third-party pip package publishing a BaseTool
subclass under the `vaf.tools` group is discovered at agent startup; each
entry point must resolve to a BaseTool subclass; a broken package logs an
error and is skipped - it never breaks startup.

Technique: the documented behavior is startup-time discovery, but
constructing an agent is not offline-safe (it reads and writes the real home
and platform config directories). So this suite drives the loader UNBOUND on
a duck-typed holder - CoreAgent._load_entry_point_tools(SimpleNamespace(
tools={})) - and monkeypatches importlib.metadata.entry_points: the loader
does `from importlib.metadata import entry_points` AT CALL TIME, so the
module-attribute patch reaches it. The private method name
`_load_entry_point_tools` is the ONE internal seam this suite touches; if it
moves, this file must move with it.
"""
from types import SimpleNamespace

import pytest

import vaf


@pytest.fixture(autouse=True)
def _ui_event_stays_local(monkeypatch):
    """The loader reports skips through the UI event funnel, which diverts
    into IPC/web-interface lanes when these vars are set (a parent VAF
    process would leak them in); unset, UI.event only prints."""
    monkeypatch.delenv("VAF_IN_WORKFLOW_TERMINAL", raising=False)
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)


class _EntryPoint:
    """Duck-typed entry point: the loader touches only .name and .load()."""

    def __init__(self, name, obj=None, load_error=None):
        self.name = name
        self._obj = obj
        self._load_error = load_error

    def load(self):
        if self._load_error is not None:
            raise self._load_error
        return self._obj


class _GoodTool(vaf.BaseTool):
    name = "ep_alpha_tool"
    description = "contract-suite entry-point tool"

    def run(self, **kwargs):
        return "ok"


class _CoderOnlyTool(vaf.BaseTool):
    name = "ep_coder_tool"
    description = "skipped: targets the coder, not the main agent"
    coder_only = True

    def run(self, **kwargs):
        return "ok"


class _NotATool:
    """Resolves fine but is not a BaseTool subclass - must be skipped."""


class _BrokenInitTool(vaf.BaseTool):
    name = "ep_broken_init"
    description = "a valid subclass whose zero-arg construction fails"

    def __init__(self):
        raise RuntimeError("synthetic construction failure")

    def run(self, **kwargs):
        return "unreachable"


def _loader():
    """The unbound loader, or an explicit explanation of what actually broke.

    Renaming this private method is NOT a breaking change for embedders, so a
    bare AttributeError here would be a false alarm. Say so where the reader
    sees it: the group and the discovery promise are the contract, this method
    is only how the suite reaches them without constructing an agent.
    """
    loader = getattr(vaf.CoreAgent, "_load_entry_point_tools", None)
    if loader is None:
        pytest.fail(
            "the internal seam CoreAgent._load_entry_point_tools is gone. This "
            "is a test-harness break, NOT necessarily a contract break: check "
            "whether the 'vaf.tools' entry-point group is still discovered at "
            "agent startup, then re-point this file at the new seam."
        )
    return loader


def _run_loader(monkeypatch, eps, tools=None):
    """Patch the discovery seam and drive the loader on a bare holder."""
    seen_groups = []

    def fake_entry_points(group=None, **kwargs):
        seen_groups.append(group)
        return list(eps)

    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)
    holder = SimpleNamespace(tools=dict(tools or {}))
    result = _loader()(holder)
    return holder, result, seen_groups


def test_the_loader_queries_the_group_named_vaf_tools_exactly(monkeypatch):
    """The group NAME is the contract an embedder writes into their
    pyproject: [project.entry-points."vaf.tools"]. Any other spelling would
    orphan every published tool package."""
    _, _, seen_groups = _run_loader(monkeypatch, [])
    assert seen_groups == ["vaf.tools"]


def test_a_tool_registers_under_the_instances_name_not_the_entry_point_name(monkeypatch):
    """Documented registration key: the tool's own .name attribute. The
    entry-point name is only a label in the publisher's metadata."""
    holder, _, _ = _run_loader(
        monkeypatch, [_EntryPoint("get_weather", _GoodTool)]
    )
    assert list(holder.tools) == ["ep_alpha_tool"]
    assert isinstance(holder.tools["ep_alpha_tool"], _GoodTool)
    assert holder.tools["ep_alpha_tool"].run() == "ok"


def test_bad_entries_are_skipped_and_a_later_good_entry_still_registers(monkeypatch):
    """The skip matrix in one pass, with the good tool LAST so the pin also
    proves the loop continues past every failure shape: coder_only=True is
    skipped silently, a non-BaseTool class is skipped, and an entry point
    whose load() raises is skipped."""
    holder, _, _ = _run_loader(
        monkeypatch,
        [
            _EntryPoint("coder", _CoderOnlyTool),
            _EntryPoint("bad_class", _NotATool),
            _EntryPoint("bad_load", load_error=RuntimeError("synthetic load failure")),
            _EntryPoint("good", _GoodTool),
        ],
    )
    assert list(holder.tools) == ["ep_alpha_tool"]


def test_a_raising_load_never_breaks_startup(monkeypatch):
    """EMBEDDING.md: 'a broken package logs an error and is skipped - it
    never breaks startup'. The loader call itself must return normally."""
    holder, result, _ = _run_loader(
        monkeypatch,
        [_EntryPoint("broken", load_error=RuntimeError("synthetic load failure"))],
        tools={"pre_existing": object()},
    )
    assert result is None
    assert list(holder.tools) == ["pre_existing"]


def test_a_raising_tool_constructor_never_breaks_startup(monkeypatch):
    """Same promise one step later: the entry point resolves to a valid
    BaseTool subclass, but its zero-arg construction fails."""
    holder, result, _ = _run_loader(
        monkeypatch,
        [_EntryPoint("broken_init", _BrokenInitTool)],
        tools={"pre_existing": object()},
    )
    assert result is None
    assert list(holder.tools) == ["pre_existing"]


def test_a_raising_entry_points_query_leaves_the_tools_untouched(monkeypatch):
    """Total discovery failure: entry_points() itself raising must not
    escape, and the holder's registry stays exactly as it was.

    The failure is deliberately NOT a TypeError: TypeError is the loader's
    defensive pre-3.10 signature fallback, explicitly not contract."""

    def exploding_entry_points(group=None, **kwargs):
        raise RuntimeError("synthetic metadata failure")

    monkeypatch.setattr("importlib.metadata.entry_points", exploding_entry_points)
    sentinel = object()
    holder = SimpleNamespace(tools={"pre_existing": sentinel})
    result = _loader()(holder)
    assert result is None
    assert holder.tools == {"pre_existing": sentinel}
