# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Transcript and chrome widgets for the full-screen terminal app.

Faithful ports of the classic lane's surfaces (tui.py): the context usage bar
with its 70/90 thresholds, the sub-agent status line with its [>] [>>] [||]
markers, event lines in the five styles, role-headed messages with timestamps,
syntax-highlighted code blocks - plus the agent's avatar, whose animations
follow the web avatar showcase and whose eye stays WHITE in every theme.
"""
import itertools
import random
import time

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Markdown, Static, TextArea

from vaf.cli.tui_app.theme_bridge import WHITE

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DOT_PULSE = [WHITE, "#e2e2e2", "#bdbdbd", "#9a9a9a", "#bdbdbd", "#e2e2e2"]


def _now() -> str:
    return time.strftime("%H:%M")


# ── transcript ──────────────────────────────────────────────────────────────────────

class UserMessage(Vertical):
    """Role header (You · HH:MM) + accent-barred body - the old message_box, app-mode."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.add_class("user-msg-wrap")
        self._text = text

    def compose(self) -> ComposeResult:
        yield Static(f"[$accent]You[/] [$text-disabled]· {_now()}[/]", classes="msg-head")
        yield Static(self._text, classes="user-msg", markup=False)


class AgentMessage(Vertical):
    """Role-headed streaming content with a separate think channel; the avatar
    lives IN the head row (`[ ● ] VAF · HH:MM`), so think and answer below run
    from the left edge instead of being indented by an avatar column.

    `feed` carries answer text, `feed_think` the model's reasoning. The avatar
    renders ONLY while this is the NEWEST agent message - the app moves the
    active slot forward and older replies drop theirs.
    """

    def __init__(self) -> None:
        super().__init__()
        self.add_class("agent-msg-wrap")
        self._answer = ""
        self._think = ""
        self.avatar = AgentAvatar(classes="msg-avatar")
        self.avatar.display = False
        # A real Markdown widget, not a Static: answers arrive as markdown and
        # showed as raw `**stars**` and raw ``` fences before. open_links=False
        # is deliberate - the default would open the system browser on a click,
        # bypassing the ux_auto_open_links setting that governs that decision.
        self._body = Markdown(open_links=False)
        self._body.add_class("agent-msg")
        self._pending = ""
        self._flushes = 0
        self._timer = None
        self._think_body = Static("", classes="agent-think")
        self._think_body.display = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="agent-head-row"):
            yield self.avatar
            yield Static(f"[$primary]VAF[/] [$text-disabled]· {_now()}[/]",
                         classes="msg-head")
        yield self._think_body
        yield self._body

    def on_mount(self) -> None:
        # Chunks arrive 20-100 times a second on the agent lane. Appending per
        # chunk costs ~14% of the UI loop; re-rendering the whole document per
        # chunk is O(n^2). Coalescing at 100 ms keeps it flat (~3.5 ms a flush
        # regardless of length) because Markdown.append reparses only the tail.
        self._timer = self.set_interval(0.1, self._flush)

    def set_avatar_visible(self, visible: bool) -> None:
        self.avatar.display = visible

    def feed(self, chunk: str) -> None:
        # Stays synchronous and trivial: the agent lane must never wait on the
        # UI. The timer below does the expensive half.
        self._answer += chunk
        self._pending += chunk

    async def _flush(self) -> None:
        # `append` reads self.source eagerly, so two un-awaited calls in flight
        # would silently swallow one another. Textual awaits an async interval
        # callback, which serializes them for us.
        app = self.app if self.is_mounted else None
        if app is None or app._exit or not self._pending:
            return
        text, self._pending = self._pending, ""
        self._flushes += 1
        await self._body.append(text)

    def feed_think(self, chunk: str) -> None:
        self._think += chunk
        self._think_body.display = True
        # A visibly SEPARATE block, not just dimmed prose: caption + italic
        # muted body ($vaf-muted is the theme's designed gray, the same one the
        # event labels use); the block chrome (left bar, panel background)
        # comes from the .agent-think CSS. The text is model output, escaped,
        # never markup.
        self._think_body.update(
            f"[$vaf-muted]· thinking[/]\n"
            f"[italic $vaf-muted]{_escape(self._think.strip())}[/]")

    def done(self) -> None:
        """Seal the bubble: stop the ticker and flush whatever is left.

        The timer is stopped from HERE and never from inside its own callback -
        doing that cancels the task currently executing it, which is one of the
        two shapes that hung app teardown in testing.
        """
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None
        if self._pending and self.is_mounted:
            try:
                self.call_next(self._flush)
            except Exception:
                pass


