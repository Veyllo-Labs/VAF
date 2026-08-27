# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The avatar animates only while somebody can see it.

MEASURED before the fix, with this file's own counting method: every
AgentMessage carries an AgentAvatar whose on_mount started a 10 Hz interval
with no reference and no stop - even at display:False, which is the state of
every avatar except the newest (the app moves the visible slot forward and
hides the previous one). 1 reply = 10 ticks/s, 10 replies = 100, 50 replies =
500 - linear, unbounded, and almost all of it rendering into display:none. A
replayed or long session would start hundreds of timers on one screen.

The fix ties the timer to visibility (Textual's Timer.pause/resume, and the
`pause=` start argument). The VISIBLE dot animates exactly as before - same
frequency, same states, same white eye. Only the invisible ones stop drawing.

WHY NOTHING HERE SLEEPS A FIXED SPAN AND COUNTS WHAT ARRIVED.
Textual's interval runs with skip=True (textual/timer.py, Timer._run): a tick
the starved event loop could not deliver is DROPPED, never caught up. So on a
loaded runner a fixed sleep yields fewer ticks, and the two assertion
directions rot differently:
  - a lower bound ("the visible one ticked >= N") goes falsely RED, which is
    how this file failed the nightly Windows leg;
  - an upper bound ("the hidden ones ticked <= 2") goes falsely GREEN, because
    starvation produces exactly the low number the fix produces. That one is
    worse: the guard silently stops proving anything and nobody notices.
