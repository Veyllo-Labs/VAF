# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""All overlays of the terminal app. Every one is keyboard-complete: arrows walk,
enter/space activates, esc goes back - the mouse is a second way in, never the
only one.

The settings screen is the `vaf settings` main menu (cli/cmd/settings.py) as a
stacked arrow menu. Boolean rows write their real config keys immediately - the
same single `Config.set` the inquirer menu performs. The provider and API model
open the model overlay, which applies them to the RUNNING agent and asks for an
API key when one is missing. The context limit and the numeric choices take a
free value through NumberScreen, the microphone submenu enumerates real devices
(once per entry, behind an fd-2 guard), and About renders the shared facts.
Rows that still need a genuinely new agent (the local GGUF, the model download)
display live values but route to `vaf settings`; each says so instead of
pretending.
"""
import os
import re
from contextlib import contextmanager

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

    def __init__(self, tool: str, reason: str, preview: str = "", notes: str = "") -> None:
        super().__init__()
        self._tool = tool
        self._reason = reason
        self._preview = preview
        self._notes = notes

    def compose(self) -> ComposeResult:
        with Vertical(id="gate-box", classes="modal-box"):
            yield Static("[bold $warning]⚠ confirmation required[/]", classes="modal-title")
            yield Static(
                f"[$text]The tool [bold]{_esc(self._tool)}[/bold] wants to run.[/]\n"
                f"[$vaf-muted]{_esc(self._reason)}[/]", classes="modal-body")
            if self._preview:
                yield Static(f"[$text]{_esc(self._preview)}[/]", classes="modal-body")
            if self._notes:
                yield Static(f"[$vaf-muted]({_esc(self._notes)})[/]", classes="modal-body")
            yield Static(
                "[$text][bold]y[/bold][/] [$vaf-muted]allow once[/]   "
                "[$text][bold]a[/bold][/] [$vaf-muted]always in this directory[/]   "
                "[$text][bold]esc[/bold][/] [$vaf-muted]cancel[/]", classes="modal-keys")

    def action_answer(self, result: str) -> None:
        self.dismiss(result)


class VoiceScreen(ModalScreen[str]):
    """The classic listen_overlay, app-mode - fed by REAL capture state.

    The meter renders what the microphone hears: `set_state` receives the
    (phase, energy, threshold) ticks the framework's capture callback emits,
    so the bar is the actual RMS level on the same logarithmic scale the
    painted classic meter used - not an animation pretending to listen.
    Escape cancels the CAPTURE (cooperative, via the app), not just the view:
    an overlay that closed while the microphone kept recording would send a
    message nobody saw being taken.
    """

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="voice-box", classes="modal-box"):
            yield Static("[bold $vaf-muted]● Calibrating …[/]", id="voice-title",
                         classes="modal-title")
            yield Static("", id="voice-bars")
            yield Static("[$vaf-muted]Speak now … (Silence to finish)[/]", id="voice-hint",
                         classes="modal-body")
            yield Static("[$text-disabled]esc[/] [$vaf-muted]cancel[/]", classes="modal-keys")

    def set_state(self, phase: str, energy: float = 0.0, threshold: float = 0.0) -> None:
        import math
        titles = {
            "calibrating": "[bold $vaf-muted]● Calibrating …[/]",
            "recording": "[bold $error]● Recording[/]",
            "speaking": "[bold $error]● SPEAKING[/]",
            "processing": "[bold $vaf-muted]… Processing[/]",
            "timeout": "[$warning]✗ No speech detected[/]",
        }
        try:
            self.query_one("#voice-title", Static).update(
                titles.get(phase, titles["recording"]))
            bar_len = min(int(math.log(float(energy) + 1) * 2), 24)
            colour = "$error" if phase == "speaking" else "$vaf-muted"
            self.query_one("#voice-bars", Static).update(
                f"[{colour}]{'█' * bar_len}[/][$vaf-border]{'░' * (24 - bar_len)}[/]")
        except Exception:
            pass                        # a late tick after dismiss is noise

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
        "ux_voice_review": ("Voice: review before send",
                            "off = transcript sends immediately"),
        "persist_server": ("Server Persistence", ""),
        "auto_start_local_server": ("Auto-Start Local Server", ""),
    }

    TITLES = {"main": "Settings", "voice": "Settings › Voice",
              "theme": "Settings › Theme", "stt_lang": "Settings › Input Language",
              "subagent_provider": "Settings › Sub-Agent Provider",
              "mic": "Settings › Microphone"}

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
        # n_ctx is a llama-server LAUNCH argument and a build-time snapshot at
        # its dominant consumers - the write lands, the note below says when.
        "n_ctx": ("Context Limit", [
            ("32768 (minimum / balanced)", 32768),
            ("65536 (large)", 65536),
            ("131072 (max)", 131072),
        ]),
    }

    # Choice keys that also take a free value; (minimum, maximum, hint) with
    # None for an unbounded side. The submenu grows a "Custom..." row.
    NUMBERS = {
        "n_ctx": (32768, None, "tokens"),
        "subagent_timeout_minutes": (0, 480, "minutes, 0 turns the timeout off"),
        "ux_auto_open_max_tabs": (1, 20, "tabs"),
    }

    # Appended to the write notify when the value does NOT reach the running
    # process - honesty about WHEN a stored choice applies.
    PICK_NOTES = {"n_ctx": "applies at the next start"}

    def __init__(self) -> None:
        super().__init__()
        self._stack = ["main"]
        self._rows: list = []
        self._row_statics: list = []
        # Microphone submenu cache: None = not loaded, list = devices,
        # str = the honest reason there are none. Filled ONCE on entering the
        # submenu; _menu_rows must never enumerate itself (see _load_mics).
        self._mic_devices = None
        # Automations submenu cache, same contract: loaded on entry (disk
        # reads), invalidated on back and after every toggle.
        self._automations = None

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
                ("choice", "n_ctx", ""),
                ("submenu", "local_model", "Select Active Model"),
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
                ("submenu", "voice", "TTS / STT Settings"),
                ("submenu", "automations", "Automations"),
                ("sep", None, ""),
                ("tools", None, "Show All Tools"),
                ("toggle", "persist_server", ""),
                ("about", None, "About VAF"),
                ("back", None, "Exit Settings"),
            ]
            return rows
        if menu == "voice":
            return [
                ("toggle", "speech_tts_enabled", ""),
                ("choice", "speech_tts_engine", ""),
                ("toggle", "speech_stt_enabled", ""),
                ("toggle", "ux_voice_review", ""),
                ("submenu", "mic", "Select Microphone"),
                ("submenu", "stt_lang", "Select Input Language"),
                ("back", None, "Back"),
            ]
        if menu.startswith("choice:"):
            return self._choice_rows(menu.split(":", 1)[1])
        if menu == "local_model":
            # Rendered straight from disk, no cache: a directory listing is
            # cheap (unlike the microphone enumeration) and the active marker
            # must follow the config the moment a pick lands.
            try:
                files, current_name = self.app._bridge.list_local_models()
            except Exception as exc:
                return [("note", None, f"models unavailable: {exc}"),
                        ("back", None, "Back")]
            if not files:
                return [("note", None, "no models in the models/ directory"),
                        ("back", None, "Back")]
            rows = []
            for f in files:
                active = f == current_name or (current_name and current_name in f)
                marker = "▍" if active else " "
                rows.append(("local_model", f,
                             f"[$primary]{marker}[/][$text]{_esc(f)}[/]"))
            return rows + [("back", None, "Back")]
        if menu == "automations":
            state = self._automations
            rows = []
            if isinstance(state, str):
                rows.append(("note", None, state))
            elif not state:
                rows.append(("note", None,
                             "no automations yet - ask the agent, or: vaf automation create"))
            else:
                for tid, name, enabled, schedule, nxt in state:
                    mark = "[$primary]●[/]" if enabled else "[dim]○[/]"
                    rows.append(("automation", tid,
                                 f"{mark} [$text]{_esc(name)}[/] "
                                 f"[dim]{_esc(schedule)} · next {_esc(nxt)}[/dim]"))
            rows.append(("automation_folder", None, "Open Automations Folder"))
            return rows + [("back", None, "Back")]
        if menu == "mic":
            current = self._cfg("speech_mic_index", None)
            state = self._mic_devices
            rows = []
            if isinstance(state, str):
                rows.append(("note", None, state))
            elif not state:
                rows.append(("note", None, "no microphones detected"))
            else:
                for idx, text in state:
                    marker = "▍" if current == idx else " "
                    rows.append(("mic", idx,
                                 f"[$primary]{marker}[/][$text]{_esc(text)}[/]"))
            return rows + [("back", None, "Back")]
        if menu == "stt_lang":
            return self._choice_rows("speech_language")
        if menu == "subagent_provider":
            return self._subagent_provider_rows()
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
        # The timeout's "no limit" is not a minutes value, it is the ENABLED
        # key switched off (see _write_choice) - so the marker follows the
        # switch, not the stored minutes, or it would name a limit that is
        # not in force.
        timeout_off = (key == "subagent_timeout_minutes"
                       and not self._toggle_state("subagent_timeout_enabled"))
        rows = []
        for display, value in options:
            if key == "subagent_timeout_minutes":
                marked = ((value == 0 and timeout_off)
                          or (value != 0 and not timeout_off and value == current))
            else:
                marked = value == current
            rows.append(("pick", (key, value),
                         f"[$primary]{'▍' if marked else ' '}[/]"
                         f"[$text]{_esc(display)}[/]"))
        if key in self.NUMBERS:
            rows.append(("number", key, "Custom..."))
        return rows + [("back", None, "Back")]

    def _subagent_provider_rows(self) -> list:
        """The sub-agent provider menu, marked by what sub-agents ACTUALLY run on.

        The marker follows `subagent_provider_override()`, not the stored name.
        Those two disagree whenever the gate key is off, and this row used to
        read the name: it marked a provider no sub-agent was running on, on a
        screen whose only job is to say which one is in force.

        Its own row kind, not the generic `pick`: the choice is a PAIR of config
        keys and writing the name alone is inert (see `set_subagent_provider`).
        A generic writer here is exactly how that happened.
        """
        from vaf.core.config import Config, subagent_provider_override
        current = subagent_provider_override() or "inherit"
        rows = [("subagent_provider", "inherit",
                 f"[$primary]{'▍' if current == 'inherit' else ' '}[/]"
                 f"[$text]Inherit from main agent[/]"),
                ("sep", None, "")]
        for name in ["local"] + sorted(getattr(Config, "PROVIDER_MODELS", {}) or {}):
            marker = "▍" if name == current else " "
            rows.append(("subagent_provider", name,
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
        if kind == "note":
            return f"[$text-disabled]{_esc(label)}[/]"
        if kind == "choice":
            name, options = self.CHOICES[arg]
            current = self._cfg(arg, self._RUNTIME_FALLBACKS.get(arg))
            shown = next((d for d, v in options if v == current), str(current))
            if (arg == "subagent_timeout_minutes"
                    and not self._toggle_state("subagent_timeout_enabled")):
                # The stored minutes are dormant while the switch is off -
                # showing them would name a limit that is not in force.
                shown = "no limit"
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
            if arg == "mic":
                self._load_mics()
            elif arg == "automations":
                self._load_automations()
            self._stack.append(arg)
            self._rebuild()
            return
        if kind == "tools":
            # On TOP of this modal, like About - the settings stack survives.
            self.app.action_tools()
            return
        if kind == "automation":
            # The classic menu's primary action: one activation flips enabled.
            try:
                from vaf.core.automation import AutomationManager
                mgr = AutomationManager()
                task = mgr.get(arg)
                if task is None:
                    self.app.notify("automation not found - list refreshed",
                                    severity="warning", timeout=2.5)
                else:
                    mgr.update(task.id, enabled=not task.enabled)
                    self.app.notify(
                        f"{task.name}: {'disabled' if task.enabled else 'enabled'}",
                        timeout=1.5)
            except Exception as exc:
                self.app.notify(f"automation toggle failed: {exc}",
                                severity="warning", timeout=3.0)
                return
            self._load_automations()
            self._rebuild()
            return
        if kind == "automation_folder":
            import subprocess
            import sys
            try:
                from vaf.core.automation import AutomationManager
                folder = str(AutomationManager().storage_dir)
                opener = ("explorer" if sys.platform == "win32"
                          else "open" if sys.platform == "darwin" else "xdg-open")
                # Detached and silenced: a chatty opener must not write into
                # the alternate screen.
                subprocess.Popen([opener, folder],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                self.app.notify(f"opened: {folder}", timeout=2.0)
            except Exception as exc:
                self.app.notify(f"could not open the folder: {exc}",
                                severity="warning", timeout=3.0)
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
            self._write_choice(*arg)
            return
        if kind == "number":
            minimum, maximum, hint = self.NUMBERS[arg]
            current = self._cfg(arg, self._RUNTIME_FALLBACKS.get(arg))
            name = self.CHOICES[arg][0]

            def _entered(value) -> None:
                if value is None:          # cancelled: change nothing at all
                    return
                self._write_choice(arg, value)

            # Pushed ON TOP of this modal - never the dismiss-then-push of the
            # provider rows below, which would throw the settings stack away.
            self.app.push_screen(
                NumberScreen(name, current, minimum=minimum, maximum=maximum,
                             hint=hint), _entered)
            return
        if kind == "local_model":
            # The swap itself runs on the bridge's lane (the new weights have
            # to load, which blocks for a while) - the row only hands the file
            # over and points at the transcript, where the progress notes land.
            self.app._bridge.apply_local_model(str(arg))
            self.app.notify(f"switching to {arg} - see the chat for progress",
                            timeout=2.5)
            self._refresh_labels()
            self.app.post_message(SettingsChanged())
            return
        if kind == "mic":
            try:
                from vaf.core.speech import get_speech_manager
                with self._quiet_fd2():
                    # Persists speech_mic_index AND re-inits the live mic.
                    get_speech_manager().set_microphone(int(arg))
            except Exception as exc:
                self.app.notify(f"microphone change failed: {exc}",
                                severity="warning", timeout=3.0)
                return
            self.app.notify(f"microphone: {arg}", timeout=1.5)
            self._refresh_labels()
            self.app.post_message(SettingsChanged())
            return
        if kind == "subagent_provider":
            from vaf.core.config import Config, set_subagent_provider
            if arg not in ("inherit", "local") and not Config.get_api_key(arg):
                # What `vaf settings` does: refuse rather than store a provider
                # nothing can reach. Every sub-agent would spawn onto a backend
                # that cannot build, one process away from any error message.
                self.app.notify(f"no API key for {arg} - set one first",
                                severity="warning", timeout=3.0)
                return
            set_subagent_provider(arg)
            self.app.notify(f"sub-agents: {arg}", timeout=1.5)
            self._stack.pop()
            self._rebuild()
            self.app.post_message(SettingsChanged())
            return
        if kind == "about":
            self.app.push_screen(AboutScreen())
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
            left = self._stack.pop()
            if left == "mic":
                self._mic_devices = None      # re-enumerate on next entry
            elif left == "automations":
                self._automations = None      # re-read on next entry
            self._rebuild()
        else:
            self.dismiss(None)

    # one chosen value for one choice key ---------------------------------------------
    def _write_choice(self, key, value) -> None:
        """Write, sync, say, return - shared by the preset rows and the Custom
        number field so the two paths cannot drift.

        `subagent_timeout_minutes` is special and DANGEROUS to write literally:
        the cleanup pass computes `cutoff = now - minutes*60`, so an ENABLED
        timeout of zero minutes times out every running sub-agent on its next
        sweep (vaf/core/subagent_ipc.py). The classic menu therefore never
        stored 0 - it switched `subagent_timeout_enabled` off and left the
        minutes alone, and a real number switches it back on. Same here; the
        preset "no limit" row used to store the 0 and carried exactly that
        defect.
        """
        from vaf.core.config import Config
        if key == "subagent_timeout_minutes":
            if int(value or 0) <= 0:
                Config.set("subagent_timeout_enabled", False)
                self.app.notify("sub-agent timeout: off", timeout=1.5)
            else:
                minutes = max(1, min(int(value), 480))
                Config.set(key, minutes)
                Config.set("subagent_timeout_enabled", True)
                self.app.notify(f"sub-agent timeout: {minutes} minutes",
                                timeout=1.5)
        else:
            Config.set(key, value)
            self._sync_live_agent(key, value)
            note = self.PICK_NOTES.get(key)
            self.app.notify(f"{key}: {value}" + (f" ({note})" if note else ""),
                            timeout=2.5 if note else 1.5)
        self._stack.pop()
        self._rebuild()
        self.app.post_message(SettingsChanged())

    # microphone helpers --------------------------------------------------------------
    def _load_mics(self) -> None:
        """Enumerate input devices ONCE per submenu entry.

        The rows must render from `self._mic_devices`: `_rebuild` and
        `_refresh_labels` re-ask `_menu_rows` on every pick and stack move,
        and each enumeration constructs a PyAudio instance - a real
        audio-stack touch (and an fd-2 writer, see `_quiet_fd2`).

        The index is PARSED from the "i: name" prefix rather than taken from
        the position: the PyAudio path pre-formats and FILTERS the list, so
        the position lies about the device index. The fallback path
        (sr.Microphone names) has no prefix, and there position IS the index.
        """
        try:
            from vaf.core.speech import get_speech_manager
            with self._quiet_fd2():
                names = get_speech_manager().list_microphones() or []
        except Exception as exc:
            self._mic_devices = f"microphones unavailable: {exc}"
            return
        devices = []
        for pos, raw in enumerate(names):
            text = str(raw)
            m = re.match(r"^(\d+):\s*", text)
            devices.append((int(m.group(1)) if m else pos, text))
        self._mic_devices = devices

    def _load_automations(self) -> None:
        """Read the automations ONCE per submenu entry (disk), same contract
        as `_load_mics`: the rows render only from `self._automations`. The
        tuple carries exactly what the classic table showed - name, enabled,
        `frequency @ time`, next run trimmed to the minute."""
        try:
            from vaf.core.automation import AutomationManager
            mgr = AutomationManager()
            self._automations = [
                (t.id, t.name, bool(t.enabled),
                 f"{t.frequency} @ {t.time}",
                 (t.next_run or "-")[:16])
                for t in mgr.list()
            ]
        except Exception as exc:
            self._automations = f"automations unavailable: {exc}"

    @contextmanager
    def _quiet_fd2(self):
        """C-level stderr to devnull for the duration.

        ALSA writes its configuration warnings from C, past every
        Python-level redirect, and under the alternate screen they shred the
        display (the app's boot order exists for the same reason). Fail-open:
        no fd games when dup fails.
        """
        try:
            saved = os.dup(2)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 2)
        except Exception:
            yield
            return
        try:
            yield
        finally:
            try:
                os.dup2(saved, 2)
                os.close(saved)
                os.close(devnull)
            except Exception:
                pass


class SettingsChanged(events.Message):
    """Posted after a settings toggle so the app can refresh dependent chrome."""


class ApiKeyScreen(ModalScreen[str]):
    """Enter or replace one provider's API key, without it ever being seen.

    Dismisses with the typed key, with "" for "keep whatever is stored", or with
    None when cancelled - three answers, because "empty" and "cancelled" mean
    opposite things to the caller and one of them must not switch the provider.

    The value NEVER leaves this screen except through that dismiss. No notify,
    no event note, no transcript line: the classic lane deliberately reported
    only a character count, and an overlay that echoed the key into the
    transcript would write it into the session file as well.
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
    ]

    # The classic lane's floor. Every provider key in the catalog is far longer
    # than this; the point is to catch a stray paste, not to validate a format
    # that differs per provider and changes without notice.
    MIN_LEN = 10

    def __init__(self, provider: str, current: str = "") -> None:
        super().__init__()
        self._provider = str(provider or "")
        self._current = str(current or "")

    def compose(self) -> ComposeResult:
        from vaf.core.config import Config

        with Vertical(id="apikey-box", classes="modal-box"):
            yield Static(f"[bold $text]API key · {_esc(self._provider)}[/]",
                         classes="modal-title")
            if self._current:
                masked = Config.mask_api_key(self._current)
                yield Static(f"[$vaf-muted]stored:[/] [$text]{_esc(masked)}[/]",
                             classes="modal-body")
                hint = "enter an new key, or leave empty to keep the stored one"
            else:
                yield Static("[$vaf-muted]no key stored for this provider[/]",
                             classes="modal-body")
                hint = "the provider cannot be used until one is set"
            yield Input(password=True, id="apikey-input",
                        placeholder="paste the key, then press enter")
            yield Static(f"[$text-disabled]{hint} · esc cancels[/]",
                         id="apikey-hint", classes="modal-keys")

    def on_mount(self) -> None:
        self.query_one("#apikey-input", Input).focus()

    @on(Input.Submitted, "#apikey-input")
    def _submitted(self, event: Input.Submitted) -> None:
        value = str(event.value or "").strip()
        if not value:
            # Empty is an answer, not a mistake: keep what is stored. The caller
            # decides whether that is enough to switch.
            self.dismiss("")
            return
        if len(value) < self.MIN_LEN:
            self.query_one("#apikey-hint", Static).update(
                f"[$error]that is only {len(value)} characters - "
                f"check the paste[/]")
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RenameScreen(ModalScreen["str | None"]):
    """One text field: a session's new name.

    Dismisses with the cleaned name, or None for cancel - and empty input IS
    cancel, because a session with an empty name would fall back to its id on
    every surface, which is a worse rename than none.
    """

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, current: str = "") -> None:
        super().__init__()
        self._current = str(current or "")

    def compose(self) -> ComposeResult:
        with Vertical(id="rename-box", classes="modal-box"):
            yield Static("[bold $text]Rename session[/]", classes="modal-title")
            yield Input(value=self._current, placeholder="session name",
                        id="rename-input")
            yield Static("[$text-disabled]enter saves · esc cancels[/]",
                         classes="modal-keys")

    def on_mount(self) -> None:
        self.query_one("#rename-input", Input).focus()

    @on(Input.Submitted, "#rename-input")
    def _submitted(self, event: Input.Submitted) -> None:
        value = " ".join(str(event.value or "").split())
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NumberScreen(ModalScreen["int | None"]):
    """Type one bounded number, or leave without changing anything.

    Dismisses with an int - never a str, because the settings rows compare the
    stored value against option values with `==`, and a str would silently
    unmark every row while looking stored. Escape and an empty submit both
    dismiss None ("change nothing"); unlike the key field there is no third
    meaning to carry. Out-of-range input updates the hint in place and keeps
    the screen open: a mistyped number must be rejected where it was typed,
    not stored and then surprise somewhere far away.
    """

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title: str, current, minimum: "int | None" = None,
                 maximum: "int | None" = None, hint: str = "") -> None:
        super().__init__()
        self._title = str(title)
        self._current = current
        self._min = minimum
        self._max = maximum
        self._hint = str(hint or "")

    def _bounds_text(self) -> str:
        if self._min is not None and self._max is not None:
            return f"{self._min}..{self._max}"
        if self._min is not None:
            return f">= {self._min}"
        if self._max is not None:
            return f"<= {self._max}"
        return ""

    def compose(self) -> ComposeResult:
        with Vertical(id="number-box", classes="modal-box"):
            yield Static(f"[bold $text]{_esc(self._title)}[/]",
                         classes="modal-title")
            yield Static(f"[$vaf-muted]current:[/] [$text]{_esc(self._current)}[/]",
                         classes="modal-body")
            yield Input(id="number-input",
                        placeholder=self._bounds_text() or "number")
            parts = " · ".join(p for p in (self._hint, self._bounds_text()) if p)
            yield Static(f"[$text-disabled]{_esc(parts)} · esc cancels[/]",
                         id="number-hint", classes="modal-keys")

    def on_mount(self) -> None:
        self.query_one("#number-input", Input).focus()

    @on(Input.Submitted, "#number-input")
    def _submitted(self, event: Input.Submitted) -> None:
        raw = str(event.value or "").strip()
        if not raw:
            self.dismiss(None)
            return
        try:
            value = int(raw)
        except ValueError:
            self.query_one("#number-hint", Static).update(
                f"[$error]{_esc(raw)} is not a whole number[/]")
            return
        if ((self._min is not None and value < self._min)
                or (self._max is not None and value > self._max)):
            self.query_one("#number-hint", Static).update(
                f"[$error]out of range ({self._bounds_text()})[/]")
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ModelScreen(ModalScreen[tuple]):
    """Pick the provider, or the model within the current provider.

    Dismisses with `(provider, model, want_key)`; the app hands that to the
    bridge, which applies it to the RUNNING agent through the engine's own
    reload. `want_key` is how a user REPLACES a key that is stored but wrong or
    expired - without it the only route to a key is picking a provider that has
    none, which a user with a bad key can never reach. What it
    does NOT offer is the local GGUF: swapping that needs a real rebuild, and
    a running llama server would keep serving the old weights - a named
    boundary, and the row says so instead of pretending.
    """

    BINDINGS = [
        Binding("escape", "close_model", "close"),
        Binding("m", "show_models", "models of this provider", show=False),
        Binding("k", "set_key", "set this provider's API key", show=False),
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
            self.dismiss((value, "", False))
        else:
            from vaf.core.config import Config
            self.dismiss((str(Config.get("provider", "local")), value, False))

    def action_set_key(self) -> None:
        """`k` on a provider row: go and set that provider's key.

        Only on a provider row, and never on `local` - it has nothing to key,
        and offering the field there would suggest otherwise.
        """
        lv = self.query_one("#model-list", ListView)
        idx = lv.index or 0
        if not (0 <= idx < len(self._rows)):
            return
        kind, value = self._rows[idx]
        if kind != "provider" or value == "local":
            return
        self.dismiss((value, "", True))

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


class AboutScreen(ModalScreen[None]):
    """Version, copyright, licence, links - the `vaf about` panel as an overlay.

    Every word comes from `info.about_facts()`; this screen only styles them.
    Three renderers used to carry their own copy of the legal lines and two
    had drifted - the facts live once now.
    """

    BINDINGS = [Binding("escape", "close_about", "close")]

    def compose(self) -> ComposeResult:
        # Lazy on purpose: info.py imports typer and the GPU detection, which
        # have no business on this module's import graph.
        from vaf.cli.cmd.info import about_facts
        facts = about_facts()

        with Vertical(id="about-box", classes="modal-box"):
            yield Static(f"[bold $text]{_esc(facts['name'])}[/]",
                         classes="modal-title")
            yield Static(
                f"[$vaf-muted]Version[/] [$text]{_esc(facts['version'])}[/]\n"
                f"[$vaf-muted]Copyright[/] [$text]{_esc(facts['copyright'])}[/]\n"
                f"[$vaf-muted]Credits[/] [$text]{_esc(facts['credits'])}[/]",
                classes="modal-body")
            yield Static(
                "\n".join(f"[$text]{_esc(line)}[/]" for line in facts["license"]),
                classes="modal-body")
            yield Static(
                "\n".join(f"[$vaf-muted]{_esc(label)}[/] [$text]{_esc(url)}[/]"
                          for label, url in facts["links"]),
                classes="modal-body")
            yield Static("[$text-disabled]esc closes[/]", classes="modal-keys")

    def action_close_about(self) -> None:
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

    BINDINGS = [
        Binding("escape", "leave_panel", "back", show=False),
        Binding("n", "new_session", "new", show=False),
        Binding("r", "rename_session", "rename", show=False),
        Binding("d", "delete_session", "delete", show=False),
    ]

    class Selected(events.Message):
        def __init__(self, session_id: str, name: str) -> None:
            super().__init__()
            self.session_id = session_id
            self.name = name

    class NewRequested(events.Message):
        pass

    class RenameRequested(events.Message):
        def __init__(self, session_id: str, name: str) -> None:
            super().__init__()
            self.session_id = session_id
            self.name = name

    class DeleteRequested(events.Message):
        def __init__(self, session_id: str, name: str) -> None:
            super().__init__()
            self.session_id = session_id
            self.name = name

    def compose(self) -> ComposeResult:
        yield Static("[bold $text]sessions[/]", classes="panel-title")
        yield ListView(id="session-list")
        yield Static("[$text-disabled]enter loads · n new · r rename · "
                     "d delete · esc back[/]", classes="modal-keys")

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
            count = entry.get("message_count")
            summary = str(entry.get("summary") or "").strip()
            marker = "[$primary]▍[/]" if sid == active_id else " "
            if sid == active_id:
                active_index = i
            # The ID is the one field a user genuinely NEEDS from this list -
            # `vaf run --session <id>` takes nothing else - and it was shown
            # only for unnamed sessions (as their stand-in name). Width budget
            # is 26 columns (panel 32 minus paddings and the indent), so the
            # meta line carries id and count, and the third line the summary
            # when there is one, else the date.
            meta = sid[:12] + (f" · {count} msg" if count else "")
            tail = (summary[:24] + "…") if len(summary) > 25 else summary
            if not tail:
                tail = str(entry.get("updated_at") or "")[:16]
            lv.append(ListItem(Static(
                f"{marker}[$text]{_esc(name)}[/]\n"
                f"  [$text-disabled]{_esc(meta)}[/]\n"
                f"  [$text-disabled]{_esc(tail)}[/]")))
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

    def _highlighted(self):
        """The entry under the cursor, or None - r/d act on THIS row."""
        try:
            idx = self.query_one("#session-list", ListView).index or 0
        except Exception:
            return None
        if 0 <= idx < len(self._entries):
            return self._entries[idx]
        return None

    def action_new_session(self) -> None:
        self.post_message(self.NewRequested())

    def action_rename_session(self) -> None:
        entry = self._highlighted()
        if entry is not None:
            self.post_message(self.RenameRequested(
                str(entry.get("id", "")), str(entry.get("name") or "")))

    def action_delete_session(self) -> None:
        entry = self._highlighted()
        if entry is not None:
            self.post_message(self.DeleteRequested(
                str(entry.get("id", "")), str(entry.get("name") or "")))

    def action_leave_panel(self) -> None:
        self.remove_class("visible")
        try:
            self.screen.query_one("#promptbox").focus()
        except Exception:
            pass
