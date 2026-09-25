# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""After the person sends a draft, the agent can say so, and can look it up.

Live incident, in one chat. The person asked for a test mail to themselves, the turn ended at
the draft, the person pressed Send, and the wake turn told the agent "Draft mail:N was SENT by
the user". The agent could not stand by that:

- it answered "the test mail is out", and the result-grounding judge called that unsupported,
  because it weighs a reply against the turn's TOOL results and the wake is none; the
  correction that followed told the person the send could not be confirmed;
- asked in the same breath about an earlier draft, it searched for a tool that lists drafts,
  found none, and reported a mail sent the evening before as still waiting;
- its own previous request in that very chat reached it labelled "previous chat", so it took
  the draft it was woken for as somebody else's.

Driven through the REAL `Agent.chat_step` and the real draft ledger (a parked WhatsApp call to
an explicit number), with only the model and the judge replaced.

MUTATION: return [] from `_turn_reports` and the wake test goes red (the judge never sees the
wake), and so does reading the report off the turn's input, which the workspace note pushes
past the judge's cut; give the runner no `_turn_wake` and its source test goes red; drop the "draft" keywords
from `_route_tools` and the router test goes red; label the same chat "previous chat" again
and the prompt test goes red.
"""
import inspect
import json

import pytest

from vaf.core import channel_message_store as store
from vaf.core import outbound_hold
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"
USER = "alice"
SESSION = "green123456"
PHONE = "+491700000000"


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    import vaf.core.subagent_ipc as ipc
    monkeypatch.setattr(ipc, "get_current_session_id", lambda: SESSION)
    store._reset_announce_state()
    from vaf.core.agent import Agent
    from vaf.core.config import Config
    _get = Config.get
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, key, default=None: False if key == "plan_gate_enabled" else _get(key, default)))
    a = Agent(register_signals=False,
              config_overrides={"provider": "openai", "api_key_openai": "sk-test"})
    a.current_session_id = SESSION
    a._current_chat_source = "web"
    a._current_username = USER
    a._current_user_scope_id = SCOPE
    return a


class _Model:
    def __init__(self, *script):
        self.script = list(script)
        self.main_calls = 0

    def __call__(self, messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        self.main_calls += 1
        step = self.script.pop(0) if self.script else "Fertig."
        if isinstance(step, str):
            yield step
            return
        name, args = step
        yield "Einen Moment."
        yield json.dumps({"tool_calls": [{"index": 0, "id": f"c{self.main_calls}", "type": "function",
                                          "function": {"name": name, "arguments": json.dumps(args)}}]})
        yield json.dumps({"finish_reason": "tool_calls"})


def _sent_draft(agent):
    """A draft this chat wrote and the person sent, exactly as the card's Send leaves it."""
    agent.api_backend.chat_completion = _Model(("send_whatsapp", {"to_phone": PHONE, "message": "Hallo Anna"}))
    agent.chat_step(user_input="Schreib Anna auf WhatsApp hallo", stream_callback=lambda t: None)
    ref = outbound_hold.created_refs(agent.history[-2]["content"])[0]
    kind, entry_id = outbound_hold.parse_ref(ref)
    agent.tools["send_whatsapp"].run = lambda **kw: "Message sent via WhatsApp."
    res = outbound_hold.send_draft(kind, entry_id, username=USER, user_scope_id=SCOPE,
                                   tools={"send_whatsapp": agent.tools["send_whatsapp"]}, wake=False)
    assert res["ok"] is True
    return ref, outbound_hold.chat_drafts(USER, SCOPE, SESSION)[0]


def _judged(agent, text, answer):
    """Run one turn whose reply is `answer`, and return what the judge was shown."""
    prompts = []

    def _judge(messages, **kw):
        prompts.append(messages[0]["content"])
        return "</grounded>"

    agent._run_validation_llm = _judge
    agent.api_backend.chat_completion = _Model(answer)
    agent.chat_step(user_input=text, stream_callback=lambda t: None)
    return prompts


REPLY = "Die Nachricht an Anna ist raus, sie wurde gesendet."
# What the runner puts in front of every turn of a chat with a workspace, the wake included:
# longer than the judge's share for one entry, so a report read off the INPUT is cut away.
WORKSPACE_NOTE = ("[SESSION WORKSPACE] All files for this chat are stored in: "
                  "/home/user/Documents/VAF_Projects/ab12cd34/green123456\n" + "x" * 600 + "\n\n")


def test_the_wake_is_evidence_for_the_reply_that_repeats_it(agent):
    ref, row = _sent_draft(agent)
    wake = outbound_hold.wake_text(row)
    agent._turn_wake = ("draft", wake)
    prompts = _judged(agent, WORKSPACE_NOTE + wake, REPLY)
    assert prompts, "the reply claims a send, so the judge is asked"
    assert f"- VAF report: {outbound_hold.DRAFT_WAKE_PREFIX}" in prompts[0]
    assert f"Draft {ref} was SENT by the user" in prompts[0]
    assert "is what VAF itself told the assistant this turn" in prompts[0]