Every measurement below therefore runs until a VISIBLE reference avatar has
delivered a set number of ticks, and compares the others against it. The same
starvation slows the reference by the same factor, the RATIO is what the fix
changes, and a machine too slow to deliver the reference ticks inside the
deadline fails with that sentence instead of quietly passing.
"""
import asyncio
import time

from textual.app import App, ComposeResult

from vaf.cli.tui_app import widgets as W

# How many ticks the visible reference avatar must deliver before a comparison
# is read, and the longest the loop may take to deliver them. At the designed
# 10 Hz these 15 ticks take 1.5 s, so the deadline tolerates a machine roughly
# thirteen times slower than this one before it reports a stalled loop.
REFERENCE_TICKS = 15
RESUME_TICKS = 5
DEADLINE = 20.0


class _Probe(App):
    """The smallest app that can hold a Transcript."""

    CSS = "Transcript { height: 1fr; }"

    def compose(self) -> ComposeResult:
        yield W.Transcript(id="t")


class _Census:
    """Counts _advance calls PER avatar instance while installed.

    Per instance, not one global total: an assertion that can name the visible
    avatar's own count can use it as the clock, which is the whole point of
    this file's measurements.
    """

    def __init__(self) -> None:
        self.counts: dict[int, int] = {}
        self._orig = None

    def __enter__(self) -> "_Census":
        self._orig = W.AgentAvatar._advance
        counts = self.counts
        orig = self._orig

        def counting(avatar):
            counts[id(avatar)] = counts.get(id(avatar), 0) + 1
            return orig(avatar)

        W.AgentAvatar._advance = counting
        return self

    def __exit__(self, *exc) -> bool:
        W.AgentAvatar._advance = self._orig
        return False

    def ticks(self, message) -> int:
        return self.counts.get(id(message.avatar), 0)

    def reset(self) -> None:
        self.counts.clear()


async def _run_until(census: _Census, reference, target: int,
                     deadline: float = DEADLINE) -> bool:
    """Let the real event loop run until `reference` has advanced `target`
    times. Returns False if the deadline expired first - the caller turns that
    into a sentence, because it means the loop stalled, not that a dot broke.
    """
    end = time.monotonic() + deadline
    while census.ticks(reference) < target:
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(0.02)
    return True


def test_hidden_avatars_do_not_tick() -> None:
    """The headline. Ten hidden avatars and one visible one: pre-fix each of
    the ten ran its own interval, so they delivered about ten times what the
    visible one did. Now they deliver nothing while it keeps its cadence."""
    with _Census() as census:
        async def _drive():
            app = _Probe()
            async with app.run_test(size=(110, 32)) as pilot:
                t = app.query_one("#t", W.Transcript)
                msgs = [W.AgentMessage() for _ in range(10)]
                for m in msgs:
                    t.mount(m)
                    m.set_avatar_visible(False)
                msgs[-1].set_avatar_visible(True)
                # Deterministic settle: pause() returns when the app is idle,
                # so every mount has landed and any tick from before the last
                # set_avatar_visible has already been counted and is about to
                # be discarded. No sleep can promise that.
                await pilot.pause()
                census.reset()
                reached = await _run_until(census, msgs[-1], REFERENCE_TICKS)
                visible = census.ticks(msgs[-1])
                hidden = sum(census.ticks(m) for m in msgs[:-1])
                assert reached, (
                    f"the visible avatar delivered only {visible} of "
                    f"{REFERENCE_TICKS} ticks in {DEADLINE}s - the event loop "
                    f"stalled, so this run measured nothing")
                assert hidden <= 2, (
                    f"{hidden} ticks from 10 hidden avatars while the visible "
                    f"one ticked {visible} - the hidden ones are animating "
                    f"again (pre-fix this ratio was about 10 to 1)")

        asyncio.run(_drive())


def test_the_visible_avatar_still_animates() -> None:
    """The other direction: the fix must not stop the dot everyone sees."""
    with _Census() as census:
        async def _drive():
            app = _Probe()
            async with app.run_test(size=(110, 32)) as pilot:
                t = app.query_one("#t", W.Transcript)
                msgs = [W.AgentMessage() for _ in range(3)]
                for m in msgs:
                    t.mount(m)
                    m.set_avatar_visible(False)
                msgs[-1].set_avatar_visible(True)
                await pilot.pause()
                census.reset()
                reached = await _run_until(census, msgs[-1], REFERENCE_TICKS)
                assert reached, (
                    f"the visible dot delivered only {census.ticks(msgs[-1])} "
                    f"of {REFERENCE_TICKS} ticks in {DEADLINE}s - it froze")

        asyncio.run(_drive())

    # The RATE the dot animates at cannot be measured on a shared runner (see
    # the module docstring), so the cadence it is designed for is pinned at its
    # source instead: a slower TICK would still deliver ticks and pass the
    # liveness check above while the animation visibly stutters.
    assert W.AgentAvatar.TICK <= 0.15, (
        f"the avatar cadence dropped to {W.AgentAvatar.TICK}s per frame")


def test_a_reshown_avatar_resumes() -> None:
    """The slot moves forward and, on stream-restart, can move BACK to a
    message that was hidden - resume must work, not only the initial state.

    A second, permanently visible avatar is the clock: it says how much the
    loop actually ran while the subject was supposed to stay quiet.
    """
    with _Census() as census:
        async def _drive():
            app = _Probe()
            async with app.run_test(size=(110, 32)) as pilot:
                t = app.query_one("#t", W.Transcript)
                reference = W.AgentMessage()
                subject = W.AgentMessage()
                for m in (reference, subject):
                    t.mount(m)
                reference.set_avatar_visible(True)
                subject.set_avatar_visible(False)
                await pilot.pause()
                census.reset()

                reached = await _run_until(census, reference, REFERENCE_TICKS)
                assert reached, (
                    f"the reference avatar delivered only "
                    f"{census.ticks(reference)} of {REFERENCE_TICKS} ticks in "
                    f"{DEADLINE}s - the event loop stalled, so this run "
                    f"measured nothing")
                assert census.ticks(subject) <= 2, (
                    f"hidden avatar ticked {census.ticks(subject)} times while "
                    f"the reference ticked {census.ticks(reference)}")

                subject.set_avatar_visible(True)
                census.reset()
                resumed = await _run_until(census, subject, RESUME_TICKS)
                assert resumed, (
                    f"reshown avatar delivered only {census.ticks(subject)} of "
                    f"{RESUME_TICKS} ticks in {DEADLINE}s - resume is broken")

        asyncio.run(_drive())


def test_the_oneshot_state_still_reverts_after_reshow() -> None:
    """success/error revert via tick counting; pausing froze the count, and
    that must not strand the state once the avatar is visible again."""
    async def _drive():
        app = _Probe()
        async with app.run_test(size=(110, 32)):
            t = app.query_one("#t", W.Transcript)
            m = W.AgentMessage()
            t.mount(m)
            m.set_avatar_visible(False)
            m.avatar.set_state("success")
            m.set_avatar_visible(True)
            # Poll to a deadline instead of sleeping a fixed span: the revert
            # needs 14 ticks at 10 Hz, and a loaded CI runner delivers them
            # late (measured: green on 3.11/3.12/3.13 and red on 3.10 in the
            # same run, green 5/5 locally). The assertion is about the state
            # reverting AT ALL, so waiting longer costs nothing and a real
            # regression still fails - it never reverts.
            deadline = time.monotonic() + DEADLINE
            while time.monotonic() < deadline:
                if m.avatar._state not in ("success", "error"):
                    break
                await asyncio.sleep(0.1)
            assert m.avatar._state not in ("success", "error"), (
                f"one-shot state never reverted: {m.avatar._state}")

    asyncio.run(_drive())
