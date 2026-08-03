# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Headless smoke of the assembled app: a FakeAgent-backed bridge drives the
real widget tree through Textual's test pilot.

What this pins is the ASSEMBLY - the seams the unit tests cannot see: the CSS
parses with the vaf-* variables from the very first paint, a submitted prompt
becomes a user message plus a streamed agent message in the transcript, the
old run-loop words open their overlays, and the gate overlay answers through
the bridge. If a widget id, a CSS variable, or the events adapter drifts, this
is the test that goes red.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from vaf.cli.tui_app.agent_bridge import AgentBridge
from vaf.cli.tui_app.app import TuiEvents, VafApp
from vaf.cli.tui_app.screens import GateScreen, PaletteScreen, SettingsScreen
from vaf.cli.tui_app.widgets import AgentMessage, UserMessage


class _FakeSession:
    def __init__(self):
        self.id = "sess-smoke-1"
        self.name = "smoke"
        self.messages = []

    def add_message(self, role, content, **kwargs):
        self.messages.append((role, content))


class _FakeAgent:
    LANGUAGE_NAMES_NATIVE = {"en": "English"}

    def __init__(self):
        self.history = []
        self.current_session_id = "sess-smoke-1"
        self.sink = None
        self.script = None            # optional: (stream_callback) -> reply

    def _detect_user_language(self, text):
        return "en"

    def chat_step(self, user_input, stream_callback=None, **kwargs):
        if self.script is not None:
            return self.script(stream_callback)
        if stream_callback:
            stream_callback("Streamed reply.")
        return "Streamed reply."

    def get_token_usage(self):
        return 1200, 32768

    def set_event_sink(self, sink):
        self.sink = sink

    def shutdown(self):
        pass


class _FakeWeb:
    def __init__(self):
        self.resolved = []

    def resolve_gate(self, session_id, decision):
        self.resolved.append((session_id, decision))
        return True

    def __getattr__(self, name):          # every emit/log the classic path mirrors to
        return lambda *a, **k: None


@pytest.fixture()
def smoke_app(monkeypatch, tmp_path):
    import vaf.cli.cmd.run as run_mod
    import vaf.core.session as session_mod
    import vaf.core.speech as speech_mod

    web = _FakeWeb()
    monkeypatch.setattr(run_mod, "get_web_interface", lambda: web)
    monkeypatch.setattr(run_mod, "get_dated_log_path",
                        lambda name, ext: tmp_path / f"{name}.{ext}")
    monkeypatch.setattr(run_mod, "_check_subagent_results", lambda tui, agent: [])
    monkeypatch.setattr(session_mod, "SessionManager",
                        type("M", (), {"__init__": lambda self, *a, **k: None,
                                       "save": lambda self, s, **k: None,
                                       "list": lambda self, **k: []}))
    monkeypatch.setattr(speech_mod, "get_speech_manager",
                        lambda: SimpleNamespace(stop=lambda: None))
    # No real IPC files in a test: the poll returns an empty board.
    monkeypatch.setattr(AgentBridge, "tasks_snapshot", lambda self: [])

    events = TuiEvents()
    bridge = AgentBridge(_FakeAgent(), _FakeSession(), None, events,
                         web_interface_getter=lambda: web)
    app = VafApp(bridge, theme_key="vaf")
    events.bind(app)
    return SimpleNamespace(app=app, bridge=bridge, web=web)


async def _settle(pilot, pred, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def test_app_boots_streams_and_routes(smoke_app):
    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 32)) as pilot:
            # First paint: chrome exists, CSS parsed with the vaf-* variables.
            assert app.query_one("#topbar") is not None
            assert app.query_one("#contextbar") is not None
            assert app.query_one("#promptbox") is not None

            # A submitted prompt becomes user message + streamed agent message.
            app._send_user("hello smoke")
            assert await _settle(pilot, lambda: bool(app.query(UserMessage)))
            assert await _settle(pilot, lambda: bool(app.query(AgentMessage)))
            assert await _settle(pilot, lambda: not bridge.busy)
            assert ("user", "hello smoke") in bridge.session.messages

            # The old run-loop word `s` opens the settings overlay; esc closes.
            app._submitted(SimpleNamespace(text="s"))
            assert await _settle(pilot,
                                 lambda: isinstance(app.screen, SettingsScreen))
            await pilot.press("escape")
            assert await _settle(pilot,
                                 lambda: not isinstance(app.screen, SettingsScreen))

    asyncio.run(_drive())
    bridge.shutdown()


