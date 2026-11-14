# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The prompt interface's own chat: a typed turn runs and is saved in one chat.

MEASURED BEFORE THE FIX: every typed turn bound the agent to a chat id no file was ever
written for (`cli-main`), so the agent started an empty history, while the turn was saved
into whatever `current_session` held: the chat `session <id>` had just switched to, or the
chat of the web task the same loop had run last. A switch therefore lost its context on the
next typed turn, and a typed turn after a web task was saved into the web chat.
`session <id>` also put the stored content into the history by hand, through `msg.get` on
the `Message` objects a loaded chat holds, which raised, so the switch failed every time.

Synthetic chats in a temporary store; the agent is a recorder of the one call the lane
makes on it.
"""
from pathlib import Path

import pytest

import vaf.core.session as session_mod
from vaf.cli.cmd import run as run_mod

ROOT = Path(__file__).resolve().parents[1]


class _Agent:
    def __init__(self):
        self.bound = []

    def load_session_context(self, session_id, *, force=False):
        self.bound.append(session_id)


@pytest.fixture
def store(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(session_mod, "default_sessions_dir", lambda: sessions)
    return session_mod.SessionManager(storage_dir=str(sessions))


def _chat(store, name):
    s = store.new(name=name)
    s.add_message("user", f"{name} question")
    store.save(s)
    return s.id


def test_a_typed_turn_after_a_switch_stays_in_the_switched_chat(store):
    """The switch, then one typed turn: the agent and the saved session are the switched
    chat, and a later typed turn after a web task on another chat returns to it.
    MUTATION: bind the agent to a fixed id again - red; return the session of another chat
    - red."""
    opened, switched, web = (_chat(store, n) for n in ("Opened", "Switched", "Web"))
    agent = _Agent()
    assert run_mod._terminal_chat(agent, store, opened).id == opened

    turn = run_mod._terminal_chat(agent, store, switched)
    assert (agent.bound[-1], turn.id) == (switched, switched)
    assert [m.content for m in turn.messages] == ["Switched question"]

    agent.bound.append(web)            # a queued web task ran on its own chat
    turn = run_mod._terminal_chat(agent, store, switched)
    assert (agent.bound[-1], turn.id) == (switched, switched)


def test_a_terminal_chat_whose_file_is_gone_is_created_under_its_id(store):
    """MUTATION: keep the session of the previous chat when the load fails - red."""
    agent = _Agent()
    turn = run_mod._terminal_chat(agent, store, "web-chat-0042")
    assert turn.id == "web-chat-0042" and agent.bound == ["web-chat-0042"]
    assert store.load("web-chat-0042").id == "web-chat-0042"


def test_a_chat_that_cannot_be_read_is_not_replaced(store, monkeypatch):
    """A load failure other than a missing file raises: no new chat is written over the
    unreadable one and the agent is not bound."""
    chat = _chat(store, "Broken")

    def fail(*a, **k):
        raise ValueError("cannot decrypt")
    monkeypatch.setattr(store, "load", fail)
    agent = _Agent()
    with pytest.raises(ValueError):
        run_mod._terminal_chat(agent, store, chat)
    assert agent.bound == []


def test_an_unreadable_chat_is_reported_and_binds_nothing(store, monkeypatch):
    """MUTATION: let the failure through - red."""
    chat = _chat(store, "Broken")

    def fail(*a, **k):
        raise ValueError("cannot decrypt")
    monkeypatch.setattr(store, "load", fail)
    shown = []
    agent = _Agent()
    tui = type("T", (), {"error": lambda self, msg: shown.append(msg)})()
    assert run_mod._bind_terminal_chat(agent, store, chat, tui) is None
    assert agent.bound == [] and len(shown) == 1 and chat in shown[0]
    assert "session <id>" in shown[0]


def test_the_loop_runs_typed_turns_in_the_terminal_chat():
    """The wiring in the prompt interface: the terminal's chat starts as the chat the run
    opened, `session <id>` switches it through the engine's replay, and every typed and
    spoken turn binds the agent and the saved session to it (the real loop's behaviour is
    pinned in tests/test_cli_web_lane_queue_tasks.py). MUTATION: drop the switch's
    assignment - red; put the fixed `cli-main` id back - red; put the hand-written replay
    back - red."""
    src = (ROOT / "vaf" / "cli" / "cmd" / "run.py").read_text(encoding="utf-8")
    loop = src[src.index("def _run_modern("):]
    loop = loop[:loop.index("\ndef ", 1)]
    assert "terminal_chat_id = current_session.id" in loop
    flat = " ".join(loop.split())
    assert flat.count("terminal_session = _bind_terminal_chat(agent, session_mgr, "
                      "terminal_chat_id, tui)") == 2
    assert "if terminal_session is None: continue" in flat
    assert '"cli-main"' not in loop

    start = loop.index('tui.confirm("Switch to this session?"')
    block = loop[start:loop.index('tui.success("Session restored to agent context!")', start)]
    assert "agent.load_session_context(loaded_session.id, force=True)" in block
    assert "terminal_chat_id = loaded_session.id" in block
    assert "agent.history.append" not in block and "msg.get(" not in block
