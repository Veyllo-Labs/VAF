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

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static

from vaf.cli.tui_app.screens import (
    GateScreen,
    HelpScreen,
    HistoryScreen,
    ModelScreen,
    PaletteScreen,
    SessionsPanel,
    SettingsChanged,
    SettingsScreen,
)
from vaf.cli.tui_app.theme_bridge import (
    THEME_ORDER,
    css_variables_for,
    initial_theme_key,
    make_textual_theme,
    persist_theme,
    textual_theme_name,
)
from vaf.cli.tui_app.widgets import (
    AgentMessage,
    ContextBar,
    EventNote,
    PromptBox,
    RenderableNote,
    SystemNote,
    TasksLine,
    ToolCard,
    TopBar,
    UserMessage,
)


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
        try:
            app.call_from_thread(fn, *args)
        except Exception:
            try:
                fn(*args)
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
    def tool_end(self, tool, ok, duration): self._ui(self._app_call, "tool_ended", tool, ok, duration)
    def gate_required(self, tool, reason):  self._ui(self._app_call, "show_gate", tool, reason)
    def gate_decision(self, decision):      self._ui(self._app_call, "gate_decided", decision)
    def presence(self, state, detail=""):   self._ui(self._app_call, "set_presence", state, detail)
    def context(self, used, total):         self._ui(self._app_call, "set_context", used, total)

    def _app_call(self, name, *args) -> None:
        getattr(self._app, name)(*args)


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
        dock: left; width: 28; background: $surface;
        border-right: solid $vaf-border; padding: 1 1; display: none;
    }
    #sessions.visible { display: block; }
    .panel-title { margin-bottom: 1; }
    .session-row { margin-bottom: 1; }
    #transcript {
        padding: 1 2 0 2;
        scrollbar-color: $vaf-border;
        scrollbar-background: $background;
        scrollbar-size-vertical: 1;
    }
    .msg-head { margin: 1 0 0 0; }
    .user-msg-wrap { height: auto; }
    .agent-msg-wrap { height: auto; }
    .user-msg {
        padding: 0 1; border-left: thick $accent; background: $surface;
    }
    .agent-msg { margin-left: 1; }
    .agent-think {
        margin: 0 0 1 1; padding: 0 1; height: auto;
        border-left: thick $vaf-border; background: $surface;
    }
    .system-note { margin: 1 0 0 1; }
    .event-note { margin: 0 0 0 1; }
    .renderable-note { margin: 1 0 0 1; }
    .code-block {
        margin: 1 0 0 1; padding: 0 1;
        border: round $vaf-border; background: $surface;
        border-title-color: $vaf-muted;
    }
    .tool-card {
        margin: 1 0 0 1; padding: 0 1; height: auto;
        border: round $vaf-border; background: $surface;
    }
    .tool-header { height: 1; }
    .tool-head-left { width: 1fr; }
    .tool-head-right { width: auto; }
    .tool-body { padding: 0 1; background: $background; }
    .subagent-line { margin: 1 0 0 1; }
    #tasksline { height: 1; margin: 0 2; background: $background; }
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
    GateScreen, VoiceScreen, SettingsScreen, ModelScreen, HistoryScreen, HelpScreen {
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
    .modal-body { margin-bottom: 1; }
    .settings-row { margin-bottom: 0; padding: 0 0; }
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
        self._avatar_host = None          # the newest AgentMessage; carries the dot
        self._presence_state = "idle"
        self._open_cards: dict = {}
        self._gate_screen = None
        self._gate_answered = False
        self._tasks_stop = threading.Event()

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
            yield VerticalScroll(id="transcript")
        yield TasksLine(id="tasksline")
        yield PromptBox(id="promptbox")
        with Horizontal(id="statusstrip"):
            yield Static(
                "[bold $text]S[/] [$vaf-muted]Settings[/]  "
                "[bold $text]C[/] [$vaf-muted]Model[/]  "
                "[bold $text]L[/] [$vaf-muted]Voice[/]  "
                "[bold $text]T[/] [$vaf-muted]Theme[/]  "
                "[bold $text]H[/] [$vaf-muted]History[/]  "
                "[bold $text]?[/] [$vaf-muted]Help[/]  "
                "[bold $text]/exit[/] [$vaf-muted]Quit[/]",
                id="keyhints",
            )
            yield ContextBar(id="contextbar")

    def on_mount(self) -> None:
        for key in THEME_ORDER:
            self.register_theme(make_textual_theme(key))
        self.theme = textual_theme_name(self._theme_key)

        box = self.query_one("#promptbox", PromptBox)
        box.border_subtitle = "enter send · ctrl+j newline"
        box.focus()
        self.query_one("#contextbar", ContextBar).styles.width = "auto"
        self.query_one("#keyhints").styles.width = "1fr"

        session = self._bridge.session
        note = (f"session restored · {len(session.messages)} messages"
                if getattr(session, "messages", None) else "new session")
        self._mount_scrolled(SystemNote(note))
        self._refresh_chrome()
        self.query_one("#sessions", SessionsPanel).refresh_sessions(
            self._bridge.list_sessions(), session.id)

        # The classic result-notifier thread, as timers: the drain runs on the
        # bridge lane, the tasks poll on its own daemon (file IO off the UI thread).
        self.set_interval(2.5, self._bridge.drain_tick)
        threading.Thread(target=self._tasks_loop, daemon=True,
                         name="vaf-tui-tasks").start()
        self._bridge.refresh_context()

        if self._initial_message:
            self._send_user(self._initial_message)

    def on_unmount(self) -> None:
        self._tasks_stop.set()

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
    def transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def _mount_scrolled(self, widget) -> None:
        # Chronology rule: anything mounted below the live agent bubble SEALS
        # it - the next stream chunk opens a NEW bubble at the bottom. Without
        # this, a turn's later chunks (e.g. the answer after a tool call) would
        # teleport into a bubble mounted before the tool card, and the
        # transcript would read out of order.
        if not isinstance(widget, AgentMessage) and self._live_msg is not None:
            self._live_msg.done()
            self._live_msg = None
        self.transcript.mount(widget)
        if isinstance(widget, AgentMessage):
            # The living dot sits beside the NEWEST reply only (web-UI rule):
            # activate it here, drop it from the previous host.
            if self._avatar_host is not None:
                self._avatar_host.set_avatar_visible(False)
            widget.set_avatar_visible(True)
            widget.avatar.set_state(self._presence_state)
            self._avatar_host = widget
        self.transcript.scroll_end(animate=False)

    def _refresh_chrome(self) -> None:
        from vaf.core.config import Config
        top = self.query_one("#topbar", TopBar)
        session = self._bridge.session
        top.session_name = (getattr(session, "name", "") or str(session.id)[:12])
        provider = str(Config.get("provider", "local"))
        model = (str(Config.get(f"api_model_{provider}", "")) if provider != "local"
                 else str(Config.get("model_name", "local")))
        top.model_chip = f"{provider} · {model}" if model else provider
        top.mic_on = bool(Config.get("speech_stt_enabled", False))

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
            self._mount_scrolled(self._live_msg)
        return self._live_msg

    def feed_agent(self, text: str) -> None:
        self._ensure_live_msg().feed(text)
        self.transcript.scroll_end(animate=False)

    def feed_think(self, text: str) -> None:
        self._ensure_live_msg().feed_think(text)
        self.transcript.scroll_end(animate=False)

    def end_agent_message(self) -> None:
        if self._live_msg is not None:
            self._live_msg.done()
        self._live_msg = None

    def turn_finished(self, tools_ran: bool) -> None:
        self.end_agent_message()

    def add_event_note(self, type_name: str, message: str, style: str) -> None:
        self._mount_scrolled(EventNote(type_name, message, style))

    def add_system_note(self, text: str) -> None:
        self._mount_scrolled(SystemNote(text))

    def add_renderable(self, obj) -> None:
        self._mount_scrolled(RenderableNote(obj))

    def tool_started(self, tool: str, preview: str) -> None:
        card = ToolCard(tool, preview)
        self._open_cards.setdefault(tool, []).append(card)
        self._mount_scrolled(card)

    def tool_ended(self, tool: str, ok: bool, duration: str) -> None:
        cards = self._open_cards.get(tool) or []
        if cards:
            cards.pop(0).finish(ok=ok, duration=duration)

    def show_gate(self, tool: str, reason: str) -> None:
        if self._gate_screen is not None:
            return
        screen = GateScreen(tool, reason)
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
        self.push_screen(PaletteScreen(), self._route_command)

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen())

    def action_model(self) -> None:
        self.push_screen(ModelScreen())

    def action_history(self) -> None:
        self.push_screen(HistoryScreen(self._bridge.history))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_voice(self) -> None:
        self.notify("Voice input lands in the next round - "
                    "`vaf run --classic` has it today.", timeout=2.5)

    def action_toggle_sessions(self) -> None:
        panel = self.query_one("#sessions", SessionsPanel)
        panel.toggle_class("visible")
        if panel.has_class("visible"):
            panel.refresh_sessions(self._bridge.list_sessions(),
                                   self._bridge.session.id)

    def action_next_theme(self) -> None:
        idx = (THEME_ORDER.index(self._theme_key) + 1) % len(THEME_ORDER)
        self._theme_key = THEME_ORDER[idx]
        persist_theme(self._theme_key)
        self.theme = textual_theme_name(self._theme_key)
        self.notify(f"theme: {self._theme_key}", timeout=1.5)

    @on(SettingsChanged)
    def _settings_changed(self, _msg: SettingsChanged) -> None:
        self._refresh_chrome()

    def _route_command(self, cmd) -> None:
        if not cmd:
            return
        routes = {
            "/settings": self.action_settings, "/model": self.action_model,
            "/voice": self.action_voice, "/theme": self.action_next_theme,
            "/history": self.action_history, "/sessions": self.action_toggle_sessions,
            "/help": self.action_help, "/exit": self.exit, "/quit": self.exit,
        }
        handler = routes.get(cmd)
        if handler:
            handler()
        else:
            self.notify(f"{cmd}: not a command here - F1 lists everything", timeout=2.0)

    # input --------------------------------------------------------------------------
    # The classic run loop's words keep working typed alone into the prompt.
    WORD_ROUTES = {
        "s": "/settings", "settings": "/settings",
        "c": "/model", "model": "/model",
        "t": "/theme", "theme": "/theme",
        "h": "/history", "history": "/history",
        "l": "/voice", "listen": "/voice",
        "?": "/help", "help": "/help",
        "exit": "/exit", "quit": "/exit", "bye": "/exit",
    }

    @on(PromptBox.Submitted)
    def _submitted(self, event: PromptBox.Submitted) -> None:
        text = event.text
        lowered = text.lower()
        if lowered.startswith("/"):
            self._route_command(lowered.split()[0])
            return
        if lowered in self.WORD_ROUTES:
            self._route_command(self.WORD_ROUTES[lowered])
            return
        self._send_user(text)

    def _send_user(self, text: str) -> None:
        self._mount_scrolled(UserMessage(text))
        self._bridge.submit_turn(text)


def run_tui(message: str = None, theme: str = None, session_id: str = None,
            verbose: bool = False) -> None:
    """Boot the agent classically (plain terminal), then hand the screen to the
    app; tear the sinks down in the reverse order on the way out."""
    from vaf.cli.tui import UI
    from vaf.cli.tui_app.agent_bridge import boot_bridge

    theme_key = initial_theme_key(theme)
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
        bridge.shutdown()