def test_the_same_words_typed_by_a_person_are_no_evidence(agent):
    """The wake is known from the queue, never from its text, which anybody can type."""
    _ref, row = _sent_draft(agent)
    agent._turn_wake = None
    prompts = _judged(agent, outbound_hold.wake_text(row), REPLY)
    assert prompts and "VAF report" not in prompts[0]


def test_the_runner_says_which_wake_a_turn_is_and_forgets_it_after(agent):
    import vaf.core.headless_runner as runner
    src = inspect.getsource(runner)
    assert 'agent._turn_wake = (_wake, str(input_text or "")) if _wake else None' in src
    assert src.count("agent._turn_wake = None") >= 1
    from vaf.core.task_queue import WAKE_KINDS, WAKE_REPORT_KINDS
    assert set(WAKE_REPORT_KINDS) == {"draft", "process"} and set(WAKE_REPORT_KINDS) < set(WAKE_KINDS)


def test_the_agent_can_ask_what_became_of_its_drafts(agent):
    ref, _row = _sent_draft(agent)
    listing = agent.execute_tool("list_drafts", {})
    assert listing.startswith("Drafts (this chat), newest first:"), listing
    assert f"Draft {ref} was SENT by the user (WhatsApp to {PHONE})" in listing

    kind, number = outbound_hold.parse_ref(ref)
    for asked in (ref, str(number)):
        one = agent.execute_tool("list_drafts", {"draft": asked})
        assert f"Draft {ref} was SENT by the user" in one, (asked, one)
    # The words decision_notes recognises, so the next turn does not report it a second time.
    assert outbound_hold._DECIDED_RE.search(one)


def test_a_caller_the_session_names_nobody_still_finds_their_drafts(agent, monkeypatch):
    """The funnel names a nameless tenant by a bucket of their scope (no lookup per call); the
    ledger keys the parked call on the real account name, looked up once at the park. The
    lookup has to ask the way the park did, or the person's own drafts are not there."""
    import vaf.core.thinking_mode as tm
    monkeypatch.setattr(tm, "_resolve_username_for_scope",
                        lambda scope: USER if scope == SCOPE else None)
    agent._current_username = None
    ref, _row = _sent_draft(agent)
    listing = agent.execute_tool("list_drafts", {})
    assert f"Draft {ref} was SENT by the user" in listing, listing


def test_somebody_else_cannot_look_up_the_draft(agent):
    ref, _row = _sent_draft(agent)
    rows = outbound_hold.draft_rows(ref, username="bob", user_scope_id="99999999-2222-3333-4444-555555555555")
    assert rows == []


@pytest.mark.parametrize("state,expected", [
    ("held", "WAITS for the user"),
    ("failed", "was NOT sent: its last attempt failed (bridge offline)"),
    ("ambiguous", "MAY already have been sent"),
    ("discarded", "was DISCARDED by the user"),
    ("sent", "was SENT by the user"),
])
def test_every_state_has_its_own_words(state, expected):
    row = {"ref": "mail:12", "state": state, "channel": "mail", "recipient": "bob@example.com",
           "error": "bridge offline"}
    assert expected in outbound_hold.status_line(row)


def test_the_router_offers_the_lookup_when_a_draft_is_the_topic(agent):
    agent.api_backend.chat_completion = _Model()
    assert "list_drafts" in agent._route_tools(
        "✉ Draft sent: Anna\nDraft call:1 was SENT by the user (WhatsApp to Anna).")
    assert "list_drafts" in agent._route_tools("Ist mein Entwurf an Chuck rausgegangen?")


def test_the_chats_own_last_message_is_not_called_another_chat(agent):
    import time
    li = {"ts": time.time() - 60, "source": "web", "preview": "schick mir eine Testmail",
          "voice": False, "session_id": SESSION}
    pm = agent.prompt_manager
    same = pm.build_prompt(agent.filename, username=USER, user_scope_id=SCOPE,
                           current_source="web", last_interaction=li, session_id=SESSION)
    assert "prior_topic" not in same and "in this chat" in same
    other = pm.build_prompt(agent.filename, username=USER, user_scope_id=SCOPE,
                            current_source="web", last_interaction=li, session_id="red654321")
    assert 'prior_topic: "schick mir eine Testmail" (previous chat' in other


def test_the_record_knows_its_chat(monkeypatch, tmp_path):
    import vaf.core.last_interaction as li
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))
    li.update_last_interaction(SCOPE, source="web", preview="hallo", session_id=SESSION)
    assert li.get_last_interaction(SCOPE)["session_id"] == SESSION
