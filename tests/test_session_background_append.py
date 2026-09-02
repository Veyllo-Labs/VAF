# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A message a background lane writes into a chat reaches the agent that is
already on that chat.

`SessionManager.append_background_message` is the one way a lane that is not
the live turn (a thinking-mode question and its nudge, an automation result, a
router-delivered messenger message) appends to a session file. It persists the
message with its kind and leaves a note; `Agent.load_session_context` consumes
the note and rebuilds from the file even when it is already on that session -
the early return that made the in-memory history authoritative is exactly what
kept the user's own chat file out of the agent's context. Five sites used to
hand-roll the load/add/save half and none could leave the note.
"""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

import vaf.core.session as session_mod
from vaf.core.session import (
    Session, SessionManager, note_transcript_changed, take_transcript_changed,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(session_mod, "default_sessions_dir", lambda: tmp_path / "sessions")
    return tmp_path / "sessions"


# ── the primitive ──────────────────────────────────────────────────────────

def test_the_message_is_persisted_with_its_kind_and_the_note_is_left(store):
    sm = SessionManager()
    sess = Session(id="telegram_12345", name="Telegram 12345")
    sess.add_message("user", "hallo")
    sm.save(sess)

    out = sm.append_background_message("telegram_12345", "Soll ich das vorbereiten?", kind="thinking")

    assert out is not None
    reloaded = SessionManager().load("telegram_12345", restore_state=False, repoint=False)
    assert [(m.role, m.content, m.kind) for m in reloaded.messages] == [
        ("user", "hallo", None),
        ("assistant", "Soll ich das vorbereiten?", "thinking"),
    ]
    assert take_transcript_changed("telegram_12345") is True
    assert take_transcript_changed("telegram_12345") is False, "the note is consumed once"


def test_a_missing_session_is_not_invented_unless_asked(store):
    """MUTATION: create unconditionally. The Web UI lanes fall back to the latest
    chat when the anchor is gone; a session invented under the anchor's id would
    hide the question in a chat nobody opens."""
    sm = SessionManager()
    assert sm.append_background_message("web-gone", "Q?", kind="thinking") is None
    assert take_transcript_changed("web-gone") is False


def test_an_outbound_first_channel_session_is_created_and_owned(store):
    """The messenger mirror builds the session when the user's first message on a
    channel is the agent's own (an automation result before any inbound), named
    and stamped with the owner scope so the ownership gates let them open it."""
    sm = SessionManager()
    out = sm.append_background_message(
        "discord_777", "Wetter: 21 Grad", kind="automation", create=True,
        name="Discord 777", user_scope_id="ab12cd34-0000-0000-0000-000000000000",
    )
    assert out is not None and out.name == "Discord 777"
    reloaded = SessionManager().load("discord_777", restore_state=False, repoint=False)
    assert reloaded.metadata.get("user_scope_id") == "ab12cd34-0000-0000-0000-000000000000"
    assert reloaded.messages[0].kind == "automation"


def test_an_empty_message_writes_nothing(store):
    sm = SessionManager()
    assert sm.append_background_message("telegram_1", "   ", create=True) is None
    assert not (store / "telegram_1.json").exists()
    assert take_transcript_changed("telegram_1") is False


def test_the_note_is_per_session_and_thread_safe_in_shape():
    note_transcript_changed("a")
    note_transcript_changed("b")
    assert take_transcript_changed("a") is True
    assert take_transcript_changed("b") is True
    assert take_transcript_changed("") is False
    assert take_transcript_changed(None) is False


# ── the consumer: a live agent rebuilds from the file ──────────────────────

def _agent_on(session_id: str, history):
    from vaf.core.agent import Agent
    a = Agent.__new__(Agent)
    a.history = list(history)
    a.state_registry = None
    a.main_persistence = None
    a.current_session_id = session_id
    a.get_token_usage = lambda: (100, 128000)
    a.init_chat = lambda: a.__dict__.__setitem__("history", [{"role": "system", "content": "sys"}])
    a._bind_session_persistence = lambda sid: None
    a._broadcast_context_status = lambda: None
    return a


def _fake_manager(messages):
    class FakeMessage(SimpleNamespace):
        pass

    fake_session = SimpleNamespace(
        messages=[FakeMessage(role=r, content=c, metadata=None, tool_calls=None,
                              tool_call_id=None, name=None) for r, c in messages],
        runtime_state=None,
        metadata={},
    )

    class FakeSessionManager:
        loads = 0

        def __init__(self, *args, **kwargs):
            pass

        def load(self, session_id, restore_state=True, repoint=True):
            FakeSessionManager.loads += 1
            return fake_session

    return FakeSessionManager


def test_a_transcript_appended_behind_the_agent_is_rebuilt(monkeypatch):
    """MUTATION: drop `changed_underneath` from the early return.

    The agent is already on the Telegram session with the user's greeting in
    memory. A background pass appends its question to the FILE and leaves the
    note. The next turn on the same session must see the question.
    """
    import vaf.core.subagent_ipc as ipc_mod
    stale = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hallo"}]
    on_disk = [("user", "hallo"), ("assistant", "Soll ich das abends vorbereiten?")]
    fsm = _fake_manager(on_disk)
    monkeypatch.setattr(session_mod, "SessionManager", fsm)
    monkeypatch.setattr(ipc_mod, "set_current_session_id", lambda sid: None)

    a = _agent_on("telegram_12345", stale)
    note_transcript_changed("telegram_12345")
    a.load_session_context("telegram_12345")

    assert fsm.loads == 1, "the file was not re-read"
    assert any(m.get("content") == "Soll ich das abends vorbereiten?" for m in a.history), (
        "the question a background pass wrote into the chat file is still missing from context")
    assert take_transcript_changed("telegram_12345") is False, "the note must be consumed by the reload"


def test_without_a_note_the_live_history_stays_authoritative(monkeypatch):
    """The early return is the design for every ordinary turn: an agent already on
    the session does not re-read its own file. The note, not the turn, decides."""
    import vaf.core.subagent_ipc as ipc_mod
    fsm = _fake_manager([("user", "hallo"), ("assistant", "irrelevant")])
    monkeypatch.setattr(session_mod, "SessionManager", fsm)
    monkeypatch.setattr(ipc_mod, "set_current_session_id", lambda sid: None)

    live = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hallo"}]
    a = _agent_on("telegram_12345", live)
    take_transcript_changed("telegram_12345")  # make sure no note is standing
    a.load_session_context("telegram_12345")

    assert fsm.loads == 0
    assert a.history == live


def test_a_note_for_another_session_is_consumed_by_the_switch(monkeypatch):
    """Switching to a session always rebuilds from its file; the note for that
    session is spent by the switch so it cannot force a second rebuild later."""
    import vaf.core.subagent_ipc as ipc_mod
    fsm = _fake_manager([("user", "hi")])
    monkeypatch.setattr(session_mod, "SessionManager", fsm)
    monkeypatch.setattr(ipc_mod, "set_current_session_id", lambda sid: None)

    a = _agent_on("web-other", [{"role": "system", "content": "sys"}])
    note_transcript_changed("telegram_12345")
    a.load_session_context("telegram_12345")

    assert a.current_session_id == "telegram_12345"
    assert take_transcript_changed("telegram_12345") is False