def test_gate_overlay_answers_through_the_bridge(smoke_app):
    app, bridge, web = smoke_app.app, smoke_app.bridge, smoke_app.web

    async def _drive():
        async with app.run_test(size=(110, 32)) as pilot:
            bridge.on_sink_event({"type": "gate_required", "tool": "host_bash",
                                  "reason": "runs on the host"})
            assert await _settle(pilot, lambda: isinstance(app.screen, GateScreen))
            await pilot.press("y")
            assert await _settle(pilot, lambda: bool(web.resolved))
            assert web.resolved[0] == ("sess-smoke-1", "allow_once")

    asyncio.run(_drive())
    bridge.shutdown()


def test_bottom_chrome_stacks_without_overlap(smoke_app):
    """The regression this pins: four same-edge `dock: bottom` widgets all land
    on the bottom edge and paint over each other. The chrome must be normal
    flow - every widget ends before the next begins."""
    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            main = app.query_one("#main").region
            prompt = app.query_one("#promptbox").region
            strip = app.query_one("#statusstrip").region
            assert main.bottom <= prompt.y, (main, prompt)
            assert prompt.bottom <= strip.y, (prompt, strip)

    asyncio.run(_drive())
    bridge.shutdown()


def test_typed_keys_reach_the_prompt_and_submit(smoke_app):
    """Real key events through the pilot - the prompt's Enter interception is a
    private-API override and must be pinned by actual key presses."""
    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            await pilot.press("h", "i", "enter")
            assert await _settle(
                pilot, lambda: ("user", "hi") in bridge.session.messages)

    asyncio.run(_drive())
    bridge.shutdown()


def test_ctrl_p_opens_our_palette_not_the_builtin(smoke_app):
    """Textual registers its builtin command palette on ctrl+p WITH priority;
    unless it is disabled, our binding never fires."""
    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+p")
            assert await _settle(pilot, lambda: isinstance(app.screen, PaletteScreen))

    asyncio.run(_drive())
    bridge.shutdown()


def test_quit_binding_keeps_priority():
    """ctrl+q must work while a modal is up (the gate!): modal screens truncate
    the non-priority binding chain, so the quit binding carries priority."""
    assert any(b.key == "ctrl+q" and b.priority for b in VafApp.BINDINGS)


def test_transcript_stays_chronological_across_interleaved_events(smoke_app):
    """The live-test regression this pins: the answer streamed AFTER a tool
    call appeared ABOVE the tool card, because all chunks fed one bubble
    mounted at turn start. Chronology rule: narration or a tool card below the
    live bubble seals it; the next chunk opens a NEW bubble at the bottom."""
    from vaf.cli.tui_app.widgets import EventNote, ToolCard

    app, bridge = smoke_app.app, smoke_app.bridge

    def _script(cb):
        cb("part one.")
        bridge.on_console_event("Router", "tools selected", "info")
        bridge.on_sink_event({"type": "tool_start", "tool": "web_search", "args": {}})
        bridge.on_sink_event({"type": "tool_end", "tool": "web_search",
                              "ok": True, "duration_ms": 100})
        cb("part two.")
        return "part one.part two."

    bridge.agent.script = _script

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app._send_user("weather please")
            assert await _settle(pilot, lambda: not bridge.busy)
            await pilot.pause()

            kids = list(app.transcript.children)
            types = [type(k).__name__ for k in kids]
            bubbles = [i for i, k in enumerate(kids) if isinstance(k, AgentMessage)]
            note_idx = next(i for i, k in enumerate(kids) if isinstance(k, EventNote))
            card_idx = next(i for i, k in enumerate(kids) if isinstance(k, ToolCard))

            # Two bubbles, event and card BETWEEN them - strictly chronological.
            assert len(bubbles) == 2, types
            assert bubbles[0] < note_idx < card_idx < bubbles[1], types
            assert "part one." in kids[bubbles[0]]._answer
            assert "part two." in kids[bubbles[1]]._answer

    asyncio.run(_drive())
    bridge.shutdown()


