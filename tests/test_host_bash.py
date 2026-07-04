# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Main-agent host shell (host_bash).

Unlike the coder's kernel-jailed bash, host_bash runs UNSANDBOXED on the host so the
main agent can do host/docker tasks. Its safety is (1) permission_level "dangerous" ->
the framework confirmation gate asks the user before each run, and (2) channel_restrictions
-> hard-blocked on remote channels (Telegram/WhatsApp/Discord) where no confirmation can
be shown. These tests pin both, the blocklist reisleine, and that it is a main-agent tool.
"""
from vaf.tools.host_bash import HostBashTool
from vaf.core.tool_contract import evaluate_tool_policy


def _policy(src, is_channel):
    return evaluate_tool_policy(
        tool_name="host_bash", tool=HostBashTool(),
        current_source=src, is_channel_session=is_channel, is_admin=True,
    )


def test_hard_blocked_on_remote_channels():
    for src in ("telegram", "whatsapp", "discord"):
        d = _policy(src, True)
        assert d.blocked, f"host_bash must be hard-blocked on {src}"


def test_local_requires_confirmation_not_blocked():
    d = _policy("web", False)
    assert not d.blocked
    assert d.requires_confirmation, "host_bash must require confirmation locally"
    d_cli = _policy("cli", False)
    assert not d_cli.blocked and d_cli.requires_confirmation


def test_is_a_dangerous_main_agent_tool():
    t = HostBashTool()
    assert t.permission_level == "dangerous"
    assert t.coder_only is False  # main agent, not the coder
    assert set(("telegram", "whatsapp", "discord")).issubset(set(t.channel_restrictions))


def test_blocklist_reisleine():
    t = HostBashTool()
    assert "BLOCKED" in t.run(command="rm -rf /")
    assert "BLOCKED" in t.run(command=":(){ :|:& };:")  # fork bomb
    assert "BLOCKED" in t.run(command="sudo rm -rf /etc")


def test_runs_a_real_host_command():
    out = HostBashTool().run(command="echo host-ok")
    assert "host-ok" in out and "HOST EXECUTION" in out


def test_channel_guard_is_non_liftable():
    # channel_restrictions is lifted when channel_tools_unrestricted is ON (fresh-install
    # default), so host_bash must ALSO refuse at the tool level when execute_tool injects the
    # authoritative is_channel_session. The real command must never run on a channel.
    out = HostBashTool().run(command="echo host-ok", _is_channel_session=True)
    assert "BLOCKED" in out and "remote" in out.lower()
    assert "host-ok" not in out, "the command must not execute on a channel session"


def test_local_session_runs_normally():
    # Explicit non-channel flag (what execute_tool injects for a Web/CLI session) still runs.
    out = HostBashTool().run(command="echo host-ok", _is_channel_session=False)
    assert "host-ok" in out


def test_empty_command():
    assert "no command" in HostBashTool().run(command="   ").lower()


def test_auto_discovered_by_the_tool_loader():
    # The main agent's _load_tools scans vaf/tools/ for BaseTool subclasses; host_bash must
    # be importable and named so it is registered without manual wiring.
    import importlib
    mod = importlib.import_module("vaf.tools.host_bash")
    assert getattr(mod, "HostBashTool").name == "host_bash"
