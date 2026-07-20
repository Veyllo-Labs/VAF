# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one rate-capped, ANSI-free line ticker for everything VAF mirrors into the browser.

Anything that streams a subprocess' or a workflow's stdout into the Web UI sends one
WebSocket frame per line. Rich draws its Live panels by REDRAWING them many times per
second, so an unfiltered mirror turns a normal run into a frame storm: one HTTP POST, one
WebSocket event and one React render per redraw.

That has now caused two incidents. 2026-07-16: the workflow terminal froze the tray browser
until the WebSocket dropped. 2026-07-20: the in-chat workflow lane pushed 48,359 frames in
181 seconds (mean 267/s, bursts of 12-18 lines every ~70 ms, which is refresh_per_second=15)
and the browser socket died mid-run; every event that would have advanced or closed the
Workflow Runtime panel was then broadcast to zero subscribers, so the panel sat at "step 1
running, 50 percent" forever.

The filter that prevents this was written after the FIRST incident - and only ever applied
to one of four copies. This module is that filter, extracted, so there is exactly one.

Ticker semantics, enforced at the emit site:
  - strip ANSI escapes and carriage returns
  - drop lines that are empty afterwards (animation frames)
  - drop repeats (consecutive by default, or against a bounded recent-set)
  - cap the rate: at most MAX_LINES_PER_WINDOW sends per WINDOW seconds
  - cap the run: at most max_lines_per_run sends in total
  - never drop silently: suppressed lines are counted and surfaced

STDLIB ONLY, on purpose: this module is imported from vaf.cli.*, vaf.tools.* and
vaf.core.platform, so a vaf-internal import here could create a cycle.
"""
import re
import time
from collections import deque
from typing import Callable, Optional

# ANSI/VT escape sequences: CSI colors and cursor moves, OSC titles, charset selects.
# Rich Live animations are made of these; the web ticker must never see them (they rendered
# as literal garbage in the browser and their sheer volume froze the tray).
ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-B])"
)


class WebTicker:
    """Turns a raw terminal stream into a web-safe, rate-capped line ticker.

    Feed it raw text with `feed()`; it calls `send_line` for the lines that survive. Call
    `close()` at the end to flush a trailing partial line and any pending suppression notice.
    """

    WINDOW = 0.25
    MAX_LINES_PER_WINDOW = 15

    def __init__(
        self,
        send_line: Callable[[str], None],
        *,
        on_activity: Optional[Callable[[], None]] = None,
        dedup_window: int = 0,
        max_lines_per_run: int = 4000,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        send_line:         called with each line that should reach the browser.
        on_activity:       called for every non-empty RAW line, even one the cap drops.
                           Watchdogs that ask "is this step still alive" must hang off this
                           and not off send_line, or throttling would look like a hang.
        dedup_window:      0 keeps only consecutive-duplicate suppression; a positive value
                           also drops a line seen within the last N distinct lines. Bounded
                           on purpose - an unbounded "seen" set is a slow memory leak in a
                           long run.
        max_lines_per_run: total ceiling. The browser keeps a few hundred lines anyway, so
                           an endless stream buys nothing and costs a socket.
        """
        self._send_line = send_line
        self._on_activity = on_activity
        self._buffer = ""
        self._last_line: Optional[str] = None
        self._recent: deque = deque(maxlen=dedup_window) if dedup_window > 0 else deque(maxlen=0)
        self._dedup_window = dedup_window
        self._window_start = 0.0
        self._window_count = 0
        self._skipped = 0
        self._sent_total = 0
        self._max_total = max_lines_per_run
        self._run_cap_announced = False
        self._clock = clock

    def feed(self, data: str) -> None:
        self._buffer += data
        while "\n" in self._buffer:
            raw, self._buffer = self._buffer.split("\n", 1)
            self._handle_line(raw)

    def close(self) -> None:
        """Flush a trailing partial line and any pending suppression notice.

        The tail deliberately BYPASSES the per-window rate cap. It is one line, at the end of
        the run, and it is usually the most interesting one (a final path, a summary). Routing
        it through the normal path meant that a run which ended inside a full rate window
        silently swallowed its own last line. The run ceiling still applies.
        """
        tail, self._buffer = self._buffer, ""
        if self._skipped:
            self._emit(f"[... {self._skipped} lines skipped]")
            self._skipped = 0
        line = ANSI_RE.sub("", tail).replace("\r", "").rstrip()
        if line.strip() and line != self._last_line and self._sent_total < self._max_total:
            self._last_line = line
            self._emit(line)

    # ── internals ────────────────────────────────────────────────────────────────
    def _handle_line(self, raw: str) -> None:
        line = ANSI_RE.sub("", raw).replace("\r", "").rstrip()
        if not line.strip():
            return

        # Activity is about the PROCESS being alive, so it is reported before any
        # throttling. A watchdog wired to send_line would read "rate-capped" as "hung".
        if self._on_activity is not None:
            try:
                self._on_activity()
            except Exception:
                pass

        if line == self._last_line:
            return
        self._last_line = line
        if self._dedup_window > 0:
            if line in self._recent:
                return
            self._recent.append(line)

        if self._sent_total >= self._max_total:
            if not self._run_cap_announced:
                self._run_cap_announced = True
                self._emit(
                    f"[... output limit of {self._max_total} lines reached, "
                    f"the rest is only in the terminal/log]"
                )
            return

        now = self._clock()
        if now - self._window_start >= self.WINDOW:
            self._window_start = now
            self._window_count = 0
        if self._window_count >= self.MAX_LINES_PER_WINDOW:
            self._skipped += 1
            return
        if self._skipped:
            self._emit(f"[... {self._skipped} lines skipped]")
            self._skipped = 0
            self._window_count += 1
        self._emit(line)
        self._window_count += 1

    def _emit(self, line: str) -> None:
        self._sent_total += 1
        try:
            self._send_line(line)
        except Exception:
            pass


