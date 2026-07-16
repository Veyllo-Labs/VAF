# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""When the workflow router finds no SAVED template match, the fallback hint
the agent reads must not talk it OUT of workflows for a request that
explicitly asked for one.

Live incident: a user asked to run a weather lookup "in einem workflow" /
"du kannst dafuer auch einen temporaeren workflow bauen" (note the real
message had "workflow" typo'd as "workflwo"). No saved template fits an
ad-hoc two-topic weather report, so the router correctly found nothing - but
the OLD fallback message used "weather" as ITS example of something that
never needs a workflow, and never mentioned create_agent_workflow(run_temp)
at all, only the saved-templates-only list_workflows tool. The model took
the hint at face value and did every step manually instead of building a
temporary workflow, exactly as it was told to.
"""
import os

import pytest

os.environ.setdefault("VAF_NONINTERACTIVE", "1")

from vaf.core.agent import Agent as CoreAgent


@pytest.fixture
def agent(monkeypatch):
    a = CoreAgent(verbose=False, register_signals=False, config_overrides={"provider": "local"})
    monkeypatch.setattr(a, "analyze_workflow", lambda user_input: None)
    monkeypatch.setattr(a, "get_live_session_subagents", lambda: [])
    a.history = []
    return a


def _last_hint(agent_obj):
    assert agent_obj.history, "no hint was appended to history"
    return agent_obj.history[-1]["content"]


def test_no_match_points_at_run_temp_when_workflow_explicitly_requested(agent):
    result = agent._try_workflow(
        "fuehre bitte eine mehrstufige websuche in einem workflow durch, "
        "du kannst dafuer auch einen temporaeren workflow bauen"
    )
    assert result is None
    hint = _last_hint(agent)
    assert "create_agent_workflow" in hint
    assert "run_temp" in hint
    assert "weather" not in hint.lower()
    # Advisory, not a near-mandate: a false substring match (see below) must
    # never be able to push the model into an unwanted run_temp call.
    assert "your own judgment" in hint or "your judgment" in hint


def test_no_match_still_recognizes_the_real_incident_typo(agent):
    """The actual live message had 'workflow' transposed to 'workflwo' - the
    detection must be typo-tolerant to this exact real-world case."""
    result = agent._try_workflow(
        "okay fuehre bitte ien mehrstufige websuche in ienm workflwo durch, "
        "du kanst dafpr uach einen Temporaeren workflwo bauen, suche nach "
        "dem wetter, dann nach newy, und erstlle ien HTML mit deinen ergbniseen"
    )
    assert result is None
    hint = _last_hint(agent)
    assert "create_agent_workflow" in hint
    assert "run_temp" in hint


def test_no_match_generic_hint_when_workflow_not_mentioned(agent):
    """No explicit ask - the hint must still not tell the model 'weather
    never needs a workflow' (the exact self-contradiction of the incident),
    but it does not need the strong run_temp push either."""
    result = agent._try_workflow("what's the weather like today?")
    assert result is None
    hint = _last_hint(agent)
    assert "weather" not in hint.lower()
    assert "create_agent_workflow" in hint  # still offered as an option


@pytest.mark.parametrize(
    "message",
    [
        "how is the local workforce doing this quarter, any hiring news",
        "can you review my workflow doc and suggest edits",
        "I need help streamlining my daily workflow, any tips",
    ],
)
def test_no_match_hint_never_becomes_a_directive_on_a_topical_mention(agent, message):
    """These messages either don't mention VAF workflows at all ('workforce')
    or mention the word without asking VAF to RUN one. The cheap substring
    detector cannot tell the difference (by design - it must stay
    typo-tolerant), so the wording itself must never escalate into something
    a compliant model could read as 'you must call run_temp now' -
    regardless of which branch fires."""
    result = agent._try_workflow(message)
    assert result is None
    hint = _last_hint(agent)
    assert "explicitly asked" not in hint
    assert "Only fall back" not in hint  # the old imperative override phrase


def test_workforce_is_excluded_from_the_explicit_ask_regex(agent):
    result = agent._try_workflow("how is the local workforce doing this quarter?")
    assert result is None
    hint = _last_hint(agent)
    # Falls into the generic (non-explicit) branch, not the workflow-mention one.
    assert "mentions a workflow" not in hint


def test_no_match_hint_stays_generic_without_an_explicit_ask(agent):
    # Both branches mention create_agent_workflow(run_temp) as an option (it
    # is the right tool for ANY ad-hoc multi-step task); what must differ is
    # the strong "the user explicitly asked" framing, reserved for a real ask.
    result = agent._try_workflow("hello, how are you?")
    assert result is None
    hint = _last_hint(agent)
    assert "explicitly asked" not in hint


def test_explicit_ask_hint_mentions_the_multistep_requirement(agent):
    """create_agent_workflow(run_temp) hard-rejects single-step plans
    (vaf/tools/agent_workflow_builder.py). The explicit-ask branch must warn
    of this too, not just the generic branch - otherwise the model gets
    pushed toward run_temp for a plausibly single-step "workflow" request
    (the literal incident example) and hits the tool's own rejection with no
    warning."""
    result = agent._try_workflow("please run this as a workflow: get the weather")
    assert result is None
    hint = _last_hint(agent)
    assert "2+ chained steps" in hint or "2+ steps" in hint