def test_think_renders_as_a_separate_muted_block():
    """The think channel must read as its OWN block: a caption plus the muted
    italic body - not merely slightly dimmed prose inside the answer."""
    msg = AgentMessage()
    msg.feed_think("pondering the request")
    assert msg._think_body.display is True
    content = msg._think_body.render()
    assert "thinking" in str(content)
    assert "pondering the request" in str(content)
    # The muted style span is the "leicht graeuliche" contract.
    assert any("vaf-muted" in str(span.style) for span in content.spans)


def test_avatar_sits_beside_the_newest_reply_only(smoke_app):
    """The web-UI rule, ported: the living dot (borderless - no box that could
    read as a bar) renders beside the NEWEST agent message; older replies drop
    it. There is no fixed avatar chrome above the prompt anymore."""
    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app._send_user("first")
            assert await _settle(pilot, lambda: not bridge.busy)
            app._send_user("second")
            assert await _settle(pilot, lambda: not bridge.busy)
            await pilot.pause()

            bubbles = list(app.query(AgentMessage))
            assert len(bubbles) >= 2
            assert bubbles[-1].avatar.display is True
            assert all(b.avatar.display is False for b in bubbles[:-1])
            # One row tall, and the eye wears its bracket BODY: `[ ● ]`.
            assert bubbles[-1].avatar.region.height == 1
            rendered = str(bubbles[-1].avatar.render())
            assert rendered.startswith("[") and rendered.endswith("]"), rendered

    asyncio.run(_drive())
    bridge.shutdown()


def test_every_avatar_frame_fits_the_body():
    """The body is TIGHT - `[ ● ]`, a 3-cell field. A frame wider than that
    would push the closing bracket around; every state must fit at every tick."""
    import re

    from vaf.cli.tui_app.widgets import AgentAvatar

    avatar = AgentAvatar()
    strip = re.compile(r"\[[^\]]*\]")
    for state in ("idle", "waiting", "thinking", "talking", "listening",
                  "working", "success", "error", "dim"):
        avatar._state = state
        avatar._oneshot_until = 140
        for tick in range(0, 130):
            avatar._tick = tick
            visible = strip.sub("", avatar._frame())
            assert len(visible) <= 3, (state, tick, repr(visible))


def test_tool_card_shows_the_result_and_only_then_offers_the_fold(smoke_app):
    """The round's payoff, end to end: the sink's result reaches the card. And
    the fold marker must NOT appear before there is something to unfold - a
    promise of hidden content that opens onto nothing was the old behaviour."""
    from vaf.cli.tui_app.widgets import ToolCard

    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            bridge.on_sink_event({"type": "tool_start", "tool": "list_files",
                                  "args": {"path": "."}})
            await pilot.pause()
            card = app.query(ToolCard).last()
            assert "▸" not in str(card._head_left.render()), (
                "the fold was offered while the card was still empty")

            bridge.on_sink_event({"type": "tool_end", "tool": "list_files",
                                  "ok": True, "duration_ms": 40,
                                  "result": "README.md\nvaf/\ntests/"})
            assert await _settle(pilot, lambda: bool(card._output))

            assert "README.md" in card._output
            assert "README.md" in str(card._body.render())
            assert "▸" in str(card._head_left.render())

    asyncio.run(_drive())
    bridge.shutdown()


# ── markdown rendering (round 4) ────────────────────────────────────────────────────

def _bubble_in(app):
    """Mount a bare AgentMessage into the transcript and return it."""
    msg = AgentMessage()
    app.transcript.mount(msg)
    return msg


def test_answers_render_as_markdown_not_raw_syntax(smoke_app):
    """The gap this round exists for: `**bold**`, list syntax and ``` fences
    were shown verbatim, because the body was a Static fed a plain string."""
    from textual.widgets import Markdown

    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            msg = _bubble_in(app)
            await pilot.pause()
            assert isinstance(msg._body, Markdown), "the body is not a Markdown widget"

            msg.feed("**bold** intro\n\n- one\n- two\n")
            assert await _settle(pilot, lambda: bool(msg._body.children))

            kinds = [type(w).__name__ for w in msg._body.children]
            assert "MarkdownParagraph" in kinds, kinds
            assert "MarkdownBulletList" in kinds, kinds

    asyncio.run(_drive())
    bridge.shutdown()


