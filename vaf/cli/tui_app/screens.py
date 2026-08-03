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
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
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


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question that does not block.

    The classic lane asked with `input()`, which owns the terminal - unusable
    here. Same shape as GateScreen: push it with a callback, answer with a key.
    """

    BINDINGS = [
        Binding("y", "answer(True)", "yes"),
        Binding("enter", "answer(True)", "yes", show=False),
        Binding("n", "answer(False)", "no"),
        Binding("escape", "answer(False)", "cancel"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box", classes="modal-box"):
            yield Static("[bold $warning]Confirm[/]", classes="modal-title")
            yield Static(f"[$text]{_esc(self._question)}[/]", classes="modal-body")
            yield Static(
                "[$text][bold]y[/bold][/] [$vaf-muted]yes[/]   "
                "[$text][bold]n[/bold] / [bold]esc[/bold][/] [$vaf-muted]no[/]",
                classes="modal-keys")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(bool(yes))


class ToolsScreen(ModalScreen[None]):
    """Every loaded tool with its description and audience.

    An overlay rather than a transcript dump: 25+ rows is a reference list, not
    a conversation event. The hidden/coder policy comes from the shared
    catalog, never a second copy.
    """

    BINDINGS = [Binding("escape", "close_tools", "close")]

    def __init__(self, rows) -> None:
        super().__init__()
        self._rows = list(rows)

    def compose(self) -> ComposeResult:
        with Vertical(id="tools-box", classes="modal-box"):
            yield Static(f"[bold $text]Tools[/] [$vaf-muted]({len(self._rows)})[/]",
                         classes="modal-title")
            yield ListView(id="tools-list")
            yield Static("[$text-disabled]esc closes[/]", classes="modal-keys")

    def on_mount(self) -> None:
        lv = self.query_one("#tools-list", ListView)
        if not self._rows:
            lv.append(ListItem(Static("[$text-disabled]no tools loaded[/]")))
        for row in self._rows:
            audience = (f"  [$vaf-muted]{_esc(row.audience)}[/]"
                        if row.audience else "")
            lv.append(ListItem(Static(
                f"[$text]{_esc(row.name):<26}[/] "
                f"[$vaf-muted]{_esc(row.description)}[/]{audience}")))
        lv.focus()

    def action_close_tools(self) -> None:
        self.dismiss(None)


class ContextNote(Static):
    """The classic `/context` panel, inline in the transcript.

    Built from the dict `agent.get_context_status()` returns; the classic lane
    formatted the same fields into a Rich panel.
    """

    def __init__(self, status: dict) -> None:
        pct = float(status.get("usage_percent") or 0.0) * 100
        tokens = int(status.get("tokens") or 0)
        total = int(status.get("max_tokens") or 0)
        lines = [
            f"[$vaf-muted]context[/]  [bold $text]{tokens:,}/{total:,}[/] "
            f"[$vaf-muted]tokens ({pct:.0f}%)[/]",
            f"[$vaf-muted]messages[/] {int(status.get('messages') or 0)}"
            f"   [$vaf-muted]files touched[/] {int(status.get('files_touched') or 0)}"
            f"   [$vaf-muted]errors[/] {int(status.get('errors') or 0)}"
            f"   [$vaf-muted]archives[/] {int(status.get('archives_available') or 0)}",
        ]
        goal = status.get("intent_goal")
        if goal:
            lines.append(f"[$vaf-muted]goal[/] [$text]{_esc(goal)}[/]")
        super().__init__("\n".join(lines))
        self.add_class("context-note")


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
        "auto_start_local_server": ("Auto-Start Local Server", ""),
    }

    TITLES = {"main": "Settings", "voice": "Settings › Voice",
              "theme": "Settings › Theme", "stt_lang": "Settings › Input Language",
              "subagent_provider": "Settings › Sub-Agent Provider"}

    # Rows that write ONE config key and are read live at their consumption
    # site: no backend call, no agent rebuild. They were "use vaf settings"
    # rows purely because the flows had not been ported, not because they
    # needed anything the app cannot do.
    #   key -> (label, [(display, value), ...])
    CHOICES = {
        "speech_tts_engine": ("TTS Engine", [
            ("Piper (local)", "piper"),
            ("System (macOS only)", "system"),
            ("Docker (HTTP)", "docker"),
        ]),
        # `speech_language` is not in DEFAULTS; the runtime falls back to
        # "en-US" (vaf/core/speech.py), so the row shows that when unset
        # rather than inventing a value or displaying None.
        "speech_language": ("Input Language", [
            ("English (US)", "en-US"), ("German (DE)", "de-DE"),
            ("Turkish (TR)", "tr-TR"), ("French (FR)", "fr-FR"),
            ("Spanish (ES)", "es-ES"), ("Chinese (CN)", "zh-CN"),
            ("Russian (RU)", "ru-RU"), ("Italian (IT)", "it-IT"),
        ]),
        "subagent_timeout_minutes": ("Timeout duration", [
            ("30 minutes", 30), ("60 minutes", 60), ("120 minutes", 120),
            ("240 minutes", 240), ("no limit", 0),
        ]),
        "ux_auto_open_max_tabs": ("Max auto-opened tabs", [
            ("1", 1), ("4", 4), ("8", 8), ("12", 12), ("20", 20),
        ]),
    }

    def __init__(self) -> None:
        super().__init__()
        self._stack = ["main"]
        self._rows: list = []
        self._row_statics: list = []

    # menu definitions ---------------------------------------------------------------
    def _cfg(self, key, default=None):
        from vaf.core.config import Config
        return Config.get(key, default)

    # Keys the runtime reads as `primary or legacy` - the row must ask the same
    # question the runtime does, or it can show "off" while the feature is on.
    _LEGACY_ALIASES = {"speech_stt_enabled": "stt_enabled"}

    # Keys the running agent holds as a snapshot rather than reading live: a
    # Config.set alone would not reach the object that acts on them.
    _AGENT_SNAPSHOT_KEYS = ("persist_server",)

    def _toggle_state(self, key) -> bool:
        if bool(self._cfg(key, False)):
            return True
        legacy = self._LEGACY_ALIASES.get(key)
        return bool(self._cfg(legacy, False)) if legacy else False

    def _sync_live_agent(self, key, value) -> None:
        if key not in self._AGENT_SNAPSHOT_KEYS:
            return
        try:
            self.app._bridge.agent.config[key] = value
        except Exception:
            pass

    def _menu_rows(self, menu: str) -> list:
        if menu == "main":
            provider = str(self._cfg("provider", "local"))
            api_model = str(self._cfg(f"api_model_{provider}", "not set"))
            rows = [
                ("provider", None,
                 f"AI Provider: [$text]{_esc(provider.upper())}[/]"),
            ]
            if provider != "local":
                rows.append(("api_model", provider,
                             f"API Model: [$text]{_esc(api_model)}[/]"))
            rows += [
                ("sep", None, ""),
                ("later", "context", f"Context Limit [$text]({int(self._cfg('n_ctx', 32768) or 32768):,})[/]"),
                ("later", "local_model", "Select Active Model"),
                ("later", "search_models", "Search & Download New Models"),
                ("toggle", "auto_start_local_server", ""),
                ("sep", None, ""),
                ("toggle", "web_ui_enabled", ""),
                ("submenu", "theme", "Theme"),
                ("toggle", "ux_auto_open_links", ""),
                ("toggle", "ux_auto_open_outputs", ""),
                ("toggle", "sub_agents_in_separate_terminals", ""),
                ("toggle", "subagent_timeout_enabled", ""),
                ("choice", "subagent_timeout_minutes", ""),
                ("choice", "ux_auto_open_max_tabs", ""),
                ("submenu", "subagent_provider", "Sub-Agent Provider"),
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
                ("choice", "speech_tts_engine", ""),
                ("toggle", "speech_stt_enabled", ""),
                ("later", "mic", "Select Microphone"),
                ("submenu", "stt_lang", "Select Input Language"),
                ("later", "wake", "Wake Word"),
                ("back", None, "Back"),
            ]
        if menu.startswith("choice:"):
            return self._choice_rows(menu.split(":", 1)[1])
        if menu == "stt_lang":
            return self._choice_rows("speech_language")
        if menu == "subagent_provider":
            return self._provider_rows("subagent_provider", include_inherit=True)
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

    # What the runtime uses when a key was never written. Only for keys that
    # are absent from Config.DEFAULTS but have a hardcoded fallback in the
    # consuming module - showing None there would be a lie.
    _RUNTIME_FALLBACKS = {"speech_language": "en-US"}

    def _choice_rows(self, key: str) -> list:
        label, options = self.CHOICES[key]
        current = self._cfg(key, self._RUNTIME_FALLBACKS.get(key))
        rows = []
        for display, value in options:
            marker = "▍" if value == current else " "
            rows.append(("pick", (key, value),
                         f"[$primary]{marker}[/][$text]{_esc(display)}[/]"))
        return rows + [("back", None, "Back")]

    def _provider_rows(self, key: str, include_inherit: bool = False) -> list:
        from vaf.core.config import Config
        current = str(self._cfg(key, "inherit" if include_inherit else "local"))
        rows = []
        if include_inherit:
            marker = "▍" if current == "inherit" else " "
            rows.append(("pick", (key, "inherit"),
                         f"[$primary]{marker}[/][$text]Inherit from main agent[/]"))
            rows.append(("sep", None, ""))
        names = ["local"] + sorted(getattr(Config, "PROVIDER_MODELS", {}) or {})
        for name in names:
            marker = "▍" if name == current else " "
            rows.append(("pick", (key, name),
                         f"[$primary]{marker}[/][$text]{_esc(name)}[/]"))
        return rows + [("back", None, "Back")]

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
            on_now = self._toggle_state(arg)
            state = "[$success]on[/]" if on_now else "[$text-disabled]off[/]"
            hint = f"  [$text-disabled]({note})[/]" if note else ""
            return f"[$vaf-muted]{name:<24}[/] {state}{hint}"
        if kind == "submenu":
            return f"[$vaf-muted]{label}[/]  [$text-disabled]›[/]"
        if kind == "back":
            return f"[$text-disabled]‹ {label}[/]"
        if kind == "later":
            return f"[$vaf-muted]{label}[/]  [$text-disabled](vaf settings)[/]"
        if kind == "choice":
            name, options = self.CHOICES[arg]
            current = self._cfg(arg, self._RUNTIME_FALLBACKS.get(arg))
            shown = next((d for d, v in options if v == current), str(current))
            return f"[$vaf-muted]{name:<24}[/] [$text]{_esc(shown)}[/]"
        if kind in ("provider", "api_model"):
            return f"[$vaf-muted]{label}[/]  [$text-disabled]›[/]"
        return f"[$text]{label}[/]"

    def _rebuild(self) -> None:
        menu = self._stack[-1]
        self._rows = self._menu_rows(menu)
        title = self.TITLES.get(menu)
        if title is None and menu.startswith("choice:"):
            title = f"Settings › {self.CHOICES[menu.split(':', 1)[1]][0]}"
        self.query_one("#settings-title", Static).update(
            f"[bold $text]{title or 'Settings'}[/]")
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
            new_val = not self._toggle_state(arg)
            Config.set(arg, new_val)
            self._sync_live_agent(arg, new_val)
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
            # The app's key, not only Textual's: get_css_variables() resolves the
            # vaf-* variables from it, and the `t` cycle counts from it.
            self.app._theme_key = key
            self.app.theme = f"vaf-{key}"
            self.app.notify(f"theme: {key}", timeout=1.5)
            self._refresh_labels()
            return
        if kind == "choice":
            self._stack.append(f"choice:{arg}")
            self._rebuild()
            return
        if kind == "pick":
            from vaf.core.config import Config
            key, value = arg
            Config.set(key, value)
            self._sync_live_agent(key, value)
            self.app.notify(f"{key}: {value}", timeout=1.5)
            self._stack.pop()
            self._rebuild()
            self.app.post_message(SettingsChanged())
            return
        if kind in ("provider", "api_model"):
            self.dismiss(None)
            self.app.action_model()
            return
        if kind == "later":
            self.app.notify("This flow needs a full restart - use `vaf settings` "
                            "in a normal terminal.", timeout=2.5)

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


class ModelScreen(ModalScreen[tuple]):
    """Pick the provider, or the model within the current provider.

    Dismisses with `(provider, model)`; the app hands that to the bridge, which
    applies it to the RUNNING agent through the engine's own reload. What it
    does NOT offer is the local GGUF: swapping that needs a real rebuild, and
    a running llama server would keep serving the old weights - a named
    boundary, and the row says so instead of pretending.
    """

    BINDINGS = [
        Binding("escape", "close_model", "close"),
        Binding("m", "show_models", "models of this provider", show=False),
    ]

    def __init__(self, provider_only: bool = False) -> None:
        super().__init__()
        self._provider_only = provider_only
        self._rows: list = []

    def compose(self) -> ComposeResult:
        with Vertical(id="model-box", classes="modal-box"):
            yield Static("[bold $text]Provider · Model[/]", classes="modal-title")
            yield ListView(id="model-list")
            yield Static("[$text-disabled]enter selects · esc closes[/]",
                         classes="modal-keys")

    def on_mount(self) -> None:
        from vaf.core.config import Config

        lv = self.query_one("#model-list", ListView)
        current = str(Config.get("provider", "local"))
        catalog = getattr(Config, "PROVIDER_MODELS", {}) or {}

        marker = "▍" if current == "local" else " "
        active_index = 0 if current == "local" else -1
        self._rows.append(("provider", "local"))
        lv.append(ListItem(Static(
            f"[$primary]{marker}[/][$vaf-muted]{'local':<12}[/] "
            f"[$text]{_esc(str(Config.get('model', '') or 'not set'))}[/]"
            f"  [$text-disabled](model chosen in `vaf settings`)[/]")))

        for i, provider in enumerate(sorted(catalog), start=1):
            configured = str(Config.get(f"api_model_{provider}", "") or "")
            entry = catalog.get(provider) or {}
            fallback = entry.get("default", "") if isinstance(entry, dict) else ""
            shown = configured or fallback
            marker = "▍" if provider == current else " "
            if provider == current:
                active_index = i
            self._rows.append(("provider", provider))
            lv.append(ListItem(Static(
                f"[$primary]{marker}[/][$vaf-muted]{_esc(provider):<12}[/] "
                f"[$text]{_esc(shown)}[/]")))

        if current != "local" and not self._provider_only:
            models = self._models_for(current)
            if models:
                self._rows.append(("sep", None))
                lv.append(ListItem(Static("[$vaf-border]─────────────────[/]")))
                configured = str(Config.get(f"api_model_{current}", "") or "")
                for model in models:
                    marker = "▍" if model == configured else " "
                    self._rows.append(("model", model))
                    lv.append(ListItem(Static(
                        f"[$primary]{marker}[/][$vaf-muted]{_esc(current)}:[/] "
                        f"[$text]{_esc(model)}[/]")))

        lv.index = max(active_index, 0)
        lv.focus()

    @staticmethod
    def _models_for(provider: str) -> list:
        """The catalog's models for one provider, in the shape it really has:
        a `default` plus a `fallback` list (verified against Config, not
        assumed). Deduplicated, default first."""
        from vaf.core.config import Config
        entry = (getattr(Config, "PROVIDER_MODELS", {}) or {}).get(provider)
        if isinstance(entry, (list, tuple)):
            names = [str(m) for m in entry]
        elif isinstance(entry, dict):
            names = ([str(entry["default"])] if entry.get("default") else [])
            names += [str(m) for m in (entry.get("fallback") or [])]
        else:
            return []
        seen, out = set(), []
        for name in names:
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    @on(ListView.Selected, "#model-list")
    def _picked(self, event: ListView.Selected) -> None:
        idx = event.list_view.index or 0
        if not (0 <= idx < len(self._rows)):
            return
        kind, value = self._rows[idx]
        if kind == "sep":
            return
        if kind == "provider":
            self.dismiss((value, ""))
        else:
            from vaf.core.config import Config
            self.dismiss((str(Config.get("provider", "local")), value))

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

    # Keys are the app's own; the command half is DERIVED, so the help can
    # never list a word the dispatcher does not have (or omit one it does).
    KEY_ROWS = [
        ("enter / ctrl+j", "send / newline"),
        ("ctrl+p", "command palette"),
        ("ctrl+s", "sessions panel"),
        ("ctrl+q", "quit"),
        ("@file", "inline a file into the message"),
        ("vaf run --classic", "the classic terminal lane"),
    ]

    @staticmethod
    def command_rows():
        from vaf.cli.commands import COMMANDS
        rows = []
        for cmd in COMMANDS:
            keys = cmd.label
            if cmd.aliases:
                keys += "  (" + ", ".join(cmd.aliases) + ")"
            rows.append((keys, cmd.help))
        return rows

    @staticmethod
    def _two_col(key: str, desc: str) -> ComposeResult:
        """Two widgets, not one padded string.

        A single Static wraps at the box edge and continues at column ZERO, so
        a long description reappeared underneath the key column and read like
        another command. Giving the description its own column makes the wrap
        land under itself.
        """
        with Horizontal(classes="help-row"):
            yield Static(f"[$text]{key}[/]", classes="help-key")
            yield Static(f"[$vaf-muted]{desc}[/]", classes="help-desc")

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box", classes="modal-box"):
            yield Static("[bold $text]Keys[/]", classes="modal-title")
            for key, desc in self.KEY_ROWS:
                yield from self._two_col(key, desc)
            yield Static("[bold $text]Commands[/]", classes="modal-title-mid")
            with VerticalScroll(id="help-commands"):
                for key, desc in self.command_rows():
                    yield from self._two_col(key, desc)
            yield Static("[$text-disabled]esc closes[/]", classes="modal-keys")

    def action_close_help(self) -> None:
        self.dismiss(None)


class PaletteScreen(ModalScreen[str]):
    BINDINGS = [Binding("escape", "dismiss_palette", "close")]

    @staticmethod
    def entries():
        """Derived from the registry - a palette entry that does not route is
        exactly the drift this round removed."""
        from vaf.cli.commands import COMMANDS
        return [(f"/{c.word}", c.help + (f"  ({', '.join(c.aliases)})"
                                         if c.aliases else ""))
                for c in COMMANDS if c.palette]

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
        self._visible = [c for c in self.entries() if needle.lower() in c[0]]
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
    """Left dock: the session list, walkable and selectable.

    A ListView rather than stacked Statics, because the panel has to be
    keyboard-complete: arrows walk it, enter loads. It posts `Selected` and
    lets the app do the loading - the panel knows nothing about the agent.
    """

    BINDINGS = [Binding("escape", "leave_panel", "back", show=False)]

    class Selected(events.Message):
        def __init__(self, session_id: str, name: str) -> None:
            super().__init__()
            self.session_id = session_id
            self.name = name

    def compose(self) -> ComposeResult:
        yield Static("[bold $text]sessions[/]", classes="panel-title")
        yield ListView(id="session-list")
        yield Static("[$text-disabled]enter loads · esc back[/]", classes="modal-keys")

    def on_mount(self) -> None:
        self._entries: list = []

    def refresh_sessions(self, sessions, active_id: str = "") -> None:
        if not self.is_attached:
            return                      # not on the screen yet: nothing to fill
        self._entries = [e for e in (sessions or [])][:20]
        try:
            lv = self.query_one("#session-list", ListView)
        except Exception:
            return
        lv.clear()
        if not self._entries:
            lv.append(ListItem(Static("[$text-disabled]no sessions[/]")))
            return
        active_index = 0
        for i, entry in enumerate(self._entries):
            sid = str(entry.get("id", ""))
            name = str(entry.get("name") or sid[:12])
            when = str(entry.get("updated_at") or "")[:16]
            count = entry.get("message_count")
            marker = "[$primary]▍[/]" if sid == active_id else " "
            if sid == active_id:
                active_index = i
            meta = f"{when}" + (f" · {count} msg" if count else "")
            lv.append(ListItem(Static(
                f"{marker}[$text]{_esc(name)}[/]\n  [$text-disabled]{_esc(meta)}[/]")))
        lv.index = active_index

    def focus_list(self) -> None:
        try:
            self.query_one("#session-list", ListView).focus()
        except Exception:
            pass

    @on(ListView.Selected, "#session-list")
    def _picked(self, event: ListView.Selected) -> None:
        idx = event.list_view.index or 0
        if 0 <= idx < len(self._entries):
            entry = self._entries[idx]
            self.post_message(self.Selected(str(entry.get("id", "")),
                                            str(entry.get("name") or "")))

    def action_leave_panel(self) -> None:
        self.remove_class("visible")
        try:
            self.screen.query_one("#promptbox").focus()
        except Exception:
            pass
