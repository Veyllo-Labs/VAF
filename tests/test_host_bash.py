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


def _force_channel_restricted(monkeypatch):
    """Force channel_tools_unrestricted OFF so the POLICY-layer channel block is active.

    Its default is ON (channels get the main agent's tools), which lifts
    channel_restrictions at the policy layer - so this must be pinned or the assertion
    depends on the machine's stored config (green locally, red on a fresh CI checkout).
    The default-ON path (where the policy's refusal of a tool that needs a person is what
    protects host_bash) is covered by test_the_channel_refusal_holds_with_the_admin_lift_on.
    """
    import vaf.core.config as cfg
    real = cfg.Config.get.__func__
    monkeypatch.setattr(
        cfg.Config, "get",
        classmethod(lambda cls, k, d=None: False if k == "channel_tools_unrestricted" else real(cls, k, d)),
    )


def test_hard_blocked_on_remote_channels(monkeypatch):
    _force_channel_restricted(monkeypatch)
    for src in ("telegram", "whatsapp", "discord"):
        d = _policy(src, True)
        assert d.blocked, f"policy must block host_bash on {src} when channel_tools_unrestricted is off"


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
    assert "channel" in t.channel_restrictions, "blocked on every chat channel, a later one included"


def test_blocklist_reisleine():
    t = HostBashTool()
    assert "BLOCKED" in t.run(command="rm -rf /")
    assert "BLOCKED" in t.run(command=":(){ :|:& };:")  # fork bomb
    assert "BLOCKED" in t.run(command="sudo rm -rf /etc")


def test_runs_a_real_host_command():
    out = HostBashTool().run(command="echo host-ok")
    assert "host-ok" in out and "HOST EXECUTION" in out


def _call_from(source, *, gate_enabled=True):
    """host_bash through the framework funnel, with the shipped default: the admin's
    channel_tools_unrestricted ON, which lifts channel_restrictions for the other tools."""
    from unittest.mock import patch

    from vaf.core.tool_dispatch import ToolCaller
    caller = ToolCaller({"host_bash": HostBashTool()}, source=source,
                        session_id=f"{source}_9001" if source != "web" else "web_local",
                        interactive=False, gate_enabled=gate_enabled,
                        user_scope_id=None, user_role="admin")
    with patch("vaf.core.trust.get_tool_policy", return_value="allow"):
        return caller.execute("host_bash", {"command": "echo host-ok"})


def test_the_channel_refusal_holds_with_the_admin_lift_on():
    """The admin's lift (default ON) also lifts the confirmation, and a channel cannot show
    one: the policy refuses a tool that needs a person there BEFORE the lift, even with a
    stored "always". The command must never run."""
    for source in ("telegram", "whatsapp", "discord"):
        out = _call_from(source)
        assert out.startswith("Security Error") and "messaging channels" in out, out
        assert "host-ok" not in out, f"the command ran on {source}"


def test_a_local_session_runs_normally():
    assert "host-ok" in _call_from("web")


def test_the_unattended_lanes_are_not_asked_and_not_refused_by_it():
    """The coder and workflow steps run host_bash unattended by decision; a coder started
    from a chat may still build. The rule protects the turn where somebody would be asked."""
    assert "host-ok" in _call_from("telegram", gate_enabled=False)


def test_empty_command():
    assert "no command" in HostBashTool().run(command="   ").lower()


def test_auto_discovered_by_the_tool_loader():
    # The main agent's _load_tools scans vaf/tools/ for BaseTool subclasses; host_bash must
    # be importable and named so it is registered without manual wiring.
    import importlib
    mod = importlib.import_module("vaf.tools.host_bash")
    assert getattr(mod, "HostBashTool").name == "host_bash"
