# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A backgrounded chat's completion must reach the person who owns it.

The live stream of a chat is delivered per SUBSCRIPTION, and a browser holds
exactly one subscription: the chat that is open. Switching chats while a turn
runs moves that subscription, so the terminal event of the old chat
(`message_complete`) went to nobody - measured live: the browser's own
handler for "a chat I am not looking at finished" (sidebar unread mark, loader
bookkeeping) was unreachable, because the event it waits for could not arrive.

The fix is a delivery decision, taken once (`session_event_coroutine`): a type
in TERMINAL_SESSION_EVENTS reaches the session's subscribers AND the owner's
other connections; every other type stays per subscription. Both producers of
the session lane - the in-process push and the HTTP fallback endpoint - take it
from that one place.
"""
import asyncio
import json
from pathlib import Path

import pytest

import vaf.core.web_interface as wi_mod

ROOT = Path(__file__).resolve().parent.parent


class _Conn:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


def _wired(monkeypatch, connections):
    """A fresh connection table on the singleton (patched ON THE INSTANCE, the
    suite's rule for this singleton). `connections` = [(conn, user, session)]."""
    wi = wi_mod.get_web_interface()
    monkeypatch.setattr(wi, "active_connections", [c for c, _, _ in connections])
    monkeypatch.setattr(wi, "connection_users", {c: u for c, u, _ in connections if u})
    monkeypatch.setattr(wi, "connection_sessions", {c: s for c, _, s in connections if s})
    return wi


def test_message_complete_is_a_terminal_event():
    assert "message_complete" in wi_mod.TERMINAL_SESSION_EVENTS


def test_the_owner_who_switched_away_still_receives_the_completion(monkeypatch):
    """THE reported failure: chat A finishes while the browser sits in chat B."""
    browser = _Conn()             # the owner, now subscribed to chat B
    wi = _wired(monkeypatch, [(browser, "scope-a", "chat-B")])
    asyncio.run(wi.broadcast_to_session_and_owner(
        "chat-A", "scope-a", {"type": "message_complete", "content": "done"}))
    assert browser.sent and browser.sent[-1]["sessionId"] == "chat-A"


def test_a_subscriber_receives_it_once_even_when_they_are_the_owner(monkeypatch):
    """The browser that stayed in the chat is subscriber AND owner: one copy."""
    browser = _Conn()
    wi = _wired(monkeypatch, [(browser, "scope-a", "chat-A")])
    asyncio.run(wi.broadcast_to_session_and_owner(
        "chat-A", "scope-a", {"type": "message_complete"}))
    assert len(browser.sent) == 1


def test_a_subscriber_who_is_not_the_owner_keeps_receiving(monkeypatch):
    """An admin watching another account's chat is a subscriber; the owner
    lane widens delivery, it never narrows the subscriber lane."""
    admin = _Conn()
    wi = _wired(monkeypatch, [(admin, "scope-admin", "chat-A")])
    asyncio.run(wi.broadcast_to_session_and_owner(
        "chat-A", "scope-a", {"type": "message_complete"}))
    assert len(admin.sent) == 1


def test_another_account_never_receives_it(monkeypatch):
    stranger = _Conn()            # a different user, in their own chat
    wi = _wired(monkeypatch, [(stranger, "scope-b", "chat-X")])
    asyncio.run(wi.broadcast_to_session_and_owner(
        "chat-A", "scope-a", {"type": "message_complete"}))
    assert stranger.sent == []


def test_unprovable_ownership_falls_back_to_the_subscriber_lane(monkeypatch):
    """No owner means the delivery stays exactly what it was, never wider."""
    away = _Conn()
    stayed = _Conn()
    wi = _wired(monkeypatch, [(away, "scope-a", "chat-B"), (stayed, "scope-a", "chat-A")])
    asyncio.run(wi.broadcast_to_session_and_owner(
        "chat-A", None, {"type": "message_complete"}))
    assert away.sent == [] and len(stayed.sent) == 1


def test_the_push_lane_routes_terminal_events_through_the_owner_lane(monkeypatch):
    """The in-process producer: `_push_session_update` schedules the owner
    coroutine for a terminal type and the plain session coroutine for any
    other type. Patched on the instance (singleton rule)."""
    wi = wi_mod.get_web_interface()
    scheduled = []

    def _fake_sched(coro, loop):
        scheduled.append(coro.__qualname__)
        coro.close()
        return object()

    monkeypatch.setattr(wi, "agent_instance", None, raising=False)
    monkeypatch.setattr(wi, "room_route_for_session", lambda sid: None)
    monkeypatch.setattr(wi, "_session_owner_scope", lambda sid: "scope-a")
    monkeypatch.setattr(wi, "_get_dispatch_loop", lambda: object())
    monkeypatch.setattr(wi_mod.asyncio, "run_coroutine_threadsafe", _fake_sched)

    wi._push_session_update("chat-A", {"type": "message_complete", "content": ""})
    assert scheduled and scheduled[-1].endswith("broadcast_to_session_and_owner"), (
        "a completion still travels per subscription only; the owner who "
        "switched chats never learns the turn ended")

    wi._push_session_update("chat-A", {"type": "agent_message_update", "content": "x"})
    assert scheduled[-1].endswith("broadcast_to_session"), (
        "the live stream must stay per subscription (display isolation)")


def test_the_http_fallback_lane_takes_the_same_decision():
    """The second producer of the session lane must not carry its own copy of
    the rule: it calls the one method the push lane calls."""
    src = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    i = src.index('"/api/subagent/stream"')
    block = src[i:i + 4000]
    assert "manager.session_event_coroutine(update.sessionId, data)" in block, (
        "the fallback endpoint decides delivery on its own; a completion that "
        "arrives over HTTP skips the owner")
    assert "manager.broadcast_to_session(update.sessionId, data)" not in block


def test_the_browser_marks_a_finished_background_chat_unread():
    """The consumer: the handler that was unreachable before must still be the
    one that answers. Pinned so a frontend refactor cannot drop the badge while
    the backend now delivers the event."""
    src = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
    i = src.index("else if (data.type === 'message_complete')")
    block = src[i:i + 1500]
    assert "setUnreadSessions(prev => new Set(prev).add(data.sessionId))" in block, (
        "message_complete for a chat that is not open no longer marks it unread")
