# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: the persona addenda an embedder re-adds after a system_prompt override.

A `system_prompt` override replaces the persona wholesale and drops the two
code-owned addenda by design. An embedder who ships their own persona (a
support bot) and still wants the memory lane or the grounded capability
answer composes them back in from these two names; their shape is therefore
a promise: the constant names the memory tools verbatim, and the builder
claims an ability only when the tools behind it are in the set it was given.
"""
import vaf


def test_continuity_addendum_names_the_memory_lane():
    text = vaf.SOUL_CONTINUITY_ADDENDUM
    assert isinstance(text, str) and text.strip()
    for tool in ("memory_search", "memory_save", "memory_update"):
        assert tool in text, f"the continuity text must name {tool}"


def test_capability_builder_claims_only_what_it_was_given():
    text = vaf.build_capability_addendum({"search_tools", "create_automation"}, 12)
    assert isinstance(text, str)
    assert "12 tools" in text, "the count must be the caller's count, verbatim"
    assert "create_automation" in text
    for absent in ("create_agent_tool", "create_skill", "create_agent_workflow"):
        assert absent not in text, f"{absent} is not in the set and must not be promised"


def test_capability_builder_makes_no_claim_from_an_empty_registry():
    text = vaf.build_capability_addendum(set(), 0)
    assert isinstance(text, str) and text.strip()
    assert "0 tools" not in text
