# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What the coder asks before it runs an inner tool - and what it deliberately does not.

MEASURED BEFORE THE FIX. The coder dispatched with a bare ``tool.run()`` and checked one
thing, the account allowlist. Its own allowlist only shaped the schema, so any discovered
tool ran when the model named it - including the admin-only ``create_agent_tool``. And the
continuation run after an incomplete result was started with no identity at all, which
reads as an unrestricted account.

WHAT HOLDS NOW, each pinned below:
- a name outside the coder allowlist is refused at dispatch, not only kept out of sight;
- the declarative policy (admin_only, channel restrictions) is applied;
- the account allowlist still decides whether an account has a tool at all.

WHAT IS DELIBERATELY NOT ASKED: a confirmation. The coder runs unattended and uses its
tools at full strength - host_bash included, also for a chat that started on a messaging
channel - for an account that may use them. A coder that stops to ask for a local build is
the friction this lane exists to remove; whether an account has host_bash is the account
allowlist's answer, exactly as for a workflow step.
"""
import ast

import pytest

from vaf.core import trust
from vaf.tools.coder import _as_the_caller, _coder_dispatch_refusal

SCOPE = "ab12cd34-0000-4000-8000-000000000001"
OTHER_SCOPE = "ab12cd34-0000-4000-8000-000000000002"
CHAT = "web_chat-1"
CHANNEL_CHAT = "telegram_9001"


@pytest.fixture(autouse=True)
def _clean_trust(monkeypatch):
    """No grant on this machine answers for these tests, and none leaks out of them."""
    monkeypatch.setattr(trust, "_chat_grants", {})
    monkeypatch.delenv(trust.CHAT_GRANTS_ENV, raising=False)
    monkeypatch.setattr("vaf.core.trust.get_tool_policy", lambda n, user_scope_id=None: "ask")
    monkeypatch.setattr("vaf.core.trust.is_trusted_dir", lambda p, user_scope_id=None: False)


def _host_bash():
    from vaf.tools.host_bash import HostBashTool
    return HostBashTool()


def _refusal(name, tool, *, allowed=None, caller_allowed=None, scope=SCOPE, role="user",
             session_id=CHAT):
    return _coder_dispatch_refusal(
        name, tool, coder_allowed=allowed if allowed is not None else {name},
        caller_allowed=caller_allowed, scope=scope, role=role, session_id=session_id,
    )


# ── the coder allowlist is a boundary, not a hint ──────────────────────────────────

def test_a_tool_outside_the_coder_allowlist_is_refused_at_dispatch():
    """The schema filter keeps it out of sight; this is what refuses it when the model
    names it anyway. Asked as the ADMIN, so no later stage (admin_only, the account
    allowlist) could be the one refusing. MUTATION: drop the coder-allowlist check - red."""
    from vaf.tools.agent_tool_builder import AgentToolBuilderTool

    out = _refusal("create_agent_tool", AgentToolBuilderTool(), allowed={"read_file"},
                   scope=None, role="admin")
    assert out is not None and "not a coding-agent tool" in out


def test_an_admin_only_tool_is_refused_for_a_tenant_even_when_allowlisted():
    """The policy stage on its own: allowlisted, and still not for a non-admin."""
    from vaf.tools.agent_tool_builder import AgentToolBuilderTool

    out = _refusal("create_agent_tool", AgentToolBuilderTool(), role="user")
    assert out is not None and out.startswith("Security Error:")
    assert "not a coding-agent tool" not in out


def test_the_account_allowlist_still_refuses():
    out = _refusal("host_bash", _host_bash(), caller_allowed={"read_file"})
    assert out is not None and "not enabled for your account" in out


# ── host_bash, at full strength ────────────────────────────────────────────────────

def test_the_coder_runs_host_bash_without_asking():
    """No grant, no dialog: the lane's decision. MUTATION: put a confirmation gate back
    into _coder_dispatch_refusal - this goes red."""
    assert _refusal("host_bash", _host_bash()) is None


def test_also_for_a_chat_that_started_on_a_messaging_channel():
    """The channel guard protects the chat turn, where somebody would have been asked.
    The coder is not handed the flag, so a coder started from Telegram can still build."""
    assert _refusal("host_bash", _host_bash(), session_id=CHANNEL_CHAT) is None
    tool = _host_bash()
    args = _as_the_caller(tool, {"command": "echo coder-ok"}, scope=SCOPE, role="user")
    assert "_is_channel_session" not in args
    assert "coder-ok" in tool.run(**args)


def test_an_admin_who_keeps_channel_restrictions_on_is_still_obeyed(monkeypatch):
    """channel_restrictions is a policy the admin can keep ON (channel_tools_unrestricted
    = false); the coder must not be the way around it."""
    monkeypatch.setattr("vaf.core.config.Config.get",
                        classmethod(lambda cls, k, d=None:
                                    False if k == "channel_tools_unrestricted" else d))
    out = _refusal("host_bash", _host_bash(), session_id=CHANNEL_CHAT)
    assert out is not None and out.startswith("Security Error:")


def test_the_coders_own_jailed_shell_is_unaffected():
    from vaf.tools.bash import BashTool

    assert _refusal("bash", BashTool()) is None


# ── identity ───────────────────────────────────────────────────────────────────────

def test_a_declared_username_now_arrives_and_is_never_the_model_s():
    """The coder's old copy assigned scope and role only; the framework rule also hands
    over a declared username - resolved from the scope, overwriting what the model wrote."""
    class _NameOnly:
        identity_kwargs = ("username",)

    args = _as_the_caller(_NameOnly(), {"username": "admin"}, scope=OTHER_SCOPE, role="user")
    assert args["username"] and args["username"] != "admin"


# ── python_exec keeps its own check ────────────────────────────────────────────────

def test_python_exec_itself_honours_the_chat_grant_and_nothing_less():
    """python_exec re-checks on its own, because unattended lanes run it without a gate.
    Before, only a stored "always" passed that check, so "for this chat" was offered by the
    dialog and then refused by the tool."""
    from vaf.core.subagent_ipc import session_context
    from vaf.tools.python_exec import PythonExecTool

    with session_context(CHAT):
        refused = PythonExecTool().run(code="print(6*7)", user_scope_id=SCOPE)
        trust.grant_tool_for_chat("python_exec", SCOPE, CHAT)
        ran = PythonExecTool().run(code="print(6*7)", user_scope_id=SCOPE)
        other = PythonExecTool().run(code="print(6*7)", user_scope_id=OTHER_SCOPE)
    assert refused.startswith("[SECURITY]")
    assert "42" in ran
    assert other.startswith("[SECURITY]"), "one person's chat grant ran code for another"


def test_a_child_inherits_the_grants_of_exactly_its_own_chat(monkeypatch):
    monkeypatch.setenv(trust.CHAT_GRANTS_ENV, "python_exec")
    monkeypatch.setenv("VAF_USER_SCOPE_ID", SCOPE)
    monkeypatch.setenv("VAF_SESSION_ID", CHAT)
    assert trust.has_chat_grant("python_exec", SCOPE, CHAT)
    assert not trust.has_chat_grant("python_exec", SCOPE, "web_chat-2")
    assert not trust.has_chat_grant("python_exec", OTHER_SCOPE, CHAT)
    assert not trust.has_chat_grant("host_bash", SCOPE, CHAT)


def test_the_spawn_env_carries_the_chat_grants():
    """The wiring as code, the same AST shape the allowlist transport is pinned with."""
    import vaf.tools.coder as mod

    tree = ast.parse(open(mod.__file__, "rb").read())
    run_fn = next(
        fn
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "CodingAgentTool"
        for fn in node.body
        if isinstance(fn, ast.FunctionDef) and fn.name == "run"
    )
    keys = {
        n.slice.id
        for n in ast.walk(run_fn)
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Name)
    }
    assert "CHAT_GRANTS_ENV" in keys, "the spawn env no longer carries the chat grants"


def test_the_dispatch_loop_uses_both_helpers():
    """Both helpers are only worth something while the loop calls them."""
    import inspect

    import vaf.tools.coder as mod

    src = inspect.getsource(mod.CodingAgentTool.run)
    assert "_coder_dispatch_refusal(" in src
    assert "_as_the_caller(" in src
