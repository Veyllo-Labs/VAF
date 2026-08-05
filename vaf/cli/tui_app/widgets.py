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
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Markdown, OptionList, Static, TextArea

from vaf.cli.tui_app.theme_bridge import WHITE

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DOT_PULSE = [WHITE, "#e2e2e2", "#bdbdbd", "#9a9a9a", "#bdbdbd", "#e2e2e2"]


def _now() -> str:
    return time.strftime("%H:%M")


# ── transcript ──────────────────────────────────────────────────────────────────────

class Transcript(VerticalScroll):
    """The conversation column, holding its own bottom while an answer streams.

    Following the newest line is a STATE here, never a measurement. Measuring it
    cannot work: a bubble's height is set by the layout pass AFTER the mount, so
    a transcript that grew from 5 rows to 8 on its own reads as "the reader
    scrolled 3 rows up" to anything that compares the offset with the maximum
    afterwards - and since growth is not something our side gets told about, no
    later event corrects that verdict. Which side of the race a run lands on
    depends on the terminal size, so the same code followed on a narrow window
    and never followed on a wide one.

    Textual's anchor removes the race instead of timing it: the scroll position
    is recomputed inside the layout pass itself, where the new height exists, so
    growth keeps the view at the bottom for as long as the anchor holds. Only a
    reader action releases it - wheel or key upwards, or a programmatic
    `scroll_to`; scrolling DOWN deliberately keeps it, and arriving back at the
    bottom takes it up again.

    What is left for us is WHEN to arm it, and that is not "at startup": an
    anchored container with less content than view gets a negative scroll
    offset, which drops the centred start block onto the bottom edge. So the
    anchor waits for content taller than the view. `virtual_size` is the signal
    for that - it is assigned during the layout pass that establishes the new
    height, which is exactly the moment our side could not observe otherwise.
    `is_anchored` stays True once armed, including after a release, so this arms
    at most once per transcript and never overrides a reader who scrolled up.
    """

    def watch_virtual_size(self, virtual_size) -> None:
        # The decision runs a refresh later on purpose. `container_size` is
        # still the PREVIOUS one while this watcher runs - `_size_updated`
        # assigns virtual_size first - and on the opening pass that is zero
        # rows, which would arm the anchor on the start block alone. Deferring
        # is safe here in a way it never was for a measured verdict: every
        # growth fires this watcher, so no single check is the only chance.
        if not self.is_anchored and self.is_mounted:
            self.call_after_refresh(self._arm_if_scrollable)

    def _arm_if_scrollable(self) -> None:
        if not self.is_anchored and self.max_scroll_y > 0:
            self.anchor()

    def clear(self) -> None:
        """Empty the conversation, anchor included.

        An emptied transcript is shorter than its view again, and an anchor held
        on that is the negative-offset case above. Dropping it here puts the
        transcript back in its pre-arming state, so the next answer that outgrows
        the view arms it afresh.
        """
        for child in list(self.children):
            child.remove()
        self.anchor(False)


