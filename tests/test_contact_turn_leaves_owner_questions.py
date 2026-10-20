# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A contact's message is never the owner's answer.

A Front Office turn runs under the OWNER's identity and scope, so every latch that is keyed on
that scope reads the owner's state. MEASURED BEFORE THE FIX, on the real `chat_step` with only the
provider faked and the Front Office set applied the way the runner applies it: a contact's "ja"
put the owner's background question, the proposal and the findings behind it into the contact's
prompt, moved the request to `replied` with the contact's text as the owner's reply, deleted the
waiting record (the next background run then classified the request from the contact's words),
resolved an automation's handoff bundle, cleared the owner's ask-first latch, latched it again with
the contact's own question, and the contact's system prompt carried the owner's last message from
another chat as `prior_topic`. The facade recipe in docs/EMBEDDING.md (`set_allowed_tools(...)`,
then `run(message_from_a_customer)`) did the same.

The signal is the allowed set: it exists for "an agent that answers somebody else under its
owner's identity", so a turn under it is a person, but never the agent's own user.

MUTATIONS, each turns the named tests red:
- drop the `_allowed_tools` clause from `_turn_is_from_the_user` -> the contact tests, the
  handoff, presence and facade tests, the seeded-latch half;
- drop the set condition at the ask-first latch setter -> the contact-question latch test;
- point the intent lock at `_turn_is_from_the_user` -> the intent test;
- drop the set guard at the chat_step prompt build -> the chat_step last-interaction test;
- drop it at the init_chat prompt build -> the init_chat last-interaction test;
- make `_turn_is_from_the_user` return False always -> the owner control.
"""
import json
import re

import pytest

import vaf
from vaf.core import handoff_bundle as hb
from vaf.core import thinking_mode as tm
from vaf.core import thinking_requests as treq
from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
from vaf.core.last_interaction import update_last_interaction
from vaf.core.platform import Platform

CONTACT_SESSION = "green123456"
OWNER_SESSION = "red654321"
SCOPE = "6f9619ff-8b86-d011-b42d-00c04fc964ff"

QUESTION = "Soll ich deinen Zahnarzttermin am Freitag absagen?"
PROPOSAL = "Absage des Zahnarzttermins per Mail an die Praxis Weber"
DETAILS = "Praxis Weber, Freitag 9:30, kostenlose Absage bis Donnerstag 18 Uhr"
OWNER_LAST = "Kuendige bitte den Handyvertrag bei Nordfunk"
LONG_MESSAGE = ("Guten Tag, ich wollte fragen, ob Herr Muster naechste Woche Zeit fuer ein "
                "kurzes Gespraech ueber das Angebot hat.")


@pytest.fixture
def make_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))

    def _make(*, front_office=True, init=True):
        from vaf.core.agent import Agent
        a = Agent(register_signals=False, run_kind="chat",
                  config_overrides={"provider": "deepseek", "api_key_deepseek": "sk-test"})
        a.current_session_id = CONTACT_SESSION
        a._current_chat_source = "whatsapp"
        # The owner's identity, as on a contact's turn: an admin, with a scope.
        a._current_username = "alice"
        a._current_user_scope_id = SCOPE
        a._current_user_role = "admin"
        monkeypatch.setattr(a, "_try_workflow", lambda *args, **kw: None)
        if front_office:
            # What the runner sets for a `from_contact` task: a queue turn a person sent.
            a._front_office_mode = True
            a._turn_is_human = True
            a.set_allowed_tools(FRONT_OFFICE_ALLOWED_TOOLS)
        if init:
            a.init_chat()
            a._bind_session_persistence(CONTACT_SESSION)
        return a
    return _make


def _seed_question(*, nudged=False):
    """The owner's open background question: a tracked request and the waiting record."""
    req = treq.add_request(SCOPE, question=QUESTION, run_seq=1,
                           proposed_action=PROPOSAL, details=DETAILS)
    tm.set_waiting_for_reply(SCOPE, username="alice", display_name="Alice",
                             question_text=QUESTION, request_id=req["id"],
                             session_id=OWNER_SESSION, channel="telegram")
    if nudged:
        data = tm._load_waiting()
        data[tm._key(SCOPE)]["nudge_sent_at_ts"] = data[tm._key(SCOPE)]["question_sent_at_ts"] + 60
        tm._save_waiting(data)
    return req


class _Provider:
    """Records every request, the main call (it carries tools) and every side call alike."""

    def __init__(self, answer="Gern. Wann passt es Ihnen?"):
        self.answer, self.texts, self.main = answer, [], []

    def __call__(self, messages=None, **kw):
        self.texts.append(json.dumps(messages or [], ensure_ascii=False, default=str))
        if kw.get("tools"):
            self.main.append(messages or [])
            yield self.answer
            return
        yield "no"


def _turn(agent, text, provider=None):
    provider = provider or _Provider()
    agent.api_backend.chat_completion = provider
    agent.chat_step(user_input=text, raw_user_input=text, stream_callback=lambda t: None)
    return provider


def _system(messages) -> str:
    return messages[0]["content"] if messages and messages[0].get("role") == "system" else ""


def _assert_owner_question_untouched(req, before):
    after = treq.get_request(SCOPE, req["id"])
    assert after["status"] == "asked", after
    assert after["user_reply"] is None and after["main_reply"] is None, after
    waiting = tm.get_waiting_for_reply(SCOPE)
    assert waiting is not None, "the contact's turn deleted the owner's waiting record"
    for field in ("question_text", "request_id", "question_sent_at_ts"):
        assert waiting[field] == before[field], field
    assert tm._key(SCOPE) not in tm._load_last_reply(), (
        "the contact's text was saved as the owner's reply for the next background run")


def _assert_nothing_of_the_question_reached_the_model(provider):
    assert provider.main, "the turn never reached the model"
    for text in provider.texts:
        for secret in (QUESTION, PROPOSAL, DETAILS):
            assert secret not in text, f"{secret!r} reached the contact's turn"


# ── the reply pickup ─────────────────────────────────────────────────────────

def test_a_contacts_yes_leaves_the_owners_question_open(make_agent):
    agent = make_agent()
    req = _seed_question()
    before = dict(tm.get_waiting_for_reply(SCOPE))

    provider = _turn(agent, "ja")

    _assert_owner_question_untouched(req, before)
    _assert_nothing_of_the_question_reached_the_model(provider)


def test_a_contacts_long_message_does_not_carry_the_question_either(make_agent):
    """The new_topic lane: a message over the confirmation length still carried the question."""
    assert len(LONG_MESSAGE) > vaf.CoreAgent._REPLY_CONFIRMATION_MAX_CHARS
    agent = make_agent()
    req = _seed_question()
    before = dict(tm.get_waiting_for_reply(SCOPE))

    provider = _turn(agent, LONG_MESSAGE)

    _assert_owner_question_untouched(req, before)
    _assert_nothing_of_the_question_reached_the_model(provider)


def test_a_contacts_yes_does_not_open_an_automations_handoff(make_agent, monkeypatch):
    monkeypatch.setattr(tm, "_latest_web_session_id", lambda scope: None)
    monkeypatch.setattr(tm, "_main_agent_busy", lambda scope: True)   # defer, no live emit
    agent = make_agent()
    step = "found three slots at the garage, Tuesday 8:00 is the cheapest"
    handoff = hb.deliver_handoff(
        SCOPE, message=QUESTION, proposed_action=PROPOSAL, details=DETAILS, username="alice",
        history=[{"role": "user", "content": "check the garage"},
                 {"role": "tool", "name": "web_search", "content": step}],
    )

    provider = _turn(agent, "ja")

    assert hb.load(SCOPE, handoff["bundle_id"])["status"] == "open", "the bundle was resolved"
    assert treq.get_request(SCOPE, handoff["id"])["status"] == "asked"
    for text in provider.texts:
        assert DETAILS not in text and step not in text, "the bundle reached the contact's turn"
        assert QUESTION not in text


def test_a_contacts_yes_after_a_nudge_does_not_rearm_the_owners_wait(make_agent):
    """The presence lane re-arms the wait and has the agent re-ask the owner's question."""
    agent = make_agent()
    _seed_question(nudged=True)
    before = dict(tm.get_waiting_for_reply(SCOPE))

    provider = _turn(agent, "ja")

    assert tm.get_waiting_for_reply(SCOPE) == before
    _assert_nothing_of_the_question_reached_the_model(provider)


def test_the_owner_still_answers_their_own_question(make_agent):
    """Control: the same seed with no set, the owner says "ja"."""
    agent = make_agent(front_office=False)
    req = _seed_question()

    provider = _turn(agent, "ja")

    assert QUESTION in json.dumps(provider.main[0], ensure_ascii=False)
    after = treq.get_request(SCOPE, req["id"])
    assert after["status"] == "replied" and after["user_reply"] == "ja", after
    assert tm.get_waiting_for_reply(SCOPE) is None


# ── the ask-first latch ──────────────────────────────────────────────────────

def test_a_contacts_turn_leaves_the_owners_latch_set(make_agent):
    agent = make_agent()
    _seed_question()
    seeded = {"preview": "Soll ich die Datei loeschen?"}
    agent._pending_user_question = dict(seeded)

    _turn(agent, "ja")

    assert agent._pending_user_question == seeded


@pytest.mark.parametrize("front_office", [True, False])
def test_a_question_to_a_contact_does_not_latch_the_owners_agent(make_agent, monkeypatch, front_office):
    """One agent serves every chat of a runner worker: a contact's "when does it suit you?"
    latched the owner's agent, and the owner's drain turns then could not start a write tool.
    Without a set (the control) the same reply latches it."""
    agent = make_agent(front_office=front_office)
    agent._pending_user_question = None
    monkeypatch.setattr(agent, "_reply_needs_user", lambda reply: True)

    _turn(agent, "Ich haette gern einen Termin")

    if front_office:
        assert agent._pending_user_question is None
    else:
        assert agent._pending_user_question, "the control no longer latches"


# ── what the contact's own chat keeps ────────────────────────────────────────

def test_the_contacts_words_stay_the_goal_of_the_contacts_chat(make_agent):
    agent = make_agent()
    provider = _turn(agent, "Ich haette gern einen Termin naechste Woche")
    assert agent.main_persistence.get_user_intent() == "Ich haette gern einen Termin naechste Woche"
    block = re.search(r"<user_intent>\s*(.*?)\s*</user_intent>", _system(provider.main[0]), re.S)
    if block:
        assert block.group(1) == "Ich haette gern einen Termin naechste Woche"


# ── the owner's last interaction ─────────────────────────────────────────────

def _seed_owner_last_interaction():
    update_last_interaction(user_scope_id=SCOPE, source="telegram", preview=OWNER_LAST,
                            session_id=OWNER_SESSION)


def test_a_contacts_prompt_carries_no_last_interaction_of_the_owner(make_agent):
    _seed_owner_last_interaction()
    agent = make_agent()

    provider = _turn(agent, "Hallo")

    system = _system(provider.main[0])
    assert "last_interaction:" not in system and "prior_topic" not in system
    assert OWNER_LAST not in system


def test_the_set_before_init_chat_keeps_the_line_out_too(make_agent):
    """The facade applies the set before init_chat; the runner after it. Both builds must hold."""
    _seed_owner_last_interaction()
    agent = make_agent(init=False)
    agent.init_chat()
    system = agent.history[0]["content"]
    assert "last_interaction:" not in system and "prior_topic" not in system
    assert OWNER_LAST not in system


def test_the_owners_own_chat_still_carries_the_line(make_agent):
    """Control: no set, the owner writes."""
    _seed_owner_last_interaction()
    agent = make_agent(front_office=False, init=False)
    agent.init_chat()
    assert f'prior_topic: "{OWNER_LAST}"' in agent.history[0]["content"]

    provider = _turn(agent, "Hallo")

    assert f'prior_topic: "{OWNER_LAST}"' in _system(provider.main[0])


# ── the facade recipe ────────────────────────────────────────────────────────

def test_the_facade_recipe_does_not_answer_the_owners_question(monkeypatch, tmp_path):
    """docs/EMBEDDING.md: `set_allowed_tools(...)`, then `run(message_from_a_customer)`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path / "vaf"))
    # The facade setdefaults this; deleting it first makes monkeypatch restore the absence.
    monkeypatch.delenv("VAF_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(tm, "_latest_web_session_id", lambda scope: None)
    monkeypatch.setattr(tm, "_main_agent_busy", lambda scope: True)
    facade = vaf.Agent(config={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    facade.set_allowed_tools({"web_search"})
    core = facade.core
    scope = getattr(core, "_current_user_scope_id", None)
    hb.deliver_handoff(scope, message=QUESTION, proposed_action=PROPOSAL, details=DETAILS)
    before = dict(tm.get_waiting_for_reply(scope))
    provider = _Provider()
    core.api_backend.chat_completion = provider

    facade.run("ja")

    assert provider.main, "the turn never reached the model"
    assert tm.get_waiting_for_reply(scope) == before
    for text in provider.texts:
        assert QUESTION not in text and PROPOSAL not in text and DETAILS not in text
