# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The chat turn ends at a draft, and the next turn hears what became of it.

Driven through the REAL `Agent.chat_step` with only the model replaced, because both halves
live in the loop and a source check cannot tell whether they fire: a round of tool calls that
parks a draft is the last thing the turn does (no second model call, no "your draft is ready"
sentence - the card is the answer), and a later turn in that chat gets one `[Context:` note
for a draft the person has decided on since.

MUTATION: delete the turn-end block after the round's deferred messages in `chat_step` and
the first test goes red (a second model call happens and its text is the answer); drop the
`_note_decided_drafts()` call and the third goes red.
"""
import json

import pytest

from vaf.core import channel_message_store as store
from vaf.core import outbound_hold
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"
USER = "alice"
SESSION = "green123456"


@pytest.fixture
def agent(monkeypatch, tmp_path):
    # The loop reads and writes its working memory relative to the working directory.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    store._reset_announce_state()
    from vaf.core.agent import Agent
    # The plan gate would bounce the first state-changing call of a turn; it is its own
    # subject, and here the call has to reach the hold. It reads the global config.
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
    """The provider: the main call (the one that carries tools) answers from `script`, one
    entry per round; every side call (router, validation) gets a short plain answer."""

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


def _draft_turn(agent):
    model = _Model(("send_whatsapp", {"to_phone": "+491700000000", "message": "Hallo Anna"}),
                   "Der Entwurf liegt bereit.")
    agent.api_backend.chat_completion = model
    out = agent.chat_step(user_input="Schreib Anna auf WhatsApp hallo", stream_callback=lambda t: None)
    return model, out


def test_a_round_that_parks_a_draft_ends_the_turn(agent):
    model, out = _draft_turn(agent)
    assert out == outbound_hold.TURN_ENDS_AT_DRAFT
    assert model.main_calls == 1, "nothing is generated after the draft: the card is the answer"
    tool_msg, last = agent.history[-2], agent.history[-1]
    assert tool_msg["role"] == "tool" and tool_msg["content"].startswith("NOT SENT YET. Draft call:")
    assert last == {"role": "assistant", "content": outbound_hold.TURN_ENDS_AT_DRAFT}
    rows = outbound_hold.chat_drafts(USER, SCOPE, SESSION)
    assert [(r["state"], r["recipient"], r["preview"]) for r in rows] == [("held", "+491700000000", "Hallo Anna")]
    assert agent._turn_stops_for_draft is False, "the flag is spent with the turn"


def test_a_round_without_a_draft_goes_on(agent):
    model = _Model(("send_whatsapp", {"message": "an mich selbst"}), "Erledigt.")
    agent.api_backend.chat_completion = model
    agent.tools["send_whatsapp"].run = lambda **kw: "Message sent via WhatsApp."
    out = agent.chat_step(user_input="Schick mir eine Notiz", stream_callback=lambda t: None)
    # What the answer finally reads is the answer guards' business; here it is only that the
    # turn went on to a second model call.
    assert model.main_calls == 2, "a send to the person's own chat is no draft"
    assert out != outbound_hold.TURN_ENDS_AT_DRAFT
    assert outbound_hold.chat_drafts(USER, SCOPE, SESSION) == []


def test_the_next_turn_hears_what_became_of_the_draft(agent):
    _draft_turn(agent)
    ref = outbound_hold.created_refs(agent.history[-2]["content"])[0]
    kind, entry_id = outbound_hold.parse_ref(ref)
    assert outbound_hold.discard_draft(kind, entry_id, username=USER, user_scope_id=SCOPE)

    seen = []

    class _Seeing(_Model):
        def __call__(self, messages=None, **kw):
            if kw.get("tools"):
                seen.append(list(messages or []))
            return super().__call__(messages=messages, **kw)

    agent.api_backend.chat_completion = _Seeing("Alles klar.")
    agent.chat_step(user_input="Lass es doch", stream_callback=lambda t: None)
    user_at = max(i for i, m in enumerate(agent.history) if m.get("role") == "user")
    note = agent.history[user_at - 1]
    assert note["role"] == "system" and note["content"].startswith("[Context:")
    assert f"Draft {ref} was DISCARDED by the user" in note["content"]
    assert agent._turn_decision_note == note["content"], "the runner stores exactly this"
    # BEFORE the input, in what the model receives too, so the person's words are not followed
    # by the engine's note: the turn block goes in front of a trailing user message, and a
    # note after the input would push the block behind the request. MUTATION: append the note
    # after the input. (A first-time user also gets the onboarding hint after the input; that
    # is older and not this note's business.)
    sent = seen[0]
    user_i = max(i for i, m in enumerate(sent) if m.get("role") == "user" and "Lass es doch" in str(m.get("content")))
    note_i = max(i for i, m in enumerate(sent) if f"Draft {ref} was DISCARDED" in str(m.get("content")))
    assert note_i == user_i - 1, (note_i, user_i)

    agent.api_backend.chat_completion = _Model("Gern.")
    agent.chat_step(user_input="Danke", stream_callback=lambda t: None)
    notes = [m for m in agent.history if m.get("role") == "system"
             and str(m.get("content")).startswith("[Context: what became") and f"Draft {ref} was" in str(m.get("content"))]
    assert len(notes) == 1, "told once"


def test_a_wake_turn_reports_its_own_draft(agent):
    """The wake text says what was sent, so the turn it starts adds no second note for it."""
    _draft_turn(agent)
    ref = outbound_hold.created_refs(agent.history[-2]["content"])[0]
    kind, entry_id = outbound_hold.parse_ref(ref)
    agent.tools["send_whatsapp"].run = lambda **kw: "Message sent via WhatsApp."
    res = outbound_hold.send_draft(kind, entry_id, username=USER, user_scope_id=SCOPE,
                                   tools={"send_whatsapp": agent.tools["send_whatsapp"]}, wake=False)
    assert res["ok"] is True
    row = outbound_hold.chat_drafts(USER, SCOPE, SESSION)[0]
    agent.api_backend.chat_completion = _Model("Ist raus.")
    agent.chat_step(user_input=outbound_hold.wake_text(row), stream_callback=lambda t: None)
    assert not any(m.get("role") == "system" and str(m.get("content")).startswith("[Context: what became")
                   for m in agent.history)