def test_a_fenced_block_becomes_a_highlighted_fence(smoke_app):
    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            msg = _bubble_in(app)
            await pilot.pause()
            msg.feed("Here:\n\n```python\nprint('hi')\n```\n")
            assert await _settle(
                pilot,
                lambda: any(type(w).__name__ == "MarkdownFence"
                            for w in msg._body.children))

            fence = [w for w in msg._body.children
                     if type(w).__name__ == "MarkdownFence"][0]
            assert fence.lexer == "python"
            assert "print('hi')" in fence.code

    asyncio.run(_drive())
    bridge.shutdown()


def test_streaming_coalesces_instead_of_reparsing_per_chunk(smoke_app):
    """The performance contract as behaviour: chunks arrive 20-100 times a
    second, and a re-render per chunk is the difference between 3.5 ms and an
    unbounded cost. Many chunks inside one tick must produce ONE flush."""
    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            msg = _bubble_in(app)
            await pilot.pause()
            for i in range(200):
                msg.feed(f"w{i} ")
            assert await _settle(pilot, lambda: msg._flushes >= 1)
            assert msg._flushes <= 5, f"{msg._flushes} flushes for 200 chunks"
            # and nothing was lost on the way
            assert "w0 " in msg._body.source and "w199 " in msg._body.source

    asyncio.run(_drive())
    bridge.shutdown()


def test_sealing_flushes_the_tail_before_the_next_widget_mounts(smoke_app):
    """The chronology rule seals a bubble the moment anything mounts below it.
    Text still sitting in the buffer at that moment must not be lost."""
    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app.feed_agent("first part. ")
            await pilot.pause()
            live = app._live_msg
            live.feed("tail that had not flushed yet")
            app.add_event_note("Router", "sealing note", "info")   # seals it
            assert await _settle(
                pilot, lambda: "tail that had not flushed yet" in live._body.source)

    asyncio.run(_drive())
    bridge.shutdown()


def test_the_answer_text_survives_for_the_session_record(smoke_app):
    """`_answer` is what the session and the chronology test read - the switch
    to a Markdown body must not make the plain text unavailable."""
    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            msg = _bubble_in(app)
            await pilot.pause()
            msg.feed("**kept** verbatim")
            await pilot.pause()
            assert msg._answer == "**kept** verbatim"

    asyncio.run(_drive())
    bridge.shutdown()


def test_quitting_mid_stream_does_not_hang_teardown(smoke_app):
    """A flush pending at teardown mounts widgets into a tree that is closing.
    Wrapped in a hard timeout: a hang here would otherwise look like a slow CI."""
    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            msg = _bubble_in(app)
            await pilot.pause()
            msg.feed("text that is still pending when the app exits")
            app.exit()

    asyncio.run(asyncio.wait_for(_drive(), timeout=15))
    bridge.shutdown()


def test_think_block_stays_plain_and_muted(smoke_app):
    """Reasoning is not markdown - it is a designed muted caption block, and a
    Markdown widget has none of the spans the think test reads."""
    from textual.widgets import Markdown

    msg = AgentMessage()
    msg.feed_think("pondering")
    assert not isinstance(msg._think_body, Markdown)


# ── commands (round 5) ──────────────────────────────────────────────────────────────

def test_no_command_word_is_ever_sent_to_the_model(smoke_app):
    """The silent fall-through this round removes: a typed `clear` used to cost
    an LLM turn and land in the session as a one-word message."""
    from vaf.cli.commands import bare_words

    app, bridge = smoke_app.app, smoke_app.bridge
    sent, routed = [], []
    app._send_user = lambda text: sent.append(text)
    app.run_command = lambda parsed: routed.append(parsed.command.word)

    for word in sorted(bare_words()):
        app._submitted(SimpleNamespace(text=word))

    assert sent == [], f"these reached the model: {sent}"
    assert len(routed) == len(bare_words()), "a catch-set word routed nowhere"

    # and the counter-example: ordinary text still goes to the model
    app._submitted(SimpleNamespace(text="what is the weather"))
    assert sent == ["what is the weather"]


def test_an_unknown_slash_command_is_reported_not_sent(smoke_app):
    from vaf.cli.tui_app.widgets import EventNote

    app, bridge = smoke_app.app, smoke_app.bridge
    sent = []
    bridge.submit_turn = lambda text: sent.append(text)

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app._submitted(SimpleNamespace(text="/restor"))
            assert await _settle(pilot, lambda: bool(app.query(EventNote)))

            note = str(app.query(EventNote).last().render())
            assert "unknown" in note.lower()
            assert "/restore" in note, "the suggestion is missing"
            assert sent == []

    asyncio.run(_drive())
    bridge.shutdown()


