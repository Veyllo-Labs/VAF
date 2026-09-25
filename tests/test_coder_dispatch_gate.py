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
- the declarative policy (admin_only, channel restrictions) is applied - by the framework's
  ToolCaller, the pipeline every lane shares, which the coder now runs its inner tools
  through (`_coder_funnel`), rather than by a copy of its questions;
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
from vaf.tools.coder import _coder_dispatch_refusal, _coder_funnel

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


def _run(name, tool, args, *, scope=SCOPE, role="user", session_id=CHAT):
    """What the coder's loop does with a local tool that passed _refusal: the funnel."""
    return _coder_funnel({name: tool}, scope=scope, role=role, session_id=session_id).execute(
        name, dict(args))


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
    """The policy stage on its own: allowlisted (the coder's own questions pass), and still
    not for a non-admin - answered by the funnel the call runs through. MUTATION: run the
    local tool with a bare tool.run() again - red."""
    from vaf.tools.agent_tool_builder import AgentToolBuilderTool

    assert _refusal("create_agent_tool", AgentToolBuilderTool(), role="user") is None
    out = _run("create_agent_tool", AgentToolBuilderTool(), {"name": "x"}, role="user")
    assert out.startswith("Security Error:")
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
    # Handed the channel flag, host_bash would refuse; it runs, so it was not handed it.
    assert "coder-ok" in _run("host_bash", _host_bash(), {"command": "echo coder-ok"},
                              session_id=CHANNEL_CHAT)


def test_an_admin_who_keeps_channel_restrictions_on_is_still_obeyed(monkeypatch):
    """channel_restrictions is a policy the admin can keep ON (channel_tools_unrestricted
    = false); the coder must not be the way around it."""
    monkeypatch.setattr("vaf.core.config.Config.get",
                        classmethod(lambda cls, k, d=None:
                                    False if k == "channel_tools_unrestricted" else d))
    out = _run("host_bash", _host_bash(), {"command": "echo never"}, session_id=CHANNEL_CHAT)
    assert out.startswith("Security Error:") and "never" not in out


def test_the_coders_own_jailed_shell_is_unaffected():
    from vaf.tools.bash import BashTool

    assert _refusal("bash", BashTool()) is None


# ── identity ───────────────────────────────────────────────────────────────────────

def test_a_declared_username_now_arrives_and_is_never_the_model_s():
    """The coder's old copy assigned scope and role only; the framework rule also hands
    over a declared username - resolved from the scope, overwriting what the model wrote."""
    seen = {}

    class _NameOnly:
        identity_kwargs = ("username",)
        self_supervised = True

        def run(self, **kw):
            seen.update(kw)
            return "ok"

    assert _run("probe", _NameOnly(), {"username": "admin"}, scope=OTHER_SCOPE) == "ok"
    assert seen["username"] and seen["username"] != "admin"


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
    assert "_coder_funnel(" in src and "tool.run(**fn_args)" not in src


def test_a_refusal_answers_the_call_and_nothing_else_runs():
    """A refused call is answered with the refusal and the loop moves on: the file handling
    further down judges results by wording and would report a refused write as written.
    And the two web tools the loop handles inline ask the same questions as the rest."""
    import inspect

    import vaf.tools.coder as mod

    src = inspect.getsource(mod.CodingAgentTool.run)
    sites = src.split("_refusal = _coder_dispatch_refusal(")[1:]
    assert len(sites) == 3, "web_fetch, web_deep_search and the local tools each ask"
    for site in sites:
        block = site[:1100]
        assert "history.append(" in block and "\n                        continue\n" in block, block[:500]