class UserMessage(Vertical):
    """Role header (You · HH:MM) + accent-barred body - the old message_box, app-mode."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.add_class("user-msg-wrap")
        self._text = text

    def compose(self) -> ComposeResult:
        yield Static(f"[$accent]You[/] [$text-disabled]· {_now()}[/]", classes="msg-head")
        yield Static(self._text, classes="user-msg", markup=False)


class WakeMessage(Vertical):
    """A turn nobody typed: what woke the agent, and where it came from.

    Not a UserMessage and not a SystemNote. `turn_started` is a no-op in this lane
    and the app mounts the user bubble itself, so a queue-driven turn would
    otherwise stream an answer into an empty transcript with no visible origin at
    all. The web UI answers the same question with its own wake card on the same
    trigger; this is that card in the terminal.
    """

    LABELS = {"timer": ("(!)", "Timer")}

    def __init__(self, text: str, kind: str = "timer") -> None:
        super().__init__()
        self.add_class("wake-msg-wrap")
        self._text = text
        self._mark, self._label = self.LABELS.get(kind, ("*", str(kind or "wake")))

    def compose(self) -> ComposeResult:
        yield Static(f"[$warning]{self._mark} {_escape(self._label)}[/] "
                     f"[$text-disabled]· {_now()}[/]", classes="msg-head")
        yield Static(self._text, classes="wake-msg", markup=False)


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


class StartBanner(Vertical):
    """The first thing a session shows: the mark left, the facts right.

    Laid out like `neofetch` because that shape answers "what am I looking at"
    in one glance - and because the alternative, a lone "new session" line, told
    the user nothing about which agent, which session or how to get back to an
    older one.

    A Vertical wrapping a centered row: the block sits in the middle of an empty
    transcript rather than pinned to the top-left corner, and it stops being
    centered the moment real content pushes it up, which is the behaviour that
    reads as "the session started here".
    """

    # The Veyllo mark as terminal art, converted from the logo. It is a line
    # drawing: the characters trace the mark's edges and the interior stays
    # open, so it reads as a drawing rather than as a slab, and it carries no
    # block glyphs at all - which is also why no exporter has to guess how to
    # tile it.
    ART = (
        "          @@@g",
        " __________@@@i___ _____",
        "@@@@@@@@@@W@@g@@@@R@@@@@@",
        ' """8@@@""""""""T@@@D""\'',
        "     B@@,       @W@@",
        "     '@@@,     g@@@",
        "      '@@@B  _@B@W",
        "        tB@RW@B@F",
        "         ]@@@@@L",
        "     _a@@@@@M@@@@@b__",
        '_@@@@@@@P"     <B@@WW@@@,',
        "'@@QB+             %Mg@B",
    )

    def __init__(self, rows, hint: str = "") -> None:
        super().__init__()
        self.add_class("start-banner")
        self._rows = list(rows)
        self._hint = hint

    def compose(self) -> ComposeResult:
        art = "\n".join(f"[{WHITE}]{_escape(line)}[/]" for line in self.ART)
        with Center(classes="banner-center"):
            with Horizontal(classes="banner-row-wrap"):
                yield Static(art, classes="banner-art")
                with Vertical(classes="banner-facts"):
                    for label, value in self._rows:
                        if label is None:              # a spacer row
                            yield Static("")
                            continue
                        yield Static(
                            f"[$vaf-muted]{label:<9}[/] [$text]{_escape(value)}[/]",
                            classes="banner-row")
        if self._hint:
            with Center():
                yield Static(f"[$text-disabled]{_escape(self._hint)}[/]",
                             classes="banner-hint")


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


# A second progress renderer lived here and was never mounted: one repo-wide hit, its
# own class statement. It took a percentage, which no sub-agent can honestly report - a
# coder run ends below its total whenever a task fails - and a phase string, which is
# model text derived from the user's prompt and therefore may not travel on the shared
# task record. Sub-agent progress is rendered by TasksLine above, as counts.


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
        """entries: iterable of (marker, label, id8, elapsed_str, progress_str).

        `progress_str` is "done/total" or empty. It is rendered as counts and never as a
        percentage: a coder run legitimately ends below its total (a failed task is
        terminal but not completed), so a bar that must reach 100% would have to lie.
        An empty string means the agent reports no counts, and it takes no space.
        """
        self._entries = list(entries)
        self.display = bool(self._entries)
        if not self._entries:
            return
        parts = []
        for marker, label, id8, tstr, progress in self._entries[:3]:
            color = "$warning" if marker == "[||]" else "$vaf-info"
            counts = f" [$vaf-info]{_escape(progress)}[/]" if progress else ""
            parts.append(
                f"[{color}]{marker}[/] [$vaf-muted]{_escape(label)} "
                f"\\[{_escape(id8)}] {tstr}[/]{counts}")
        if len(self._entries) > 3:
            parts.append(f"[$vaf-muted]+{len(self._entries) - 3} more[/]")
        self.update(" [$text-disabled]|[/] ".join(parts))


