# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Runtime diagnostics from the tool-loading family go through UI.event.

These functions run DURING a session, not only at boot: the per-turn
`_maybe_refresh_dynamic_tools` hot-reloads tool files, `reload_custom_tools`
fires when the agent builds a tool mid-turn, and `reload_api_backend` runs on
a provider switch from the settings screen. A raw `print()` from any of them
lands under the terminal app's alternate screen and corrupts the display -
the funnel routes the same line to the app's console sink instead (and to the
plain styled console everywhere else, which also gains the web-log mirror).

Boot-only and shutdown-only prints elsewhere in agent.py stay prints on
purpose: boot runs before the app takes the screen, shutdown after it
returns it, so the plain terminal carries them by design.
"""
import inspect
import re
import sys
import threading
from types import SimpleNamespace

import pytest

from vaf.core.agent import Agent


# The functions that can emit while a session is live. Any print() creeping
# back into one of them is the exact defect this round removed.
_FUNNELED = (
    "reload_api_backend",
    "_load_tools",
    "_load_custom_tools",
    "_load_entry_point_tools",
    "_load_mcp_tools",
    "reload_custom_tools",
    "_maybe_refresh_dynamic_tools",
)


def test_the_tool_loading_family_never_prints():
    for name in _FUNNELED:
        src = inspect.getsource(getattr(Agent, name))
        code_lines = [ln for ln in src.splitlines()
                      if not ln.strip().startswith("#")]
        offenders = [ln.strip() for ln in code_lines
                     if re.search(r"\bprint\(", ln)]
        assert not offenders, (
            f"{name} prints to raw stdout again - under the alternate screen "
            f"that corrupts the app: {offenders}")


@pytest.fixture
def sink(monkeypatch):
    """A registered console sink in app mode, with the web bridge stubbed so
    the test never touches the live web-interface singleton."""
    from vaf.cli.tui import UI

    monkeypatch.delenv("VAF_IN_WORKFLOW_TERMINAL", raising=False)
    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    import vaf.core.web_interface as web_mod
    monkeypatch.setattr(web_mod, "get_web_interface",
                        lambda: SimpleNamespace(log=lambda *a, **k: None))

    events = []

    def _sink(type_name, title, style):
        events.append((type_name, title, style))

    UI.add_console_sink(_sink)
    UI.set_app_mode(True)
    yield events
    UI.set_app_mode(False)
    UI.remove_console_sink(_sink)


def test_a_broken_custom_registry_reaches_the_sink_not_stdout(sink, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "vaf.core.custom_tools_registry", None)
    dummy = SimpleNamespace(tools={})
    Agent.reload_custom_tools(dummy)
    assert any(t == "Warning" and "registry unavailable" in m
               for t, m, s in sink), sink
    assert capsys.readouterr().out == "", "the warning leaked to raw stdout"


def test_a_failed_per_turn_refresh_reaches_the_sink_not_stdout(sink, capsys):
    def _boom():
        raise RuntimeError("kaputt")

    dummy = SimpleNamespace(
        _tools_fs_last_check=0.0,
        _tools_fs_sig="old",
        _tools_fs_signature=lambda: "new",
        reload_builtin_tools=_boom,
        reload_custom_tools=_boom,
    )
    Agent._maybe_refresh_dynamic_tools(dummy)
    warnings = [m for t, m, s in sink if t == "Warning"]
    assert any("built-in reload failed" in m for m in warnings), sink
    assert any("custom reload failed" in m for m in warnings), sink
    assert capsys.readouterr().out == "", "the warning leaked to raw stdout"


def test_a_verbose_backend_build_failure_reaches_the_sink_not_stdout(sink, monkeypatch, capsys):
    import vaf.core.agent as agent_mod

    monkeypatch.delenv("VAF_PROVIDER", raising=False)
    monkeypatch.setattr(agent_mod.Config, "load",
                        classmethod(lambda cls: {"provider": "veyllo"}))

    def _no_build(provider):
        raise RuntimeError("no key")

    dummy = SimpleNamespace(
        verbose=True,
        provider="local",
        _config_overrides=None,
        _backend_swap_lock=threading.Lock(),
        _build_api_backend=_no_build,
    )
    assert Agent.reload_api_backend(dummy) is False
    assert any(t == "Debug" and "cannot build 'veyllo'" in m
               for t, m, s in sink), sink
    assert capsys.readouterr().out == "", "the debug line leaked to raw stdout"