def _escape(text: str) -> str:
    return text.replace("[", r"\[")


class EventNote(Static):
    """The old event(): `│ Type       message` in the five styles.

    Unknown styles fall back to info - the engine emits ad-hoc styles like
    "yellow", and an unknown word must never crash a narration line.
    """

    STYLES = {"info": "$primary", "success": "$success", "warning": "$warning",
              "error": "$error", "dim": "$vaf-muted"}

    def __init__(self, type_name: str, message: str, style: str = "info") -> None:
        color = self.STYLES.get(style, "$primary")
        super().__init__(
            f"[{color}]│[/] [$vaf-muted]{_escape(str(type_name))[:10]:<10}[/] "
            f"[{color}]{_escape(str(message))}[/]"
        )
        self.add_class("event-note")


class SystemNote(Static):
    def __init__(self, text: str) -> None:
        super().__init__(f"[$text-disabled]· {_escape(text)}[/]")
        self.add_class("system-note")


class RenderableNote(Static):
    """Mounts a raw Rich renderable (the drain's result Panels) into the transcript."""

    def __init__(self, renderable) -> None:
        super().__init__(renderable)
        self.add_class("renderable-note")


class ToolCard(Vertical):
    """A tool call: header with live spinner/status, click to fold the output."""

    def __init__(self, tool: str, args_preview: str = "") -> None:
        super().__init__()
        self.add_class("tool-card")
        self._tool = tool
        self._args = args_preview
        self._output = ""
        self._status = "running"
        self._duration = ""
        self._frames = itertools.cycle(SPINNER)
        self._collapsed = True
        self._head_left = Static("", classes="tool-head-left")
        self._head_right = Static("", classes="tool-head-right")
        self._body = Static("", classes="tool-body")
        self._body.display = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="tool-header"):
            yield self._head_left
            yield self._head_right
        yield self._body

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.09, self._render_header)
        self._render_header()

    def _render_header(self) -> None:
        if self._status == "running":
            icon = f"[$primary]{next(self._frames)}[/]"
            tail = "[$text-disabled]running[/]"
        elif self._status == "ok":
            icon = "[$success]⏺[/]"
            tail = f"[$text-disabled]{self._duration}[/]"
        else:
            icon = "[$error]⏺[/]"
            tail = "[$error]error[/]"
        # The fold marker only appears once there is something to unfold - a
        # promise of hidden content that opens onto nothing is worse than none.
        fold = ("  [$text-disabled]" + ("▸" if self._collapsed else "▾") + "[/]"
                if self._output else "")
        self._head_left.update(
            f"{icon} [bold $text]{_escape(self._tool)}[/]"
            f"[$vaf-muted]({_escape(self._args)})[/]{fold}"
        )
        self._head_right.update(tail)

    def set_output(self, text: str) -> None:
        self._output = text
        preview = text if len(text) < 400 else text[:400] + " …"
        self._body.update(f"[$vaf-muted]{_escape(preview)}[/]")
        self._render_header()

    def on_unmount(self) -> None:
        try:
            self._timer.stop()
        except Exception:
            pass

    def finish(self, ok: bool = True, duration: str = "") -> None:
        self._status = "ok" if ok else "error"
        self._duration = duration
        try:
            self._timer.stop()
        except Exception:
            pass
        self._render_header()

    def on_click(self, event: events.Click) -> None:
        if self._status == "running":
            return
        self._collapsed = not self._collapsed
        self._body.display = not self._collapsed
        self._render_header()


class SubagentLine(Static):
    """The coder/researcher progress line inside the transcript."""

    def __init__(self, name: str) -> None:
        super().__init__("")
        self.add_class("subagent-line")
        self._name = name
        self.set_progress(0, "starting")

    def set_progress(self, pct: int, phase: str) -> None:
        cells = 24
        filled = round(pct / 100 * cells)
        bar = f"[$primary]{'━' * filled}[/][$vaf-border]{'━' * (cells - filled)}[/]"
        self.update(
            f"[$vaf-info]◍[/] [bold]{_escape(self._name)}[/]  {bar}  "
            f"[$vaf-muted]{pct:3d}%[/]  [$text-disabled]{_escape(phase)}[/]"
        )


