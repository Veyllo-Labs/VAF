# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The transcript follows the session - switch and boot both replay.

Before this, a session switch swapped the agent's context and left the OLD
conversation on screen above the new session's turns, and a resumed session
booted into an empty transcript as if nothing had ever been said. The bridge
now emits `transcript_replay(entries, fresh)`: fresh=True on a switch (clear
first), fresh=False at boot (the start banner stays, the conversation mounts
beneath it).

Replayed bubbles carry their REAL time (the persisted timestamp's HH:MM),
not "now", and replayed agent messages are static: complete content at
construction, no flush ticker - feed/done against a just-scheduled mount
races on_mount and would leave the 100 ms interval running forever, the
same class as the avatar-timer leak.
"""
import time
from types import SimpleNamespace

from vaf.cli.tui_app.agent_bridge import AgentBridge
from vaf.core.session import Message


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _session(messages, sid="green123456"):
    return SimpleNamespace(id=sid, messages=messages)


_RAW = [
    {"role": "system", "content": "prompt", "timestamp": "2026-08-06T08:00:00"},
    {"role": "user", "content": "hallo", "timestamp": "2026-08-06T09:15:00"},
    {"role": "assistant", "content": "", "timestamp": "2026-08-06T09:15:01"},
    {"role": "assistant", "content": "hi!", "timestamp": "2026-08-06T09:15:30"},
]

# What the live lane really hands over. `SessionManager.load` runs every stored
# session through `Session.from_dict`, which rebuilds each entry as a Message
# dataclass - a dict never reaches the bridge. Fixtures made of dicts kept this
# file green while a session switch raised
# "'Message' object has no attribute 'get'" on every attempt.
_MESSAGES = [Message.from_dict(m) for m in _RAW]

_EXPECTED = [("user", "hallo", "09:15"), ("assistant", "hi!", "09:15")]


def test_entries_carry_the_conversation_and_its_times():
    entries = AgentBridge._transcript_entries(_session(_MESSAGES))
    assert entries == _EXPECTED, (
        "system rows and empty bodies belong out, real rows keep their time")


def test_entries_also_read_plain_dicts():
    """The shape older callers and hand-written fixtures use stays readable."""
    assert AgentBridge._transcript_entries(_session(_RAW)) == _EXPECTED


def test_untouched_reads_the_same_shapes():
    """One definition of "did the user speak", whatever the row looks like."""
    assert AgentBridge.session_is_untouched(_session(
        [Message.from_dict(_RAW[0])])) is True
    assert AgentBridge.session_is_untouched(_session(_MESSAGES)) is False
    assert AgentBridge.session_is_untouched(_session(_RAW)) is False
    assert AgentBridge.session_is_untouched(_session(
        [("user", "hallo", "09:15")])) is False


def _bridge(loaded_session):
    events = []

    class _Events:
        def __getattr__(self, name):
            def _rec(*args):
                events.append((name, *args))
            return _rec

    agent = SimpleNamespace(get_token_usage=lambda: (1, 2),
                            set_event_sink=lambda s: None,
                            shutdown=lambda: None,
                            load_session_context=lambda sid: None)
    b = AgentBridge(agent, loaded_session,
                    SimpleNamespace(load=lambda sid: loaded_session),
                    _Events(),
                    web_interface_getter=lambda: SimpleNamespace(
                        resolve_gate=lambda *a: True))
    return b, events


def test_a_switch_replays_fresh_after_the_switch_note():
    session = _session(_MESSAGES)
    b, events = _bridge(session)
    b.load_session("green123456")
    assert _wait(lambda: any(e[0] == "transcript_replay" for e in events)), events
    names = [e[0] for e in events]
    assert names.index("session_switched") < names.index("transcript_replay")
    replay = next(e for e in events if e[0] == "transcript_replay")
    assert replay[2] is True, "a switch must clear the old conversation"
    assert replay[1] == [("user", "hallo", "09:15"), ("assistant", "hi!", "09:15")]
    b.shutdown()


def test_boot_replay_keeps_the_banner_and_an_empty_session_stays_silent():
    session = _session(_MESSAGES)
    b, events = _bridge(session)
    b.request_transcript_replay()
    assert _wait(lambda: any(e[0] == "transcript_replay" for e in events))
    replay = next(e for e in events if e[0] == "transcript_replay")
    assert replay[2] is False, "boot must not clear the start banner"
    b.shutdown()

    b2, events2 = _bridge(_session([]))
    b2.request_transcript_replay()
    time.sleep(0.3)
    assert not any(e[0] == "transcript_replay" for e in events2), (
        "an empty session replayed - a fresh start loses its clean banner")
    b2.shutdown()


# ── the app half ────────────────────────────────────────────────────────────────────

def _replaying_app():
    import vaf.cli.tui_app.app as app_mod

    record = {"cleared": 0, "mounted": [], "notes": []}

    class _A(app_mod.VafApp):
        transcript = property(lambda s: SimpleNamespace(
            clear=lambda: record.__setitem__("cleared", record["cleared"] + 1)))

    a = _A.__new__(_A)
    a._live_msg = "stale"
    a._avatar_host = "stale"
    a._mount_scrolled = lambda w: record["mounted"].append(w)
    a.add_system_note = lambda t: record["notes"].append(t)
    return a, record


def test_fresh_replay_clears_and_mounts_in_order():
    from vaf.cli.tui_app.widgets import AgentMessage, UserMessage

    a, record = _replaying_app()
    a.replay_transcript([("user", "hallo", "09:15"),
                         ("assistant", "hi!", "09:16")], True)
    assert record["cleared"] == 1
    assert a._live_msg is None and a._avatar_host is None
    assert len(record["mounted"]) == 2
    assert isinstance(record["mounted"][0], UserMessage)
    assert isinstance(record["mounted"][1], AgentMessage)


def test_boot_replay_does_not_clear():
    a, record = _replaying_app()
    a.replay_transcript([("user", "hallo", "09:15")], False)
    assert record["cleared"] == 0
    assert len(record["mounted"]) == 1


def test_long_sessions_are_capped_with_an_honest_note():
    a, record = _replaying_app()
    entries = [("user", f"m{i}", "") for i in range(55)]
    a.replay_transcript(entries, True)
    assert len(record["mounted"]) == a.REPLAY_CAP
    assert record["mounted"][0]._text == "m15", "the NEWEST messages must win"
    assert record["notes"] and "15 older messages not shown" in record["notes"][0]


# ── the widget half ─────────────────────────────────────────────────────────────────

def test_replayed_heads_show_the_real_time_not_now():
    import inspect

    from vaf.cli.tui_app.widgets import AgentMessage, UserMessage

    head = next(iter(UserMessage("hi", when="09:15").compose()))
    assert "09:15" in str(head.content)

    # AgentMessage.compose enters a container and cannot run detached; the
    # same fallback expression is pinned at source level instead.
    assert "self._when or _now()" in inspect.getsource(AgentMessage.compose)


def test_a_static_agent_message_never_starts_the_flush_ticker():
    from vaf.cli.tui_app.widgets import AgentMessage

    msg = AgentMessage(static_text="hallo")
    assert msg._static and msg._answer == "hallo"
    msg.on_mount()          # would raise / create a timer on the stream path
    assert msg._timer is None, (
        "a replayed bubble armed the 100 ms flush interval - it would tick forever")
