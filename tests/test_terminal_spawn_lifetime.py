# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A spawned terminal window must close when the work succeeded, and only then.

Live incident 2026-07-20: a sub-agent window opened and never closed. Two stacked faults.

(a) Every Linux emulator was invoked as `bash -c "<command>; exec bash"`. `exec bash`
    replaces the finished child with an interactive shell, so the window could not close no
    matter what the child did. The auto-close countdown worked perfectly: the terminal showed
    "[OK] Terminal closing." and then a shell prompt.

(b) The window should not have been visible at all. The piped WebUI branch was chosen from
    the process-global VAF_WEBUI_ACTIVE, which was set on WebSocket connect and cleared when
    the last connection dropped. The browser socket had died two minutes earlier, so a
    web-launched sub-agent opened a host terminal window.

Removing the shell tail alone would have been a regression, not a fix: a FAILING run's window
would then vanish before the error could be read, and `--no-auto-close` (a documented flag)
worked only BECAUSE of `exec bash`. Holding the window is therefore the child's job now, in
Python, so Windows, macOS and Linux behave identically. A shell tail could not do that: macOS
`do script` runs the user's login shell, zsh since Catalina, where the bash `read -n1 -p`
idiom errors out, and the Windows branch has no tail at all.
"""
import ast
from pathlib import Path

import pytest

from vaf.core.platform import Platform

_REPO = Path(__file__).resolve().parents[1]


def _source(rel: str) -> str:
    # read_bytes().decode("utf-8"): vaf/core/platform.py is not cp1252-decodable, so a bare
    # read_text() would pass on Linux and fail on the Windows CI runner only. The CRLF
    # normalisation is the second half of the same trap: git can check the file out with
    # CRLF there, and a pattern like ")\n" then matches nothing.
    return (_REPO / rel).read_bytes().decode("utf-8").replace("\r\n", "\n")


def _command_strings(rel: str):
    """Every string literal in the module that looks like a spawn command line."""
    tree = ast.parse(_source(rel))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append("".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            ))
    return out


def test_no_spawn_keeps_a_shell_alive_after_the_command_exits():
    """The exact defect: a tail that outlives the child. Comments are excluded by
    construction (this reads string literals from the AST), which matters because
    platform.py DESCRIBES the old behaviour in prose right next to the fixed code."""
    offenders = [
        s for s in _command_strings("vaf/core/platform.py")
        if "exec bash" in s or "exec $SHELL" in s or "exec zsh" in s
    ]
    assert not offenders, (
        "A terminal spawn re-execs a shell after the command, so the window can never "
        f"close: {offenders}"
    )


def test_windows_still_closes_its_window_and_was_not_touched():
    """cmd /c terminates with the command; /k would keep it open. The Windows branch is
    correct as it stands and this change must not have edited it."""
    literals = _command_strings("vaf/core/platform.py")
    assert any("cmd /c" in s for s in literals), "the Windows branch must still use cmd /c"
    assert not any("cmd /k" in s.lower() for s in literals), "cmd /k keeps the window open"


def test_auto_close_lives_in_exactly_one_place():
    """It was duplicated in two CLI modules (Rule 2). Two copies of "when may I close the
    user's window" is two chances to get the failure case wrong."""
    offenders = []
    for rel in ("vaf/cli/cmd/subagent.py", "vaf/cli/cmd/workflow.py"):
        src = _source(rel)
        if "from vaf.cli.autoclose import" not in src:
            offenders.append(f"{rel} does not use the shared implementation")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.FunctionDef) and "auto_close" in node.name:
                offenders.append(f"{rel}:{node.lineno} still defines {node.name}")
    assert not offenders, "\n".join(offenders)


def test_failure_holds_the_window_and_success_closes_it(monkeypatch, capsys):
    """The contract that makes removing `exec bash` safe."""
    import io

    from vaf.cli import autoclose

    monkeypatch.setattr(autoclose, "_is_interactive_window", lambda: True)
    monkeypatch.setattr(autoclose.sys, "stdin", io.StringIO("\n"))

    with pytest.raises(SystemExit) as failed:
        autoclose.finish_terminal(success=False)
    assert failed.value.code == 1
    assert "Press Enter to close" in capsys.readouterr().out

    monkeypatch.setattr(autoclose.time, "sleep", lambda _s: None)
    with pytest.raises(SystemExit) as ok:
        autoclose.finish_terminal(success=True, delay=1)
    assert ok.value.code == 0
    assert "Terminal closing" in capsys.readouterr().out


def test_no_auto_close_still_holds_the_window(monkeypatch, capsys):
    """--no-auto-close is documented ("Don't auto-close terminal"). It used to work ONLY as
    a side effect of `exec bash`: the child skipped the countdown, exited, and the appended
    shell kept the window. Remove the tail without this and the flag would do the OPPOSITE of
    what it promises."""
    import io

    from vaf.cli import autoclose

    monkeypatch.setattr(autoclose, "_is_interactive_window", lambda: True)
    monkeypatch.setattr(autoclose.sys, "stdin", io.StringIO("\n"))
    with pytest.raises(SystemExit) as e:
        autoclose.finish_terminal(success=True, no_auto_close=True)
    assert e.value.code == 0
    assert "Press Enter to close" in capsys.readouterr().out


