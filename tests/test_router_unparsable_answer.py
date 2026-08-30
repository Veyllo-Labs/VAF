# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""When the tool router's own answer cannot be read, the turn must not start from nothing.

Observed twice on 2026-08-30, once on a messenger reply to a background question:

    | Router  No tools selected (Router response was not a valid tool list)
    | Router  Safety Net: Router found none. Using list_tools, search_tools.

Two things were wrong with that, and neither is the router failing - a model that answers
"no tools needed" is allowed to.

1. The log said the answer was invalid and never said WHAT it was, so the next occurrence is
   an inference rather than a grep. This repo already states that rule for the reply-context
   note; it applied here too.
2. The fallback handed the turn discovery tools ONLY, so a model that had just been using
   tools had to go and find them again - a wasted turn on exactly the turns where routing had
   already failed once. The recent-tool list is decay-tracked and is already merged on the
   normal path; using it here introduces nothing new."""
import types

from vaf.core.agent import Agent


def _agent(recent=(), registry=()):
    ns = types.SimpleNamespace(
        tools={name: object() for name in (list(registry) or ["list_tools", "search_tools"])},
        _recent_tools={name: 3 for name in recent},
        _active_tools=None,
    )
    ns._get_recent_tools = types.MethodType(Agent._get_recent_tools, ns)
    ns._merge_tool_lists = types.MethodType(Agent._merge_tool_lists, ns)
    return ns


def _safety_net(agent):
    """The product's fallback, expressed exactly as chat_step applies it."""
    discovery = [t for t in ("list_tools", "search_tools") if t in agent.tools]
    return agent._merge_tool_lists(discovery, agent._get_recent_tools())


def test_the_fallback_keeps_the_tools_the_turn_was_already_using():
    a = _agent(recent=["memory_search", "send_telegram"],
               registry=["list_tools", "search_tools", "memory_search", "send_telegram"])
    out = _safety_net(a)
    assert out[:2] == ["list_tools", "search_tools"], "discovery must stay first"
    assert "memory_search" in out and "send_telegram" in out, \
        "a turn that had just used tools was made to re-find them"


def test_the_fallback_is_still_discovery_when_nothing_was_used():
    a = _agent(recent=[], registry=["list_tools", "search_tools"])
    assert _safety_net(a) == ["list_tools", "search_tools"]


def test_the_fallback_never_names_a_tool_that_is_not_registered():
    """`_recent_tools` outlives a registry change - a thinking run drops tools the chat has."""
    a = _agent(recent=["memory_search", "git_add_commit"],
               registry=["list_tools", "search_tools", "memory_search"])
    out = _safety_net(a)
    assert "git_add_commit" not in out
    assert "memory_search" in out


def test_the_fallback_does_not_duplicate():
    a = _agent(recent=["list_tools", "memory_search"],
               registry=["list_tools", "search_tools", "memory_search"])
    out = _safety_net(a)
    assert out.count("list_tools") == 1


# ── the unreadable answer must be readable in the log ─────────────────────────────────────

def test_the_router_logs_what_it_could_not_parse():
    """A guard on the source: the message must carry the answer, not only the verdict."""
    from pathlib import Path
    src = (Path(Agent.__module__.replace(".", "/")).with_suffix(".py"))
    code = (Path(__file__).resolve().parent.parent / src).read_text(encoding="utf-8")
    marker = "No tools selected (unparsable answer)"
    assert marker in code, "the router no longer reports WHAT it could not read"
    block = code.split(marker, 1)[0][-600:]
    assert "_bad" in block, "the offending answer is not captured"
    assert "[:200]" in block, "model output must be truncated before it reaches a log"
    assert "[ROUTER] unparsable tool answer" in code, \
        "the occurrence must survive in the domain log, not only on the console"
