# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A message the agent could not persist must not be reported as delivered.

Both web deliveries in thinking mode try the anchor chat, and fall back to the newest
web chat when that one is gone. The anchor attempt checked whether the append actually
landed; the FALLBACK did not, in either function. So a chat deleted between being named
as the newest one and being written to - or a save that failed - produced an emit, an
unread badge and a log line saying the message had been sent, for a message that exists
nowhere. The run then waits for an answer to a question nobody was ever shown.

The asymmetry is the whole finding: the right check was three lines above, twice.
"""
from types import SimpleNamespace

import pytest

import vaf.core.thinking_mode as tm


class _Interface:
    """A web interface that records what it was asked to show."""

    def __init__(self):
        self.emitted = []

    def emit_agent_message_append(self, **kw):
        self.emitted.append(kw)

    def emit_session_unread(self, sid):
        self.emitted.append({"unread": sid})


@pytest.fixture()
def wired(monkeypatch):
    interface = _Interface()
    manager = SimpleNamespace(append_background_message=lambda *a, **k: None)
    monkeypatch.setattr(tm, "_latest_web_session_id", lambda scope: "chat-newest")
    return interface, manager


def test_the_question_is_not_claimed_when_the_fallback_chat_cannot_take_it(wired, monkeypatch):
    interface, manager = wired
    monkeypatch.setattr(tm, "get_web_interface", lambda: interface, raising=False)
    monkeypatch.setattr(tm, "SessionManager", lambda *a, **k: manager, raising=False)

    result = tm.emit_message_to_web_ui("scope-a", "Soll ich das tun?", session_id="chat-gone")

    assert result is None, "a message that was not stored is not a delivery"
    assert interface.emitted == [], "and nothing may be shown for it"


def test_the_nudge_is_not_claimed_when_the_fallback_chat_cannot_take_it(wired, monkeypatch):
    """The same asymmetry, in the other function.

    A nudge that returns True stops the run chasing the user, so a claimed one that was
    never written means the person is simply never asked again.
    """
    interface, manager = wired
    import vaf.core.messaging_connections as mc
    import vaf.core.session as session_mod
    import vaf.core.web_interface as web

    monkeypatch.setattr(mc, "send_to_main_messenger", lambda *a, **k: (False, ""))
    monkeypatch.setattr(web, "get_web_interface", lambda: interface)
    monkeypatch.setattr(session_mod, "SessionManager", lambda *a, **k: manager)

    assert tm._send_nudge("scope-a", "user", "Alice", session_id="chat-gone") is False
    assert interface.emitted == []