def test_a_piped_child_exits_at_once(monkeypatch, capsys):
    """In WebUI mode there is no window to hold: the countdown's output is forwarded into
    the browser console as spam, and a late exit used to overlap the next workflow step.

    The predicate is asserted DIRECTLY against a stdout that claims to be a TTY. Asserting
    only on finish_terminal() under pytest would be a tautology: capsys replaces stdout with
    something whose isatty() is already False, so the test would pass even with both env
    checks deleted."""
    from vaf.cli import autoclose

    class _RealTty:
        def isatty(self):
            return True

    monkeypatch.setattr(autoclose.sys, "stdout", _RealTty())

    monkeypatch.setenv("VAF_SPAWN_MODE", "piped")
    monkeypatch.delenv("VAF_WEBUI_ACTIVE", raising=False)
    assert autoclose._is_interactive_window() is False, "an explicit piped spawn has no window"

    monkeypatch.delenv("VAF_SPAWN_MODE", raising=False)
    monkeypatch.setenv("VAF_WEBUI_ACTIVE", "1")
    assert autoclose._is_interactive_window() is False, "a web-UI process has no window"

    # An EXPLICIT terminal spawn wins over the ambient flag: the spawner knows what it opened,
    # while VAF_WEBUI_ACTIVE only says the PARENT serves a web UI and is inherited by
    # everything it starts. Ambient-first would make such a window unable to hold on failure.
    monkeypatch.setenv("VAF_SPAWN_MODE", "terminal")
    assert autoclose._is_interactive_window() is True

    monkeypatch.setenv("VAF_SPAWN_MODE", "piped")
    with pytest.raises(SystemExit) as e:
        autoclose.finish_terminal(success=False, no_auto_close=True)
    assert e.value.code == 1
    assert capsys.readouterr().out == "", "a piped child must stay silent"


def test_every_uvicorn_entry_point_declares_the_process_serves_a_web_ui():
    """THE blocker adversarial review found in this change: the desktop app does NOT go
    through web_server.run_server. vaf/tray.py has its own start_uvicorn that imports the app
    and drives uvicorn itself, so putting the declaration only in run_server left the fix
    inert on exactly the path where the incident happened."""
    offenders = []
    for rel in ("vaf/core/web_server.py", "vaf/tray.py"):
        src = _source(rel)
        if "uvicorn" not in src:
            continue
        if "mark_webui_process()" not in src:
            offenders.append(rel)
    assert not offenders, (
        "These modules serve the web app but never declare it, so sub-agents spawned from "
        "them can fall back to opening host terminal windows: " + ", ".join(offenders)
    )


def test_the_automation_terminal_child_owns_its_window():
    """`vaf automation run <id>` is what run_task spawns into a terminal window. It used to
    be held open by the `; exec bash` tail; without that it must hold its own window, or a
    manual automation run flashes past unreadably."""
    src = _source("vaf/core/automation.py")
    run_cmd = src[src.index('@automation_app.command("run")'):]
    run_cmd = run_cmd[:run_cmd.index("@automation_app.command", 10)]
    assert "finish_terminal" in run_cmd


def test_per_spawn_values_are_embedded_in_the_command():
    """extra_env was silently discarded by all three real-terminal branches (Popen without
    env=), so a terminal child lost VAF_SESSION_ID - for which no CLI flag exists. env= alone
    cannot fix it: on macOS the window is opened by an Apple event to the already-running
    Terminal.app, whose shell inherits ITS environment, and on Linux gnome-terminal is a
    D-Bus client of a long-lived server."""
    cmd = Platform._with_env_prefix("python -m vaf.main subagent run x", {
        "VAF_SESSION_ID": "sess-a", "VAF_TASK_ID": "t1",
    })
    assert cmd.startswith("env ")
    assert "VAF_SESSION_ID=sess-a" in cmd and "VAF_TASK_ID=t1" in cmd
    assert cmd.endswith("python -m vaf.main subagent run x")
    # No values, no wrapper.
    assert Platform._with_env_prefix("cmd", {}) == "cmd"


def test_env_prefix_quotes_hostile_values():
    """A value reaches a shell command line, so it must be quoted, not interpolated."""
    cmd = Platform._with_env_prefix("run", {"VAF_SESSION_ID": "a b; rm -rf /"})
    assert "; rm -rf /" not in cmd.replace("'a b; rm -rf /'", "")
    assert "'a b; rm -rf /'" in cmd


def test_spawn_mode_can_be_decided_per_spawn():
    """The piped-vs-window decision must not hang on ambient process state that a transient
    socket drop can flip."""
    src = _source("vaf/core/platform.py")
    assert 'VAF_SPAWN_MODE' in src
    assert '_ee.get("VAF_WEBUI_ACTIVE") or os.environ.get("VAF_WEBUI_ACTIVE", "")' in src


def test_the_webui_flag_is_not_cleared_when_a_browser_disconnects():
    """It means "this process serves a web UI", not "a browser is attached right now". The
    old meaning let a transient socket drop change process-wide spawn behaviour."""
    src = _source("vaf/core/web_server.py")
    assert 'os.environ.pop("VAF_WEBUI_ACTIVE"' not in src
    assert 'os.environ["VAF_WEBUI_ACTIVE"] = "1"' in src


def test_a_piped_child_is_told_its_mode_explicitly():
    """It used to inherit the flag by luck from os.environ.copy(). A grandchild spawn must
    make the same piped choice, and the child's auto-close must know it has no window."""
    src = _source("vaf/core/platform.py")
    assert 'env["VAF_SPAWN_MODE"] = "piped"' in src
    assert 'env["VAF_WEBUI_ACTIVE"] = "1"' in src
