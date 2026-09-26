# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The tool router classifies the person's words, and a tool a suggestion note names stays
callable.

MEASURED BEFORE THE FIX, in three live turns of the web app ("Setz mit <script> einen lokalen
Testserver auf und teste ihn."): the workflow router suggested an unrelated template, and its
[WORKFLOW SUGGESTION] note carried description="...". The tool router read that note together
with the message, found "script" inside "description", and forced coding_agent, git_status and
git_add_commit into all three turns; in one of them host_bash, the tool the task needed, fell out
of the capped set. The workflow and skill routers already read the raw message
(chat_step(raw_user_input=...)); the tool router had been left on the enriched text.

MUTATION: route on the enriched user_input again and the first two tests go red; drop the note
pin and the third goes red.
"""
import pytest

from vaf.core.platform import Platform

TEXT = "Setz einen lokalen Testserver auf und teste ihn."
WORKFLOW_HINT = {"name": "Create File", "workflow_id": "create_file",
                 "variables": {"description": "ein Testserver", "filename": "server.py"}}
SKILL_HINT = {"name": "Deploy", "skill_id": "deploy"}


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.current_session_id = "green123456"

    def model(messages=None, **kw):
        yield "Fertig."

    a.api_backend.chat_completion = model
    return a


def _turn(agent, monkeypatch, *, user_input, raw=None, hint=None, routed=("web_search",)):
    seen = []

    def route(text):
        seen.append(text)
        return list(routed)

    def try_workflow(text, stream_callback, route_input=None):
        if hint == "workflow":
            agent._pending_workflow_hint = dict(WORKFLOW_HINT)
        elif hint == "skill":
            agent._pending_skill_hint = dict(SKILL_HINT)
        return None

    monkeypatch.setattr(agent, "_route_tools", route)
    monkeypatch.setattr(agent, "_try_workflow", try_workflow)
    agent.chat_step(user_input=user_input, raw_user_input=raw, stream_callback=lambda t: None)
    return seen, list(agent._active_tools or [])


def test_the_router_reads_the_persons_words_not_the_workflow_note(agent, monkeypatch):
    """The CLI hands over the raw message; the note is what chat_step itself prepends."""
    seen, _tools = _turn(agent, monkeypatch, user_input=TEXT, hint="workflow")
    assert seen == [TEXT]


def test_the_router_reads_the_raw_message_not_the_lane_enrichment(agent, monkeypatch):
    enriched = ("[SESSION WORKSPACE] All files for this chat are stored in: /tmp/x\n"
                'To edit or modify: coding_agent(task="<your task>", project_path="/tmp/x")\n\n' + TEXT)
    seen, _tools = _turn(agent, monkeypatch, user_input=enriched, raw=TEXT, hint="workflow")
    assert seen == [TEXT]


@pytest.mark.parametrize("hint, tool", [("workflow", "execute_workflow"), ("skill", "use_skill")])
def test_the_tool_a_note_names_is_callable(agent, monkeypatch, hint, tool):
    """The note tells the agent to call this tool; the router, reading only the person's
    words, has no reason to pick it."""
    assert tool in agent.tools
    _seen, tools = _turn(agent, monkeypatch, user_input=TEXT, hint=hint)
    assert tool in tools


def test_the_pin_lasts_one_turn(agent, monkeypatch):
    _turn(agent, monkeypatch, user_input=TEXT, hint="workflow")
    _seen, tools = _turn(agent, monkeypatch, user_input="Und jetzt das Wetter?", hint=None)
    assert "execute_workflow" not in tools


def test_the_measured_trigger(agent, monkeypatch):
    """Why it mattered: the keyword heuristics match inside words, so the note's
    `description` reads as "script" and forces the coding tools. The person's own words do
    not. (Evidence for the fix above; the heuristics themselves are unchanged.)"""
    from vaf.core.agent import _build_workflow_suggestion_note

    agent.api_backend.chat_completion = lambda messages=None, **kw: iter(())
    note = _build_workflow_suggestion_note(WORKFLOW_HINT, TEXT)
    assert "coding_agent" in agent._route_tools(note + "\n" + TEXT)
    assert "coding_agent" not in agent._route_tools(TEXT)
