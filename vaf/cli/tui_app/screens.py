# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""All overlays of the terminal app. Every one is keyboard-complete: arrows walk,
enter/space activates, esc goes back - the mouse is a second way in, never the
only one.

The settings screen is the `vaf settings` main menu (cli/cmd/settings.py) as a
stacked arrow menu. Boolean rows write their real config keys immediately - the
same single `Config.set` the inquirer menu performs. Rows whose flows need an
agent rebuild or a real backend (provider/model switches, context limit, model
download, microphone) display live values but route to `vaf settings` for now;
each says so instead of pretending.
"""
import time

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

from vaf.cli.themes import THEMES
from vaf.cli.tui_app.theme_bridge import THEME_ORDER, persist_theme


def _esc(text) -> str:
    return str(text).replace("[", r"\[")


class GateScreen(ModalScreen[str]):
    """The confirmation gate as a modal - warning-colored, keyboard-first.

    Dismisses with "once" / "always" / "cancel"; the bridge maps those onto the
    engine's allow_once/allow_always/cancel contract and answers the waiting
    gate through web_interface.resolve_gate.
    """

    BINDINGS = [
        Binding("y", "answer('once')", "allow once"),
        Binding("a", "answer('always')", "always"),
        Binding("escape", "answer('cancel')", "cancel"),
    ]

    def __init__(self, tool: str, reason: str) -> None:
        super().__init__()
        self._tool = tool
        self._reason = reason

    def compose(self) -> ComposeResult:
        with Vertical(id="gate-box", classes="modal-box"):
            yield Static("[bold $warning]⚠ confirmation required[/]", classes="modal-title")
            yield Static(
                f"[$text]The tool [bold]{_esc(self._tool)}[/bold] wants to run.[/]\n"
                f"[$vaf-muted]{_esc(self._reason)}[/]", classes="modal-body")
            yield Static(
                "[$text][bold]y[/bold][/] [$vaf-muted]allow once[/]   "
                "[$text][bold]a[/bold][/] [$vaf-muted]always in this directory[/]   "
                "[$text][bold]esc[/bold][/] [$vaf-muted]cancel[/]", classes="modal-keys")

    def action_answer(self, result: str) -> None:
        self.dismiss(result)


class VoiceScreen(ModalScreen[str]):
    """The classic listen_overlay, app-mode. Ships in this round but is routed
    to a notice until the speech wiring lands (next round)."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    LEVELS = "▁▂▃▄▅▆▇"

    def compose(self) -> ComposeResult:
        with Vertical(id="voice-box", classes="modal-box"):
            yield Static("[bold $error]● Recording[/]", id="voice-title", classes="modal-title")
            yield Static("", id="voice-bars")
            yield Static("[$vaf-muted]Speak now … (Silence to finish)[/]", id="voice-hint",
                         classes="modal-body")
            yield Static("[$text-disabled]esc[/] [$vaf-muted]cancel[/]", classes="modal-keys")

    def on_mount(self) -> None:
        import random
        self._rand = random
        self.set_interval(0.09, self._animate)

    def _animate(self) -> None:
        bars = "".join(f"[$error]{self._rand.choice(self.LEVELS)}[/]" for _ in range(24))
        self.query_one("#voice-bars", Static).update(bars)

    def show_heard(self, text: str) -> None:
        self.query_one("#voice-title", Static).update("[bold $success]✓ Heard[/]")
        self.query_one("#voice-hint", Static).update(f'[$text]"{_esc(text)}"[/]')

    def action_cancel(self) -> None:
        self.dismiss("")


