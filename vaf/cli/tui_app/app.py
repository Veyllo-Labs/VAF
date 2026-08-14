# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The full-screen terminal app behind `vaf run`, and its entry point.

Assembly only: widgets render, screens overlay, the bridge owns the agent lane.
This module wires the three together - the events adapter marshals bridge
callbacks onto the UI thread, the prompt routes the old run-loop words and
slash commands, and `run_tui` runs the classic boot BEFORE the app takes the
screen (model loading writes C-level stderr that would corrupt app mode).

Interaction contract: every overlay is keyboard-complete (arrows walk, enter or
space activates, esc goes back), the status strip shows the old letter hints,
and the letters typed alone into the prompt still route the way the classic
loop's words did.
"""
import threading
import time

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal

from vaf.cli.commands import parse, suggest
from vaf.cli.history import append_history, load_history
from vaf.cli.themes import ThemeManager
from vaf.cli.tui_app.screens import (
    ApiKeyScreen,
    ConfirmScreen,
    ContextNote,
    GateScreen,
    HelpScreen,
    HistoryScreen,
    ModelScreen,
    PaletteScreen,
    RenameScreen,
    SessionsPanel,
    SettingsChanged,
    SettingsScreen,
    ToolsScreen,
    VoiceScreen,
)
from vaf.cli.tui_app.theme_bridge import (
    THEME_ORDER,
    css_variables_for,
    initial_theme_key,
    make_textual_theme,
    textual_theme_name,
)
from vaf.cli.tui_app.widgets import (
    AgentMessage,
    CompletionPopup,
    ContextBar,
    EventNote,
    KeyHints,
    PromptBox,
    RenderableNote,
    StartBanner,
    StatusStrip,
    SystemNote,
    TasksLine,
    ToolCard,
    TopBar,
    Transcript,
    UserMessage,
    WakeMessage,
)


def _vaf_version() -> str:
    try:
        from vaf.version import __version__
        return str(__version__)
    except Exception:
        return ""


def _agent_name() -> str:
    """The Soul's name, which is what the agent actually calls itself.

    Deliberately NOT hardcoded to "VAF": identity.json is where a user names
    their agent, and the system prompt builds the persona from the same place.

    The workspace is the machine owner's, looked up by the configured username
    rather than the literal "admin" - the two are only the same on installs
    whose account happens to be called admin, and everywhere else this read the
    wrong (usually empty) workspace.
    """
    try:
        from vaf.auth.user_workspace import get_user_workspace
        from vaf.core.config import get_local_admin_username
        name = (get_user_workspace(get_local_admin_username()).get_identity() or {}).get("name")
        if name:
            return str(name)
    except Exception:
        pass
    return "unnamed (set one in Settings)"


def _local_datetime() -> str:
    """The user's configured timezone and date format, never the raw server
    clock - `vaf/core/user_time.py` is the single source for user-facing time."""
    try:
        from vaf.core.user_time import format_user_datetime
        return format_user_datetime()
    except Exception:
        import time as _time
        return _time.strftime("%Y-%m-%d %H:%M")


class TuiEvents:
    """The bridge's events contract, marshalled onto the UI thread.

    Bridge callbacks arrive on the agent lane (or a gate thread); Textual only
    mutates widgets on its own thread. `call_from_thread` raises when invoked
    FROM the app thread (turn_started fires synchronously in submit_turn), so
    same-thread calls fall through to a direct invocation.
    """

    def __init__(self) -> None:
        self._app = None

    def bind(self, app: "VafApp") -> None:
        self._app = app

    def _ui(self, fn, *args) -> None:
        app = self._app
        if app is None:
            return
        # Decide by state rather than by exception: call_from_thread builds its
        # coroutine BEFORE it can fail, so catching the failure leaves an
        # un-awaited coroutine behind. Two states matter.
        try:
            loop = app._loop
            on_app_thread = app._thread_id == threading.get_ident()
        except Exception:
            loop, on_app_thread = None, True

        if loop is None or loop.is_closed():
            # The app is gone (or going): a late event from the lane, the tasks
            # poll or the gate thread has nowhere to render. Drop it - touching
            # widgets from here would be worse than losing the line.
            return
        try:
            fn(*args) if on_app_thread else app.call_from_thread(fn, *args)
        except Exception:
            pass

    def turn_started(self, text):           pass  # the app mounted the user message itself
    def agent_message_start(self):          self._ui(self._app_call, "begin_agent_message")
    def agent_chunk(self, text):            self._ui(self._app_call, "feed_agent", text)
    def agent_think(self, text):            self._ui(self._app_call, "feed_think", text)
    def agent_message_done(self):           self._ui(self._app_call, "end_agent_message")
    def turn_finished(self, tools_ran):     self._ui(self._app_call, "turn_finished", tools_ran)
    def event_note(self, t, m, s):          self._ui(self._app_call, "add_event_note", t, m, s)
    def system_note(self, text):            self._ui(self._app_call, "add_system_note", text)
    def renderable(self, obj):              self._ui(self._app_call, "add_renderable", obj)
    def tool_start(self, tool, preview):    self._ui(self._app_call, "tool_started", tool, preview)
    def tool_end(self, tool, ok, duration, output=""): self._ui(self._app_call, "tool_ended", tool, ok, duration, output)
    def gate_required(self, tool, reason, preview="", notes=""):  self._ui(self._app_call, "show_gate", tool, reason, preview, notes)
    def gate_decision(self, decision):      self._ui(self._app_call, "gate_decided", decision)
    def presence(self, state, detail=""):   self._ui(self._app_call, "set_presence", state, detail)
    def context(self, used, total):         self._ui(self._app_call, "set_context", used, total)
    def context_status(self, status):       self._ui(self._app_call, "add_context_note", status)
    def session_switched(self, sid, count): self._ui(self._app_call, "session_switched", sid, count)
    def session_list(self, sessions):       self._ui(self._app_call, "show_session_list", sessions)
    def chrome_changed(self):               self._ui(self._app_call, "_refresh_chrome")
    def wake_message(self, text, kind):      self._ui(self._app_call, "add_wake_message", text, kind)
    def transcript_replay(self, entries, fresh): self._ui(self._app_call, "replay_transcript", entries, fresh)
    def voice_level(self, phase, energy, threshold): self._ui(self._app_call, "voice_level", phase, energy, threshold)
    def voice_done(self, text, note):        self._ui(self._app_call, "voice_done", text, note)

    def _app_call(self, name, *args) -> None:
        getattr(self._app, name)(*args)


def _room_clock(ts) -> str:
    """A frame's wall clock as HH:MM for the head row.

    The rule it follows - an unusable `ts` renders as empty, never as a wrong time -
    belongs to the protocol, so it lives with the frames. Kept as a name here because
    the call site reads better with it and the tests point at it.
    """
    from vaf.core.a2a.room import frame_clock
    return frame_clock(ts)


class VafApp(App):
    """Assembly only: widgets render, screens overlay, the injected bridge owns
    every behavior. The chrome order (top bar / transcript / tasks line /
    prompt / status strip) is normal document flow, NOT
    same-edge docks - Textual places every `dock: bottom` widget at the same
    edge (they overlap, painted over each other), while flow children of the
    vertical screen stack in yield order."""

    TITLE = "VAF"

    # The builtin palette would win the ctrl+p binding (it is registered with
    # priority); this app has its own palette overlay on that key.
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        # priority: quit must work while a modal (gate, settings) is up -
        # modal screens truncate the non-priority binding chain.
        Binding("ctrl+q", "quit", "quit", priority=True),
        Binding("ctrl+p", "palette", "commands"),
        Binding("ctrl+s", "toggle_sessions", "sessions"),
        Binding("f1", "help", "help"),
    ]

    CSS = """
    Screen { background: $background; color: $foreground; }
    #topbar { dock: top; height: 1; padding: 0 1; background: $surface; }
    #topbar-left { width: 1fr; }
    #topbar-right { width: auto; }
    #main { height: 1fr; }
    #sessions {
        dock: left; width: 32; background: $surface;
        border-right: solid $vaf-border; padding: 1 1; display: none;
    }
    #sessions.visible { display: block; }
    .panel-title { margin-bottom: 1; }
    .session-row { margin-bottom: 1; }
    /* While the start block is alone, the transcript centres it vertically -
       an empty session should look like a beginning, not like one line of
       output stuck to the ceiling. The class comes off the moment real
       content mounts, so the transcript scrolls normally from then on. */
    #transcript.only-banner { align: center middle; }
    #transcript {
        padding: 1 2 0 2;
        scrollbar-color: $vaf-border;
        scrollbar-background: $background;
        scrollbar-size-vertical: 1;
    }
    .msg-head { margin: 1 0 0 0; }
    .user-msg-wrap { height: auto; }
    .agent-msg-wrap { height: auto; }
    .wake-msg-wrap { height: auto; }
    .peer-msg-wrap { height: auto; }
    .user-msg {
        padding: 0 1; border-left: thick $accent; background: $surface;
    }
    /* $warning, not $accent: the bar colour IS the answer to "who said this".
       Nobody typed it - something woke the agent. */
    .wake-msg {
        padding: 0 1; border-left: thick $warning; background: $surface;
    }
    /* The Markdown body ships its own padding; strip it so the answer lines
       up with the head row, then style the blocks it mounts. */
    .agent-msg { margin-left: 1; padding: 0; height: auto; }
    .agent-msg MarkdownFence {
        margin: 1 0; padding: 0 1;
        background: $surface; border-left: thick $vaf-border;
    }
    .agent-msg MarkdownH1, .agent-msg MarkdownH2, .agent-msg MarkdownH3 {
        color: $primary; margin: 1 0 0 0;
    }
    .agent-think {
        margin: 0 0 1 1; padding: 0 1; height: auto;
        border-left: thick $vaf-border; background: $surface;
    }
    .start-banner { height: auto; width: 1fr; margin: 0 0 2 0; }
    .banner-center { height: auto; }
    .banner-row-wrap { width: auto; max-width: 100%; height: auto; }
    .banner-art { width: auto; padding: 0 4 0 0; }
    .banner-facts { width: auto; max-width: 100%; height: auto; padding: 1 0 0 0; }
    .banner-row { height: 1; width: auto; }
    /* `width: auto` is what makes Center able to centre it: a Static defaults
       to filling its parent, and centring a full-width box moves nothing. */
    .banner-hint { margin-top: 2; width: auto; max-width: 100%; }
    .system-note { margin: 1 0 0 1; }
    .event-note { margin: 0 0 0 1; }
    .renderable-note { margin: 1 0 0 1; }
    .tool-card {
        margin: 1 0 0 1; padding: 0 1; height: auto;
        border: round $vaf-border; background: $surface;
    }
    .tool-header { height: 1; }
    .tool-head-left { width: 1fr; }
    .tool-head-right { width: auto; }
    .tool-body { padding: 0 1; background: $background; }
    #tasksline { height: 1; margin: 0 2; background: $background; }
    #completion {
        height: auto; max-height: 8; margin: 0 1;
        background: $surface; border: round $vaf-border;
        display: none;
    }
    .agent-avatar {
        width: 5; height: 1;
        margin: 1 1 0 0;
        content-align: center middle;
    }
    .agent-head-row { height: auto; }
    .msg-head { width: auto; }
    #promptbox {
        height: auto; max-height: 8; min-height: 3;
        margin: 0 1 1 1; border: round $vaf-border;
        border-title-color: $primary; background: $surface; padding: 0 1;
    }
    #promptbox:focus { border: round $vaf-border-active; }
    #statusstrip { height: 1; padding: 0 1; background: $background; }
    GateScreen, VoiceScreen, SettingsScreen, ModelScreen, HistoryScreen, HelpScreen,
    NumberScreen, AboutScreen, ApiKeyScreen, ConfirmScreen, ToolsScreen, RenameScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    .modal-box {
        width: 64; height: auto; max-height: 30;
        padding: 1 2; background: $surface; border: round $vaf-border;
    }
    #gate-box { border: round $warning; }
    #voice-box { border: round $error; width: 46; }
    .modal-title { margin-bottom: 1; }
    .modal-title-mid { margin-top: 1; margin-bottom: 1; }
    #help-box { width: 76; }
    .modal-body { margin-bottom: 1; }
    .settings-row { margin-bottom: 0; padding: 0 0; }
    .help-row { height: auto; }
    .help-key { width: 27; }
    .help-desc { width: 1fr; }
    #voice-bars { margin-bottom: 1; }
    #settings-list {
        background: $surface; height: auto; max-height: 22; margin-bottom: 1;
    }
    #model-list, #history-list {
        background: $surface; height: auto; max-height: 12; margin-bottom: 1;
    }
    PaletteScreen { align: center top; background: rgba(0, 0, 0, 0.5); }
    #palette-box {
        width: 60; height: auto; max-height: 20; margin-top: 3;
        background: $surface; border: round $vaf-border; padding: 1 1;
    }
    #palette-input { background: $background; border: none; margin-bottom: 1; }
    #palette-list { background: $surface; height: auto; max-height: 14; }
    ListItem { background: $surface; padding: 0 1; }
    ListItem.-highlight { background: $background; }
    """

    def __init__(self, bridge, theme_key: str = "vaf",
                 initial_message: str = None) -> None:
        # Set BEFORE super().__init__(): get_css_variables runs during App
        # setup, and the vaf-* variables must exist from the first CSS parse.
        self._theme_key = theme_key if theme_key in THEME_ORDER else "vaf"
        super().__init__()
        self._bridge = bridge
        self._initial_message = initial_message
        self._live_msg = None
        self._restart_requested = False
        self._avatar_host = None          # the newest AgentMessage; carries the dot
        # Set by `clear` while a turn is still streaming: its remaining chunks
        # belong to a discarded conversation and must not paint (see _cmd_clear).
        self._muted_turn = False
        self._presence_state = "idle"
        self._open_cards: dict = {}
        self._gate_screen = None
        self._gate_answered = False
        self._tasks_stop = threading.Event()
        from vaf.cli.autosuggest import create_autosuggest
        self._suggester = create_autosuggest()

    # theme plumbing -----------------------------------------------------------------
    def get_css_variables(self):
        base = super().get_css_variables()
        base.update(css_variables_for(getattr(self, "_theme_key", "vaf")))
        return base

    # layout -------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Horizontal(id="main"):
            yield SessionsPanel(id="sessions")
            yield Transcript(id="transcript")
        yield TasksLine(id="tasksline")
        yield CompletionPopup(id="completion")
        yield PromptBox(id="promptbox")
        with StatusStrip(id="statusstrip"):
            yield KeyHints(id="keyhints")
            yield ContextBar(id="contextbar")

    def on_mount(self) -> None:
        # Themes are registered and selected HERE, not in the constructor.
        # Setting `App.theme` fires `_watch_theme`, which schedules its CSS
        # refresh with `call_next` - on an app that is not running yet those
        # callbacks are lost, and the palette never lands. Tried and reverted.
        for key in THEME_ORDER:
            self.register_theme(make_textual_theme(key))
        self.theme = textual_theme_name(self._theme_key)

        box = self.query_one("#promptbox", PromptBox)
        box.border_subtitle = "enter send · ctrl+j newline · tab complete · up history"
        box.popup = self.query_one("#completion", CompletionPopup)
        box.load_history(load_history())
        box.focus()
        self.query_one("#contextbar", ContextBar).styles.width = "auto"
        self.query_one("#keyhints").styles.width = "1fr"

        self.transcript.add_class("only-banner")
        self._mount_scrolled(StartBanner(*self._banner_facts()))
        self._refresh_chrome()
        self._bridge.request_session_list()

        # The classic result-notifier thread, as timers: the drain runs on the
        # bridge lane, the tasks poll on its own daemon (file IO off the UI thread).
        self.set_interval(2.5, self._bridge.drain_tick)
        # A SECOND, independent interval - deliberately not folded into the one
        # above. That one polls a shared file for finished children; this one pops
        # an in-memory heap for "something woke me". A 60-second timer must not
        # inherit the file poll's cadence, and a slow drain must not delay a wake.
        self.set_interval(1.0, self._bridge.queue_tick)
        threading.Thread(target=self._tasks_loop, daemon=True,
                         name="vaf-tui-tasks").start()
        self._bridge.refresh_context()
        # A resumed session's conversation belongs on screen, under the
        # banner - not behind an empty transcript that pretends a fresh start.
        self._bridge.request_transcript_replay()

        if self._initial_message:
            self._send_user(self._initial_message)

    def on_unmount(self) -> None:
        self._tasks_stop.set()
        # While this thread is alive and the loop is still open. bridge.shutdown()
        # runs only after app.run() returned, which is too late to stop a drain
        # closure already sitting in the lane's queue.
        self._bridge.begin_stopping()

    def _tasks_loop(self) -> None:
        while not self._tasks_stop.wait(1.5):
            entries = self._bridge.tasks_snapshot()
            try:
                self.call_from_thread(
                    self.query_one("#tasksline", TasksLine).set_tasks, entries)
            except Exception:
                return                        # the app is gone; so is the poll

    # helpers ------------------------------------------------------------------------
    @property
    def transcript(self) -> Transcript:
        return self.query_one("#transcript", Transcript)

    def _mount_scrolled(self, widget) -> None:
        # Chronology rule: anything mounted below the live agent bubble SEALS
        # it - the next stream chunk opens a NEW bubble at the bottom. Without
        # this, a turn's later chunks (e.g. the answer after a tool call) would
        # teleport into a bubble mounted before the tool card, and the
        # transcript would read out of order.
        if not isinstance(widget, AgentMessage) and self._live_msg is not None:
            self._live_msg.done()
            self._live_msg = None
        if not isinstance(widget, StartBanner):
            self.transcript.remove_class("only-banner")
        self.transcript.mount(widget)
        if isinstance(widget, AgentMessage):
            # The living dot sits beside the NEWEST reply only (web-UI rule):
            # activate it here, drop it from the previous host.
            if self._avatar_host is not None:
                self._avatar_host.set_avatar_visible(False)
            widget.set_avatar_visible(True)
            widget.avatar.set_state(self._presence_state)
            self._avatar_host = widget

    def _banner_facts(self):
        """The rows of the start banner, and the one line that tells a user how
        to get back into an older conversation - the panel alone never said."""
        from vaf.core.config import Config

        session = self._bridge.session
        messages = len(getattr(session, "messages", []) or [])
        name = str(getattr(session, "name", "") or "") or str(session.id)[:12]
        state = "resumed" if messages else "new"

        provider = str(Config.get("provider", "local"))
        model = str(Config.get(f"api_model_{provider}", "") if provider != "local"
                    else Config.get("model", ""))

        rows = [
            ("VAF", f"Veyllo Agentic Framework {_vaf_version()}"),
            (None, None),
            ("agent", _agent_name()),
            ("session", f"{name}  ({state}"
                        + (f", {messages} messages)" if messages else ")")),
            ("id", str(session.id)),
            ("model", f"{provider} · {model}" if model else provider),
            ("time", _local_datetime()),
        ]
        hint = ("ctrl+s lists your sessions - arrows to walk, enter to load. "
                "F1 shows every key and command.")
        return rows, hint

    def _refresh_chrome(self) -> None:
        from vaf.core.config import Config
        top = self.query_one("#topbar", TopBar)
        session = self._bridge.session
        top.session_name = (getattr(session, "name", "") or str(session.id)[:12])
        provider = str(Config.get("provider", "local"))
        # The local model lives under `model`; there is no `model_name` key.
        model = str(Config.get(f"api_model_{provider}", "") if provider != "local"
                    else Config.get("model", ""))
        top.model_chip = f"{provider} · {model}" if model else provider
        # The same OR the runtime asks (vaf/core/speech.py is_stt_enabled).
        top.mic_on = bool(Config.get("speech_stt_enabled", False)
                          or Config.get("stt_enabled", False))

    def set_presence(self, state: str, detail: str = "") -> None:
        self._presence_state = state
        self.query_one("#topbar", TopBar).working = state in (
            "thinking", "working", "talking")
        if self._avatar_host is not None:
            self._avatar_host.avatar.set_state(state)

    def set_context(self, used: int, total: int) -> None:
        self.query_one("#contextbar", ContextBar).set_usage(used, total)

    # transcript mutations (called by TuiEvents, always on the UI thread) ------------
    def begin_agent_message(self) -> None:
        # Lazy on purpose: the bubble mounts with the FIRST chunk, not at turn
        # start - router/context narration fires before streaming, and an
        # eagerly mounted bubble would sit above it (or stay behind as an empty
        # header when the turn streams nothing).
        self._live_msg = None

    def _ensure_live_msg(self) -> AgentMessage:
        if self._live_msg is None:
            self._live_msg = AgentMessage()
            # A turn discarded by `clear` keeps its bubble UNMOUNTED: the
            # streaming path stays untouched (feed() buffers, _flush() is a
            # no-op while unmounted, no interval is ever armed), and nothing
            # paints into the transcript the user just emptied.
            if not self._muted_turn:
                self._mount_scrolled(self._live_msg)
        return self._live_msg

    def feed_agent(self, text: str) -> None:
        # No scroll here, and none anywhere else in this module: the transcript
        # anchors itself to its own bottom (see Transcript in widgets.py).
        self._ensure_live_msg().feed(text)

    def feed_think(self, text: str) -> None:
        self._ensure_live_msg().feed_think(text)

    def end_agent_message(self) -> None:
        if self._live_msg is not None:
            self._live_msg.done()
        self._live_msg = None
        # The discarded turn is over; the next one paints normally again.
        self._muted_turn = False

    def turn_finished(self, tools_ran: bool) -> None:
        self.end_agent_message()

    def add_event_note(self, type_name: str, message: str, style: str) -> None:
        self._mount_scrolled(EventNote(type_name, message, style))

    def add_system_note(self, text: str) -> None:
        self._mount_scrolled(SystemNote(text))

    def add_wake_message(self, text: str, kind: str = "timer") -> None:
        """What woke the agent, on its own card, before the turn it caused."""
        self._mount_scrolled(WakeMessage(text, kind))

    def add_renderable(self, obj) -> None:
        self._mount_scrolled(RenderableNote(obj))

    def add_context_note(self, status: dict) -> None:
        self._mount_scrolled(ContextNote(status))

    def session_switched(self, session_id: str, message_count: int) -> None:
        self._refresh_chrome()
        self.add_system_note(
            f"switched to session {session_id[:12]} · {message_count} messages")
        if self.query_one("#sessions", SessionsPanel).has_class("visible"):
            self._bridge.request_session_list()

    # The newest messages a replay paints as widgets. Enough to read back the
    # working context; a 400-message session as 800 mounted widgets would make
    # every switch pay seconds for scrollback nobody reads - /export carries
    # the full record.
    REPLAY_CAP = 40

    def replay_transcript(self, entries, fresh: bool) -> None:
        """Paint a session's conversation. `fresh` clears first (a session
        SWITCH must not leave the old conversation above the new one); the
        boot replay keeps the start banner and mounts beneath it."""
        if fresh:
            self.transcript.clear()
            self._live_msg = None
            self._avatar_host = None
        entries = list(entries or [])
        shown = entries[-self.REPLAY_CAP:]
        if len(entries) > len(shown):
            self.add_system_note(
                f"{len(entries) - len(shown)} older messages not shown - "
                f"/export <file> writes the full conversation")
        for role, text, when in shown:
            if role == "user":
                self._mount_scrolled(UserMessage(text, when=when))
            else:
                # static_text: complete content, no flush ticker - feed/done
                # against a just-scheduled mount races on_mount.
                self._mount_scrolled(AgentMessage(when=when, static_text=text))

    def tool_started(self, tool: str, preview: str) -> None:
        card = ToolCard(tool, preview)
        self._open_cards.setdefault(tool, []).append(card)
        self._mount_scrolled(card)

    def tool_ended(self, tool: str, ok: bool, duration: str, output: str = "") -> None:
        cards = self._open_cards.get(tool) or []
        if cards:
            card = cards.pop(0)
            if output:
                card.set_output(output)
            card.finish(ok=ok, duration=duration)

    def show_gate(self, tool: str, reason: str, preview: str = "", notes: str = "") -> None:
        if self._gate_screen is not None:
            return
        screen = GateScreen(tool, reason, preview, notes)
        self._gate_screen = screen
        self._gate_answered = False

        def _answered(result) -> None:
            self._gate_screen = None
            if self._gate_answered:
                # Resolved elsewhere (web UI, timeout) while the modal was up -
                # answering again would cancel a gate that no longer waits.
                return
            self._gate_answered = True
            self._bridge.answer_gate(result or "cancel")
            self.add_system_note(f"gate: {result or 'cancelled'}")

        self.push_screen(screen, _answered)

    def gate_decided(self, decision: str) -> None:
        # Resolved elsewhere (web UI, timeout): take the modal down if it is up.
        # dismiss() pops the STACK TOP, so only dismiss while the gate modal IS
        # the top screen - with another overlay above it, leave it; its own
        # answer path is disarmed via the flag either way.
        if self._gate_screen is not None:
            self._gate_answered = True
            if self.screen is self._gate_screen:
                screen, self._gate_screen = self._gate_screen, None
                try:
                    screen.dismiss(None)
                except Exception:
                    pass
            self.add_system_note(f"gate: {decision}")

    # actions ------------------------------------------------------------------------
    def action_palette(self) -> None:
        self.push_screen(PaletteScreen(), self._run_palette_choice)

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen())

    def action_model(self) -> None:
        def _chosen(choice) -> None:
            if not choice:
                return
            from vaf.core.config import Config
            provider, model, want_key = choice
            stored = Config.get_api_key(provider) if provider else ""
            # Ask when the user asked (`k`), and when the provider cannot work
            # without it. The bridge refuses a keyless switch either way; this
            # is the difference between being refused and being helped.
            if provider and provider != "local" and (want_key or not stored):
                def _keyed(key) -> None:
                    if key is None:          # cancelled: change nothing at all
                        return
                    self._bridge.apply_provider_change(provider, model, new_key=key)

                self.push_screen(ApiKeyScreen(provider, stored), _keyed)
                return
            self._bridge.apply_provider_change(provider, model)

        self.push_screen(ModelScreen(), _chosen)

    def action_history(self) -> None:
        self.push_screen(HistoryScreen(self._bridge.history))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_voice(self) -> None:
        """`l`: the classic listen flow - overlay up, capture on the bridge's
        listen thread, the transcript sent as a turn. Escape cancels the
        CAPTURE through the bridge, not only the view."""
        if isinstance(self.screen, VoiceScreen):
            return                      # one capture at a time; esc cancels

        def _closed(_result) -> None:
            self._bridge.cancel_listen()

        self.push_screen(VoiceScreen(), _closed)
        self._bridge.listen_voice()

    def voice_level(self, phase: str, energy: float, threshold: float) -> None:
        if isinstance(self.screen, VoiceScreen):
            self.screen.set_state(phase, energy, threshold)

    def voice_done(self, text, note: str) -> None:
        """Close the overlay and route the transcript.

        Through `_send_user` - the SAME path a typed message takes - so the
        turn gets its "You" bubble and its history entry; the bridge
        deliberately does not submit (a lane-side submit streamed an answer
        into a transcript with no visible question). With `ux_voice_review`
        on, the transcript lands in the input box instead: read, fix what the
        transcription misheard, enter sends - escape or editing it away costs
        nothing, which is the whole point of reviewing.
        """
        if isinstance(self.screen, VoiceScreen):
            self.screen.dismiss("")
        if text:
            from vaf.core.config import Config
            if Config.get("ux_voice_review", False):
                box = self.query_one("#promptbox", PromptBox)
                box.text = text
                box.focus()
                self.add_system_note("heard - edit if needed, enter sends")
            else:
                self._send_user(text)
            return
        if note and note != "cancelled":
            self.add_event_note("Voice", note, "warning")

    def action_toggle_sessions(self) -> None:
        panel = self.query_one("#sessions", SessionsPanel)
        panel.toggle_class("visible")
        if panel.has_class("visible"):
            # Listing globs and json-loads up to 20 session files; that belongs
            # on the lane, not on the thread that has to stay responsive.
            self._bridge.request_session_list()
            panel.focus_list()
        else:
            self.query_one("#promptbox", PromptBox).focus()

    def show_session_list(self, sessions) -> None:
        panel = self.query_one("#sessions", SessionsPanel)
        panel.refresh_sessions(sessions, str(self._bridge.session.id))
        if panel.has_class("visible"):
            panel.focus_list()

    def action_next_theme(self) -> None:
        """Cycle themes for THIS SESSION - browsing is not choosing.

        This used to persist on every press, and it burned the same person
        twice: looking through the list rewrites the startup default with each
        step, and whoever walks it once ends on the LAST entry - matrix, which
        reads as a plain green terminal, so the next `vaf run` looked like the
        VAF theme was gone entirely. The classic lane never persisted here
        (`theme <name>` in run.py sets only the process cache; the config was
        written by `vaf settings` alone) - this restores that contract.
        Saving a theme is the Settings > Theme row, a deliberate selection.
        """
        idx = (THEME_ORDER.index(self._theme_key) + 1) % len(THEME_ORDER)
        self._apply_session_theme(THEME_ORDER[idx])

    @on(PromptBox.Changed)
    def _prompt_changed(self, event) -> None:
        """One place recomputes both affordances: the inline ghost suggestion
        and the completion menu. Cheap enough to run per keystroke - the
        suggester is an in-memory lookup and `complete()` only touches the
        filesystem for an `@` token."""
        box = self.query_one("#promptbox", PromptBox)
        before = box._text_before_cursor()
        try:
            box.suggestion = self._suggester.suggest(before) or ""
        except Exception:
            box.suggestion = ""
        popup = self.query_one("#completion", CompletionPopup)
        if before.startswith("/") or "@" in before:
            from vaf.cli.completion import complete
            popup.open_with(complete(before))
        elif popup.is_open:
            popup.close()

    @on(SessionsPanel.RoomSelected)
    def _room_picked(self, event: SessionsPanel.RoomSelected) -> None:
        """A room was picked in the panel: show it, do not load it.

        Routed through the same handler `/room <id>` uses, so there is one way to
        render a room and not two that drift. The live conversation stays where it
        was - a room is a read-only view, not a place the prompt box writes into.

        Deliberate boundary: this is a one-shot replay with no refresh loop, so the
        typing signal the web derives (Room.activity + the runner's turn marker)
        has nothing to ride on here. Building a poll for ONE indicator would be the
        tail wagging the view; the day this panel goes live-updating, the signal is
        one call away.
        """
        self.query_one("#sessions", SessionsPanel).remove_class("visible")
        self.query_one("#promptbox", PromptBox).focus()
        if event.room_id:
            self._cmd_room([event.room_id])

    @on(SessionsPanel.Selected)
    def _session_picked(self, event: SessionsPanel.Selected) -> None:
        panel = self.query_one("#sessions", SessionsPanel)
        panel.remove_class("visible")
        self.query_one("#promptbox", PromptBox).focus()
        if event.session_id and event.session_id != str(self._bridge.session.id):
            self._bridge.load_session(event.session_id)

    @on(SessionsPanel.NewRequested)
    def _session_new_requested(self, _event) -> None:
        self.query_one("#sessions", SessionsPanel).remove_class("visible")
        self.query_one("#promptbox", PromptBox).focus()
        self._bridge.new_session()

    @on(SessionsPanel.RenameRequested)
    def _session_rename_requested(self, event) -> None:
        sid, name = event.session_id, event.name

        def _entered(value) -> None:
            if value:
                self._bridge.rename_session(sid, value)

        # On TOP of the open panel - the list stays where the user was.
        self.push_screen(RenameScreen(name), _entered)

    @on(SessionsPanel.DeleteRequested)
    def _session_delete_requested(self, event) -> None:
        sid = event.session_id
        label = event.name or sid[:12]

        def _answered(yes) -> None:
            if yes:
                self._bridge.delete_session(sid)

        self.push_screen(
            ConfirmScreen(f"Delete session {label}? The file is removed."),
            _answered)

    @on(SettingsChanged)
    def _settings_changed(self, _msg: SettingsChanged) -> None:
        self._refresh_chrome()

    # commands -----------------------------------------------------------------------
    def _handlers(self) -> dict:
        """word -> callable(args). The registry decides WHICH words exist and
        where they run; this maps them to what the app actually does."""
        return {
            "help": lambda a: self.action_help(),
            "settings": lambda a: self.action_settings(),
            "model": lambda a: self.action_model(),
            "theme": self._cmd_theme,
            "history": lambda a: self.action_history(),
            "sessions": lambda a: self.action_toggle_sessions(),
            "session": self._cmd_session,
            "tools": lambda a: self.action_tools(),
            "context": lambda a: self._bridge.show_context(),
            "clear": self._cmd_clear,
            "undo": lambda a: self._bridge.undo_last_change(),
            "restore": lambda a: self._bridge.restore_context(),
            "export": self._cmd_export,
            "room": self._cmd_room,
            "listen": lambda a: self.action_voice(),
            "halt": lambda a: self._bridge.stop_speech(),
            "restart": lambda a: self._request_restart(),
            "exit": lambda a: self.exit(),
        }

    def _run_palette_choice(self, choice) -> None:
        """The palette hands back a `/word`; route it the same way a typed one
        goes, so there is no second dispatch path to keep in step."""
        if not choice:
            return
        parsed = parse(str(choice))
        if parsed.command is not None:
            self.run_command(parsed)

    def run_command(self, parsed) -> None:
        """Execute one parsed command, asking first when it says to."""
        cmd = parsed.command
        handler = self._handlers().get(cmd.word)
        if handler is None:                       # registry grew a word we forgot
            self.add_event_note("Command", f"/{cmd.word} is not wired here", "warning")
            return
        if cmd.lane == "agent" and self._bridge.busy:
            self.add_system_note(f"/{cmd.word}: queued, runs after the current turn")
        if cmd.confirm:
            self.push_screen(ConfirmScreen(cmd.confirm),
                             lambda yes: handler(parsed.args) if yes else None)
        else:
            handler(parsed.args)

    def _cmd_room(self, args) -> None:
        """Paint an agent room as a group chat, or list the rooms with no argument.

        A room is NOT a session: it has many writers and no single history to load, so
        it is painted into the transcript as a read-only view rather than switched to.
        The live conversation stays where it was.
        """
        from vaf.cli.tui_app.widgets import PeerMessage
        try:
            from vaf.core.config import get_local_admin_scope_id
            from vaf.core.a2a.room import (Room, describe, joined_rooms,
                                            unread_counts)
            from vaf.core.a2a.store import StoreError, UnsafeName
        except Exception as exc:                      # pragma: no cover - import guard
            self.add_event_note("Rooms", f"not available: {exc}", "warning")
            return

        key = str(get_local_admin_scope_id() or "local")
        if not args:
            rooms = joined_rooms(key)
            if not rooms:
                self.add_system_note("No agent rooms yet - `vaf a2a create` opens one")
                return
            pending = unread_counts(key)
            lines = [f"{room.room_id} ({room.kind}) as {identity.role}"
                     f"{f' - {pending[room.room_id]} unread' if pending.get(room.room_id) else ''}"
                     for room, identity in rooms]
            self.add_system_note("Rooms: " + " | ".join(lines))
            return

        room_id = str(args[0])
        try:
            room = Room.open(room_id)
        except (StoreError, UnsafeName):
            self.add_event_note("Rooms", f"no room {room_id!r} on this machine", "warning")
            return

        rows = room.transcript()
        if not rows:
            self.add_system_note(f"{room_id} is empty")
            return
        self.add_system_note(
            f"{room_id} ({room.kind}{', closed' if room.closed else ''}) - "
            f"{len(rows)} messages")
        # The task board, derived like everything else about a room (Room.tasks):
        # a directive, or anything a report chain answers. Shown as notes above the
        # replay so "what is in flight here" is readable without scrolling the talk.
        try:
            board = room.tasks()
        except Exception:
            board = []
        for task in board:
            arrow = f" -> {task['assignee_label']}" if task["assignee_label"] else ""
            self.add_system_note(
                f"[{task['status']}] {task['title']} "
                f"({task['requester_label']}{arrow})")
        for entry in rows[-self.REPLAY_CAP:]:
            text = describe(entry)
            self._mount_scrolled(PeerMessage(
                entry["label"], text, badge=entry["role"], kind=entry["kind"],
                when=_room_clock(entry["ts"]),
            ))

    def _cmd_theme(self, args) -> None:
        if not args:
            self.action_next_theme()
            return
        name = args[0].lower()
        if name not in THEME_ORDER:
            self.add_event_note(
                "Theme", f"unknown theme {name!r} - have: "
                         + ", ".join(THEME_ORDER), "warning")
            return
        self._apply_session_theme(name)

    def _apply_session_theme(self, key: str) -> None:
        """One theme for this session, on all three surfaces that must agree:
        the app key (get_css_variables + the `t` cycle count), Textual's
        stylesheet, and the per-process ThemeManager cache (which the settings
        marker and every classic renderer read). NO config write - see
        action_next_theme for why browsing must not choose."""
        from vaf.cli.themes import ThemeManager
        self._theme_key = key
        ThemeManager.set_theme(key)
        self.theme = textual_theme_name(key)
        self.notify(f"theme: {key} - this session; Settings › Theme saves it",
                    timeout=2.5)

    def _cmd_session(self, args) -> None:
        if not args:
            self.action_toggle_sessions()
            return
        word = str(args[0]).lower()
        # The classic lane had `session list` and `session current`; treating
        # them as IDs sent both into a red "cannot load" note.
        if word == "list":
            panel = self.query_one("#sessions", SessionsPanel)
            if not panel.has_class("visible"):
                self.action_toggle_sessions()      # open, never close
            else:
                self._bridge.request_session_list()
                panel.focus_list()
            return
        if word == "current":
            self._bridge.describe_session()
            return
        if word == "new":
            self._bridge.new_session()
            return
        if word == "rename":
            rest = " ".join(str(a) for a in args[1:]).strip()
            if not rest:
                self.add_event_note("Command",
                                    "usage: /session rename <new name>",
                                    "warning")
                return
            self._bridge.rename_session(str(self._bridge.session.id), rest)
            return
        self._bridge.load_session(args[0])

    def _cmd_export(self, args) -> None:
        if not args:
            # The classic contract: no default filename, an honest usage line.
            self.add_event_note("Export", "usage: /export <file>", "warning")
            return
        self._bridge.export_session(args[0])

    def _cmd_clear(self, args) -> None:
        # The transcript goes NOW (the user asked for it); the agent-side reset
        # is queued on the lane and confirms itself when it lands.
        self.transcript.clear()
        self._live_msg = None
        self._avatar_host = None
        # A turn that is STILL STREAMING belongs to the conversation the user
        # just discarded: without this its remaining chunks mount a fresh reply
        # into the empty transcript, with no visible question above it and a
        # history that `clear_conversation` deletes moments later. Same shape as
        # the voice round's missing "You" bubble - an answer nobody can see the
        # question for. Chunks are dropped until the next turn starts.
        self._muted_turn = bool(self._bridge.busy)
        if self._muted_turn:
            self.add_system_note("cleared - the reply still running was discarded")
        self._bridge.clear_conversation()

    def action_tools(self) -> None:
        from vaf.cli.tool_catalog import describe_tools
        self.push_screen(ToolsScreen(describe_tools(self._bridge.agent)))

    def _request_restart(self) -> None:
        """Exec only AFTER the screen is released - see run_tui's finally."""
        self._restart_requested = True
        self.exit()

    # input --------------------------------------------------------------------------
    @on(PromptBox.Submitted)
    def _submitted(self, event: PromptBox.Submitted) -> None:
        # The classic loop's unconditional barge-in, BEFORE any parsing: TTS is
        # asynchronous and routinely outlives the turn that produced it, so any
        # submitted input - a message, /help, even a typo - silences running
        # speech. Quiet on purpose; only the explicit `halt` narrates.
        self._bridge.stop_speech(announce=False)
        parsed = parse(event.text)
        if parsed.command is not None:
            self.run_command(parsed)
            return
        if parsed.unknown_word:
            # A /slash form NEVER reaches the model. Inline rather than a toast:
            # a command that did not run belongs in the conversation record.
            hint = suggest(parsed.unknown_word)
            extra = f" - did you mean /{hint[0]}?" if hint else ""
            self.add_event_note(
                "Command", f"unknown: /{parsed.unknown_word}{extra} "
                           f"(F1 lists everything)", "warning")
            return
        self._send_user(event.text)

    def _send_user(self, text: str) -> None:
        # The same two stores the classic prompt writes: the shared history file
        # and the learned corpus (whose save is debounced, so this is free).
        try:
            append_history(text)
            self._suggester.add_to_history(text)
        except Exception:
            pass
        self._mount_scrolled(UserMessage(text))
        # A turn nobody started can be running (a fired timer). Without this the
        # message gets a sealed bubble, a reply that is not its own, and no
        # explanation. Same sentence the slash-command path already prints.
        if self._bridge.busy:
            self.add_system_note("queued, runs after the current turn")
        self._bridge.submit_turn(text)


def _exec_restart() -> None:
    """Replace this process with a fresh `vaf run`.

    The exec branches are ported verbatim from the classic lane - the `.exe`
    and `-m vaf` cases are load-bearing on Windows. What is NOT ported is its
    npm teardown: `npm_process` is initialised to None there and never
    assigned, so that block has always been dead. The frontend, if this
    process owns one at all, is torn down through its manager.
    """
    import os
    import sys

    from vaf.cli.ui import UI as _UI
    try:
        from vaf.core.frontend_manager import FrontendManager
        FrontendManager().stop_frontend(wait_for_exit=True)
    except Exception:
        pass
    try:
        if sys.argv[0].endswith(".exe"):
            os.execl(sys.argv[0], sys.argv[0], *sys.argv[1:])
        elif sys.argv[0].endswith("__main__.py"):
            os.execl(sys.executable, sys.executable, "-m", "vaf", *sys.argv[1:])
        else:
            os.execl(sys.executable, sys.executable, *sys.argv)
    except Exception as exc:
        _UI.error(f"Restart failed: {exc}")


def run_tui(message: str = None, theme: str = None, session_id: str = None,
            verbose: bool = False) -> None:
    """Boot the agent classically (plain terminal), then hand the screen to the
    app; tear the sinks down in the reverse order on the way out."""
    from vaf.cli.tui import UI
    from vaf.cli.tui_app.agent_bridge import boot_bridge

    theme_key = initial_theme_key(theme)
    # Seed the per-process theme cache the same way the modern lane does
    # (`_run_modern` in run.py). Without it a `--theme` flag reaches the app
    # but not the readers of ThemeManager.current(): the settings overlay would
    # mark a different row than the colors on screen.
    ThemeManager.set_theme(theme_key)
    events = TuiEvents()
    bridge = boot_bridge(events, theme_key, session_id, verbose)

    app = VafApp(bridge, theme_key=theme_key, initial_message=message)
    events.bind(app)
    UI.add_console_sink(bridge.on_console_event)
    UI.set_app_mode(True)
    try:
        app.run()
    finally:
        UI.set_app_mode(False)
        UI.remove_console_sink(bridge.on_console_event)
        try:
            app._suggester.flush()      # the debounced save must not be lost
        except Exception:
            pass
        bridge.shutdown()
        # Now that the alternate screen is gone, the plain terminal can carry
        # the session id and the way back - the classic lane printed the same
        # thing, just with a blocking question in front of it.
        if getattr(bridge, "farewell", ""):
            print(bridge.farewell)

    # ONLY here, with the alternate screen released and the tty restored:
    # exec'ing from inside the app would hand the new process a raw terminal.
    if getattr(app, "_restart_requested", False):
        _exec_restart()