def test_theme_takes_its_argument_instead_of_cycling(smoke_app):
    from vaf.cli.tui_app.theme_bridge import THEME_ORDER

    app, bridge = smoke_app.app, smoke_app.bridge
    target = THEME_ORDER[-1]

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app._submitted(SimpleNamespace(text=f"theme {target}"))
            await pilot.pause()
            assert app._theme_key == target
            assert app.theme == f"vaf-{target}"

    asyncio.run(_drive())
    bridge.shutdown()


def test_a_destructive_command_asks_before_it_runs(smoke_app):
    """`clear` discards the conversation context; it must not fire on a typo."""
    from vaf.cli.tui_app.screens import ConfirmScreen

    app, bridge = smoke_app.app, smoke_app.bridge
    cleared = []
    bridge.clear_conversation = lambda: cleared.append(True)

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app._submitted(SimpleNamespace(text="clear"))
            assert await _settle(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            assert cleared == [], "it ran before the answer"

            await pilot.press("escape")           # say no
            assert await _settle(pilot,
                                 lambda: not isinstance(app.screen, ConfirmScreen))
            assert cleared == []

            app._submitted(SimpleNamespace(text="clear"))
            assert await _settle(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")                # say yes
            assert await _settle(pilot, lambda: cleared == [True])

    asyncio.run(_drive())
    bridge.shutdown()


def test_clear_empties_the_transcript_immediately(smoke_app):
    app, bridge = smoke_app.app, smoke_app.bridge
    bridge.clear_conversation = lambda: None

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app._send_user("something to clear")
            assert await _settle(pilot, lambda: bool(app.query(UserMessage)))
            app._cmd_clear(())
            await pilot.pause()
            assert not app.query(UserMessage)
            assert app._live_msg is None and app._avatar_host is None

    asyncio.run(_drive())
    bridge.shutdown()


def test_halt_never_waits_for_the_busy_lane(smoke_app):
    """Stopping speech must work WHILE a turn runs - and the lane is exactly
    the thread that is busy."""
    import threading

    app, bridge = smoke_app.app, smoke_app.bridge
    stopped = threading.Event()
    release = threading.Event()

    import vaf.core.speech as speech_mod
    speech_mod.get_speech_manager = lambda: SimpleNamespace(
        stop=lambda: stopped.set())

    def _blocking(cb):
        release.wait(timeout=5)
        return "done"

    bridge.agent.script = _blocking

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app._send_user("long answer")
            assert await _settle(pilot, lambda: bridge.busy)
            # Every turn calls stop() once as barge-in; clear that signal or the
            # test would pass without the command ever running.
            stopped.clear()
            app._submitted(SimpleNamespace(text="halt"))
            assert await _settle(pilot, lambda: stopped.is_set(), timeout=3), (
                "halt waited for the busy lane - the lane is what is busy")
            release.set()
            assert await _settle(pilot, lambda: not bridge.busy)

    asyncio.run(_drive())
    bridge.shutdown()


def test_the_tools_overlay_uses_the_shared_catalog(smoke_app):
    from vaf.cli.tui_app.screens import ToolsScreen

    app, bridge = smoke_app.app, smoke_app.bridge

    class _Tool:
        description = "does a thing"

    bridge.agent.tools = {"update_intent": _Tool(), "web_search": _Tool()}

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app._submitted(SimpleNamespace(text="tools"))
            assert await _settle(pilot, lambda: isinstance(app.screen, ToolsScreen))
            names = [r.name for r in app.screen._rows]
            assert "web_search" in names
            assert "update_intent" not in names, "an internal tool was advertised"

    asyncio.run(_drive())
    bridge.shutdown()


def test_restart_execs_only_after_the_screen_is_released(smoke_app, monkeypatch):
    """`os.execl` from inside the app would hand the new process an alternate
    screen and a raw tty."""
    app, bridge = smoke_app.app, smoke_app.bridge

    async def _drive():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app._request_restart()
            await pilot.pause()

    asyncio.run(_drive())
    assert app._restart_requested is True
    bridge.shutdown()