class SettingsScreen(ModalScreen[None]):
    """The `vaf settings` main menu as ONE arrow-driven stacked menu."""

    BINDINGS = [
        Binding("escape", "go_back", "back/close"),
        Binding("space", "activate_row", "toggle", show=False),
    ]

    # Boolean rows that are safe to flip live: one Config.set, no agent rebuild -
    # exactly what the inquirer menu does for them (cli/cmd/settings.py main_menu).
    TOGGLES = {
        "web_ui_enabled": ("Web UI Dashboard", "restart VAF to apply"),
        "ux_auto_open_links": ("Auto-Open Links", ""),
        "ux_auto_open_outputs": ("Auto-Open Outputs", ""),
        "sub_agents_in_separate_terminals": ("Separate Terminals", ""),
        "subagent_timeout_enabled": ("Sub-Agent Timeout", ""),
        "speech_tts_enabled": ("Speech Output (TTS)", ""),
        "speech_stt_enabled": ("Speech-to-Text (STT)", ""),
        "persist_server": ("Server Persistence", ""),
    }

    TITLES = {"main": "Settings", "voice": "Settings › Voice", "theme": "Settings › Theme"}

    def __init__(self) -> None:
        super().__init__()
        self._stack = ["main"]
        self._rows: list = []
        self._row_statics: list = []

    # menu definitions ---------------------------------------------------------------
    def _cfg(self, key, default=None):
        from vaf.core.config import Config
        return Config.get(key, default)

    def _menu_rows(self, menu: str) -> list:
        if menu == "main":
            provider = str(self._cfg("provider", "local"))
            api_model = str(self._cfg(f"api_model_{provider}", "not set"))
            rows = [
                ("later", "provider", f"AI Provider: [$text]{_esc(provider.upper())}[/]"),
                ("later", "subagent_provider",
                 f"Sub-Agent Provider: [$text]{_esc(str(self._cfg('subagent_provider', 'inherit')).upper())}[/]"),
            ]
            if provider != "local":
                rows.append(("later", "api_model", f"API Model: [$text]{_esc(api_model)}[/]"))
            rows += [
                ("sep", None, ""),
                ("later", "context", f"Context Limit [$text]({int(self._cfg('n_ctx', 32768) or 32768):,})[/]"),
                ("later", "local_model", "Select Active Model"),
                ("later", "search_models", "Search & Download New Models"),
                ("sep", None, ""),
                ("toggle", "web_ui_enabled", ""),
                ("submenu", "theme", "Theme"),
                ("toggle", "ux_auto_open_links", ""),
                ("toggle", "ux_auto_open_outputs", ""),
                ("toggle", "sub_agents_in_separate_terminals", ""),
                ("toggle", "subagent_timeout_enabled", ""),
                ("submenu", "voice", "TTS / STT / Wake Word Settings"),
                ("later", "automations", "Automations"),
                ("sep", None, ""),
                ("later", "tools", "Show All Tools"),
                ("toggle", "persist_server", ""),
                ("back", None, "Exit Settings"),
            ]
            return rows
        if menu == "voice":
            return [
                ("toggle", "speech_tts_enabled", ""),
                ("later", "tts_engine",
                 f"TTS Engine: [$text]{_esc(str(self._cfg('speech_tts_engine', 'piper')))}[/]"),
                ("toggle", "speech_stt_enabled", ""),
                ("later", "mic", "Select Microphone"),
                ("later", "stt_lang", "Select Input Language"),
                ("later", "wake", "Wake Word"),
                ("back", None, "Back"),
            ]
        if menu == "theme":
            from vaf.cli.themes import ThemeManager
            rows = []
            for i, key in enumerate(THEME_ORDER):
                c = THEMES[key]
                marker = "▍" if key == ThemeManager.current() else " "
                rows.append(("theme", i,
                             f"[$primary]{marker}[/][$text]{key:<12}[/] "
                             f"[{c['primary']}]●[/][{c['secondary']}]●[/][{c['accent']}]●[/]"))
            return rows + [("back", None, "Back")]
        return [("back", None, "Back")]

    # rendering ----------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="settings-box", classes="modal-box"):
            yield Static("", id="settings-title", classes="modal-title")
            yield ListView(id="settings-list")
            yield Static("[$text-disabled]↑↓ select · enter/space toggles · esc back[/]",
                         classes="modal-keys")

    def on_mount(self) -> None:
        self._rebuild()

    def _row_markup(self, kind: str, arg, label: str) -> str:
        if kind == "sep":
            return "[$vaf-border]─────────────────[/]"
        if kind == "toggle":
            name, note = self.TOGGLES[arg]
            on_now = bool(self._cfg(arg, False))
            state = "[$success]on[/]" if on_now else "[$text-disabled]off[/]"
            hint = f"  [$text-disabled]({note})[/]" if note else ""
            return f"[$vaf-muted]{name:<24}[/] {state}{hint}"
        if kind == "submenu":
            return f"[$vaf-muted]{label}[/]  [$text-disabled]›[/]"
        if kind == "back":
            return f"[$text-disabled]‹ {label}[/]"
        if kind == "later":
            return f"[$vaf-muted]{label}[/]  [$text-disabled](vaf settings)[/]"
        return f"[$text]{label}[/]"

    def _rebuild(self) -> None:
        menu = self._stack[-1]
        self._rows = self._menu_rows(menu)
        self.query_one("#settings-title", Static).update(
            f"[bold $text]{self.TITLES.get(menu, 'Settings')}[/]")
        lv = self.query_one("#settings-list", ListView)
        lv.clear()
        self._row_statics = []
        for kind, arg, label in self._rows:
            static = Static(self._row_markup(kind, arg, label))
            self._row_statics.append(static)
            lv.append(ListItem(static))
        lv.focus()
        lv.index = 0

    def _refresh_labels(self) -> None:
        self._rows = self._menu_rows(self._stack[-1])
        for static, (kind, arg, label) in zip(self._row_statics, self._rows):
            static.update(self._row_markup(kind, arg, label))

    # activation ---------------------------------------------------------------------
    def _activate(self, idx: int) -> None:
        if not (0 <= idx < len(self._rows)):
            return
        kind, arg, label = self._rows[idx]
        if kind == "sep":
            return
        if kind == "back":
            self.action_go_back()
            return
        if kind == "toggle":
            from vaf.core.config import Config
            new_val = not bool(self._cfg(arg, False))
            Config.set(arg, new_val)
            self.app.notify(f"{self.TOGGLES[arg][0]}: {'on' if new_val else 'off'}",
                            timeout=1.5)
            self._refresh_labels()
            self.app.post_message(SettingsChanged())
            return
        if kind == "submenu":
            self._stack.append(arg)
            self._rebuild()
            return
        if kind == "theme":
            key = THEME_ORDER[arg]
            persist_theme(key)
            self.app.theme = f"vaf-{key}"
            self.app.notify(f"theme: {key}", timeout=1.5)
            self._refresh_labels()
            return
        if kind == "later":
            self.app.notify("This flow lands in the next round - use `vaf settings` "
                            "in a normal terminal for now.", timeout=2.5)

    @on(ListView.Selected, "#settings-list")
    def _selected(self, event: ListView.Selected) -> None:
        self._activate(event.list_view.index or 0)

    def action_activate_row(self) -> None:
        lv = self.query_one("#settings-list", ListView)
        self._activate(lv.index or 0)

    def action_go_back(self) -> None:
        if len(self._stack) > 1:
            self._stack.pop()
            self._rebuild()
        else:
            self.dismiss(None)


