# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""CoreAgent.complete: the three-branch backend dispatch, without building an engine.

Driven as a bound method on a SimpleNamespace (the house pattern from
test_provider_swap_single_implementation), because what is under test is the DISPATCH -
which backend answers, in which order, with which gate - not the engine lifecycle.

The branch order is the documented backend selection plus the Rule-4.6 compound gate:
`provider` alone lies after a failed API init, so the API lane requires `api_backend`
too, and a non-local provider with no backend falls THROUGH to the local lanes instead
of erroring - the same fallback load_model performs.
"""
from types import SimpleNamespace

import pytest

from vaf.core.agent import Agent as CoreAgent


class _FakeBackend:
    def __init__(self):
        self.calls = []

    def chat_completion(self, messages, temperature, max_tokens, stream, model,
                        tools=None, tool_choice=None):
        self.calls.append({"messages": messages, "model": model, "tools": tools})
        yield "API_ANSWER"


def _agent(provider="local", api_backend=None, use_server=False, llm=None):
    ns = SimpleNamespace(provider=provider, api_backend=api_backend,
                         use_server=use_server, llm=llm, history=[])
    ns.complete = CoreAgent.complete.__get__(ns)
    return ns


def test_the_api_lane_reuses_the_agents_own_backend(monkeypatch):
    """Identity matters: self.api_backend carries an embedder's passed keys and the
    event sink; a fresh manager would silently drop both."""
    import vaf.core.completion as comp

    seen = {}
    real = comp.complete

    def spy(messages, **kwargs):
        seen.update(kwargs)
        return real(messages, **kwargs)

    monkeypatch.setattr(comp, "complete", spy)
    backend = _FakeBackend()
    agent = _agent(provider="openai", api_backend=backend)

    assert agent.complete("q") == "API_ANSWER"
    assert seen["backend"] is backend
    assert backend.calls[0]["tools"] is None


def test_rule_46_fallthrough_a_failed_api_init_uses_the_local_lane(monkeypatch):
    """provider='openai' with api_backend=None (init failed) must not error out - the
    server lane serves, mirroring load_model's own fallback."""
    import requests

    posted = []

    def fake_post(url, json=None, timeout=None):
        posted.append(url)

        class _R:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": "LOCAL_ANSWER"}}]}
        return _R()

    monkeypatch.setattr(requests, "post", fake_post)
    agent = _agent(provider="openai", api_backend=None, use_server=True)

    assert agent.complete("q") == "LOCAL_ANSWER"
    assert posted, "the server lane was never consulted"


def test_the_in_process_llm_lane_answers_and_strips():
    class _FakeLlama:
        def create_chat_completion(self, messages, max_tokens, temperature):
            return {"choices": [{"message": {"content": "<think>x</think>LLM_ANSWER"}}]}

    agent = _agent(provider="local", llm=_FakeLlama())
    assert agent.complete("q") == "LLM_ANSWER"


def test_nothing_loaded_means_none():
    assert _agent().complete("q") is None


@pytest.mark.parametrize("kind", ["api", "server", "llm", "none"])
def test_no_lane_touches_the_history(monkeypatch, kind):
    import requests

    def fake_post(url, json=None, timeout=None):
        class _R:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": "x"}}]}
        return _R()

    monkeypatch.setattr(requests, "post", fake_post)

    class _FakeLlama:
        def create_chat_completion(self, **kw):
            return {"choices": [{"message": {"content": "x"}}]}

    agent = {
        "api": lambda: _agent(provider="openai", api_backend=_FakeBackend()),
        "server": lambda: _agent(use_server=True),
        "llm": lambda: _agent(llm=_FakeLlama()),
        "none": lambda: _agent(),
    }[kind]()
    agent.complete("q")
    assert agent.history == [], f"the {kind} lane wrote into the conversation"
