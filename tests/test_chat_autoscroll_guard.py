# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Guard for the chat auto-scroll in web/app/page.tsx.

The regression this pins: the effect that attaches the scroll listener carried an
EMPTY dependency list. Three early returns sit above the chat column (authChecking,
authError, !isAuthenticated), so on the first commit the column does not exist yet,
`containerRef.current` is null and the effect bails out. With no dependencies it
never ran again, which made the failure permanent and invisible: `isAtBottomRef`
kept its initial `true` for the life of the page, so the auto-scroll fired on every
streamed chunk regardless of where the person had scrolled to. Reading anything
further up while an answer streamed was impossible, the view was dragged back down.

A text assertion rather than a rendered test: there is no JS test runner in this
repo, and the property being pinned is a property of the source, not of a render.
"""
import re
from pathlib import Path

_PAGE = Path(__file__).resolve().parent.parent / "web" / "app" / "page.tsx"
_CSS = Path(__file__).resolve().parent.parent / "web" / "app" / "globals.css"


def _source() -> str:
    return _PAGE.read_text(encoding="utf-8")


def test_the_scroll_listener_effect_is_reattached_when_the_chat_appears():
    src = _source()
    marker = "container.addEventListener('scroll', handleScroll"
    assert marker in src, "the chat scroll listener is gone"
    # the dependency list closing this effect is the first `}, [...]);` after the marker
    tail = src.split(marker, 1)[1]
    m = re.search(r"\}, \[([^\]]*)\]\);", tail)
    assert m, "could not find the dependency list of the scroll listener effect"
    deps = [d.strip() for d in m.group(1).split(",") if d.strip()]
    assert deps, (
        "the scroll listener effect has an empty dependency list: it runs once, before "
        "the auth gates have released the chat column, finds no container and never "
        "retries - leaving the at-bottom detection dead and the auto-scroll unconditional"
    )
    for gate in ("authChecking", "authError", "isAuthenticated"):
        assert gate in deps, (
            f"{gate} guards an early return above the chat column but is not a dependency "
            f"of the scroll listener effect, so the listener is not attached when that "
            f"gate releases. Dependencies found: {deps}"
        )


def test_the_at_bottom_check_still_guards_the_auto_scroll():
    """The listener only matters because the auto-scroll asks it before scrolling.
    If that condition is ever dropped, attaching the listener buys nothing."""
    src = _source()
    assert "isAtBottomRef.current = scrollTop + clientHeight >= scrollHeight" in src, \
        "the at-bottom detection no longer writes isAtBottomRef"
    assert re.search(r"else if \(isAtBottomRef\.current\b", src), \
        "the auto-scroll no longer checks isAtBottomRef before scrolling"


def test_sending_a_message_returns_the_view_to_the_bottom():
    """The other half of the same behaviour, and the one that was missing.

    The auto-scroll only ever CONTINUES to stick to the bottom; nothing ever
    returned it there. So once the at-bottom detection worked (see above), anyone
    who had scrolled up to look at an earlier message stayed up there while their
    own message and the entire reply appeared out of sight below. Measured in a
    live UI: 0 scroll calls over a whole turn, view parked at top=0 while the
    content grew to 1295px in an 849px window.

    Sending is an explicit "show me what happens next", so it re-arms the anchor.
    Reading further up DURING a stream must still hold the view, which is the
    listener's job and is pinned by the test above.
    """
    src = _source()
    marker = "expectNewAssistantRef.current = true;"
    assert marker in src, "the send path changed shape"
    # the anchor has to be re-armed in the same block that appends the user message
    before = src.split(marker, 1)[0][-700:]
    assert "isAtBottomRef.current = true;" in before, (
        "sending a message no longer returns the view to the bottom, so the person "
        "who scrolled up never sees their own message or the reply"
    )


def test_the_auto_scroll_sets_the_position_instead_of_animating():
    """Smooth scrolling is not merely slow here, it does nothing at all.

    Measured in the desktop window against the chat container: the identical call
    with behavior:'smooth' moved it 0px, the same target without smooth moved the
    full 1163px, and a direct scrollTop assignment moved it 1315px. So the auto
    scroll ran on every chunk and the view never went anywhere, which is what made
    the chat look like it had stopped following the answer.

    It would be the wrong tool even where it works: the stream emits every 80ms
    (headless_runner.STREAM_EMIT_THROTTLE_SEC) while a smooth animation needs
    300-500ms, so every one of them is retargeted long before it arrives.
    """
    src = _source()
    block = src.split("else if (isAtBottomRef.current", 1)
    assert len(block) == 2, "the auto-scroll branch changed shape"
    body = block[1].split("}, [messages", 1)[0]
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("//"))
    assert "smooth" not in code, (
        "the auto-scroll asks for a smooth scroll again; measured to move the view "
        "0px in the desktop window while a direct assignment moves it fully"
    )
    assert "setChatScroll(" in code, (
        "the auto-scroll no longer sets a position through the shared setter, so it "
        "either does not scroll at all or scrolls without declaring the intent - and "
        "an undeclared position is put back by the guard within a frame"
    )


def test_scroll_anchoring_stays_off_for_the_chat_column():
    """The browser must not scroll the chat on its own.

    Scroll anchoring silently adjusts scrollTop to keep a chosen element in place
    when content around it resizes. In a streaming chat that drags the view back
    down while the person reads further up, and it is invisible from the code:
    measured here as 57 scroll events with ZERO scrollTop writes of our own.
    Switching it off made the view stay where it was put.

    Following the newest message is the auto-scroll's job, which sets the position
    explicitly. It must not ALSO be the browser's, or the two fight each other.
    """
    css = _CSS.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in css.splitlines() if not ln.lstrip().startswith(("/*", "*", "//")))
    assert "overflow-anchor: none" in code, \
        "scroll anchoring is enabled again for the chat column"
    # both the container and its contents: an inner element can anchor just as well
    assert ".vaf-chat-col * { overflow-anchor: none; }" in code, \
        "only the container is exempt; a child element can still act as the anchor"


# ---------------------------------------------------------------------------
# The position guard, and what it does to every OTHER scroll in the file
# ---------------------------------------------------------------------------


def _chat_container_scroll_writes(src: str):
    """Every `<name>.scrollTop = ...` whose `<name>` was bound from containerRef.

    The chat column is not the only scroller on the page (the suggestion list, the
    thinking panel and the report list have their own), so the receiver is resolved
    back to its nearest preceding binding rather than matched by name.
    """
    out = []
    for m in re.finditer(r"(\w+)\.scrollTop\s*=[^=]", src):
        name = m.group(1)
        binds = list(re.finditer(
            r"(?:const|let)\s+" + re.escape(name) + r"\s*=\s*([^;\n]+)", src[:m.start()]
        ))
        if not binds:
            continue
        if "containerRef" in binds[-1].group(1):
            out.append(m)
    return out


def test_every_chat_scroll_declares_the_position_as_the_intent():
    """A write that does not declare the intent is undone before the next paint.

    The chat column holds the position it was put in: a per-frame loop remembers the
    intended scrollTop and restores it whenever the view moved without a person
    touching the wheel, a key or the surface, because the engine moves it on its own
    (measured in the tray window). The loop cannot tell our writes from the engine's.

    So a programmatic scroll that only assigns scrollTop scrolls and is reverted in
    the same frame, which on screen is a control that does nothing. That is what
    happened to the prompt navigator, to the session-switch scroll restore and to the
    sub-agent panel's height preservation: three of the four scroll sites in this file
    wrote the position and never the intent, while only the auto-scroll did both.

    Pinned as a property of every site rather than of the three that were fixed, so a
    fifth one cannot be added by hand with the same omission.
    """
    src = _source()
    writes = _chat_container_scroll_writes(src)
    assert writes, "no scroll write on the chat container found at all - did it move?"
    for m in writes:
        window = src[max(0, m.start() - 300):m.start()]
        assert "scrollIntentRef.current" in window, (
            "a scrollTop write on the chat container does not declare the new position "
            "as the scroll intent, so the position guard undoes it within a frame:\n"
            + src[max(0, m.start() - 200):m.end() + 60]
        )


def test_the_position_guard_still_runs_and_still_yields_to_a_person():
    src = _source()
    assert "const recentlyHuman = performance.now() - lastHuman < 300;" in src, \
        "the position guard no longer distinguishes an engine move from a person's"
    assert re.search(r"evts\.forEach\(e => el\.addEventListener\(e, human", src), \
        "the guard no longer listens for real interaction, so it would fight the user"


def test_the_prompt_navigator_jumps_through_the_shared_helper():
    """The right-edge rail's click, which is what made the guard's reach visible.

    It used to call scrollIntoView({behavior:'smooth'}) directly. Two things were
    wrong with that at once: smooth scrolling on this very container was measured to
    move it 0px in the desktop window, and the guard reverted whatever it did move,
    because a click on the rail is not an interaction with the scroller and therefore
    never counts as human. The rail sits OUTSIDE the column it scrolls.
    """
    src = _source()
    block = src.split("Prompt navigator:", 1)
    assert len(block) == 2, "the prompt navigator is gone or was renamed"
    body = block[1].split("})()}", 1)[0]
    assert "scrollIntoView" not in body, (
        "the prompt navigator asks for scrollIntoView again: measured to move this "
        "container 0px, and undone by the position guard where it does move"
    )
    assert "scrollChatToMessage(" in body, \
        "the prompt navigator no longer jumps through the shared helper"


def test_the_prompt_navigator_widens_the_window_by_turns():
    """The window is cut by user TURNS, and the rail has to speak the same unit.

    visibleMessages keeps the last MSG_TURNS + msgOffset user prompts. The rail's old
    arithmetic subtracted that same constant from a MESSAGE count, so on a normal
    conversation - several messages per turn - it decided that visible prompts were
    hidden and revealed the entire history to reach one of them.
    """
    src = _source()
    body = src.split("Prompt navigator:", 1)[1].split("})()}", 1)[0]
    assert "turnIdxs.length - MSG_TURNS - turnPos" in body, (
        "the prompt navigator no longer computes the needed offset in turns; a "
        "message-count subtraction here reveals the whole history on most clicks"
    )
    assert "filteredMessages.length - MSG_PAGE_SIZE" not in body, \
        "the message-count arithmetic is back"


def test_the_navigator_jump_waits_for_the_render_instead_of_a_timer():
    """Widening the window is a state change; the bubble exists one commit later.

    The previous version waited a fixed 80ms and then queried the DOM. A commit that
    took longer than that - a long conversation being re-rendered is exactly that
    case - left the query empty and the click did nothing at all.
    """
    src = _source()
    assert "pendingJumpRef" in src, "the parked jump is gone"
    marker = "pendingJumpRef.current = null;"
    assert marker in src, "the parked jump is never consumed"
    effect = src.split("Run the prompt navigator's parked jump", 1)
    assert len(effect) == 2, "the effect that runs the parked jump is gone"
    body = effect[1].split("}, [", 1)
    assert len(body) == 2, "could not find the dependency list of the parked-jump effect"
    deps = [d.strip() for d in body[1].split("]", 1)[0].split(",") if d.strip()]
    assert "visibleMessages" in deps and "msgOffset" in deps, (
        "the parked jump no longer waits on the window that has to widen first; "
        f"dependencies found: {deps}"
    )
    assert "setTimeout" not in effect[1].split("}, [", 1)[0], \
        "the jump is back on a timer instead of on the render it is waiting for"
