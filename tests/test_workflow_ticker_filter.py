# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The workflow web ticker must survive a Rich Live animation storm.

Live incident 2026-07-16: the research agent's Live animation streamed
hundreds of ANSI lines per second into the Web UI (one HTTP POST + one
WebSocket event + one React render each) until the tray browser froze and
the WebSocket dropped. _WebTickerFilter enforces ticker semantics at the
emit site; this pins its contract.
"""
import vaf.cli.cmd.workflow as wf


class _FakeTime:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def test_animation_storm_is_capped_stripped_and_deduped(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(wf, "time", fake)
    sent = []
    f = wf._WebTickerFilter(sent.append)

    # 300 distinct ANSI-colored lines inside ONE rate window.
    for i in range(300):
        f.feed(f"\x1b[1;38;2;0;212;255mprogress frame {i}\x1b[0m\r\n")

    assert len(sent) == f.MAX_LINES_PER_WINDOW  # hard cap held
    assert all("\x1b" not in s and "\r" not in s for s in sent)  # web-safe
    assert sent[0] == "progress frame 0"

    # Next window: the skipped volume is surfaced once, then flow resumes.
    fake.now += 1.0
    f.feed("real content after the storm\n")
    assert sent[-2] == f"[... {300 - f.MAX_LINES_PER_WINDOW} lines skipped]"
    assert sent[-1] == "real content after the storm"


def test_control_frames_and_duplicate_redraws_are_dropped(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(wf, "time", fake)
    sent = []
    f = wf._WebTickerFilter(sent.append)

    # Pure cursor-control / clear-line frames collapse to nothing.
    f.feed("\x1b[2K\x1b[1A\n\x1b[0m   \n")
    # A Live panel redraws the same visible line over and over.
    for _ in range(20):
        f.feed("\x1b[36mSection 1/2:\x1b[0m \x1b[37mResearch\x1b[0m\n")

    assert sent == ["Section 1/2: Research"]


def test_partial_lines_are_buffered_until_newline(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(wf, "time", fake)
    sent = []
    f = wf._WebTickerFilter(sent.append)

    f.feed("chunk one, ")
    f.feed("chunk two")
    assert sent == []
    f.feed(" - done\n")
    assert sent == ["chunk one, chunk two - done"]


def test_osc_title_sequences_are_stripped(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(wf, "time", fake)
    sent = []
    f = wf._WebTickerFilter(sent.append)
    f.feed("\x1b]0;window title\x07visible text\n")
    assert sent == ["visible text"]
