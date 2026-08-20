# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A tool declares which parts of the caller's identity it needs; the dispatcher obeys.

`execute_tool` used to hand out `user_scope_id` / `username` / `user_role` from roughly
forty hardcoded lists of OUR tool names. That had two costs. It drifted - a tool added to
one list and forgotten in the sibling one is exactly how the write/read jail ended up
asymmetric. And a tool an embedder registers through `Agent.add_tool()` could never receive
an identity at all, because the dispatcher only recognised names it already knew;
`docs/EMBEDDING.md` could only warn about that, with no primitive to point at.

`BaseTool.identity_kwargs` is that primitive, in the shape the class already uses for
`channel_restrictions`. A tuple rather than a boolean because the real usage has five
distinct combinations: 51 tools take scope+username, 22 scope only, 15 role+scope, and one
each of username-only and all three. A boolean would have to pass all three, and tools that
never received `username` would start seeing one - `mail_utils.store_username_from_kwargs`
reads exactly that value, so it would be a silent behaviour change.

THE POINT OF THIS FILE is the frozen baseline below. It is the identity map as it was
measured from the dispatcher immediately BEFORE the migration, tool by tool. The migration
is only correct if every tool still receives precisely what it received then - not one key
fewer (a tool silently loses its scope and reads another tenant's data) and not one more
(a tool starts branching on a value it never had). That is a claim which has to be
checkable, so it is checked here rather than asserted in a commit message.
"""
import importlib
import inspect
import pkgutil
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import vaf.core.agent as agent_mod
from conftest import bind_chat_stages
from vaf.core.agent import Agent
from vaf.tools.base import BaseTool

# ── The frozen baseline: what each tool received from the hardcoded name lists ──
# Extracted from execute_tool before the migration. Do NOT "fix" an entry to make a test
# pass - a mismatch means the migration changed what a tool gets, which is the bug this
# file exists to catch.
IDENTITY_BASELINE = {
    "add_automation_note": ["user_scope_id"],
    "add_automation_todo": ["user_scope_id"],
    "analyze_image": ["user_scope_id"],
    "archive_mail": ["user_scope_id", "username"],
    "ask_user": ["user_scope_id", "username"],
    "browser_agent": ["user_scope_id"],
    "create_automation": ["user_role", "user_scope_id"],
    "create_calendar_event": ["user_scope_id", "username"],
    "create_contact": ["user_scope_id", "username"],
    "create_skill": ["user_scope_id", "username"],
    "delete_automation": ["user_role", "user_scope_id"],
    "delete_automation_note": ["user_scope_id"],
    "delete_automation_todo": ["user_scope_id"],
    "delete_calendar_event": ["user_scope_id", "username"],
    "delete_contact": ["user_scope_id", "username"],
    "delete_mail": ["user_scope_id", "username"],
    "delete_skill": ["user_scope_id", "username"],
    "discord_inbox": ["user_scope_id", "username"],
    "document_editor": ["user_scope_id"],
    # Both gained user_role on 2026-07-31: `file_access` refuses to be declared
    # without the identity that resolves it, and the role is what recognises a
    # SECOND admin - who was jailed here while every other file gate freed them.
    "document_viewer": ["user_role", "user_scope_id"],
    "edit_file": ["user_role", "user_scope_id"],
    "find_discord_messages": ["user_scope_id", "username"],
    "find_files": ["user_role", "user_scope_id"],
    "find_mail": ["user_scope_id", "username"],
    "find_telegram_messages": ["user_scope_id", "username"],
    "find_whatsapp_messages": ["user_scope_id", "username"],
    "folder_size": ["user_role", "user_scope_id"],
    "forward_mail": ["user_scope_id", "username"],
    "get_contact": ["user_scope_id", "username"],
    "github_create_issue": ["user_scope_id", "username"],
    "github_get_file": ["user_scope_id", "username"],
    "github_get_file_structure": ["user_scope_id", "username"],
    "github_get_tree": ["user_scope_id", "username"],
    "github_list_directory": ["user_scope_id", "username"],
    "github_list_issues": ["user_scope_id", "username"],
    "github_list_pulls": ["user_scope_id", "username"],
    "github_list_repos": ["user_scope_id", "username"],
    "github_search_files": ["user_scope_id", "username"],
    "github_update_file": ["user_scope_id", "username"],
    "label_mail": ["user_scope_id", "username"],
    "learn_attached_knowledge": ["user_scope_id"],
    "learn_document": ["user_role", "user_scope_id"],
    "librarian_agent": ["user_role", "user_scope_id"],
    "list_automation_notes": ["user_scope_id"],
    "list_automation_todos": ["user_scope_id"],
    "list_automations": ["user_role", "user_scope_id"],
    "list_calendar_events": ["user_scope_id", "username"],
    "list_contacts": ["user_scope_id", "username"],
    "list_email_accounts": ["user_scope_id", "username"],
    "list_files": ["user_role", "user_scope_id"],
    "list_skills": ["user_scope_id", "username"],
    "list_trash": ["user_role", "user_scope_id"],
    "mail_inbox": ["user_scope_id", "username"],
    "mark_mail_answered": ["user_scope_id", "username"],
    "memory_save": ["user_scope_id"],
    # username added with the cross-chat half of the tool: the contact lookup that keeps
    # other people's conversations out of the answer is keyed on the caller's account.
    "memory_search": ["user_scope_id", "username"],
    "python_sandbox": ["user_scope_id"],
    "read_automation": ["user_role", "user_scope_id"],
    "read_discord_chat": ["user_scope_id", "username"],
    "read_file": ["user_role", "user_scope_id"],
    "read_mail": ["user_scope_id", "username"],
    "read_skill": ["user_scope_id", "username"],
    "read_telegram_chat": ["user_scope_id", "username"],
    "read_whatsapp_chat": ["user_scope_id", "username"],
    "replace_editor_selection": ["user_scope_id"],
    "reply_mail": ["user_scope_id", "username"],
    "restore_automation": ["user_role", "user_scope_id"],
    "schedule_reminder": ["user_scope_id", "username"],
    # The four messenger senders gained user_role on 2026-08-02: `file_access` refuses
    # to be declared without the identity that resolves it, and the role is what
    # recognises a SECOND admin - who would otherwise be jailed to their own tree while
    # attaching a file to an outgoing message. send_slack stays: no path parameter.
    "send_discord": ["user_role", "user_scope_id", "username"],
    "send_mail": ["user_role", "user_scope_id", "username"],
    "send_slack": ["user_scope_id", "username"],
    "send_telegram": ["user_role", "user_scope_id", "username"],
    "send_to_user": ["user_role", "user_scope_id", "username"],
    "send_whatsapp": ["user_role", "user_scope_id", "username"],
    "telegram_inbox": ["user_scope_id", "username"],
    "thinking_workspace_handoff": ["user_scope_id"],
    "thinking_workspace_read": ["user_scope_id"],
    "thinking_workspace_write": ["user_scope_id"],
    "tree": ["user_role", "user_scope_id"],
    "update_automation": ["user_role", "user_scope_id"],
    "update_calendar_event": ["user_scope_id", "username"],
    "update_contact": ["user_scope_id", "username"],
    "update_intent": ["user_scope_id"],
    "update_skill": ["user_scope_id", "username"],
    "update_user_identity": ["username"],
    "update_working_memory": ["user_scope_id"],
    "use_skill": ["user_scope_id"],
    "whatsapp_call": ["user_scope_id", "username"],
    "whatsapp_inbox": ["user_scope_id", "username"],
    "write_file": ["user_role", "user_scope_id"],
}

# ask_user is deliberately NOT declarative and stays a hand-written branch: it injects the
# identity ONLY for background runs (_run_kind in automation/thinking). The reason is in the
# dispatcher comment - a non-admin's private question would otherwise be delivered to the
# ADMIN's messenger. A declaration injects unconditionally, which would change that.
CONDITIONAL_BY_DESIGN = {"ask_user"}

VALID_KEYS = {"user_scope_id", "username", "user_role"}


def _all_tool_classes():
    import vaf.core
    import vaf.tools
    seen = {}
    for pkg in (vaf.tools, vaf.core):
        for m in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
            try:
                mod = importlib.import_module(m.name)
            except Exception:
                continue
            for _, obj in inspect.getmembers(mod, inspect.isclass):
                if issubclass(obj, BaseTool) and obj is not BaseTool and getattr(obj, "name", None):
                    seen[obj.name] = obj
    return seen


TOOLS = _all_tool_classes()


# ── The migration must not have changed what any tool receives ───────────────

@pytest.mark.parametrize("tool_name", sorted(set(IDENTITY_BASELINE) - CONDITIONAL_BY_DESIGN))
def test_every_tool_declares_exactly_what_it_used_to_be_given(tool_name):
    cls = TOOLS.get(tool_name)
    assert cls is not None, f"{tool_name} no longer resolves to a tool class"
    declared = sorted(getattr(cls, "identity_kwargs", ()) or ())
    expected = sorted(IDENTITY_BASELINE[tool_name])
    assert declared == expected, (
        f"{tool_name} received {expected} before the migration but declares {declared}. "
        "Fewer keys = it silently loses its scope; more = it starts branching on a value "
        "it never had."
    )


def test_the_one_conditional_tool_stays_undeclared():
    """ask_user must NOT declare - its injection is conditional on the run kind."""
    for name in CONDITIONAL_BY_DESIGN:
        cls = TOOLS.get(name)
        assert cls is not None
        assert not getattr(cls, "identity_kwargs", ()), (
            f"{name} declared identity_kwargs, which injects unconditionally - but its "
            "branch injects only for background runs, on purpose."
        )


def test_no_tool_declares_a_key_the_dispatcher_cannot_supply():
    bad = {n: c.identity_kwargs for n, c in TOOLS.items()
           if set(getattr(c, "identity_kwargs", ()) or ()) - VALID_KEYS}
    assert not bad, f"unknown identity keys (silently ignored at dispatch): {bad}"


def test_the_dispatcher_no_longer_hands_out_identity_by_tool_name():
    """The deletion this change is for. A leftover name list would drift again, and would
    still leave a third-party tool with nothing.

    Scans the WHOLE of execute_tool rather than a delimited region: the assignment has since
    moved into vaf/core/tool_dispatch.py, and a guard anchored on two markers inside a method
    stops guarding the moment either marker moves. Whole-method is both simpler and stricter.
    """
    import inspect

    src = inspect.getsource(agent_mod.Agent.execute_tool)
    leftovers = re.findall(r'tool_args\["(user_scope_id|username|user_role)"\]\s*=', src)
    # ask_user's conditional branch is the single sanctioned exception (2 keys) - its
    # injection depends on the run kind, so it cannot become a declaration.
    assert len(leftovers) <= 2, (
        f"identity is assigned by tool NAME in {len(leftovers)} places: {leftovers}"
    )


# ── Runtime: the declaration is actually honoured, for anyone ────────────────

class _SpyTool(BaseTool):
    """A tool the dispatcher has never heard of - i.e. what an embedder registers."""
    name = "third_party_spy"
    description = "spy"
    permission_level = "read"
    identity_kwargs = ("user_scope_id", "user_role")
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        super().__init__()
        self.seen = None

    def run(self, **kwargs):
        self.seen = dict(kwargs)
        return "ok"


def _dispatch(tool, model_args, *, scope="deadbeef-0000-0000-0000-000000000000",
              role="user", username="tenant"):
    fake = bind_chat_stages(SimpleNamespace(
        tools={tool.name: tool}, _event_sink=None, _allow_once_tools={tool.name},
        _noninteractive=True, _current_turn_thinking_mode=False, _current_chat_source="web",
        current_session_id=None, _current_user_scope_id=scope, _current_user_role=role,
        _current_username=username, _record_tool_used=lambda name: None,
        _plan_gate_decision=lambda name, tool, tool_args=None: None,
        _proactive_reply_gate_decision=lambda name, tool, args: None,
        _ask_first_gate_decision=lambda name, tool: None,
        _room_mode_gate_decision=lambda name, tool: None,
    ))
    with patch("vaf.core.trust.get_tool_policy", return_value="always"), patch(
        "vaf.core.trust.is_trusted_dir", return_value=True
    ):
        return Agent.execute_tool(fake, tool.name, model_args)


def test_a_tool_the_dispatcher_never_heard_of_receives_its_identity():
    """THE framework half. Before this, identity came from a list of built-in names, so a
    tool registered via Agent.add_tool() got nothing no matter what it did."""
    spy = _SpyTool()
    result = _dispatch(spy, {})
    assert spy.seen is not None, f"tool never ran: {result}"
    assert spy.seen.get("user_scope_id") == "deadbeef-0000-0000-0000-000000000000"
    assert spy.seen.get("user_role") == "user"


def test_only_the_declared_keys_are_passed():
    """Declaring scope+role must not also deliver username - that is the whole reason the
    declaration is a tuple and not a boolean."""
    spy = _SpyTool()
    _dispatch(spy, {})
    assert "username" not in spy.seen


def test_a_model_supplied_identity_is_overwritten_not_honored():
    """The escalation this closes: arguments start out as whatever the MODEL produced."""
    spy = _SpyTool()
    _dispatch(spy, {"user_role": "admin", "user_scope_id": "00000000-0000-0000-0000-000000000001"})
    assert spy.seen.get("user_role") == "user"
    assert spy.seen.get("user_scope_id") == "deadbeef-0000-0000-0000-000000000000"


def test_a_third_party_tool_never_receives_the_agent_back_door():
    """`_agent` is a second, undeclared way to learn who is calling - anything holding the
    live agent reads scope, name and role straight off it, and the timer tools do.

    vaf/tools/base.py and docs/EMBEDDING.md now say plainly that this is chat-lane plumbing
    handed to a fixed set of built-in NAMES, not a supported surface. That sentence is only
    worth writing if it stays true, and the way it would stop being true is someone making
    `_agent` conditional on something other than the name - a declaration, a capability flag,
    a default. Then a registered tool could ask for the engine and walk around the whole
    identity contract, which is the opposite of what identity_kwargs is for."""
    spy = _SpyTool()
    _dispatch(spy, {})
    assert "_agent" not in spy.seen, (
        "a tool the dispatcher has never heard of received the live agent object - that is "
        "an identity back door, and EMBEDDING.md promises it does not exist"
    )


def test_a_tool_that_declares_nothing_receives_nothing():
    """The safe direction: not declaring means not getting, rather than getting by accident."""
    class _Quiet(_SpyTool):
        name = "third_party_quiet"
        identity_kwargs = ()

    quiet = _Quiet()
    _dispatch(quiet, {})
    assert not (VALID_KEYS & set(quiet.seen)), quiet.seen


# ── WHICH keys is settled above. This is WHAT the fallback resolves to ───────
#
# Everything above measures which keys a tool receives. The nameless case decides what the
# `username` key CONTAINS, and it was wrong in a way no count could see: the fallback was the
# literal "admin", while every store keyed on a name asks `get_local_admin_username()` - which
# registration sets to the FIRST USER'S CHOSEN NAME (vaf/api/auth_routes.py). On any
# installation whose owner is not literally called "admin", the fallback named nobody.
#
# Not a corner: `Agent` resets `_current_username` to None on every switch into a session that
# has no stored username, and that is 3154 of 3178 stored sessions. The scope covers the
# scope-aware stores; the NAME-ONLY ones - the cloud account list and its sync directory - saw
# a stranger with an empty account list.

def _assign_as_nameless_caller(local_admin_name):
    """Run the real assigner on an installation whose owner is called `local_admin_name`."""
    import vaf.core.config as config_mod
    import vaf.core.credential_store as cred_mod
    from vaf.core.tool_dispatch import assign_declared_identity

    class _NameOnly(_SpyTool):
        name = "third_party_name_only"
        identity_kwargs = ("username",)

    with patch.object(config_mod, "get_local_admin_username", lambda: local_admin_name), \
         patch.object(cred_mod, "get_local_admin_username", lambda: local_admin_name):
        args = assign_declared_identity(_NameOnly(), {}, user_scope_id=None,
                                        username=None, user_role=None)
        # The REAL consumer decides whether that name means "the owner" - asking it is the
        # whole assertion. Re-deriving the rule here would reproduce the call site instead of
        # exercising it, and would stay green through exactly the mutation this test exists
        # for.
        owner = cred_mod._cred_key_username(args["username"]) is None
    return args["username"], owner


def test_the_nameless_caller_is_the_configured_owner_not_a_literal():
    """THE regression, asserted through `_cred_key_username` rather than against a constant.

    Counter-proof that was actually run: putting the literal "admin" back makes this red and
    leaves every other test in this file green - the fallback's VALUE is invisible to a guard
    that only counts keys.
    """
    assigned, recognised_as_owner = _assign_as_nameless_caller("sam")
    assert recognised_as_owner, (
        f"a caller with no username was assigned {assigned!r}, which the credential store does "
        f"not recognise as the machine owner. Their credentials, cloud accounts and sync "
        f"directory resolve to a user that does not exist."
    )


def test_the_default_installation_is_unaffected():
    """The control. Without it the test above also passes for a fallback that returns any
    constant at all, and it is the case that hid the defect: when the owner IS called "admin",
    literal and configured name agree and nothing looks wrong."""
    assigned, recognised_as_owner = _assign_as_nameless_caller("admin")
    assert assigned == "admin" and recognised_as_owner


# ── the other half: nameless is not the same as "the owner" ──────────────────
#
# The fix above was right about the literal and wrong about what replaces it. A nameless caller
# was then resolved to the OWNER unconditionally - and nameless is overwhelmingly a TENANT whose
# name is missing from the session metadata. Measured on a live installation: of 3238 stored
# sessions 24 carry a username; of the remainder 3208 carry a NON-OWNER scope and 0 carry the
# owner's. The same number that proved the first defect (24 of ~3200) says nothing about which
# person the other ~3200 are, and reading it that way is what produced the second one.
#
# It matters because three stores decide ownership on the NAME ALONE - github_tools
# (_get_github_account_for_user, which takes user_scope_id and never references it),
# cloud_routes._get_cloud_config and cloud_storage._get_cloud_accounts - so for them the owner's
# name IS the owner's data. And "no name" cannot express the difference: seven owner-branches
# read `if not username or username == local_admin`, so passing None is the owner too.

FOREIGN_SCOPE = "deadbeef-0000-0000-0000-000000000000"


def test_a_nameless_tenant_does_not_become_the_owner():
    """THE refusal-side assertion, run through the store that ignores the scope entirely.

    Asserted on `_get_github_account_for_user` rather than on the resolved string, because that
    function is where the damage was: it branches on the name only, so whatever the assigner
    puts there decides whether the owner's GitHub account and token come back. Asserting "the
    name is not the owner's" would pass for any constant at all; asserting the ACCOUNT is
    refused is the property.

    Counter-proof run: dropping the scope check in `resolve_caller_username` - i.e. going back
    to the owner unconditionally - turns this red while the tests above stay green, because
    every one of them asks whether a legitimate caller gets through.
    """
    from vaf.core.config import Config
    from vaf.tools.github_tools import _get_github_account_for_user
    from vaf.core.tool_dispatch import assign_declared_identity

    class _NameOnly(_SpyTool):
        name = "third_party_name_only"
        identity_kwargs = ("user_scope_id", "username")

    populated = {"accounts": [{"account_id": "acct-1", "enabled": True}]}
    _real_get = Config.get

    def _fake_get(key, default=None):
        if key == "github_config":
            return populated
        if key == "github_config_by_user":
            return {}
        return _real_get(key, default)

    with patch.object(Config, "get", staticmethod(_fake_get)):
        owner_args = assign_declared_identity(_NameOnly(), {}, user_scope_id=None,
                                              username=None, user_role=None)
        tenant_args = assign_declared_identity(_NameOnly(), {}, user_scope_id=FOREIGN_SCOPE,
                                               username=None, user_role=None)
        owner_sees = _get_github_account_for_user(owner_args["username"], None)
        tenant_sees = _get_github_account_for_user(tenant_args["username"], FOREIGN_SCOPE)

    assert owner_sees is not None, (
        "the machine owner can no longer reach their own GitHub account - the fallback has to "
        "keep working for the case it exists for, or this fix simply denies everything"
    )
    assert tenant_sees is None, (
        "a caller with a FOREIGN scope and no username reached the owner's GitHub account. "
        "That gate branches on the name alone, so resolving a nameless caller to the owner "
        "hands the owner's account and token to every tenant whose name is missing from the "
        "session metadata."
    )


def test_two_different_tenants_do_not_share_one_bucket():
    """Why the synthetic name is per-scope and not one constant.

    The literal had every nameless tenant addressing the SAME name, so their name-keyed data
    was not merely mis-filed, it was pooled together. Any replacement constant would repeat
    that; the scope is what keeps them apart."""
    from vaf.core.config import resolve_caller_username

    a = resolve_caller_username(None, FOREIGN_SCOPE)
    b = resolve_caller_username(None, "cafebabe-0000-0000-0000-000000000000")
    assert a != b, "two tenants share one name-keyed bucket again"
    assert a == resolve_caller_username(None, FOREIGN_SCOPE), (
        "the same tenant gets a different name on a second call - their data would scatter"
    )


def test_the_rule_exists_once_and_the_dispatcher_asks_it():
    """The conversion, not just the behaviour.

    `automation` and `thinking_mode` both had this rule and both had it RIGHT, with the reason
    written out. The dispatcher - the one path every tool call goes through - had its own naive
    answer, which is how a rule that existed twice still failed where it counted. If automation
    grows its own copy back, the next divergence is a matter of time."""
    import inspect

    from vaf.core import automation

    src = inspect.getsource(automation._resolve_username)
    assert "resolve_caller_username" in src, "the automation lane hand-rolls the rule again"
    # The LITERAL with its quotes, not the bare word: `scope_` also occurs inside
    # `user_scope_id`, and matching that was a guard reading text instead of code - the exact
    # failure mode tests/README.md warns about, caught here by its own first run.
    assert '"scope_"' not in src, (
        "the synthetic tenant name is being constructed here again rather than asked for"
    )


# ── what the rule does NOT reach, measured rather than assumed ───────────────
#
# The gate above decides what a caller with NO name becomes. It is therefore blind to any lane
# that supplies a name of its own, and three such lanes were measured while it was built. They
# are frozen here because each one is a live mis-keying today, and because a green suite around
# `resolve_caller_username` would otherwise read as "the name question is settled".

REMAINING_LITERAL_NAME_SOURCES = {
    # file:line -> why the shared rule cannot see it
    "vaf/core/channel_history.py:90":
        "_session_identity reads meta.get('username') or 'admin' and is the WRITER of the "
        "channel message store, while the reader gets its name from the assigner. For a "
        "session with no username the two now disagree - writer literal, reader owner or "
        "tenant bucket - so a sync writes one file and the query reads another.",
    "vaf/api/discord_bridge.py:130":
        "The bridges stamp a hardcoded 'admin' into task metadata, which headless_runner "
        "copies into session metadata and the agent into _current_username. It arrives TRUTHY, "
        "so the rule returns it unchanged and never gets to ask the scope. WHAT THAT COSTS IN "
        "USER DATA IS UNMEASURED: a literal-named message store on the installation where this "
        "was written held 980 rows, and counting them was mistaken for finding orphaned "
        "traffic - 980 rows carried TWO distinct bodies, one of them 653 times, i.e. test "
        "pollution. The real store held 116 rows, 114 of them distinct. So the code path is "
        "real and its damage is not demonstrated; do not quote a row count for it.",
    "vaf/core/system_prompt.py:610":
        "get_user_workspace('admin') is hardcoded and runs on EVERY prompt build, reading "
        "identity.json and soul.md from the literal's workspace - and creating it. The persona "
        "editor writes the owner's. Nothing here involves an assigner at all.",
}


@pytest.mark.parametrize("site", sorted(REMAINING_LITERAL_NAME_SOURCES))
def test_the_named_remainder_still_looks_the_way_it_was_measured(site):
    """Not a fix - a receipt. Each of these was checked against the finished rule and confirmed
    unreached by it; if one is repaired or moved, this points at the note that explains what it
    was for rather than leaving a stale claim in a docstring."""
    path, line = site.rsplit(":", 1)
    text = (Path(__file__).resolve().parents[1] / path).read_bytes().decode().split("\n")
    body = "\n".join(text[int(line) - 3:int(line) + 2])
    assert '"admin"' in body or "'admin'" in body, (
        f"{site} no longer carries the literal name it was frozen for. If it was fixed, delete "
        f"this entry and say so; the note explaining it is in this file.\n"
        f"{REMAINING_LITERAL_NAME_SOURCES[site]}"
    )


def test_the_workflow_engine_does_not_pre_empt_the_shared_fallback():
    """The second site of the same decision, and the reason a fix in the assigner alone was
    not enough.

    `WorkflowEngine.__init__` substituted the literal itself, so `self.username` reached
    `assign_declared_identity` already truthy - the one function that knows this rule was
    never asked. Fixing only the assigner left the workflow lane on the old answer while
    every test here went green, which is exactly the invisible half of a loosening.

    Counter-proof run: putting `or "admin"` back in the engine leaves the WHOLE suite green
    (3060 tests) without this one. That is what "unproven" looks like from the inside.
    """
    import vaf.core.config as config_mod
    import vaf.core.credential_store as cred_mod
    from vaf.workflows.engine import WorkflowEngine

    with patch.object(config_mod, "get_local_admin_username", lambda: "sam"), \
         patch.object(cred_mod, "get_local_admin_username", lambda: "sam"):
        engine = WorkflowEngine(tools={}, username=None)
        recognised_as_owner = cred_mod._cred_key_username(engine.username) is None

    assert recognised_as_owner, (
        f"a workflow started without a username runs as {engine.username!r}, which the stores "
        f"do not recognise as the machine owner"
    )


def test_a_real_username_is_never_replaced_by_the_owner():
    """The fallback must stay a fallback. Widening it to "resolve the owner" would hand every
    named tenant the owner's identity, which is the opposite failure and a far worse one."""
    import vaf.core.config as config_mod
    from vaf.core.tool_dispatch import assign_declared_identity

    class _NameOnly(_SpyTool):
        name = "third_party_name_only"
        identity_kwargs = ("username",)

    with patch.object(config_mod, "get_local_admin_username", lambda: "sam"):
        args = assign_declared_identity(_NameOnly(), {}, user_scope_id=None,
                                        username="tenant", user_role=None)
    assert args["username"] == "tenant"