class SettingsChanged(events.Message):
    """Posted after a settings toggle so the app can refresh dependent chrome."""


class ModelScreen(ModalScreen[None]):
    """Provider · model overview (read-only in this round: a switch needs the
    agent rebuild flow, which lands with the settings round)."""

    BINDINGS = [Binding("escape", "close_model", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="model-box", classes="modal-box"):
            yield Static("[bold $text]Provider · Model[/]", classes="modal-title")
            yield ListView(id="model-list")
            yield Static("[$text-disabled]switching lands next round · esc closes[/]",
                         classes="modal-keys")

    def on_mount(self) -> None:
        from vaf.core.config import Config
        lv = self.query_one("#model-list", ListView)
        current_provider = str(Config.get("provider", "local"))
        catalog = getattr(Config, "PROVIDER_MODELS", {}) or {}
        for provider in catalog:
            entry = catalog.get(provider) or {}
            default = entry.get("default", "") if isinstance(entry, dict) else ""
            marker = "▍" if provider == current_provider else " "
            lv.append(ListItem(Static(
                f"[$primary]{marker}[/][$vaf-muted]{_esc(provider):<12}[/] "
                f"[$text]{_esc(default)}[/]")))
        if not catalog:
            lv.append(ListItem(Static("[$text-disabled]no provider catalog loaded[/]")))
        lv.focus()

    @on(ListView.Selected, "#model-list")
    def _picked(self, event: ListView.Selected) -> None:
        self.app.notify("Provider switching lands next round - use `vaf settings`.",
                        timeout=2.5)

    def action_close_model(self) -> None:
        self.dismiss(None)


class HistoryScreen(ModalScreen[None]):
    """The turns of the live session."""

    BINDINGS = [Binding("escape", "close_history", "close")]

    def __init__(self, entries) -> None:
        super().__init__()
        self._entries = entries

    def compose(self) -> ComposeResult:
        with Vertical(id="history-box", classes="modal-box"):
            yield Static("[bold $text]History[/]", classes="modal-title")
            yield ListView(id="history-list")
            yield Static("[$text-disabled]esc closes[/]", classes="modal-keys")

    def on_mount(self) -> None:
        lv = self.query_one("#history-list", ListView)
        if not self._entries:
            lv.append(ListItem(Static("[$text-disabled]no messages yet[/]")))
        for when, text in self._entries[-12:]:
            short = text if len(text) < 70 else text[:70] + " …"
            lv.append(ListItem(Static(
                f"[$text-disabled]{_esc(when)}[/] [$text]{_esc(short)}[/]")))
        lv.focus()

    def action_close_history(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Every key and command in one place."""

    BINDINGS = [Binding("escape", "close_help", "close")]

    ROWS = [
        ("enter / ctrl+j", "send / newline"),
        ("ctrl+p", "command palette"),
        ("s  (/settings)", "settings"),
        ("c  (/model)", "provider and model"),
        ("l  (/voice)", "voice input (next round)"),
        ("t  (/theme)", "next theme"),
        ("h  (/history)", "history"),
        ("ctrl+s  (/sessions)", "sessions panel"),
        ("/exit · ctrl+q", "quit"),
        ("@file", "inline a file into the message"),
        ("vaf run --classic", "the classic terminal lane"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box", classes="modal-box"):
            yield Static("[bold $text]Keys & Commands[/]", classes="modal-title")
            for key, desc in self.ROWS:
                yield Static(f"[$text]{key:<24}[/] [$vaf-muted]{desc}[/]",
                             classes="settings-row")
            yield Static("[$text-disabled]esc closes[/]", classes="modal-keys")

    def action_close_help(self) -> None:
        self.dismiss(None)


class PaletteScreen(ModalScreen[str]):
    BINDINGS = [Binding("escape", "dismiss_palette", "close")]

    COMMANDS = [
        ("/settings", "Settings (s)"),
        ("/model", "Provider and model (c)"),
        ("/theme", "Next theme (t)"),
        ("/history", "History (h)"),
        ("/sessions", "Sessions panel (Ctrl+S)"),
        ("/voice", "Voice input (next round)"),
        ("/help", "All keys and commands (?)"),
        ("/exit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-box"):
            yield Input(placeholder="Type a command …", id="palette-input")
            yield ListView(id="palette-list")

    def on_mount(self) -> None:
        self._visible = []
        self._fill("")
        self.query_one("#palette-input", Input).focus()

    def _fill(self, needle: str) -> None:
        lv = self.query_one("#palette-list", ListView)
        lv.clear()
        self._visible = [c for c in self.COMMANDS if needle.lower() in c[0]]
        for cmd, desc in self._visible:
            lv.append(ListItem(Static(f"[bold $text]{cmd}[/]  [$vaf-muted]{desc}[/]")))

    @on(Input.Changed, "#palette-input")
    def _changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    @on(Input.Submitted, "#palette-input")
    def _submitted(self, event: Input.Submitted) -> None:
        if self._visible:
            self.dismiss(self._visible[0][0])

    @on(ListView.Selected)
    def _selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index or 0
        if self._visible:
            self.dismiss(self._visible[idx][0])

    def action_dismiss_palette(self) -> None:
        self.dismiss("")


class SessionsPanel(Vertical):
    """Left dock: the real session list (switching lands next round)."""

    def on_mount(self) -> None:
        self.refresh_sessions([])

    def refresh_sessions(self, sessions, active_id: str = "") -> None:
        for child in list(self.children):
            child.remove()
        self.mount(Static("[bold $text]sessions[/]", classes="panel-title"))
        for entry in sessions[:12]:
            sid = str(entry.get("id", ""))
            name = str(entry.get("name") or sid[:12])
            when = str(entry.get("updated_at") or "")[:16]
            marker = "[$primary]▍[/]" if sid == active_id else "  "
            self.mount(Static(
                f"{marker}[$text]{_esc(name)}[/]\n  [$text-disabled]{_esc(when)}[/]",
                classes="session-row"))
        if not sessions:
            self.mount(Static("[$text-disabled]no sessions[/]", classes="session-row"))
