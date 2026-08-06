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
    SessionsPanel,
    SettingsChanged,
    SettingsScreen,
    ToolsScreen,
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
    """
    try:
        from vaf.auth.user_workspace import get_user_workspace
        name = (get_user_workspace("admin").get_identity() or {}).get("name")
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
    def gate_required(self, tool, reason):  self._ui(self._app_call, "show_gate", tool, reason)
    def gate_decision(self, decision):      self._ui(self._app_call, "gate_decided", decision)
    def presence(self, state, detail=""):   self._ui(self._app_call, "set_presence", state, detail)
    def context(self, used, total):         self._ui(self._app_call, "set_context", used, total)
    def context_status(self, status):       self._ui(self._app_call, "add_context_note", status)
    def session_switched(self, sid, count): self._ui(self._app_call, "session_switched", sid, count)
    def session_list(self, sessions):       self._ui(self._app_call, "show_session_list", sessions)
    def chrome_changed(self):               self._ui(self._app_call, "_refresh_chrome")
    def wake_message(self, text, kind):      self._ui(self._app_call, "add_wake_message", text, kind)

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
    NumberScreen, AboutScreen, ApiKeyScreen, ConfirmScreen, ToolsScreen {
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
        self.notify("Voice input lands in the next round - "
                    "`vaf run --classic` has it today.", timeout=2.5)

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
        idx = (THEME_ORDER.index(self._theme_key) + 1) % len(THEME_ORDER)
        self._theme_key = THEME_ORDER[idx]
        persist_theme(self._theme_key)
        self.theme = textual_theme_name(self._theme_key)
        self.notify(f"theme: {self._theme_key}", timeout=1.5)

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

    @on(SessionsPanel.Selected)
    def _session_picked(self, event: SessionsPanel.Selected) -> None:
        panel = self.query_one("#sessions", SessionsPanel)
        panel.remove_class("visible")
        self.query_one("#promptbox", PromptBox).focus()
        if event.session_id and event.session_id != str(self._bridge.session.id):
            self._bridge.load_session(event.session_id)

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
        self._theme_key = name
        persist_theme(name)
        self.theme = textual_theme_name(name)
        self.notify(f"theme: {name}", timeout=1.5)

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
