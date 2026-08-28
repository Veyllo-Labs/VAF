# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Asked "what can you do?", the persona must turn the question around and
claim only what this session's registry really holds.

The identity text bans the generic assistant list but never said what a GOOD
answer looks like, so the model either undersold itself or fell back to the
banned list. The code-owned capability addendum closes that gap and rides the
persona block under the continuity addendum's delivery rules: soul path and
no-soul fallback alike, never the embedder override. Its claims are grounded:
the tool count is the live registry's count, and each ability line (build your
own tool, team up via sub-agents/workflows, standing orders) appears only when
the tools behind it are actually registered - a session without them must not
promise them.
"""
from types import SimpleNamespace

from vaf.core.system_prompt import SystemPromptManager, build_capability_addendum

_HEADER = "When asked what you can do"


def _tool(name):
    return SimpleNamespace(name=name)


_FULL = [_tool(n) for n in (
    "search_tools", "list_tools", "create_agent_tool", "create_skill",
    "create_agent_workflow", "coding_agent", "create_automation",
)]


def _identity_block(tools, override=None):
    agent = SimpleNamespace(_system_prompt_override=override) if override is not None else None
    p = SystemPromptManager(tools=tools, model_name="TestModel", agent_instance=agent).build_prompt()
    return p.split("<identity>", 1)[1].split("</identity>", 1)[0]


def test_capability_answer_rides_the_persona_block():
    ident = _identity_block(_FULL)
    assert _HEADER in ident
    assert f"{len(_FULL)} tools" in ident, "the count must be the live registry's count"
    for named in ("search_tools", "create_agent_tool", "create_agent_workflow", "create_automation"):
        assert named in ident, f"the grounded ability line naming {named} is missing"


def test_claims_follow_the_registry():
    ident = _identity_block([_tool("search_tools"), _tool("list_tools")])
    assert _HEADER in ident
    assert "2 tools" in ident
    for absent in ("create_agent_tool", "create_skill", "create_agent_workflow", "create_automation"):
        assert absent not in ident, f"a session without {absent} must not promise it"


def test_embedder_override_does_not_carry_it():
    ident = _identity_block(_FULL, override="You are Captain Redbeard, a pirate.")
    assert _HEADER not in ident, (
        "an override replaces the persona wholesale; the addendum must not ride it"
    )


def test_empty_registry_makes_no_count_claim():
    text = build_capability_addendum(set(), 0)
    assert _HEADER in text
    assert "0 tools" not in text
