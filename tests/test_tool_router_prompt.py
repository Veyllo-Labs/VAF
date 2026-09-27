# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The tool router ranks, sees the conversation, reads its tools by category, and the keyword
table only hints.

MEASURED BEFORE (scripts/router_eval.py, twenty fixed cases, and a month of router log lines):
the router LLM, told only to list names, picked a single tool in 164 of 272 turns; the keyword
table forced 5 to 8 tools into a turn on stems inside words ("report" forced the git tools,
"antwort" the mail tools on a WhatsApp reply); the router saw the last 300 characters of the
previous answer and nothing else of the conversation, so a reply to "Bob wrote yesterday ..."
was routed to WhatsApp and only a keyword rescued the mail reply; the set was sorted by name
before the cap, so the plan tool update_working_memory was the first rider cut (missing in 7 of
20 turns, 5 of them turns with a write tool).

MUTATION: sort the task tools by name again and the ranking test goes red; sort the riders by
name and the plan-tool test goes red; union the keyword matches into a successful answer again
and the hint test goes red; return [] from a failed router call and the failure test goes red;
drop `_router_history` from the prompt and the conversation test goes red; list the tools flat
and the category test goes red.
"""
import pytest

from vaf.core.agent import _TURN_RIDERS, _apply_tool_cap, _task_tools_first
from vaf.core.platform import Platform

PINS = {"list_tools", "search_tools"}


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    from vaf.core.agent import Agent
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.current_session_id = "green123456"
    return a


def _router(agent, answer):
    """Point the router's LLM at a fixed answer; returns the list the prompts land in."""
    prompts = []

    def backend(messages=None, **kw):
        prompts.append(str(messages[0]["content"]))
        if isinstance(answer, Exception):
            raise answer
        yield answer

    agent.api_backend.chat_completion = backend
    return prompts


# ── order through the cap ─────────────────────────────────────────────────────────────────

def test_the_router_ranking_survives_the_cap():
    ranked = [f"z_tool_{i:02d}" for i in range(10)] + ["a_tool_late", "b_tool_late", "c_tool_late"]
    out = _apply_tool_cap(_task_tools_first(set(ranked) | PINS, ranked, _TURN_RIDERS), 12, PINS)
    assert out[2:] == ranked[:12], "the cap must cut the router's last picks, not by name"


def test_the_plan_tool_is_the_last_rider_to_be_cut():
    """update_working_memory leads the riders: the plan gate refuses a write tool until a plan
    is stored, and this is the tool that stores it."""
    task = [f"task_{i}" for i in range(6)]
    tools = set(task) | set(_TURN_RIDERS) | PINS
    out = _apply_tool_cap(_task_tools_first(tools, task, _TURN_RIDERS), 8, PINS)
    assert "update_working_memory" in out
    assert "update_user_identity" not in out


# ── the router's own answer ───────────────────────────────────────────────────────────────

def test_the_router_keeps_its_order(agent):
    _router(agent, "send_whatsapp, get_contact")
    assert agent._route_tools("Schick Bob per WhatsApp, dass ich später komme.") == [
        "send_whatsapp", "get_contact"]


def test_keyword_matches_are_hints_when_the_router_answers(agent):
    prompts = _router(agent, "send_whatsapp")
    out = agent._route_tools("Antworte ihm mit ja.")
    assert out == ["send_whatsapp"], "a keyword match must not be forced next to a real answer"
    assert "Keyword hints" in prompts[0] and "reply_mail" in prompts[0]


@pytest.mark.parametrize("answer", [RuntimeError("provider down"), "", "Gern! Wie kann ich helfen?"])
def test_keyword_matches_answer_when_the_router_fails(agent, answer):
    _router(agent, answer)
    assert "reply_mail" in agent._route_tools("Antworte ihm mit ja.")


def test_a_password_in_the_message_keeps_the_store_forced(agent):
    _router(agent, "ssh")
    out = agent._route_tools("Hier das Passwort für meinen Server: geheim123")
    assert out[0] == "store_credential" and "ssh" in out


# ── what the router reads ─────────────────────────────────────────────────────────────────

def test_the_router_sees_the_conversation(agent):
    agent.history = [
        {"role": "user", "content": "[SESSION WORKSPACE] files live in /tmp/x\n\n"
                                    "Such mir die letzte Mail von Bob."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "find_mail", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "find_mail", "content": "ok"},
        {"role": "assistant", "content": "Bob hat gestern geschrieben: Angebot Gartenbau."},
        {"role": "user", "content": "Antworte ihm, dass es passt."},
    ]
    prompts = _router(agent, "reply_mail")
    agent._route_tools("Antworte ihm, dass es passt.")
    block = prompts[0].split("Recent conversation (oldest first):\n", 1)[1].split("User request:", 1)[0]
    assert "User: [SESSION WORKSPACE] files live in /tmp/x Such mir die letzte Mail von Bob." in block
    assert "Assistant: Bob hat gestern geschrieben: Angebot Gartenbau. [tools used: find_mail]" in block
    assert "dass es passt" not in block, "the routed message itself is the request, not history"


def test_a_long_user_message_keeps_its_own_words(agent):
    agent.history = [{"role": "user", "content": "[NOTE] " + "x" * 900 + " meine eigenen Worte"},
                     {"role": "assistant", "content": "ok"},
                     {"role": "user", "content": "weiter"}]
    assert agent._router_history().splitlines()[0].endswith("meine eigenen Worte")


def test_the_router_prompt_groups_tools_by_category(agent):
    prompts = _router(agent, "find_mail")
    agent._route_tools("Such die Mail von Bob.")
    mail = prompts[0].split("## mail\n", 1)[1].split("\n\n", 1)[0]
    assert "- find_mail:" in mail and "- send_whatsapp:" not in mail
    assert "## whatsapp\n" in prompts[0]
