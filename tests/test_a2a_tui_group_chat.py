# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The terminal app shows a room as a group chat.

Two properties matter and they pull against each other: a room has N speakers, and the
ordinary two-speaker conversation must look exactly as it did. The second is why
UserMessage and AgentMessage are left alone rather than rebuilt on a shared base.
"""
import ast
from pathlib import Path

import pytest

from vaf.cli.commands import lookup
from vaf.cli.tui_app.app import _room_clock
from vaf.cli.tui_app.widgets import AgentMessage, PeerMessage, UserMessage

ROOT = Path(__file__).resolve().parents[1]


def _rendered(widget):
    """The head and body strings a widget composes, without running an app.

    Textual keeps a Static's text in a name-mangled attribute until the widget is
    mounted, and `renderable` is still None at that point. Reading the private field is
    deliberate: driving a full app just to read two strings would make these tests slow
    and flaky, and the alternative - asserting on a screenshot - would fail on every
    unrelated style change.
    """
    out = []
    for part in widget.compose():
        content = getattr(part, "_Static__content", None)
        out.append("" if content is None else str(content))
    return out


# ── many speakers ───────────────────────────────────────────────────────────

def test_three_speakers_get_three_different_heads():
    """MUTATION: bake the speaker into the widget the way UserMessage bakes in "You".

    A room has N speakers. A widget that knows only two of them turns a group chat
    into a monologue with the wrong name on it.
    """
    heads = [_rendered(PeerMessage(name, "hello", badge="peer"))[0]
             for name in ("Alice", "Bob", "Codex")]

    assert len({h for h in heads}) == 3
    assert "Alice" in heads[0] and "Bob" in heads[1] and "Codex" in heads[2]


def test_an_unknown_peer_shows_its_own_handle_not_the_agent_name():
    """MUTATION: fall back to "VAF" for a speaker with no display name.

    Attributing a stranger's message to the local agent is worse than showing a raw
    handle, because the reader cannot tell it happened.
    """
    head = _rendered(PeerMessage("p-3f9a1c", "who is this", badge="peer"))[0]
    assert "p-3f9a1c" in head
    assert "VAF" not in head


def test_the_speaker_is_kept_apart_from_the_text():
    """MUTATION: render the label into the body string.

    voice_turn already holds this rule for a transcript with several people in it: a
    renderer must never have to parse a name back out of a message.
    """
    head, body = _rendered(PeerMessage("Alice", "the deploy is done", badge="leader"))

    assert "Alice" in head
    assert body == "the deploy is done"
    assert "Alice" not in body


def test_the_role_and_the_kind_are_shown_without_touching_the_text():
    head, body = _rendered(
        PeerMessage("Worker", "logs collected", badge="worker", kind="report"))

    assert "worker" in head and "report" in head
    assert body == "logs collected"


def test_an_ordinary_message_shows_no_kind():
    head, _body = _rendered(PeerMessage("Alice", "hi", badge="peer", kind="say"))
    assert "(say)" not in head


# ── the two-speaker path is untouched ──────────────────────────────────────

def test_the_ordinary_conversation_renders_exactly_as_before():
    """MUTATION: rebuild UserMessage or AgentMessage on top of PeerMessage.

    Almost every session is two speakers. Sharing a base to save a dozen lines would
    put the path that matters most at risk for the smallest possible gain, so the
    duplication is deliberate and this test is what keeps it honest.
    """
    user_head, user_body = _rendered(UserMessage("what is the status", when="09:30"))
    assert user_head == "[$accent]You[/] [$text-disabled]· 09:30[/]"
    assert user_body == "what is the status"

    # AgentMessage composes a live avatar and cannot be built outside a running app,
    # so its head is pinned at the source instead. Said plainly rather than dressed up:
    # this asserts the format string, not a render.
    source = (ROOT / "vaf" / "cli" / "tui_app" / "widgets.py").read_text(encoding="utf-8")
    assert 'f"[$primary]VAF[/] [$text-disabled]· {self._when or _now()}[/]"' in source


def test_the_widgets_do_not_share_a_base_class():
    for widget in (UserMessage, AgentMessage):
        assert not issubclass(widget, PeerMessage)


# ── the command ─────────────────────────────────────────────────────────────

def test_the_room_command_is_registered_and_reaches_a_handler():
    """MUTATION: add the word to the registry and forget the handler.

    run_command answers an unwired word with a warning note, so the command would
    exist, be offered in the palette, and do nothing.
    """
    command = lookup("room")
    assert command is not None and command.args == "<id>"
    assert command.lane == "agent", "opening a room reads the store, not the UI thread"

    tree = ast.parse((ROOT / "vaf" / "cli" / "tui_app" / "app.py").read_text(encoding="utf-8"))
    handlers, defined = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            defined.add(node.name)
            if node.name == "_handlers":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Dict):
                        handlers |= {k.value for k in sub.keys
                                     if isinstance(k, ast.Constant)}
    assert "room" in handlers, "the registry knows /room but the app does not"
    assert "_cmd_room" in defined


def test_every_registered_word_has_a_handler():
    """The general form of the test above, so the next command cannot slip through."""
    from vaf.cli.commands import COMMANDS

    tree = ast.parse((ROOT / "vaf" / "cli" / "tui_app" / "app.py").read_text(encoding="utf-8"))
    handlers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_handlers":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    handlers |= {k.value for k in sub.keys if isinstance(k, ast.Constant)}

    missing = sorted(c.word for c in COMMANDS if c.word not in handlers)
    assert not missing, f"registered commands with no handler: {missing}"


# ── the advisory clock ─────────────────────────────────────────────────────

def test_a_frame_time_becomes_a_clock_and_a_broken_one_becomes_nothing():
    """ts is advisory in the protocol and is used for exactly this. An unusable value
    must render as empty rather than as a wrong time, because a wrong timestamp in a
    transcript is worse than a missing one."""
    assert len(_room_clock(1765000000.0)) == 5
    assert _room_clock("not a time") == ""
    assert _room_clock(None) == ""


@pytest.mark.parametrize("bad", ["", "nope", float("nan")])
def test_the_clock_never_raises(bad):
    _room_clock(bad)
