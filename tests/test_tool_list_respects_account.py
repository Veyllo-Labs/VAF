# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What a user is OFFERED must match what they may run.

The account allowlist was enforced at dispatch and nowhere else, so a user whose
admin had taken away `coding_agent` still saw it in the '/' suggestions - and in
the sub-agent hotbar built on the same list - and only learned it was refused
after invoking it. A menu that lists what it will then refuse is worse than a
short menu: it advertises a capability the account does not have.

The read side of the allowlist is a framework primitive (`account_allows_tool`)
rather than a filter each surface rebuilds, because the EXEMPTIONS are part of
the answer: a scopeless caller and an admin are unrestricted. A lister that
consulted the resolver alone would strip an admin's own tools.
"""
import re
from pathlib import Path

import pytest

from vaf.core import tool_dispatch as td

_REPO = Path(__file__).resolve().parents[1]
_SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope


@pytest.fixture
def resolver_slot():
    """Save/RESTORE the process-wide resolver - it outlives a test otherwise."""
    before = td.get_account_allowlist_resolver()
    yield
    td.set_account_allowlist_resolver(before)


# ── the primitive ─────────────────────────────────────────────────────────────

def test_a_restricted_account_is_only_offered_its_own_tools(resolver_slot):
    td.set_account_allowlist_resolver(lambda scope: {"read_file"})
    assert td.account_allows_tool("read_file", _SCOPE, "user") is True
    assert td.account_allows_tool("coding_agent", _SCOPE, "user") is False


def test_an_unregistered_resolver_restricts_nothing(resolver_slot):
    td.set_account_allowlist_resolver(None)
    assert td.account_allows_tool("coding_agent", _SCOPE, "user") is True


def test_the_exemptions_travel_with_the_answer(resolver_slot):
    # This is the reason the primitive exists rather than a per-surface filter.
    td.set_account_allowlist_resolver(lambda scope: frozenset())
    assert td.account_allows_tool("coding_agent", None, None) is True, \
        "a caller with no scope is the machine owner, never restricted"
    assert td.account_allows_tool("coding_agent", _SCOPE, "admin") is True, \
        "admins are never restricted"
    assert td.account_allows_tool("coding_agent", _SCOPE, "user") is False


def test_an_empty_answer_allows_nothing(resolver_slot):
    td.set_account_allowlist_resolver(lambda scope: [])
    assert td.account_allows_tool("read_file", _SCOPE, "user") is False


def test_a_raising_resolver_raises_through(resolver_slot):
    # Each caller owns its fail-closed: the funnel refuses the call, a lister
    # drops the entry. Swallowing it here would make both silently fail OPEN.
    def boom(scope):
        raise RuntimeError("backend down")
    td.set_account_allowlist_resolver(boom)
    with pytest.raises(RuntimeError):
        td.account_allows_tool("read_file", _SCOPE, "user")


def test_the_dispatcher_still_refuses_through_the_same_primitive(resolver_slot):
    """The enforcement is a CALLER of the primitive now, not a second copy."""
    src = (_REPO / "vaf" / "core" / "tool_dispatch.py").read_bytes().decode("utf-8")
    body = src.split("def _account_allowlist_blocks", 1)[1].split("\n    def ", 1)[0]
    assert "account_allows_tool(" in body, "the funnel stopped using the shared answer"
    assert "resolve_account_allowlist(" not in body, \
        "the funnel grew its own copy of the lookup again"
    assert "policy_admin_flag(" not in body, \
        "the funnel grew its own copy of the exemptions again"


# ── every lane that hands a tool list to a browser ────────────────────────────

def test_all_three_tools_list_producers_filter_by_account():
    """Three places send `tools_list`; a filter on two of them is a hole, because
    the client keeps whichever arrived last."""
    src = (_REPO / "vaf" / "core" / "web_server.py").read_bytes().decode("utf-8")
    producers = [m.start() for m in re.finditer(r'"type":\s*"tools_list"', src)]
    assert len(producers) >= 3, f"expected at least 3 tools_list senders, found {len(producers)}"
    # The CALL, not the definition. Checking only that the helper exists nearby
    # let a mutation that deleted the actual filter line pass: the helper was
    # still defined three lines above the loop that no longer used it.
    calls = [
        "if not _gt_account_allows(name):",              # get_tools handler
        "if _oc_allows(name)",                            # on-connect push
        "if not account_allows_tool(name, _scope, _role):",  # refresh broadcast
    ]
    for call in calls:
        assert call in src, f"a tools_list producer no longer filters: {call!r} is gone"


def test_the_listing_filters_fail_closed():
    # A crashed resolver must not turn a restricted account into a full menu.
    src = (_REPO / "vaf" / "core" / "web_server.py").read_bytes().decode("utf-8")
    for fn in ("_gt_account_allows", "_oc_allows"):
        # Fixed window, not a blank-line split: a docstring contains blank lines,
        # so splitting on one truncates the body and the check passes on nothing.
        body = src.split(f"def {fn}", 1)[1][:900]
        assert "except Exception:" in body and "return False" in body, \
            f"{fn} does not fail closed"
