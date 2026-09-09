# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The agent's question to the person marks the contact's chat (vaf/core/agent.py,
vaf/core/headless_runner.py).

In Front Office mode a send tool that names no foreign recipient reaches the owner: that is
the back-channel question, and the inbox shows the contact's chat as waiting for the person
until they open the chat or answer, or the agent writes to the contact again. The runner stamps which chat the
turn belongs to next to the Front Office flag and resets it wherever that flag resets.

MUTATION: drop the mark in Agent._record_owner_question and the first test goes red; stamp
the chat without resetting it and the source guard goes red.
"""
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core import channel_message_store as store
from vaf.core import inbox
from vaf.core.agent import Agent
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"
NOW = time.time() - 3600.0
RUNNER = Path(__file__).resolve().parent.parent / "vaf" / "core" / "headless_runner.py"


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.config as cfg_mod
    cfg = {"whatsapp_config": {"whitelist": [], "reply_window_hours": 72}, "telegram_config": {}, "discord_config": {}}
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    import vaf.core.contacts_store as contacts
    monkeypatch.setattr(contacts, "front_office_endpoints", lambda username=None, user_scope_id=None, channel="whatsapp": {"+491700000042"})
    monkeypatch.setattr(contacts, "is_local_admin_caller", lambda username, user_scope_id: False)
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    store._reset_announce_state()   # cancels the timers an earlier test left behind; the dicts stay the module's own
    import vaf.core.session as session_mod
    monkeypatch.setattr(session_mod, "_room_rows", lambda scope: [])


def _agent(front_office=True, chat={"channel": "whatsapp", "chat_id": "+491700000042"}):
    return SimpleNamespace(_event_sink=None, _is_channel_turn=lambda: True, _front_office_mode=front_office,
                           _front_office_chat=chat, _current_username="alice", _current_user_scope_id=SCOPE,
                           tools={}, _active_tools=None,
                           _record_owner_question=lambda *a, **k: Agent._record_owner_question(agent_box[0], *a, **k))


agent_box = [None]


def _dispatch(agent, name, args, result):
    agent_box[0] = agent
    return Agent._chat_post_dispatch(agent, name, args, result)


def _mark():
    return store.chat_marks("alice", user_scope_id=SCOPE).get(("whatsapp", "+491700000042"), {}).get("owner_asked_ts")


def test_a_successful_send_to_the_owner_in_front_office_marks_the_contacts_chat(world):
    store.append_message("alice", "+491700000042", "can I get the invoice?", ts=NOW - 900, user_scope_id=SCOPE)
    store.append_message("alice", "+491700000042", "let me ask", direction="out", ts=NOW - 800, user_scope_id=SCOPE)
    assert _dispatch(_agent(), "send_whatsapp", {"message": "Bob asks for the invoice, ok?"}, "Message sent to owner") == "Message sent to owner"
    assert _mark() is not None
    row = next(r for r in inbox.list_conversations("alice", SCOPE, now=NOW + 5000)["rows"] if r["key"] == "whatsapp:+491700000042")
    assert row["waits"] and row["waits_reason"] == inbox.WAITS_OWNER_ASKED
    # The agent writing to the contact again answers its own question.
    store.append_message("alice", "+491700000042", "the invoice is on its way", direction="out", ts=time.time() + 5, user_scope_id=SCOPE)
    row = next(r for r in inbox.list_conversations("alice", SCOPE, now=time.time() + 10)["rows"] if r["key"] == "whatsapp:+491700000042")
    assert not row["waits"]


def test_a_send_to_a_third_party_a_failed_send_and_a_normal_turn_mark_nothing(world):
    store.append_message("alice", "+491700000042", "hi", ts=NOW - 900, user_scope_id=SCOPE)
    _dispatch(_agent(), "send_whatsapp", {"message": "hi", "to_phone": "+491700000099"}, "Message sent")
    assert _mark() is None, "a send to a third party is not a question to the owner"
    _dispatch(_agent(), "send_telegram", {"message": "x"}, "Error: Telegram not configured")
    assert _mark() is None, "a failed send asked nobody"
    _dispatch(_agent(front_office=False), "send_telegram", {"message": "x"}, "Sent")
    assert _mark() is None, "outside Front Office there is no contact to record on"
    _dispatch(_agent(chat=None), "send_to_user", {"message": "x"}, "Sent")
    assert _mark() is None, "an unresolved chat (a bare @lid) has no row"
    _dispatch(_agent(), "send_to_user", {"message": "x"}, "Sent via Telegram")
    assert _mark() is not None, "send_to_user reaches the owner by definition"


def test_the_runner_stamps_the_chat_where_it_sets_the_mode_and_resets_it_where_it_resets_it():
    src = RUNNER.read_text(encoding="utf-8")
    sets = len(re.findall(r"agent\._front_office_mode = True", src))
    resets = len(re.findall(r"agent\._front_office_mode = False", src))
    assert sets == len(re.findall(r"agent\._front_office_chat = _front_office_chat_ref\(", src)) == 1
    assert resets == len(re.findall(r"agent\._front_office_chat = None", src)) == 2
    from vaf.core.headless_runner import _front_office_chat_ref
    assert _front_office_chat_ref({"telegram_chat_id": "7"}, "alice") == {"channel": "telegram", "chat_id": "7"}
    assert _front_office_chat_ref({"discord_author_id": "42"}, "alice") == {"channel": "discord", "chat_id": "42"}
    assert _front_office_chat_ref({}, "alice") is None
