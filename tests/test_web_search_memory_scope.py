# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Whose memories a failed web search is allowed to fall back on.

When every web provider fails, `web_search` answers from VAF's own long-term memory. That
answer is per-user data, so the question "whose" is a security question, and it used to be
answered from `os.environ["VAF_SESSION_ID"]`: a process-global variable that every tool call
of every worker rewrites. With `parallel_main_workers` above one - five on an API provider -
the session it named at the moment of the fallback could belong to somebody else, and one
user's web search would answer with another user's memories.

The shape of the leak matters for how it was fixed. A MISSING scope was already safe: in
server mode `run_memory_search_sync` refuses outright. What leaked was a scope that was
present and real and WRONG. No amount of fail-closed handling of None would have caught it,
because None was never the value.

So the tool now DECLARES what it needs (`identity_kwargs`) and the dispatcher assigns the
caller's own scope, the same way every other per-user tool already worked. The resolution
inside the tool is deleted rather than hardened: a second way to answer "who is calling" is
the thing that goes wrong, not the way it is written.

The four tests that already covered this fallback never set `VAF_SESSION_ID` at all, so the
scope path was unexercised - they measured the shaping of results and nothing about ownership.
"""
import os
from unittest.mock import patch

import pytest

from vaf.tools.search import WebSearchTool, _search_internal_knowledge, get_web_search_results

ALICE = "6f9619ff-8b86-d011-b42d-00c04fc964ff"    # synthetic
BOB = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"      # synthetic


@pytest.fixture
def asked():
    """Record which scope the memory layer is asked for, and answer nothing."""
    seen = []

    def _spy(query, k=5, user_scope_id=None, caller=None):
        seen.append(user_scope_id)
        return ""

    with patch("vaf.memory.rag.run_memory_search_sync", side_effect=_spy):
        yield seen


# ── the declaration is what makes the dispatcher hand it over ────────────────

def test_the_tool_declares_the_scope_it_needs():
    """Without the declaration the dispatcher hands it nothing, and the tool would be back to
    resolving an identity by itself - which is the bug."""
    assert "user_scope_id" in (WebSearchTool.identity_kwargs or ())


# ── the caller's scope is the one that is asked ──────────────────────────────

def test_the_callers_scope_is_the_one_searched(asked):
    _search_internal_knowledge("anything", 5, user_scope_id=ALICE)
    assert [str(s) for s in asked] == [ALICE]


def test_a_different_caller_gets_their_own(asked):
    _search_internal_knowledge("anything", 5, user_scope_id=BOB)
    assert [str(s) for s in asked] == [BOB]


def test_no_scope_stays_no_scope(asked):
    """Absent is safe - the memory layer refuses outright in server mode. What must never
    happen is absent turning into somebody else's."""
    _search_internal_knowledge("anything", 5)
    assert asked == [None]


def test_an_unparseable_scope_is_no_scope_rather_than_a_guess(asked):
    _search_internal_knowledge("anything", 5, user_scope_id="not-a-uuid")
    assert asked == [None]


# ── the environment can no longer decide ─────────────────────────────────────

def test_a_foreign_session_in_the_environment_is_ignored(asked, monkeypatch):
    """THE regression. A stale or foreign `VAF_SESSION_ID` - which is what a process-global
    variable becomes the moment a second worker exists - must not select whose memory is read.
    """
    monkeypatch.setenv("VAF_SESSION_ID", "some-other-users-session")
    _search_internal_knowledge("anything", 5, user_scope_id=ALICE)
    assert [str(s) for s in asked] == [ALICE], (
        "the environment overrode the caller's own scope - this is the cross-user leak"
    )


def test_the_environment_cannot_supply_a_scope_on_its_own(asked, monkeypatch):
    monkeypatch.setenv("VAF_SESSION_ID", "some-other-users-session")
    _search_internal_knowledge("anything", 5)
    assert asked == [None]


def test_the_tool_no_longer_reads_the_process_global_at_all():
    """Belt and braces on the deletion: a reader looking for how ownership is decided must
    find one answer, not two."""
    import ast
    import inspect

    import vaf.tools.search as search_mod

    # The DOCSTRING names the old mechanism on purpose - that is the history a reader needs.
    # Strip it, so the guard is about the code and cannot be satisfied or broken by prose.
    tree = ast.parse(inspect.getsource(search_mod._search_internal_knowledge).lstrip())
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]
    code = ast.unparse(fn)
    assert "VAF_SESSION_ID" not in code
    assert "environ" not in code


# ── the scope survives the whole call chain ──────────────────────────────────

def test_the_chain_from_the_search_entrypoint_carries_it(asked):
    """`get_web_search_results` is the function both the tool and the agent's deep-research
    helper call; the scope has to survive that hop or the fix only works in a unit test."""
    with patch("vaf.tools.search._search_brave_api", return_value=([], "no_results")), \
         patch("vaf.tools.search._search_google_cse", return_value=([], "no_results")), \
         patch("vaf.tools.search._search_google", return_value=([], "no_results")), \
         patch("vaf.tools.search._search_duckduckgo", return_value=[]):
        get_web_search_results("anything", 5, user_scope_id=ALICE)
    assert [str(s) for s in asked] == [ALICE]


def test_the_agents_deep_research_helper_passes_its_own_scope():
    """The second caller. It is a method on the agent, so the scope is right there - and it
    was the one place that could have kept the old behaviour alive after the tool was fixed."""
    import inspect

    import vaf.core.agent as agent_mod

    src = inspect.getsource(agent_mod.Agent.perform_web_search)
    assert 'user_scope_id=getattr(self, "_current_user_scope_id", None)' in src, (
        "perform_web_search calls the search chain without an identity, so its memory "
        "fallback would answer from whatever the environment happens to say"
    )
