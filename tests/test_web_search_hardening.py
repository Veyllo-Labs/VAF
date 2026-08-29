# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Three properties `web_search` must hold before anything asks it what the OUTSIDE world says.

All three were live on the default configuration path:

1. With no Brave/Google key and Google serving its unusual-traffic page, the provider chain
   falls through to VAF's own long-term memory and labels it `Internal Knowledge (RAG)`. A
   caller checking whether something CHANGED would then be answered with the very memory it
   was checking against - indistinguishable, and self-confirming.
2. Every `web_search` call had the last `role="user"` message attached as `user_question`. In a
   background run that is a private chat line, and it is sent onward into per-page analysis and
   into the cache key.
3. The result cache is a shared directory holding query and result in clear text, keyed without
   the user scope - so on a server one tenant's search was served to another."""
import types

from vaf.core.agent import Agent
from vaf.tools.search import WebSearchTool
import vaf.tools.search as search


# ── 1. the internal-knowledge fallback must be refusable ──────────────────────────────────

def _no_web(monkeypatch):
    """Every web provider comes back empty - the exact state that reaches the last resort."""
    monkeypatch.setattr(search.Config, "get_api_key", staticmethod(lambda name: ""))
    monkeypatch.setattr(search, "_search_google", lambda q, n: ([], "no_results"))
    monkeypatch.setattr(search, "_search_duckduckgo", lambda q, n: [])


def test_memory_can_be_served_as_a_web_result_by_default(monkeypatch):
    """Pinned deliberately: this is the behaviour the ordinary chat still wants, because there
    the hit is LABELLED as memory and the model is told to say so."""
    _no_web(monkeypatch)
    monkeypatch.setattr(search, "_search_internal_knowledge",
                        lambda q, n, user_scope_id=None: [{"title": "t", "body": "b",
                                                           "href": "memory://internal/1",
                                                           "source": "internal_knowledge"}])
    results, source, _ = search.get_web_search_results("anything", 3)
    assert source == "Internal Knowledge (RAG)"
    assert results


def test_no_internal_fallback_returns_honest_emptiness(monkeypatch):
    """A caller asking about the outside world gets nothing rather than its own memory back."""
    _no_web(monkeypatch)
    called = {"n": 0}

    def _never(q, n, user_scope_id=None):
        called["n"] += 1
        return [{"title": "t", "body": "b", "href": "memory://internal/1"}]

    monkeypatch.setattr(search, "_search_internal_knowledge", _never)
    results, source, _ = search.get_web_search_results("anything", 3, no_internal_fallback=True)
    assert results == []
    assert source != "Internal Knowledge (RAG)"
    assert called["n"] == 0        # not merely discarded - never asked


# ── 2. no private chat line rides along on a background search ────────────────────────────

def _agent_stub(run_kind, history):
    ns = types.SimpleNamespace(_run_kind=run_kind, history=history)
    ns._is_thinking_run = types.MethodType(Agent._is_thinking_run, ns)
    ns._web_search_user_question = types.MethodType(Agent._web_search_user_question, ns)
    return ns


def _resolve_user_question(agent, arguments):
    """The PRODUCT's rule - the same method chat_step calls, never a copy of it.

    A re-implementation here would keep passing after the product's own answer changed, which is
    the one thing these two tests exist to catch."""
    return Agent._web_search_user_question(agent, arguments)


def test_background_search_does_not_carry_the_last_user_message():
    history = [{"role": "user", "content": "meine private Nachricht an den Assistenten"}]
    bg = _agent_stub("thinking", history)
    assert _resolve_user_question(bg, {"query": "Bahnstreik Berlin"}) == "Bahnstreik Berlin"


def test_a_chat_turn_still_carries_it():
    """The ride-along is what makes per-page analysis answer the user's ACTUAL question, so a
    normal chat turn must keep it."""
    history = [{"role": "user", "content": "Wie komme ich morgen nach Hamburg?"}]
    chat = _agent_stub("chat", history)
    assert _resolve_user_question(chat, {"query": "Bahnstreik"}) == "Wie komme ich morgen nach Hamburg?"


# ── 3. the result cache is per user ───────────────────────────────────────────────────────

def test_cache_key_separates_users():
    t = WebSearchTool()
    a = t._ws_cache_key("gleiche frage", 5, True, False, "q", "scope-a")
    b = t._ws_cache_key("gleiche frage", 5, True, False, "q", "scope-b")
    assert a != b


def test_cache_key_is_stable_for_one_user():
    t = WebSearchTool()
    assert (t._ws_cache_key("q", 5, True, False, "uq", "scope-a")
            == t._ws_cache_key("q", 5, True, False, "uq", "scope-a"))