# ── the agent (avatar + presence) ───────────────────────────────────────────────────

class AgentAvatar(Static):
    """The living white dot: dark rounded body, white eye.

    States and timings mirror the web avatar showcase: idle (float + occasional
    blink), waiting (slow breathe), thinking (fast pulse + glow), talking (beat),
    working (satellite orbits), success (ring), error (flash), listening (arcs).
    The eye is WHITE in every theme - identity, not decoration.
    """

    TICK = 0.1

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.add_class("agent-avatar")
        self._state = "idle"
        self._tick = 0
        self._blink_at = random.randint(45, 90)
        self._oneshot_until = 0
        self._prev_state = "idle"

    def on_mount(self) -> None:
        self.set_interval(self.TICK, self._advance)
        self.update(self._render_body())

    def set_state(self, state: str) -> None:
        if state in ("success", "error"):
            self._prev_state = self._state if self._state not in ("success", "error") else "idle"
            self._oneshot_until = self._tick + 14
        self._state = state

    def _advance(self) -> None:
        self._tick += 1
        if self._state in ("success", "error") and self._tick >= self._oneshot_until:
            self._state = self._prev_state
        self.update(self._render_body())

    def _render_body(self) -> str:
        # The agent's BODY: a literal bracket shell around the animated eye -
        # `[ ● ]`. Drawn as characters, not as a CSS border, so it is one row
        # tall and can never render as a wide box in any font.
        return f"[$vaf-muted]\\[[/]{self._frame()}[$vaf-muted]][/]"

    def _center(self, inner: str, visible: int) -> str:
        # 3 content cells inside the bracket body: `[ ● ]`, tight on purpose -
        # every animation phase must fit this field.
        width = 3
        pad = max(0, width - visible)
        left = pad // 2
        return " " * left + inner + " " * (pad - left)

    def _frame(self) -> str:
        t = self._tick
        s = self._state
        if s == "idle":
            if t >= self._blink_at:
                if t >= self._blink_at + 2:
                    self._blink_at = t + random.randint(50, 110)
                return self._center("[#e8e8e8]─[/]", 1)
            drift = [0, 0, 1, 0, 0, -1][(t // 22) % 6]
            if drift > 0:
                return self._center(f" [{WHITE}]●[/]", 2)
            if drift < 0:
                return self._center(f"[{WHITE}]●[/] ", 2)
            return self._center(f"[{WHITE}]●[/]", 1)
        if s == "waiting":
            phase = (t // 5) % 8
            chars = ["·", "•", "●", "●", "●", "●", "•", "·"]
            shades = ["#9a9a9a", "#c4c4c4", "#e8e8e8", WHITE, WHITE, "#e8e8e8", "#c4c4c4", "#9a9a9a"]
            return self._center(f"[{shades[phase]}]{chars[phase]}[/]", 1)
        if s == "thinking":
            phase = (t // 2) % 4
            core = ["●", "◉", "●", "◉"][phase]
            glow = ["#4a4a4a", "#7a7a7a", "#9a9a9a", "#7a7a7a"][phase]
            return self._center(f"[{glow}]∘[/][{WHITE}]{core}[/][{glow}]∘[/]", 3)
        if s == "talking":
            phase = (t // 2) % 4
            beat = ["●", "◉", "●", "•"][phase]
            return self._center(f"[{WHITE}]{beat}[/]", 1)
        if s == "listening":
            phase = (t // 3) % 2
            core = ["●", "◉"][phase]
            return self._center(f"[$error]([/][{WHITE}]{core}[/][$error])[/]", 3)
        if s == "working":
            pos = (t // 2) % 4
            seq = [("[$vaf-muted]·[/][{w}]●[/] ", 3), (" [{w}]◉[/] ", 3),
                   (" [{w}]●[/][$vaf-muted]·[/]", 3), (" [{w}]◉[/] ", 3)]
            tpl, vis = seq[pos]
            return self._center(tpl.format(w=WHITE), vis)
        if s == "success":
            k = self._oneshot_until - self._tick
            if k > 6:
                return self._center(f"[$success]([/][{WHITE}]●[/][$success])[/]", 3)
            return self._center(f"[$vaf-muted]([/][{WHITE}]●[/][$vaf-muted])[/]", 3)
        if s == "error":
            flash = (self._tick % 2) == 0
            return self._center(f"[$error]{'!' if flash else '●'}[/]", 1)
        if s == "dim":
            return self._center("[$text-disabled]●[/]", 1)
        return self._center(f"[{WHITE}]●[/]", 1)


# ── chrome ──────────────────────────────────────────────────────────────────────────

class TopBar(Horizontal):
    """Brand + session + mic + model chip. The dot is the agent.

    Two Statics, not one markup string: `[right]` is NOT a markup tag (it would
    parse as an unknown style and be dropped silently), so right-alignment is
    layout - a 1fr left cell and an auto right cell.
    """

    working = reactive(False)
    session_name = reactive("")
    model_chip = reactive("")
    mic_on = reactive(False)

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar-left")
        yield Static("", id="topbar-right")

    def on_mount(self) -> None:
        self._pulse = itertools.cycle(DOT_PULSE)
        self.set_interval(0.18, self.refresh_line)
        self.refresh_line()

    def refresh_line(self) -> None:
        dot_color = next(self._pulse) if self.working else WHITE
        state = "[$vaf-muted]working[/]" if self.working else "[$text-disabled]ready[/]"
        mic = (f"[$success]◉[/] [$vaf-muted]mic[/]  [$text-disabled]│[/]  "
               if self.mic_on else "")
        self.query_one("#topbar-left", Static).update(
            f" [{dot_color}]●[/] [bold $text]VAF[/]  [$text-disabled]│[/]  "
            f"[$text]{_escape(self.session_name)}[/]  {state}"
        )
        self.query_one("#topbar-right", Static).update(
            f"{mic}[$vaf-muted]{_escape(self.model_chip)}[/]")

    def watch_working(self, _: bool) -> None:
        if self.is_mounted:
            self.refresh_line()


class TasksLine(Static):
    """Sub-agent / paused-workflow status above the prompt - the classic lane's
    status line, same markers: [>] active, [>>] workflow, [||] paused, +N more."""

    def on_mount(self) -> None:
        self._entries = []
        self.display = False

    def set_tasks(self, entries) -> None:
        """entries: iterable of (marker, label, id8, elapsed_str)."""
        self._entries = list(entries)
        self.display = bool(self._entries)
        if not self._entries:
            return
        parts = []
        for marker, label, id8, tstr in self._entries[:3]:
            color = "$warning" if marker == "[||]" else "$vaf-info"
            parts.append(
                f"[{color}]{marker}[/] [$vaf-muted]{_escape(label)} "
                f"\\[{_escape(id8)}] {tstr}[/]")
        if len(self._entries) > 3:
            parts.append(f"[$vaf-muted]+{len(self._entries) - 3} more[/]")
        self.update(" [$text-disabled]|[/] ".join(parts))


class ContextBar(Static):
    """The classic usage bar: ▰▱ cells, green under 70, yellow over, red over 90."""

    def on_mount(self) -> None:
        self.set_usage(0, 1)

    def set_usage(self, used: int, total: int) -> None:
        total = max(1, int(total or 1))
        used = max(0, int(used or 0))
        pct = min(100, int(used / total * 100))
        color = "$success"
        if pct > 70:
            color = "$warning"
        if pct > 90:
            color = "$error"
        cells = 18
        filled = int(pct / 100 * cells)
        bar = "▰" * filled + "▱" * (cells - filled)
        self.update(
            f"[$vaf-muted]Context[/] [{color}]{bar}[/] [bold $text]{pct}%[/] "
            f"[$vaf-muted]({used:,}/{total:,} Tok)[/]"
        )


class PromptBox(TextArea):
    """Multiline input: Enter sends, Ctrl+J inserts a newline."""

    BINDINGS = [Binding("ctrl+j", "newline", "newline", show=False)]

    class Submitted(events.Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(text))
                self.clear()
            return
        await super()._on_key(event)

    def action_newline(self) -> None:
        self.insert("\n")
