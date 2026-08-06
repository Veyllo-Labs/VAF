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
"""
import asyncio

from textual.app import App, ComposeResult

from vaf.cli.tui_app import widgets as W


def _count_ticks(n_messages: int, visible_last: bool = True,
                 seconds: float = 1.0) -> int:
    """Mount n AgentMessages, make only the last avatar visible (the app's
    contract), and count _advance calls over a period."""
    calls = {"n": 0}
    orig = W.AgentAvatar._advance

    def counting(self):
        calls["n"] += 1
        return orig(self)

    W.AgentAvatar._advance = counting
    try:
        class _P(App):
            CSS = "Transcript { height: 1fr; }"

            def compose(self) -> ComposeResult:
                yield W.Transcript(id="t")

        async def _drive():
            app = _P()
            async with app.run_test(size=(110, 32)):
                t = app.query_one("#t", W.Transcript)
                msgs = []
                for _ in range(n_messages):
                    m = W.AgentMessage()
                    t.mount(m)
                    msgs.append(m)
                for m in msgs:
                    m.set_avatar_visible(False)
                if msgs and visible_last:
                    msgs[-1].set_avatar_visible(True)
                await asyncio.sleep(0.1)
                calls["n"] = 0
                await asyncio.sleep(seconds)

        asyncio.run(_drive())
    finally:
        W.AgentAvatar._advance = orig
    return calls["n"]


def test_hidden_avatars_do_not_tick() -> None:
    """The headline. Before the fix this measured ~10 ticks per hidden avatar
    per second; the visible one accounts for everything now (with headroom for
    scheduler jitter)."""
    ticks = _count_ticks(10, visible_last=True, seconds=1.0)
    assert ticks <= 15, (
        f"{ticks} ticks/s for 10 messages with one visible avatar - the "
        f"hidden ones are animating again (pre-fix baseline was ~100)"
    )


def test_the_visible_avatar_still_animates() -> None:
    """The other direction: the fix must not stop the dot everyone sees."""
    ticks = _count_ticks(3, visible_last=True, seconds=1.0)
    assert ticks >= 5, f"only {ticks} ticks/s - the visible dot froze"


def test_a_reshown_avatar_resumes() -> None:
    """The slot moves forward and, on stream-restart, can move BACK to a
    message that was hidden - resume must work, not only the initial state."""
    calls = {"n": 0}
    orig = W.AgentAvatar._advance

    def counting(self):
        calls["n"] += 1
        return orig(self)

    W.AgentAvatar._advance = counting
    try:
        class _P(App):
            def compose(self) -> ComposeResult:
                yield W.Transcript(id="t")

        async def _drive():
            app = _P()
            async with app.run_test(size=(110, 32)):
                t = app.query_one("#t", W.Transcript)
                m = W.AgentMessage()
                t.mount(m)
                m.set_avatar_visible(False)
                await asyncio.sleep(0.5)
                hidden = calls["n"]
                m.set_avatar_visible(True)
                calls["n"] = 0
                await asyncio.sleep(0.8)
                assert hidden <= 2, f"hidden avatar ticked {hidden} times"
                assert calls["n"] >= 4, (
                    f"reshown avatar only ticked {calls['n']} times - "
                    f"resume is broken")

        asyncio.run(_drive())
    finally:
        W.AgentAvatar._advance = orig


def test_the_oneshot_state_still_reverts_after_reshow() -> None:
    """success/error revert via tick counting; pausing froze the count, and
    that must not strand the state once the avatar is visible again."""
    class _P(App):
        def compose(self) -> ComposeResult:
            yield W.Transcript(id="t")

    async def _drive():
        app = _P()
        async with app.run_test(size=(110, 32)):
            t = app.query_one("#t", W.Transcript)
            m = W.AgentMessage()
            t.mount(m)
            m.set_avatar_visible(False)
            m.avatar.set_state("success")
            m.set_avatar_visible(True)
            await asyncio.sleep(1.8)          # 14 ticks at 10 Hz
            assert m.avatar._state not in ("success", "error"), (
                f"one-shot state never reverted: {m.avatar._state}")

    asyncio.run(_drive())