class ContextBar(Static):
    """The classic usage bar: ▰▱ cells, green under 70, yellow over, red over 90.

    It shares its row with the key hints and gives ground first when the two
    do not fit: the token counts go, then the caption and half the bar. The
    percentage is the last thing standing, because it is the number a user
    actually reads off this bar.
    """

    LEVELS = ("full", "short", "bare")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._used = 0
        self._total = 1
        self._level = "full"

    def on_mount(self) -> None:
        self.set_usage(0, 1)

    def set_level(self, level: str) -> None:
        if level in self.LEVELS and level != self._level:
            self._level = level
            self.set_usage(self._used, self._total)

    def width_for(self, level: str, used: int = None, total: int = None) -> int:
        """Cells this bar would occupy at `level` - what the strip budgets on."""
        used = self._used if used is None else used
        total = self._total if total is None else total
        return len(self._plain(*self._parts(used, total, level)))

    @staticmethod
    def _plain(caption: str, bar: str, pct: str, tail: str) -> str:
        return " ".join(p for p in (caption, bar, pct, tail) if p)

    @staticmethod
    def _parts(used: int, total: int, level: str):
        total = max(1, int(total or 1))
        used = max(0, int(used or 0))
        pct = min(100, int(used / total * 100))
        cells = 18 if level != "bare" else 10
        filled = int(pct / 100 * cells)
        return (
            "Context" if level != "bare" else "",
            "▰" * filled + "▱" * (cells - filled),
            f"{pct}%",
            f"({used:,}/{total:,} Tok)" if level == "full" else "",
        )

    def set_usage(self, used: int, total: int) -> None:
        self._used, self._total = used, total
        caption, bar, pct, tail = self._parts(used, total, self._level)
        percent = int(pct.rstrip("%"))
        color = "$success"
        if percent > 70:
            color = "$warning"
        if percent > 90:
            color = "$error"
        chunks = []
        if caption:
            chunks.append(f"[$vaf-muted]{caption}[/]")
        chunks.append(f"[{color}]{bar}[/]")
        chunks.append(f"[bold $text]{pct}[/]")
        if tail:
            chunks.append(f"[$vaf-muted]{tail}[/]")
        self.update(" ".join(chunks))


class KeyHints(Static):
    """The left half of the status strip: whole hints, or fewer hints.

    Clipping is what Textual does by default when the row runs out, and it
    lands mid-label - "/exit" without its "Quit" reads as a rendering fault
    rather than as a hint that had to go. Dropping whole pairs from the right
    is the same information loss, told honestly.
    """

    PAIRS = (("S", "Settings"), ("C", "Model"), ("L", "Voice"),
             ("T", "Theme"), ("H", "History"), ("?", "Help"), ("/exit", "Quit"))

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._shown = len(self.PAIRS)

    @property
    def shown(self) -> int:
        """How many hint pairs are on screen - whole ones, always."""
        return self._shown

    def on_mount(self) -> None:
        self._paint()

    @classmethod
    def width_for(cls, count: int) -> int:
        pairs = cls.PAIRS[:max(0, count)]
        if not pairs:
            return 0
        return sum(len(k) + 1 + len(label) for k, label in pairs) + 2 * (len(pairs) - 1)

    def show(self, count: int) -> None:
        count = max(0, min(len(self.PAIRS), count))
        if count != self._shown:
            self._shown = count
            self._paint()

    def _paint(self) -> None:
        self.update("  ".join(f"[bold $text]{key}[/] [$vaf-muted]{label}[/]"
                              for key, label in self.PAIRS[:self._shown]))


class StatusStrip(Horizontal):
    """One row, two tenants, and the arithmetic that keeps them off each other.

    The full hints are 68 cells and the full context bar is 51; below ~120
    columns they do not both fit, which is every ordinary terminal. Textual
    resolves that overflow by clipping the left widget, so the fix cannot live
    in CSS - somebody has to decide what gives way. Live state gives up its
    detail first, static hints go last and whole.
    """

    def on_resize(self, event: events.Resize) -> None:
        self.fit(event.size.width)

    def fit(self, width: int) -> None:
        try:
            hints = self.query_one(KeyHints)
            context = self.query_one(ContextBar)
        except Exception:
            return                          # composed but not mounted yet
        gap = 2
        for level in ContextBar.LEVELS:
            need = context.width_for(level)
            if KeyHints.width_for(len(KeyHints.PAIRS)) + gap + need <= width:
                context.set_level(level)
                hints.show(len(KeyHints.PAIRS))
                return
        context.set_level("bare")
        room = width - context.width_for("bare") - gap
        count = len(KeyHints.PAIRS)
        while count > 0 and KeyHints.width_for(count) > room:
            count -= 1
        hints.show(count)


