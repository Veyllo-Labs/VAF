# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The workflow router routes on the RAW user message, not the enriched one.

Live incident: the WebUI lane prepends the [SESSION WORKSPACE]
preamble (wording: coding_agent, projects, write_file) to the user message
BEFORE chat_step, and the router routed on that - a plain websearch+HTML
request matched a CODE workflow, and the variable extractor stuffed the whole
preamble into query=. Because a template "matched", the no-match run_temp hint
(the fbf9250 fix) never fired either; the model declined the garbage
suggestion and did all steps manually (44-step turn).

Pinned here:
- _try_workflow(route_input=...): router match, variable extraction, the
  workflow-mention detection and the intent lock all consume the RAW text.
- The [WORKFLOW SUGGESTION] note advertises create_agent_workflow(run_temp)
  as the fallback whenever the user's own message mentions a workflow, so a
  wrong template match can no longer eat an explicit workflow request.
- _mentions_workflow stays typo-tolerant and excludes "workforce".

_try_workflow is exercised unbound on a SimpleNamespace (same pattern as
test_thinking_read_cap) so no model/backend is needed.
"""
import types

import pytest

from vaf.core.agent import Agent, _build_workflow_suggestion_note, _mentions_workflow

PREAMBLE = (
    "[SESSION WORKSPACE] All files for this chat are stored in: "
    "/home/user/Documents/VAF_Projects/aa11bb22/chat1\n"
    "write_file with a relative path lands here automatically.\n"
    'To edit or modify: coding_agent(task="<task>", project_path="/home/user/x")\n\n'
)
RAW_WITH_WORKFLOW = (
    "Okay fuehre bitte eine mehrstufige websuche in einem workflow durch, "
    "suche das wetter und erstelle ein HTML mit den ergebnissen"
)
RAW_NO_WORKFLOW = "suche bitte das wetter und erstelle ein HTML damit"


def _agent_ns(analyze_returns=None):
    ns = types.SimpleNamespace()
    ns._seen = {}

    def _cfg_get(key, default=None):
        if key == "workflows_enabled":
            return True
        return default

    def _analyze(text):
        ns._seen["router_input"] = text
        return analyze_returns

    def _explicit(text):
        ns._seen["explicit_input"] = text
        return (None, None)

    class _Persistence:
        def update_user_intent(self, text):
            ns._seen["intent"] = text

        def reset_validation_retry_count(self):
            pass

    ns.config = types.SimpleNamespace(get=_cfg_get)
    ns.prompt_manager = types.SimpleNamespace(user_language="en")
    ns.get_live_session_subagents = lambda: []
    ns.analyze_workflow = _analyze
    ns._extract_explicit_workflow = _explicit
    ns._detect_user_language = lambda t: "en"
    ns.history = []
    ns.main_persistence = _Persistence()
    return ns


@pytest.fixture(autouse=True)
def _no_automation_env(monkeypatch):
    monkeypatch.delenv("VAF_IN_AUTOMATION", raising=False)
    monkeypatch.delenv("VAF_THINKING_MODE", raising=False)


def test_router_and_explicit_parse_receive_the_raw_message():
    ns = _agent_ns(analyze_returns=None)
    Agent._try_workflow(ns, PREAMBLE + RAW_WITH_WORKFLOW, None,
                        route_input=RAW_WITH_WORKFLOW)
    assert ns._seen["router_input"] == RAW_WITH_WORKFLOW
    assert ns._seen["explicit_input"] == RAW_WITH_WORKFLOW


def test_no_match_hint_strength_follows_the_raw_message_not_the_enrichment():
    # RAW mentions a workflow -> the STRONG hint, even though detection on the
    # enriched text would also have fired here.
    ns = _agent_ns(analyze_returns=None)
    Agent._try_workflow(ns, PREAMBLE + RAW_WITH_WORKFLOW, None,
                        route_input=RAW_WITH_WORKFLOW)
    assert len(ns.history) == 1
    assert "user's message mentions a workflow" in ns.history[0]["content"]
    assert "run_temp" in ns.history[0]["content"]

    # RAW does NOT mention one, but the enriched text does (e.g. quoted doc
    # content) -> the weak generic hint: detection keys on the user's words.
    ns2 = _agent_ns(analyze_returns=None)
    enriched = PREAMBLE + "[quoted doc: 'our workflow'] " + RAW_NO_WORKFLOW
    Agent._try_workflow(ns2, enriched, None, route_input=RAW_NO_WORKFLOW)
    assert len(ns2.history) == 1
    assert "user's message mentions a workflow" not in ns2.history[0]["content"]


def test_variables_and_intent_come_from_the_raw_message_on_a_match():
    ns = _agent_ns(analyze_returns="research_and_document")
    result = Agent._try_workflow(ns, PREAMBLE + RAW_WITH_WORKFLOW, None,
                                 route_input=RAW_WITH_WORKFLOW)
    assert result is None  # suggestion mode, agent decides

    hint = ns._pending_workflow_hint
    assert hint["workflow_id"] == "research_and_document"
    for value in (hint.get("variables") or {}).values():
        assert "[SESSION WORKSPACE]" not in str(value)
        assert "coding_agent" not in str(value)

    # Intent lock stores the raw request, not the preamble.
    assert ns._seen["intent"] == RAW_WITH_WORKFLOW


def test_without_route_input_behavior_is_unchanged():
    ns = _agent_ns(analyze_returns=None)
    Agent._try_workflow(ns, RAW_WITH_WORKFLOW, None)
    assert ns._seen["router_input"] == RAW_WITH_WORKFLOW


def test_suggestion_note_offers_run_temp_only_on_a_workflow_mention():
    hint = {"workflow_id": "research_and_document", "name": "Research & Document",
            "variables": {"topic": "wetter"}}
    with_mention = _build_workflow_suggestion_note(hint, RAW_WITH_WORKFLOW)
    assert "[WORKFLOW SUGGESTION]" in with_mention
    assert 'topic="wetter"' in with_mention
    assert "run_temp" in with_mention
    assert "does NOT fit" in with_mention

    without_mention = _build_workflow_suggestion_note(hint, RAW_NO_WORKFLOW)
    assert "run_temp" not in without_mention


def test_mentions_workflow_is_typo_tolerant_and_excludes_workforce():
    assert _mentions_workflow("bitte einen workflwo bauen") is True   # incident typo
    assert _mentions_workflow("ein Workflow bitte") is True
    assert _mentions_workflow("news about the workforce") is False
    assert _mentions_workflow("") is False
    assert _mentions_workflow(None) is False
