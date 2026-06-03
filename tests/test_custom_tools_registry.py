"""Regression test for the custom-tools manifest lock.

The manifest mutators (register_tool / delete_tool / update_tool_source /
update_tool_permissions) hold `_manifest_lock` to make their read-modify-write
atomic, and call `load_manifest()` — which re-acquires the same lock — from
inside that critical section. When `_manifest_lock` was a plain threading.Lock,
that re-acquisition deadlocked the worker thread forever. Because the lock is
process-global and the deadlocked thread never released it, the zombie poisoned
every later manifest operation too (each one then hit the agent's 120s tool
timeout). That is exactly what bricked tool creation: create_agent_tool wrote the
.py file, deadlocked in register_tool, got abandoned on timeout, and the new tool
was never registered ("Unknown tool"). The lock must be an RLock.

These tests must each finish well under a second; a regression makes them hang
until the watchdog timeout fires.
"""
import threading

import pytest

from vaf.core import custom_tools_registry as registry


_TOOL_CODE = (
    "from vaf.tools.base import BaseTool\n"
    "class ProbeTool(BaseTool):\n"
    "    name = 'probe_tool'\n"
    "    description = 'regression probe'\n"
    "    permission_level = 'read'\n"
    "    side_effect_class = 'none'\n"
    "    def run(self, **kwargs):\n"
    "        return 'ok'\n"
)


@pytest.fixture()
def isolated_registry(tmp_path, monkeypatch):
    """Point the registry at a throwaway dir so tests never touch real data."""
    monkeypatch.setattr(registry, "get_custom_tools_dir", lambda: tmp_path)
    return tmp_path


def _run_without_deadlock(fn, timeout=5.0):
    """Run *fn* on a daemon thread; fail if it does not return in *timeout* s."""
    err: dict = {}

    def _target():
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 — surfaced to the caller
            err["exc"] = exc

    th = threading.Thread(target=_target, daemon=True)
    th.start()
    th.join(timeout=timeout)
    assert not th.is_alive(), (
        "manifest operation deadlocked — `_manifest_lock` must be a reentrant RLock"
    )
    if "exc" in err:
        raise err["exc"]


def test_manifest_lock_is_reentrant():
    # The whole bug reduces to this: a mutator re-acquires the lock while holding it.
    assert isinstance(registry._manifest_lock, type(threading.RLock()))


def test_register_tool_does_not_deadlock(isolated_registry):
    def go():
        registry.save_tool_file("probe_tool.py", _TOOL_CODE)
        registry.register_tool(
            tool_name="probe_tool",
            filename="probe_tool.py",
            created_by="agent",
            shared_with=[],
        )

    _run_without_deadlock(go)
    assert "probe_tool" in registry.get_all_custom_tool_names()
    assert registry.load_custom_tool_class("probe_tool") is not None


def test_all_mutators_do_not_deadlock(isolated_registry):
    """register → update_source → update_permissions → delete, all under the lock."""
    def go():
        registry.save_tool_file("probe_tool.py", _TOOL_CODE)
        registry.register_tool(
            tool_name="probe_tool",
            filename="probe_tool.py",
            created_by="agent",
            shared_with=[],
        )
        registry.update_tool_source(
            "probe_tool", _TOOL_CODE.replace("'ok'", "'ok2'"), updated_by="agent"
        )
        registry.update_tool_permissions("probe_tool", ["*"])
        registry.delete_tool("probe_tool")

    _run_without_deadlock(go)
    assert "probe_tool" not in registry.get_all_custom_tool_names()