class MirroredStdout:
    """A file-like stdout/stderr replacement: the real stream still gets everything, the
    browser gets the ticker's filtered view.

    `interactive` decides what isatty() reports, and it is NOT a constant:

    - False for in-process lanes whose output is mirrored into the browser (the in-chat
      workflow executor and the workflow builder). Rich gates Live on isatty() and on
      Console.is_terminal, and the Console resolves its file lazily from sys.stdout, so a
      False here stops the 15 fps animation at the source instead of filtering its frames
      afterwards.
    - the real stream's value for the separate-terminal lane, where the process really is
      attached to the terminal window the user is watching. Forcing False there would strip
      the colour and the live TUI out of a window whose whole purpose is to be watched.
    """

    def __init__(
        self,
        stream,
        send_line: Optional[Callable[[str], None]] = None,
        *,
        interactive: Optional[bool] = None,
        on_activity: Optional[Callable[[], None]] = None,
        dedup_window: int = 0,
        max_lines_per_run: int = 4000,
    ):
        self._stream = stream
        self._ticker = (
            WebTicker(
                send_line,
                on_activity=on_activity,
                dedup_window=dedup_window,
                max_lines_per_run=max_lines_per_run,
            )
            if send_line is not None else None
        )
        self._interactive = interactive

    def write(self, data):
        try:
            self._stream.write(data)
            self._stream.flush()
        except Exception:
            pass
        if self._ticker is not None:
            self._ticker.feed(data)

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def close_ticker(self):
        """Flush the mirror's tail. Must be called before the original stream is restored,
        or the last partial line (and a pending suppression notice) is lost."""
        if self._ticker is not None:
            self._ticker.close()

    def isatty(self):
        if self._interactive is not None:
            return bool(self._interactive)
        return getattr(self._stream, "isatty", lambda: False)()

    def fileno(self):
        return getattr(self._stream, "fileno", lambda: -1)()

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")
