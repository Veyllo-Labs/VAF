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

What has to hold: the switch saves the first visible bubble, its offset and a key for its
message; the return finds that message again in whatever messages are on screen (the server's
transcript lacks the browser's own log lines, which shifts indices), places it with
`scrollChatToMessage(idx, offset)`, falls back to the number when it is not rendered, and holds
it from the replay until the bottom pin's deadline; a touch, a prompt jump or a send ends the
hold, input of the last 300 ms only postpones it, and `preserveChatScroll` stands down while it
runs.

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
    # Universal newlines: git can check this out with CRLF on the Windows CI runner, and
    # several needles below span lines.
    return PAGE.read_text(encoding="utf-8")


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


# ── which message, after the server's transcript replaced the kept picture ──

_KEPT = """[
    {role: 'user', content: 'Q0'}, {role: 'assistant', content: 'A0'},
    {role: 'system', content: 'Connected to the coder'},
    {role: 'user', content: 'Q1'}, {role: 'assistant', content: 'A1 with a long reply'},
    {role: 'user', content: 'Q2'}, {role: 'assistant', content: 'A2'},
]"""
# What the server sends for the same chat: no browser log line, so everything below it moves
# up by one, and the stored reply text can differ from the streamed one (link conversion).
_SERVER = """[
    {role: 'user', content: 'Q0'}, {role: 'assistant', content: 'A0'},
    {role: 'user', content: 'Q1'}, {role: 'assistant', content: 'A1 with a long reply'},
    {role: 'user', content: 'Q2'}, {role: 'assistant', content: 'A2 (stored)'},
]"""


def test_a_cached_log_line_above_the_message_does_not_move_the_anchor():
    """The kept picture holds the browser's own `system` log lines, which the server never
    sends: after the swap the saved index names the NEXT message (the prompt below the
    answer), and a raw index lookup succeeds on it. MUTATION: resolve to `anchor.idx` - red;
    count `system` rows into the ordinal - red."""
    out = _run(f"""(() => {{
        const kept = {_KEPT}, server = {_SERVER};
        const key = m.anchorKey(kept, 4);
        const anchor = {{idx: 4, offset: -30, key}};
        return {{key, kept: m.resolveAnchor(kept, anchor), server: m.resolveAnchor(server, anchor)}};
    }})()""")
    assert out["key"] == {"role": "assistant", "ordinal": 3, "head": "A1 with a long reply"}
    assert out["kept"] == 4
    assert out["server"] == 3, "the anchor lands on the message below the saved one"


def test_the_same_place_is_found_when_the_stored_text_differs():
    """No log line moved anything, but the stored reply reads differently from the streamed
    one: found by its place, not lost."""
    out = _run(f"""(() => {{
        const kept = {_KEPT}, server = {_SERVER};
        return m.resolveAnchor(server, {{idx: 5, offset: 0, key: m.anchorKey(kept, 6)}});
    }})()""")
    assert out == 5


def test_a_message_that_is_not_there_gives_the_number_back():
    """A browser log line the server's picture does not have, a place past the end, and a
    role that does not match: null, so the caller writes the saved number."""
    out = _run(f"""(() => {{
        const kept = {_KEPT}, server = {_SERVER};
        return [
            m.resolveAnchor(server, {{idx: 2, offset: 0, key: m.anchorKey(kept, 2)}}),
            m.resolveAnchor(server.slice(0, 3), {{idx: 6, offset: 0, key: m.anchorKey(kept, 6)}}),
            m.resolveAnchor(server, {{idx: 3, offset: 0, key: {{role: 'user', ordinal: 3, head: 'x'}}}}),
            m.anchorKey(kept, 99),
        ];
    }})()""")
    assert out == [None, None, None, None]


def test_a_log_line_kept_in_both_is_found_by_its_text():
    """An active chat keeps the browser's log lines next to the server's transcript, possibly
    at another place: found by its text, the nearest one to where it was."""
    out = _run("""(() => {
        const kept = [{role: 'system', content: 'L'}, {role: 'user', content: 'Q'}, {role: 'system', content: 'L'}];
        const now = [{role: 'user', content: 'Q'}, {role: 'system', content: 'L'}, {role: 'system', content: 'L'}];
        return [m.resolveAnchor(now, {idx: 2, offset: 0, key: m.anchorKey(kept, 2)}),
                m.resolveAnchor(now, {idx: 0, offset: 0, key: m.anchorKey(kept, 0)})];
    })()""")
    assert out == [2, 1]


def test_a_twin_below_the_saved_index_is_not_taken_for_the_message():
    """Role and text alone do not tell a message from its twin: a running coder's identical
    tool rows, empty rounds, a prompt sent twice. After a log line above it is dropped, the
    saved index holds the twin BELOW, with the same role and text. The fast path therefore
    also checks the place among the non-`system` rows.
    MUTATION: drop the place check from the fast path - red."""
    out = _run("""(() => {
        const S = (c) => ({role: 'system', content: c}), U = (c) => ({role: 'user', content: c}),
              A = (c) => ({role: 'assistant', content: c}), T = (c) => ({role: 'tool', content: c});
        const tools = [U('Q'), S('L'), T('OK'), T('OK'), T('OK'), A('done')];
        const empty = [U('Q'), S('L'), A(''), A(''), A('done')];
        const asked = [U('Q'), A('A'), S('L1'), S('L2'), U('Q'), A('B'), U('Q'), A('C')];
        const without = (rows) => rows.filter(r => r.role !== 'system');
        const at = (kept, idx) => m.resolveAnchor(without(kept), {idx, offset: 0, key: m.anchorKey(kept, idx)});
        return {second_tool: at(tools, 3), third_tool: at(tools, 4), second_empty_round: at(empty, 3),
                second_prompt: at(asked, 4), third_prompt: at(asked, 6)};
    })()""")
    assert out == {"second_tool": 2, "third_tool": 3, "second_empty_round": 2,
                   "second_prompt": 2, "third_prompt": 4}


def test_a_neighbour_with_the_same_role_and_place_but_other_text_is_not_the_message():
    """A browser log line keeps its text: when another log line stands at the saved index, with
    the same place among the other rows, the line is found by its text elsewhere.
    MUTATION: drop the text check from the fast path - red."""
    out = _run("""(() => {
        const kept = [{role: 'user', content: 'Q'}, {role: 'system', content: 'L1'}, {role: 'system', content: 'L2'}];
        const now = [{role: 'user', content: 'Q'}, {role: 'system', content: 'L2'}, {role: 'system', content: 'X'}];
        return m.resolveAnchor(now, {idx: 2, offset: 0, key: m.anchorKey(kept, 2)});
    })()""")
    assert out == 1


def test_an_anchor_without_a_key_keeps_its_index():
    out = _run("""[m.resolveAnchor([{role: 'user'}, {role: 'user'}], {idx: 1, offset: 0}),
                   m.resolveAnchor([{role: 'user'}], {idx: 1, offset: 0})]""")
    assert out == [1, None]


# ── the wiring in page.tsx, pinned at the source ───────────────────────────


def test_the_switch_saves_the_anchor_and_the_return_places_it():
    """MUTATION: drop the anchor from the save - red; restore the number before trying the
    anchor - red; drop the numeric fallback - red; place the saved index unresolved - red."""
    src = _page()
    save = _between(src, "sessionScrollPositions.current[currentSessionId] = {", "};")
    assert "anchor: readChatAnchor(messages)," in save, "the switch saves only the number again"
    assert "type SavedChatScroll = { scrollTop: number; wasAtBottom: boolean; anchor: ChatAnchor | null };" in src

    effect = _between(src, "const grew = _chatGrew();", "}, [messages, loading,")
    restore = effect.split("} else if (isAtBottomRef.current && grew) {", 1)[0]
    anchored = ("const anchor = restore !== 'bottom' && !restore.wasAtBottom ? restore.anchor : null;\n"
                "                const at = anchor ? resolveAnchor(messages, anchor) : null;\n"
                "                if (anchor && at !== null && scrollChatToMessage(at, anchor.offset)) return;")
    assert anchored in restore, "the restore no longer places the saved message where it is now"
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
    reader = _between(src, "const readChatAnchor = useCallback((shown: Message[]): ChatAnchor | null => {", "}, []);")
    assert "querySelectorAll<HTMLElement>('[data-msg-idx]')" in reader
    assert "const picked = pickAnchor(rows, c.getBoundingClientRect().top);" in reader
    assert "return picked && { ...picked, key: anchorKey(shown, picked.idx) ?? undefined };" in reader, \
        "the saved anchor carries no key - the server's transcript cannot find its message"
    assert "from '@/components/chat/chatAnchor';" in src


def test_the_held_index_is_resolved_on_every_commit():
    """The guard measures the bubble carrying `back.idx` in the DOM it holds, so the index is
    re-resolved in a layout effect, after the commit and before the guard's next frame.
    MUTATION: hold `back.saved.anchor.idx` - red; resolve in a passive effect - red; drop the
    switch's first resolution - red."""
    src = _page()
    effect = _between(src, "useLayoutEffect(() => {\n        const back = returnAnchorRef.current;", "}, [messages]);")
    assert "if (back) back.idx = resolveAnchor(messages, back.saved.anchor);" in effect
    switch = _between(src, "const handleSessionSwitch = (id: string) => {", "const switchAfterDelete")
    assert "armed: false, idx: resolveAnchor(cached, savedAnchor) }" in switch
    keeper = _between(src, "const tick = () => {", "raf = requestAnimationFrame(tick);")
    assert "const row = back.idx === null ? null : el.querySelector<HTMLElement>(`[data-msg-idx=\"${back.idx}\"]`);" \
        in keeper, "the guard holds an index that was never resolved against the messages on screen"
    assert "back.saved.anchor.idx" not in keeper


def test_the_return_hold_lives_from_the_replay_to_the_pin_deadline():
    """The anchor counterpart of the bottom stick. MUTATION: `!back.armed` in the hold - red;
    drop the deadline from the hold - red; the switch arming it for a chat left at its bottom -
    red; a replay that does not arm the deadline - red; a guard that never ends it after the
    deadline - red."""
    src = _page()
    switch = _between(src, "const handleSessionSwitch = (id: string) => {", "const switchAfterDelete")
    assert "const savedAnchor = savedScroll && !savedScroll.wasAtBottom ? savedScroll.anchor : null;" in switch
    assert "? { sid: id, saved: { ...savedScroll, anchor: savedAnchor }, armed: false," in switch
    assert "bottomPinUntilRef.current = 0;" in switch.split("const savedAnchor", 1)[1], \
        "the pin of the chat being left runs on into the one switched to"

    held = _between(src, "const heldReturnAnchor = () => {", "};")
    assert "return back && back.armed && back.sid === currentSessionIdRef.current\n" in held, \
        "the hold no longer runs exactly while it is armed, for the open chat"
    assert "&& bottomPinUntilRef.current > performance.now() ? back : null;" in held, \
        "the hold no longer ends at its deadline"

    replay = _between(src, "if (data.type === 'live_replay') {", "if (data.type === 'new_log')")
    arm = replay.split("if (back && back.sid === data.sessionId && !back.armed) {", 1)
    assert len(arm) == 2, "the replay no longer arms the return hold"
    body = arm[1].split("}", 1)[0]
    assert "back.armed = true;" in body
    assert "bottomPinUntilRef.current = performance.now() + BOTTOM_PIN_MS;" in body

    keeper = _between(src, "const tick = () => {", "raf = requestAnimationFrame(tick);")
    end = "} else if (returnAnchorRef.current?.armed && bottomPinUntilRef.current <= performance.now()) {"
    assert end in keeper, "the hold is not ended by its deadline, or ended by something else"
    assert keeper.split(end, 1)[1].lstrip().split("\n", 1)[0].startswith("//")
    assert "returnAnchorRef.current = null;" in keeper.split(end, 1)[1].split("}", 1)[0]


def test_input_inside_the_cooldown_postpones_the_hold_without_ending_it():
    """A scroll in the chat being left, then this chat's replay within 300 ms: the guard's pin
    branch is skipped for the cooldown, and an `else` that cleared the armed anchor there
    handed the rest of the deadline to the bottom pin, which pulled the chat to its bottom.
    MUTATION: drop the deadline check from the clear - red."""
    src = _page()
    keeper = _between(src, "const tick = () => {", "raf = requestAnimationFrame(tick);")
    assert "} else if (returnAnchorRef.current?.armed) {" not in keeper, \
        "the armed anchor is cleared while the 300 ms after a person's input only postpone it"
    assert "returnAnchorRef.current?.armed && bottomPinUntilRef.current <= performance.now()" in keeper


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
    """MUTATION: drop `endReturnHold()` from `human()` - red; hand-copy a clear into it again -
    red; hold the bottom for a chat that came back scrolled up - red."""
    src = _page()
    human = _between(src, "const human = () => {", "};")
    assert "endReturnHold();" in human, \
        "a wheel or touch no longer ends the return hold - the view is pulled back to the anchor"
    for copy in ("returnAnchorRef.current = null;", "stickToBottomRef.current = null;",
                 "bottomPinUntilRef.current = 0;"):
        assert copy not in human, f"`human()` hand-copies `{copy}` beside the one helper again"

    keeper = _between(src, "const tick = () => {", "raf = requestAnimationFrame(tick);")
    pin = keeper.split("if (bottomPinUntilRef.current > performance.now() && performance.now() - lastHuman >= 300) {", 1)
    assert len(pin) == 2
    branch = pin[1].split("} else if (returnAnchorRef.current?.armed", 1)[0]
    assert branch.index("const back = heldReturnAnchor();") \
        < branch.index("scrollIntentRef.current = Math.max(0, el.scrollHeight - el.clientHeight);"), \
        "the bottom is held before the anchor is asked"
    anchored = branch.split("if (back) {", 1)[1].split("} else {", 1)[0]
    assert "back.idx" in anchored
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


def test_a_prompt_jump_before_the_replay_is_not_undone_by_the_bottom_stick():
    """A chat left at its bottom: the switch sets the stick, and the replay parks 'bottom' and
    arms the pin for it. A prompt jump made in between used to end only the anchor and the
    pin, so the replay then pulled the view from the chosen prompt to the bottom.
    MUTATION: let the helper keep the stick - red."""
    src = _page()
    helper = _between(src, "const endReturnHold = () => {", "};")
    assert "stickToBottomRef.current = null;" in helper, \
        "a prompt jump before the replay leaves the stick, and the replay parks the bottom over it"
    jump = _between(src, "const jump = (turnPos: number, trueIdx: number) => {", "scrollChatToMessage(trueIdx);")
    assert jump.index("endReturnHold();") < jump.index("pendingJumpRef.current = trueIdx;"), \
        "the jump parks its target before it ends the return"
    # What the stick does at the replay is only ever conditional on it.
    replay = _between(src, "if (data.type === 'live_replay') {", "if (data.type === 'new_log')")
    parks = replay.split("if (stickToBottomRef.current === data.sessionId) {", 1)
    assert len(parks) == 2 and "pendingScrollRestore.current = 'bottom';" in parks[1].split("}", 1)[0]
    assert "pendingScrollRestore.current = 'bottom';" not in parks[0]


def test_the_person_asking_for_another_place_ends_the_return_hold():
    """A prompt jump and a send are the person asking for another place, and neither touches
    the scroller: the rail and the composer sit outside it. Without this the hold pulled the
    view back to the saved message every frame until its deadline; measured for a send 0.3 s
    after a return, desktop and 390 px: the new bubble stayed out of view.
    MUTATION: drop the call from the jump - red; from the chat's send - red; from the room's
    send - red; let the helper keep the pin - red."""
    src = _page()
    helper = _between(src, "const endReturnHold = () => {", "};")
    assert "returnAnchorRef.current = null;" in helper
    assert "bottomPinUntilRef.current = 0;" in helper
    # One clear for every way out: the helper is the only place that drops the anchor or the
    # stick outside the switch, the replay's own end of the stick and the deadline.
    assert src.count("returnAnchorRef.current = null;") == 2, \
        "the return anchor is cleared by hand somewhere beside endReturnHold and the deadline"

    jump = _between(src, "const jump = (turnPos: number, trueIdx: number) => {", "scrollChatToMessage(trueIdx);")
    assert "endReturnHold();" in jump
    # The navigator still centres: no offset passed.
    assert "scrollChatToMessage(trueIdx);" in src

    chat = _between(src, "const sendMessage = async (e?: React.FormEvent, overrideText?: string) => {",
                    "expectNewAssistantRef.current = true;")
    assert chat.rstrip().endswith("isAtBottomRef.current = true;\n        endReturnHold();"), \
        "the chat's send follows the answer but leaves a return hold running"
    room = _between(src, "const sendToAppRoom = async (text: string, resendOf?: string) => {",
                    "setAppRoomSending(next);")
    assert "isAtBottomRef.current = true;\n        endReturnHold();" in room, \
        "the Veyllo App room's send follows its line but leaves a return hold running"