class CompletionPopup(OptionList):
    """The completion menu, in normal flow just above the prompt.

    NOT a modal screen: a modal would take focus, and the prompt has to keep
    receiving keystrokes so the list narrows as the user types. It never gets
    focus at all - `PromptBox` drives it by calling these methods, which also
    means the list's own bindings can never fight the prompt's.
    """

    def on_mount(self) -> None:
        self.display = False
        self._candidates: list = []

    @property
    def is_open(self) -> bool:
        return bool(self.display and self._candidates)

    def open_with(self, candidates) -> None:
        self._candidates = list(candidates)
        self.clear_options()
        if not self._candidates:
            self.display = False
            return
        self.add_options([
            f"{c.label}" + (f"  [$vaf-muted]{c.meta}[/]" if c.meta else "")
            for c in self._candidates])
        self.display = True
        self.highlighted = 0

    def close(self) -> None:
        self._candidates = []
        self.clear_options()
        self.display = False

    def move(self, delta: int) -> None:
        if not self._candidates:
            return
        count = len(self._candidates)
        current = self.highlighted if self.highlighted is not None else 0
        self.highlighted = (current + delta) % count

    def current(self):
        if not self._candidates:
            return None
        idx = self.highlighted if self.highlighted is not None else 0
        return self._candidates[idx] if 0 <= idx < len(self._candidates) else None


class PromptBox(TextArea):
    """Multiline input with the affordances the classic prompt had.

    Enter sends, Ctrl+J inserts a newline - and around that: persistent history
    on the arrow keys, the learned inline suggestion accepted with Tab or
    Right, and a completion menu for `/commands` and `@paths`.

    The one collision to respect is Enter: while the menu is open it ACCEPTS,
    it does not send. Everything consumed here calls both `stop()` and
    `prevent_default()`, or TextArea's own bindings would still run.
    """

    BINDINGS = [Binding("ctrl+j", "newline", "newline", show=False)]

    class Submitted(events.Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.popup = None            # set by the app once both are mounted
        self._history: list = []     # newest first, as the store returns it
        self._hist_index = -1        # -1 = not browsing
        self._draft = ""

    # history ------------------------------------------------------------------------
    def load_history(self, entries) -> None:
        self._history = [e for e in entries if e and e.strip()]

    def remember(self, text: str) -> None:
        if text and text.strip():
            self._history.insert(0, text)
        self._hist_index = -1
        self._draft = ""

    def _recall(self, delta: int) -> bool:
        """Walk the history. Returns True when it consumed the key."""
        if not self._history:
            return False
        if self._hist_index == -1:
            if delta < 0:
                return False                 # nothing newer than the draft
            self._draft = self.text
        new_index = self._hist_index + delta
        if new_index < -1:
            new_index = -1
        if new_index >= len(self._history):
            new_index = len(self._history) - 1
        self._hist_index = new_index
        self.text = self._draft if new_index == -1 else self._history[new_index]
        self.move_cursor(self.document.end)
        return True

    # keys ---------------------------------------------------------------------------
    def _accept_completion(self) -> bool:
        cand = self.popup.current() if (self.popup and self.popup.is_open) else None
        if cand is None:
            return False
        for _ in range(cand.replace):
            self.action_delete_left()
        self.insert(cand.insert)
        self.popup.close()
        return True

    async def _on_key(self, event: events.Key) -> None:
        popup_open = bool(self.popup and self.popup.is_open)

        if event.key in ("up", "down") and popup_open:
            event.stop(); event.prevent_default()
            self.popup.move(1 if event.key == "down" else -1)
            return

        if event.key in ("tab", "enter") and popup_open:
            event.stop(); event.prevent_default()
            self._accept_completion()
            return

        if event.key == "escape" and popup_open:
            event.stop(); event.prevent_default()
            self.popup.close()
            return

        if event.key == "enter":
            event.stop(); event.prevent_default()
            text = self.text.strip()
            if text:
                self.remember(text)
                self.post_message(self.Submitted(text))
                self.clear()
            return

        if event.key == "tab":
            event.stop(); event.prevent_default()
            if self.suggestion:
                self.insert(self.suggestion)     # the classic accept-then-complete
            elif self.popup is not None:
                from vaf.cli.completion import complete
                self.popup.open_with(complete(self._text_before_cursor()))
            return

        if event.key == "up" and self.cursor_at_first_line:
            if self._recall(+1):
                event.stop(); event.prevent_default()
                return

        if event.key == "down" and self.cursor_at_last_line:
            if self._recall(-1):
                event.stop(); event.prevent_default()
                return

        await super()._on_key(event)

    def _text_before_cursor(self) -> str:
        row, col = self.cursor_location
        line = self.document.get_line(row)
        return str(line)[:col]

    def action_newline(self) -> None:
        self.insert("\n")
