# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A chat left scrolled up comes back at the same message (web/components/chat/chatAnchor.ts,
web/app/page.tsx).

The defect: the switch saved a raw scrollTop. It lands on the same content only when the
column has the width it had when it was saved, and the agent window decides that width: it
reopens after the return (narrowing the column over 300 ms) or stays closed because its worker
finished. Worse, the replayed window opened through `preserveChatScroll`, which took the
height change between its call and the next frame for content added ABOVE the view, while the
server's picture is shorter at its END than the kept one: 84 px off, measured on a running chat
with its coder window, desktop and 390 px alike.

What has to hold: the switch saves the first visible bubble and its offset; the return places
that bubble there again (`scrollChatToMessage(idx, offset)`), falls back to the number when the
bubble is not rendered, and holds it from the replay until the bottom pin's deadline; a touch
or a prompt jump ends the hold; `preserveChatScroll` stands down while it runs.

The helpers are the web's REAL code, transpiled with the app's own TypeScript and run in node
(the pattern of tests/test_live_view_web.py); skipped without node. The wiring lives in
page.tsx's effects and socket handler, so it is pinned at the source.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = ROOT / "web" / "components" / "chat" / "chatAnchor.ts"
PAGE = ROOT / "web" / "app" / "page.tsx"
_TS = ROOT / "web" / "node_modules" / "typescript"