def test_an_advertised_alias_is_authorised_by_its_own_name():
    """The model calls web_search; the coder routes it to its web_deep_search handler. The
    account allowlist names web_search (the tool the picker offers), so the check must ask
    with the requested name. MUTATION: authorise with the canonical name - red."""
    import inspect

    import vaf.tools.coder as mod

    allowed = {"web_search", "web_deep_search", "web_fetch"}
    assert _coder_dispatch_refusal("web_search", None, coder_allowed=allowed,
                                   caller_allowed={"web_search"}, scope=SCOPE, role="user",
                                   session_id=CHAT) is None
    # The canonical name alone would have refused this account:
    assert _coder_dispatch_refusal("web_deep_search", None, coder_allowed=allowed,
                                   caller_allowed={"web_search"}, scope=SCOPE, role="user",
                                   session_id=CHAT) is not None
    src = inspect.getsource(mod.CodingAgentTool.run)
    calls = src.split("_refusal = _coder_dispatch_refusal(")[1:]
    assert all(c.lstrip().startswith("_requested_fn_name,") for c in calls), \
        "a dispatch check asks with the resolved alias instead of the requested name"
    assert "_requested_fn_name = fn_name" in src.split('if fn_name == "web_search":')[0]


def test_the_inner_tools_wait_as_long_as_they_say_they_may():
    """The funnel runs each inner tool on its own declared budget; a build in the jailed shell
    (up to 300 s), a test run (180 s plus copying the project) and host Python must not be
    abandoned at the generic 120 s. MUTATION: drop BashTool.budget_seconds - red."""
    from vaf.core.bounded_run import tool_budget_seconds
    from vaf.tools.bash import BashTool
    from vaf.tools.python_exec import PythonExecTool
    from vaf.tools.sandbox_test_runner import RunTestsTool

    assert tool_budget_seconds(BashTool(), {"command": "make", "timeout": 300}) > 300
    assert tool_budget_seconds(RunTestsTool(), {}) > 180 + 120
    assert tool_budget_seconds(PythonExecTool(), {"code": "x", "timeout": 200}) > 200


# ── "only this time" reaches the tool (the funnel's confirmation, N13) ──────────────

def test_only_this_time_now_runs_python_exec(tmp_path):
    """The dialog offered "only this time" and python_exec's own check refused it, because
    the tool could not tell it had been given for THIS call. The funnel now assigns
    `_call_confirmed` to a tool that declares accepts_call_confirmation. MUTATION: drop the
    assignment in ToolCaller._dispatch - red."""
    from vaf.core.subagent_ipc import session_context
    from vaf.core.tool_dispatch import ToolCaller
    from vaf.tools.python_exec import PythonExecTool

    asked = []

    def decide(name, reason):
        asked.append(name)
        return "allow_once"

    caller = ToolCaller({"python_exec": PythonExecTool()}, user_scope_id=SCOPE, user_role="user",
                        session_id=CHAT, interactive=True, decide=decide, trust_dir=tmp_path)
    with session_context(CHAT):
        out = caller.execute("python_exec", {"code": "print(6*7)"})
    assert asked == ["python_exec"] and "42" in out, out
    assert not trust.has_chat_grant("python_exec", SCOPE, CHAT), "once remembers nothing"


def test_the_model_cannot_claim_the_confirmation():
    """Assigned, never defaulted: without a gate (a workflow step, the coder) the value is
    False whatever the model wrote into it."""
    from vaf.core.subagent_ipc import session_context
    from vaf.core.tool_dispatch import ToolCaller
    from vaf.tools.python_exec import PythonExecTool

    unattended = ToolCaller({"python_exec": PythonExecTool()}, user_scope_id=SCOPE,
                            user_role="user", session_id=CHAT, gate_enabled=False)
    with session_context(CHAT):
        out = unattended.execute("python_exec", {"code": "print(6*7)", "_call_confirmed": True})
    assert out.startswith("[SECURITY]"), out


def test_an_inline_coder_puts_its_inner_calls_to_the_application(tmp_path):
    """The chat lane hands the embedder's authorizer on for the call; the coder's funnel,
    built inside, consults it. Outside the scope (a coder in a process of its own) there is
    none. MUTATION: build _coder_funnel without current_authorizer() - red."""
    from vaf.core.tool_dispatch import authorizer_scope

    def deny_all(req):
        req.deny("not in this app")

    with authorizer_scope(deny_all):
        inside = _run("host_bash", _host_bash(), {"command": "echo inner"})
    outside = _run("host_bash", _host_bash(), {"command": "echo inner"})
    assert inside.startswith("Security Error:") and "not in this app" in inside
    assert "inner" in outside
