# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Router must route SKILLS (and intent-based workflows) on cloud/API providers too.

Pins the fix for the bug where the LLM router tier — the only tier that can offer a
skill — was hardcoded to the local llama.cpp server (http://127.0.0.1:8080) and gated on
self.use_server. With a cloud provider (use_server=False) the router fell straight to
Tier-3 keyword matching, which knows workflows only, so a skill like "witz_erzaehler" was
never offered (e.g. "erzähl mir einen Witz" surfaced nothing).

The fix routes the LLM tier through self.api_backend (the same backend the main agent
uses) when there is no local server. Faked backend — no network, no real key.
"""
import pytest


# ── Fake API backend (no network): chat_completion yields the canned router reply ────

class _FakeBackend:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def chat_completion(self, messages, max_tokens=None, temperature=None, stream=False, **kw):
        self.calls += 1
        # The real APIBackendManager yields chunks; the router does list()+join.
        return iter([self.reply])


@pytest.fixture(scope="module")
def agent():
    from vaf.core.agent import Agent
    a = Agent(register_signals=False)
    # Force the "cloud provider" shape: no local server, no in-process lib.
    a.use_server = False
    a.llm = None
    a._current_user_scope_id = None  # admin → list_skills (patched below) is unfiltered anyway
    return a


@pytest.fixture
def one_skill(monkeypatch):
    """Make the router see exactly one skill, independent of on-disk ~/.vaf data."""
    skill = {"id": "witz_erzaehler", "name": "Witz-Erzähler",
             "description": "Erzählt einen guten Witz mit Setup, Timing und Pointe."}
    monkeypatch.setattr("vaf.skills.templates.list_skills", lambda *a, **k: [skill])
    return skill


def test_skill_routed_on_cloud_backend(agent, one_skill):
    # use_server=False but api_backend present → LLM tier MUST run and match the skill.
    agent.api_backend = _FakeBackend("skill:witz_erzaehler")
    agent._pending_skill_match = None
    result = agent.analyze_workflow("erzähl mir einen Witz")
    assert agent.api_backend.calls == 1, "router did not use api_backend on a cloud provider"
    assert result is None, "a skill match returns None (it is a side-channel, not a workflow)"
    assert agent._pending_skill_match == {"skill_id": "witz_erzaehler", "name": "Witz-Erzähler"}


def test_none_reply_yields_no_skill(agent, one_skill):
    agent.api_backend = _FakeBackend("none")
    agent._pending_skill_match = None
    result = agent.analyze_workflow("erzähl mir einen Witz")
    assert result is None
    assert agent._pending_skill_match is None


def test_workflow_routed_on_cloud_backend(agent):
    from vaf.workflows.templates import get_workflow_templates
    templates = get_workflow_templates()
    if not templates:
        pytest.skip("no workflow templates available to match")
    wid = next(iter(templates))
    agent.api_backend = _FakeBackend(f"workflow:{wid}")
    agent._pending_skill_match = None
    result = agent.analyze_workflow("do the thing")
    assert result == wid


def test_no_backend_falls_back_to_tier3(agent, one_skill):
    # No server, no api_backend, no llm → Tier-3 (workflows only), no crash, no skill.
    agent.api_backend = None
    agent._pending_skill_match = None
    result = agent.analyze_workflow("erzähl mir einen Witz")
    assert agent._pending_skill_match is None, "Tier-3 fallback must not offer a skill"
    assert result is None or isinstance(result, str)
    assert agent._workflow_selection_tier in (2, 3)