def _run(body: str):
    """Run `body` (a JS expression over chatAnchor.ts's exports `m`) in node, return its JSON."""
    if shutil.which("node") is None or not _TS.is_dir():
        pytest.skip("node or web/node_modules/typescript is not installed")
    script = (
        "const ts = require(process.argv[1]);"
        "const fs = require('fs');"
        "const src = fs.readFileSync(process.argv[2], 'utf8');"
        "const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS,"
        " target: ts.ScriptTarget.ES2019}}).outputText;"
        "const mod = {exports: {}}; new Function('module', 'exports', 'require', js)(mod, mod.exports, require);"
        "const m = mod.exports;"
        "const out = (() => (" + body + "))();"
        "process.stdout.write(JSON.stringify(out === undefined ? null : out));"
    )
    res = subprocess.run(["node", "-e", script, str(_TS), str(ANCHOR)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _page():
    # CRLF-normalised: git can check this out with CRLF on the Windows CI runner.
    return PAGE.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _between(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i + len(start))]


# ── the geometry, run in node ──────────────────────────────────────────────


def test_the_bubble_straddling_the_top_edge_is_the_anchor():
    """MUTATION: `bottom > viewTop + 1` -> `top >= viewTop` picks the next bubble - red."""
    out = _run("""m.pickAnchor([
        {idx: 40, top: 0, bottom: 80},
        {idx: 41, top: 80, bottom: 90},
        {idx: 42, top: 90, bottom: 300},
        {idx: 43, top: 300, bottom: 420},
    ], 100)""")
    assert out == {"idx": 42, "offset": -10}


def test_a_bubble_ending_at_the_edge_is_not_visible():
    """Ends exactly at the edge, or within the pixel of rounding: skipped."""
    out = _run("""[
        m.pickAnchor([{idx: 1, top: 20, bottom: 100}, {idx: 2, top: 100, bottom: 200}], 100),
        m.pickAnchor([{idx: 1, top: 20, bottom: 101}, {idx: 2, top: 101, bottom: 200}], 100),
        m.pickAnchor([{idx: 1, top: 20, bottom: 101.5}, {idx: 2, top: 101.5, bottom: 200}], 100),
    ]""")
    assert out == [{"idx": 2, "offset": 0}, {"idx": 2, "offset": 1}, {"idx": 1, "offset": -80}]


def test_no_rendered_bubble_gives_no_anchor():
    assert _run("[m.pickAnchor([], 0), m.pickAnchor([{idx: 3, top: -90, bottom: -10}], 0)]") == [None, None]


def test_the_rows_are_taken_in_dom_order_not_by_index():
    """The idx is a position in the FULL messages array; the order on screen is the DOM's."""
    out = _run("""m.pickAnchor([{idx: 7, top: 50, bottom: 150}, {idx: 5, top: 150, bottom: 250}], 100)""")
    assert out == {"idx": 7, "offset": -50}


def test_the_anchored_position_follows_the_bubble():
    """The measured case: saved 51 at -174; after the column changed, 51 sits at -90 at the
    same scrollTop, so the view has to move 84 px further down to show it at -174 again.
    MUTATION: a sign flip of `offset` - red."""
    out = _run("""[
        m.anchoredScrollTop(1442, -90 + 60, 60, -174),
        m.anchoredScrollTop(1442, -174 + 60, 60, -174),
        m.anchoredScrollTop(0, 300, 60, 20),
    ]""")
    assert out == [1526, 1442, 220]


# ── the wiring in page.tsx, pinned at the source ───────────────────────────


def test_the_switch_saves_the_anchor_and_the_return_places_it():
    """MUTATION: drop the anchor from the save - red; restore the number before trying the
    anchor - red; drop the numeric fallback - red."""
    src = _page()
    save = _between(src, "sessionScrollPositions.current[currentSessionId] = {", "};")
    assert "anchor: readChatAnchor()," in save, "the switch saves only the number again"
    assert "type SavedChatScroll = { scrollTop: number; wasAtBottom: boolean; anchor: ChatAnchor | null };" in src

    effect = _between(src, "const grew = _chatGrew();", "}, [messages, loading,")
    restore = effect.split("} else if (isAtBottomRef.current && grew) {", 1)[0]
    anchored = ("if (restore !== 'bottom' && !restore.wasAtBottom && restore.anchor\n"
                "                    && scrollChatToMessage(restore.anchor.idx, restore.anchor.offset)) return;")
    assert anchored in restore, "the restore no longer places the saved message"
    fallback = "setChatScroll(restore === 'bottom' || restore.wasAtBottom"
    assert restore.index(anchored) < restore.index(fallback), \
        "the number is written before the anchor is tried"
    assert ": restore.scrollTop);" in restore.split(fallback, 1)[1], \
        "the numeric fallback for a bubble outside the virtual window is gone"
    # The parked follow of a room line stays first and untouched.
    assert restore.index("'followFrom' in restore") < restore.index(anchored)


def test_scroll_to_message_with_an_offset_puts_the_top_there():
    """Without an offset the prompt navigator's centring is unchanged."""
    src = _page()
    helper = _between(src, "const scrollChatToMessage = useCallback((trueIdx: number, offset?: number) => {",
                      "}, [setChatScroll]);")
    assert "if (offset !== undefined) {" in helper
    assert "setChatScroll(anchoredScrollTop(c.scrollTop, rowTop, viewTop, offset));" in helper
    assert "setChatScroll(c.scrollTop + (rowTop - viewTop) - Math.max(0, (c.clientHeight - el.offsetHeight) / 2));" \
        in helper, "the centred jump changed"
    reader = _between(src, "const readChatAnchor = useCallback((): ChatAnchor | null => {", "}, []);")
    assert "querySelectorAll<HTMLElement>('[data-msg-idx]')" in reader
    assert "return pickAnchor(rows, c.getBoundingClientRect().top);" in reader
    assert "from '@/components/chat/chatAnchor';" in src


def test_the_return_hold_lives_from_the_replay_to_the_pin_deadline():
    """The anchor counterpart of the bottom stick. MUTATION: the switch arming it for a chat
    left at its bottom - red; a replay that does not arm the deadline - red; a guard that
    never ends it after the deadline - red."""
    src = _page()
    switch = _between(src, "const handleSessionSwitch = (id: string) => {", "const switchAfterDelete")
    assert "const savedAnchor = savedScroll && !savedScroll.wasAtBottom ? savedScroll.anchor : null;" in switch
    assert "? { sid: id, saved: { ...savedScroll, anchor: savedAnchor }, armed: false }" in switch
    assert "bottomPinUntilRef.current = 0;" in switch.split("const savedAnchor", 1)[1], \
        "the pin of the chat being left runs on into the one switched to"

    held = _between(src, "const heldReturnAnchor = () => {", "};")
    for cond in ("back.armed", "back.sid === currentSessionIdRef.current",
                 "bottomPinUntilRef.current > performance.now()"):
        assert cond in held, f"the hold no longer checks {cond}"

    replay = _between(src, "if (data.type === 'live_replay') {", "if (data.type === 'new_log')")
    arm = replay.split("if (back && back.sid === data.sessionId && !back.armed) {", 1)
    assert len(arm) == 2, "the replay no longer arms the return hold"
    body = arm[1].split("}", 1)[0]
    assert "back.armed = true;" in body
    assert "bottomPinUntilRef.current = performance.now() + BOTTOM_PIN_MS;" in body

    keeper = _between(src, "const tick = () => {", "raf = requestAnimationFrame(tick);")
    assert "} else if (returnAnchorRef.current?.armed) {" in keeper
    assert "returnAnchorRef.current = null;" in keeper.split("} else if (returnAnchorRef.current?.armed) {", 1)[1]


def test_the_transcript_of_the_chat_coming_back_is_placed_by_its_anchor():
    """Both history_update sites that park 'bottom' for the stick park the saved position
    for the anchor. MUTATION: drop one - red."""
    src = _page()
    hist = _between(src, "else if (data.type === 'history_update')", "else if (data.type === 'context_checkpoint')")
    stick = "if (stickToBottomRef.current === data.sessionId) pendingScrollRestore.current = 'bottom';"
    assert hist.count(stick + "\n") == 2
    assert hist.count("parkReturnAnchor(data.sessionId);") == 2
    for part in hist.split(stick)[1:]:
        assert part.lstrip().startswith("parkReturnAnchor(data.sessionId);"), \
            "a transcript parks the bottom for the stick but not the position for the anchor"
    park = _between(src, "const parkReturnAnchor = (sid: string) => {", "};")
    assert "if (back && back.sid === sid) pendingScrollRestore.current = back.saved;" in park


def test_the_guard_holds_the_message_and_a_touch_ends_it():
    """MUTATION: delete the clear in `human()` - red; hold the bottom for a chat that came
    back scrolled up - red."""
    src = _page()
    human = _between(src, "const human = () => {", "};")
    stick = human.index("stickToBottomRef.current = null;")
    assert "returnAnchorRef.current = null;" in human[stick:], \
        "a wheel or touch no longer ends the return hold - the view is pulled back to the anchor"

    keeper = _between(src, "const tick = () => {", "raf = requestAnimationFrame(tick);")
    pin = keeper.split("if (bottomPinUntilRef.current > performance.now() && performance.now() - lastHuman >= 300) {", 1)
    assert len(pin) == 2
    branch = pin[1].split("} else if (returnAnchorRef.current?.armed) {", 1)[0]
    assert branch.index("const back = heldReturnAnchor();") \
        < branch.index("scrollIntentRef.current = Math.max(0, el.scrollHeight - el.clientHeight);"), \
        "the bottom is held before the anchor is asked"
    anchored = branch.split("if (back) {", 1)[1].split("} else {", 1)[0]
    assert "back.saved.anchor.idx" in anchored
    assert "anchoredScrollTop(el.scrollTop, row.getBoundingClientRect().top," in anchored
    assert "back.saved.anchor.offset" in anchored
    assert "scrollIntentRef.current = Math.min(Math.max(0, el.scrollHeight - el.clientHeight), Math.max(0, target));" \
        in anchored, "the anchored intent is not clamped like every other one"
    assert "tolerance = 1;" in anchored
    assert "Math.abs(now - want) > tolerance)" in keeper


def test_preserve_chat_scroll_stands_down_during_the_hold():
    """MUTATION: remove the check - red; move it out of the frame (before the replay arms the
    hold, which happens after the window reopened) - red."""
    src = _page()
    body = _between(src, "const preserveChatScroll = (update: () => void) => {", "const appendSubAgentLine")
    frame = body.split("requestAnimationFrame(() => {", 1)
    assert len(frame) == 2
    assert "heldReturnAnchor()" not in frame[0], "the hold is read when the window opens, before the replay arms it"
    cb = frame[1]
    assert "if (heldReturnAnchor()) return;" in cb, "preserveChatScroll writes over the return hold again"
    assert cb.index("if (heldReturnAnchor()) return;") < cb.index("setChatScroll("), \
        "preserveChatScroll writes before it checks the hold"
    # Outside the hold it is what it was.
    assert "setChatScroll(prevScrollTop + scrollDelta);" in cb
    assert "setChatScroll(nextContainer.scrollHeight);" in cb


def test_a_prompt_jump_ends_the_return_hold():
    """The rail sits outside the column, so its click is no touch of the scroller; without
    this the hold pulled the view back from the jump. MUTATION: drop the clear - red."""
    src = _page()
    jump = _between(src, "const jump = (turnPos: number, trueIdx: number) => {", "scrollChatToMessage(trueIdx);")
    assert "returnAnchorRef.current = null;" in jump
    assert "bottomPinUntilRef.current = 0;" in jump
    # The navigator still centres: no offset passed.
    assert "scrollChatToMessage(trueIdx);" in src
