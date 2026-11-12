# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The browser's half of "an active chat looks the same when you come back to it"
(web/components/chat/liveView.ts, web/lib/sessionCache.ts, web/app/page.tsx).

The helpers are the web's REAL code, transpiled with the app's own TypeScript and run in node
(the pattern of tests/test_turn_rounds_web.py); skipped without node. The wiring in page.tsx
lives inside one React effect, so it is pinned at the source.

What has to hold: the server wins (a kept "generating" flag never outvotes the server outside
the short window after a local send, a kept card or bubble is never re-added); history rows
carry their turn and their stored files; a replayed frame is quiet; a reconnect keeps the open
chat; a dropped socket clears every preview place; a chip of one chat never lands in another;
the composer is per chat and in memory; no chat message is written to browser storage.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "web" / "components" / "chat" / "liveView.ts"
CACHE = ROOT / "web" / "lib" / "sessionCache.ts"
PAGE = ROOT / "web" / "app" / "page.tsx"
_TS = ROOT / "web" / "node_modules" / "typescript"


def _run(module: Path, body: str, setup: str = ""):
    """Run `body` (a JS expression over the module's exports `m`) in node, return its JSON."""
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
        + setup +
        "const out = (() => (" + body + "))();"
        "process.stdout.write(JSON.stringify(out === undefined ? null : out));"
    )
    res = subprocess.run(["node", "-e", script, str(_TS), str(module)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _page():
    return PAGE.read_text(encoding="utf-8")


def _between(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i + len(start))]


# ── AC8: history rows carry their turn and their files ───────────────────────────

def test_history_message_forwards_turn_and_files():
    """MUTATION: historyMessage without downloadFiles - red."""
    wire = {"role": "assistant", "content": "Here.", "timestamp": "2026-01-01T10:00:00",
            "turnId": "t1", "round": 2, "roundAnswer": True,
            "files": [{"path": "/w/report.md", "name": "report.md"},
                      {"path": "/w/shot.png", "name": "shot.png", "purpose": "check", "toolId": "c1"},
                      "junk", {"name": "no path"}]}
    out = _run(LIVE, f"m.historyMessage({json.dumps(wire)}, 4, 99)")
    assert out["turnId"] == "t1" and out["_order"] == 4
    assert out["round"] == 2 and out["roundAnswer"] is True
    assert out["downloadFiles"] == [{"path": "/w/report.md", "name": "report.md"},
                                    {"path": "/w/shot.png", "name": "shot.png",
                                     "purpose": "check", "toolId": "c1"}]


def test_history_message_without_files_or_turn_is_unchanged():
    wire = {"role": "user", "content": "hi", "images": [{"url": "/api/file?path=a", "name": "a"}]}
    out = _run(LIVE, f"m.historyMessage({json.dumps(wire)}, 0, 99)")
    assert out == {"role": "user", "content": "hi", "timestamp": 99,
                   "images": [{"url": "/api/file?path=a", "name": "a"}], "_order": 0}


def test_the_page_rebuilds_history_through_the_helper():
    """Rule 2: one rebuild, in the helper, for every history_update."""
    src = _page()
    hist = _between(src, "else if (data.type === 'history_update')", "const cachedMsgs")
    assert "historyMessage(m, idx, Date.now())" in hist
    assert "role: m.role," not in hist, "a second hand copy of the rebuild is back"


# ── AC9: the server wins ─────────────────────────────────────────────────────────

def test_a_kept_generating_flag_never_outvotes_the_server():
    """MUTATION: let the kept flag win whenever it is set - red."""
    now = 1_000_000
    cases = [
        (False, {"isGenerating": True}, 0, False),             # no local send: server wins
        (False, {"isGenerating": True}, now - 20_000, False),  # send long ago: server wins
        (False, {"isGenerating": True}, now - 1_000, True),    # the enqueue race window
        (False, {"isGenerating": False}, now - 1_000, False),
        (True, None, 0, True),
    ]
    for server, kept, sent, expected in cases:
        out = _run(LIVE, f"m.entryActivity({json.dumps(server)}, {json.dumps(kept)}, {sent}, {now})")
        assert out["active"] is expected, (server, kept, sent)


def test_only_system_lines_are_kept_beside_the_server():
    """MUTATION: re-add kept tool cards (or bubbles, or older prompts) - red."""
    out = _run(LIVE, "['system', 'user', 'tool', 'assistant', 'workflow'].map(r => m.keepCachedOrphan({role: r}))")
    assert out == [True, False, False, False, False]
    merge = _between(_page(), "const orphans = cachedMsgs", ";")
    assert "keepCachedOrphan(m, hydratedServerMsgs, lastUserSendTimeRef.current, cachedMsgs)" in merge


def test_the_prompt_just_sent_survives_an_early_snapshot():
    """The server computed its answer before this tab's chat frame reached it: the person's own
    prompt, sent moments ago, stays until the server's transcript has it.
    MUTATION: drop kept user prompts again - red; keep any older prompt - red."""
    now = 1_000_000
    keep = lambda msg, server: _run(LIVE, f"m.keepCachedOrphan({json.dumps(msg)}, {json.dumps(server)}, {now})")
    just_sent = {"role": "user", "content": "go", "timestamp": now + 5}
    assert keep(just_sent, []) is True
    assert keep(just_sent, [{"role": "user", "content": "go"}]) is False      # the server has it
    # E5: "go" sent twice; the server has the first one only. MUTATION: match by content - red.
    first = {"role": "user", "content": "go", "timestamp": now - 90_000}
    two = _run(LIVE, f"m.keepCachedOrphan({json.dumps(just_sent)}, {json.dumps([first])}, {now}, "
                     f"{json.dumps([first, just_sent])})")
    assert two is True
    assert keep({"role": "user", "content": "old", "timestamp": now - 60_000}, []) is False
    assert _run(LIVE, f"m.keepCachedOrphan({json.dumps(just_sent)}, [], 0)") is False   # no local send


def test_a_replay_plan_replaces_closes_and_keeps_closed():
    """The replay resets the kept worker window when it carries worker events, closes it when
    the worker's last word was idle or error, and keeps a finished workflow's panel closed.
    MUTATION: no reset on worker events - red; a finished window not closed - red;
    workflow_done ignored - red."""
    plan = lambda evs: _run(LIVE, f"m.planReplay({json.dumps(evs)})")
    assert plan([{"type": "agent_message_update"}]) == {
        "workerEvents": False, "finishedWorker": False, "workflowDone": False}
    assert plan([{"type": "coder_state"}])["workerEvents"] is True
    assert plan([{"type": "subagent_output_stream"}])["workerEvents"] is True
    assert plan([{"type": "subagent_update", "presence": "online"},
                 {"type": "browser_frame_update"},
                 {"type": "subagent_update", "presence": "idle"}])["finishedWorker"] is True
    assert plan([{"type": "subagent_update", "presence": "idle"},
                 {"type": "subagent_update", "presence": "online"}])["finishedWorker"] is False
    assert plan([{"type": "workflow_start"}, {"type": "workflow_done"}])["workflowDone"] is True
    # A new specialist that started after the old one said idle is running: its window stays.
    # MUTATION: ignore later starts - red.
    assert plan([{"type": "subagent_update", "presence": "idle"},
                 {"type": "tool_update", "subType": "start", "name": "browser_agent"}])["finishedWorker"] is False
    assert plan([{"type": "subagent_update", "presence": "idle"},
                 {"type": "tool_update", "subType": "start", "name": "read_file"}])["finishedWorker"] is True


def test_the_page_follows_the_plan():
    replay = _between(_page(), "if (data.type === 'live_replay') {", "if (data.type === 'new_log')")
    loop = replay.index("for (const ev of replayed) {")
    reset = replay[replay.index("if (plan.workerEvents) {"):loop]
    # The console's duplicate filters are reset with the window, or every line this page
    # already showed once is refused and the console comes back empty.
    for needle in ("subAgentLogSetRef.current = new Set();", "subAgentOutputSetRef.current = new Set();",
                   "setSubAgentState({ ...IDLE_SUB_AGENT_STATE, isOpen: false });"):
        assert needle in reset, needle
    after = replay[loop:]
    assert "if (plan.finishedWorker) setSubAgentState(prev => ({ ...prev, isOpen: false }));" in after
    # A finished workflow is not replayed at all: the one store must not lose another chat's run.
    loop_block = replay[replay.index("for (const ev of replayed) {"):]
    assert "if (plan.workflowDone && /^workflow_/.test(ev.type)) continue;" in loop_block
    # Nothing the replay drew slides in, and a chat left at its bottom is there again.
    assert "setMessages(prev => { snapshotLenRef.current = prev.length; return prev; });" in after
    assert "pendingScrollRestore.current = 'bottom';" in after
    assert "bottomPinUntilRef.current = performance.now() + BOTTOM_PIN_MS;" in after
    # The scroll keeper follows the moving bottom while the pin lasts, and a touch ends it.
    src = _page()
    keeper = _between(src, "const tick = () => {", "raf = requestAnimationFrame(tick);")
    assert "if (bottomPinUntilRef.current > performance.now() && performance.now() - lastHuman >= 300) {" in keeper
    assert "scrollIntentRef.current = Math.max(0, el.scrollHeight - el.clientHeight);" in keeper
    human = _between(src, "const human = () => {", "};")
    assert "bottomPinUntilRef.current = 0;" in human
    # E2: a touch also drops the bottom a replay still on its way would pull back to.
    assert "stickToBottomRef.current = null;" in human


def test_the_bottom_is_for_the_chat_switched_to_and_only_once():
    """E1: a later history_update of the same chat (a reconnect, a regenerate, the forget
    repaint) must not jump to the bottom. MUTATION: a boolean stick that only a replay
    clears - red; the replay not ending the stick - red."""
    src = _page()
    switch = _between(src, "const handleSessionSwitch = (id: string) => {", "const switchAfterDelete")
    assert "stickToBottomRef.current = (!savedScroll || savedScroll.wasAtBottom) ? id : null;" in switch
    hist = _between(src, "else if (data.type === 'history_update')", "else if (data.type === 'context_checkpoint')")
    assert hist.count("if (stickToBottomRef.current === data.sessionId) pendingScrollRestore.current = 'bottom';") == 2
    replay = _between(src, "if (data.type === 'live_replay') {", "if (data.type === 'new_log')")
    assert "if (stickToBottomRef.current === data.sessionId) {" in replay
    assert "stickToBottomRef.current = null;" in replay
    assert "stickToBottomRef = useRef<string | null>(null)" in src


def test_the_hold_releases_once_by_replay_or_by_timeout():
    """MUTATION: a hold whose timeout does nothing - red; take for another chat - red."""
    out = _run(LIVE, """(() => {
        const timers = []; const fake = {set: (fn, ms) => { timers.push({fn, ms, on: true}); return timers.length - 1; },
                                         clear: (h) => { timers[h].on = false; }};
        const h = new m.LiveHistoryHold(1500, fake);
        const fired = [];
        h.hold('A', ['a1'], () => fired.push('A'));
        const other = h.take('B');
        const mine = h.take('A');
        const again = h.take('A');
        h.hold('C', ['c1'], () => fired.push(h.take('C')));
        timers.filter(t => t.on).forEach(t => t.fn());
        return {other: other ?? null, mine, again: again ?? null, fired, ms: timers[0].ms, cleared: !timers[0].on};
    })()""")
    assert out == {"other": None, "mine": ["a1"], "again": None, "fired": [["c1"]], "ms": 1500, "cleared": True}


def test_a_running_chats_transcript_is_drawn_with_its_replay():
    src = _page()
    hist = _between(src, "else if (data.type === 'history_update')", "else if (data.type === 'context_checkpoint')")
    assert "if (data.live === true) {\n                        holdLiveHistory(data.sessionId, finalMsgs);" in hist
    # The load spinner waits with it, so a reload never flashes the empty start screen.
    assert "if (data.live !== true) setHistoryLoading(false);" in hist
    assert "setHistoryLoading(false);" in _between(src, "const applyLiveHistory = (", "const holdLiveHistory")
    replay = _between(src, "if (data.type === 'live_replay') {", "if (data.type === 'new_log')")
    assert replay.index("applyLiveHistory(data.sessionId)") < replay.index("handleFrame({ ...ev, replay: true })")
    # The server's worker picture replaces the kept one before it is replayed.
    assert replay.index("setSubAgentState({ ...IDLE_SUB_AGENT_STATE, isOpen: false })") \
        < replay.index("handleFrame({ ...ev, replay: true })")
    assert "!eventBelongsHere(data, activeSessionId, 'chat')" in replay


# ── AC10: a replay is quiet ──────────────────────────────────────────────────────

QUIET_GUARDS = [
    # message_complete of a turn that ended but is not stored yet: no sound, no read-out,
    # no reaction
    "if (completeIsActiveSession && !data.replay) try {",
    "if (completeIsActiveSession && !data.replay && config.tts_auto_speak",
    "if (!data.replay && lastCh === '?') fireAgentReaction('permission', 3000);",
    # tool_update: no hold timer, no reaction, no cursor
    "if (data.replay || elapsed >= TOOL_ANIM_MIN_MS) release();",
    "if (!data.replay && (subType === 'end' || subType === 'error')) {",
    "if (!data.replay && toolName === 'learn_attached_knowledge') {",
    "} else if (!data.replay && subType === 'start') {",
    # learn_state: no cursor walk
    "if (!data.replay && _phase === 'learning' && !learnCursorActiveRef.current) {",
    # file_created: no workspace refetch per replayed file
    "if (!data.replay && (viewedWorkspaceSidRef.current == null",
]


@pytest.mark.parametrize("guard", QUIET_GUARDS)
def test_replayed_frames_are_quiet(guard):
    """MUTATION: remove any one guard - red."""
    assert guard in _page()


def test_the_replay_goes_through_the_one_dispatch():
    src = _page()
    assert "const handleFrame = (data: any) => {" in src
    assert "handleFrame(data);" in _between(src, "socket.onmessage = (event) => {", "};")
    # Never a replay inside a replay. A replayed message_complete (a turn that ended and is not
    # stored yet) is quiet by the guards above.
    assert "ev.type !== 'live_replay'" in src


# ── AC3, AC11: the open chat ─────────────────────────────────────────────────────

def test_the_session_ref_moves_with_the_switch():
    src = _page()
    switch = _between(src, "const handleSessionSwitch = (id: string) => {", "const switchAfterDelete")
    assert switch.index("currentSessionIdRef.current = id;") < switch.index("setCurrentSessionId(id);")
    hist = _between(src, "else if (data.type === 'history_update')", "const isActive")
    assert hist.index("currentSessionIdRef.current = data.sessionId;") < hist.index("setCurrentSessionId(data.sessionId);")


def test_a_reconnect_keeps_the_open_chat():
    """MUTATION: drop the autoLoad branch - red."""
    hist = _between(_page(), "else if (data.type === 'history_update')", "const isActive")
    branch = "if (data.autoLoad && prevSessionId && data.sessionId !== prevSessionId) {"
    assert "                        reaskRef.current = prevSessionId;\n" \
           "                        requestHistory(wsSocketRef.current, prevSessionId, false);\n" \
           "                        return;" in hist
    assert branch in hist
    assert hist.index(branch) < hist.index("setCurrentSessionId(data.sessionId);")


# ── AC12, AC13 ───────────────────────────────────────────────────────────────────

def test_a_dropped_socket_clears_every_preview_place():
    """MUTATION: no reset in onclose - red."""
    onclose = _between(_page(), "socket.onclose = () => {", "socket.onerror")
    assert "setDocPreviewWaits({});" in onclose


def test_a_chat_opened_from_the_server_drops_its_kept_preview_place():
    hist = _between(_page(), "else if (data.type === 'history_update')", "const isActive")
    assert "if (typeof data.live === 'boolean') clearDocPreviewWait(data.sessionId);" in hist


def test_a_chip_of_one_chat_never_lands_in_another():
    """MUTATION: drop the session term of the flush filter - red."""
    complete = _between(_page(), "else if (data.type === 'message_complete')", "else if (data.type ===")
    assert "&& (!f.sessionId || f.sessionId === data.sessionId));" in complete


# ── AC14: the composer per chat, in memory ───────────────────────────────────────

def test_composer_drafts_are_per_chat_and_bounded():
    """MUTATION: no bound - red; MUTATION: drop the most recently left chat first - red."""
    out = _run(LIVE, """(() => {
        const d = new m.ComposerDrafts(3);
        const draft = (t) => ({text: t, images: [], selections: []});
        d.save('A', draft('text of A'));
        d.save('B', draft(''));                       // an empty box is not kept
        d.save('C', draft('c')); d.save('D', draft('d')); d.save('E', draft('e'));
        const a = d.take('A');                         // A was left longest ago: gone
        const e = d.take('E');
        const again = d.take('E');                     // taken back into the composer
        return {a: a.text, e: e.text, again: again.text, size: d.size, b: d.take('B').text};
    })()""")
    assert out == {"a": "", "e": "e", "again": "", "size": 2, "b": ""}
    kept = _run(LIVE, """(() => {
        const d = new m.ComposerDrafts(5);
        d.save('X', {text: '', images: [], selections: []});
        const emptyKept = d.size;
        d.save('Y', {text: '', images: [], selections: [{start: 1, end: 4}]});
        return {emptyKept, sel: d.take('Y').selections.length};
    })()""")
    assert kept == {"emptyKept": 0, "sel": 1}
    default = _run(LIVE, "m.MAX_COMPOSER_DRAFTS")
    assert default == 20


def test_composer_swaps_on_every_chat_change():
    src = _page()
    switch = _between(src, "const handleSessionSwitch = (id: string) => {", "const switchAfterDelete")
    assert "swapComposer(currentSessionId, id);" in switch
    hist = _between(src, "else if (data.type === 'history_update')", "const isActive")
    assert "if (prevSessionId) swapComposer(prevSessionId, data.sessionId);" in hist
    assert "if (sessionSwitched) setAttachedImages([]);" not in src
    swap = _between(src, "const swapComposer = (", "\n    };")
    assert "localStorage" not in swap and "sessionStorage" not in swap


# ── AC15: no chat message in browser storage ─────────────────────────────────────

def test_no_message_content_is_written_to_browser_storage():
    """MUTATION: write the cache to localStorage again - red."""
    cache = CACHE.read_text(encoding="utf-8")
    assert "setItem" not in cache
    assert "loadSessionCache" not in cache and "saveSessionCache" not in cache
    src = _page()
    assert "loadSessionCache" not in src and "saveSessionCache" not in src
    assert "vaf_session_cache" not in src
    assert "purgeLegacySessionCache();" in src
    live = LIVE.read_text(encoding="utf-8")
    assert "localStorage" not in live.split("*/", 1)[1] and "setItem" not in live


def test_the_cache_is_bounded_on_every_update():
    """MUTATION: bound the chat only when it first joins the cache - red."""
    out = _run(CACHE, """(() => {
        const opt = {currentSessionId: 'A', sessionIdsInOrder: ['A', 'B']};
        let c = m.cacheChat({}, 'A', [1, 2], opt, {maxSessions: 1, maxMessagesPerSession: 3});
        c = m.cacheChat(c, 'A', [1, 2, 3, 4, 5, 6], opt, {maxSessions: 1, maxMessagesPerSession: 3});
        const afterUpdate = c['A'];
        c = m.cacheChat(c, 'B', [9], {currentSessionId: 'B', sessionIdsInOrder: ['A', 'B']},
                        {maxSessions: 1, maxMessagesPerSession: 3});
        return {afterUpdate, keys: Object.keys(c)};
    })()""")
    assert out == {"afterUpdate": [4, 5, 6], "keys": ["B"]}
    assert "cacheChat(sessionCache.current, currentSessionId, messages," in _page()


def test_a_refused_reask_takes_the_servers_chat():
    """A reconnect re-asks for the open chat; a refusal (deleted, another account) takes the
    chat the server auto-loaded. MUTATION: no error branch - red."""
    src = _page()
    err = _between(src, "else if (data.type === 'error') {", "else if (data.type === 'context_checkpoint')")
    assert "if (reaskRef.current && autoLoadedRef.current) {" in err
    assert "requestHistory(wsSocketRef.current, fallback, true);" in err
    hist = _between(src, "else if (data.type === 'history_update')", "const isActive")
    assert "reaskRef.current = prevSessionId;" in hist


def test_another_account_on_the_tab_gets_nothing_of_the_first():
    """MUTATION: no account check on reconnect - red; sign-out keeps the workflow panel - red."""
    src = _page()
    opened = _between(src, "socket.onopen = () => {", "socket.send(JSON.stringify({ type: 'get_sessions' }));")
    assert "if (!me || (was && key(was) !== key(me))) {" in opened and "forgetAccountState();" in opened
    forget = _between(src, "const forgetAccountState = () => {", "\n    };")
    for needle in ("sessionCache.current = {};", "composerDraftsRef.current = new ComposerDrafts", "forgetWorkflowState();"):
        assert needle in forget, needle
    logout = _between(src, "onLogout={() => {", "}}")
    assert "forgetWorkflowState();" in logout
    assert src.count("forgetWorkflowState();\n") >= 4    # sign-out, two ended sessions, account change
    # D1: what is on screen goes too, before the next account is shown.
    for needle in ("setMessages([]);", "setRoomView(null);", "currentSessionIdRef.current = null;",
                   "liveHistoryRef.current.take();", "setSessionViewerState({});"):
        assert needle in forget, needle
    # D2: fail closed - no answer from /api/auth/me is treated like another account.
    assert "if (!me || (was && key(was) !== key(me))) {" in opened
    assert ".catch(() => settle(null));" in opened
    # The re-ask waits for the check, so the old view is not drawn again meanwhile.
    hist = _between(src, "else if (data.type === 'history_update')", "const isActive")
    assert "if (accountCheckRef.current) {\n                            deferredReaskRef.current = prevSessionId;" in hist
    # D3: a session that ran out reaches /login past the app; the login page clears the panel.
    login = (ROOT / "web" / "app" / "login" / "page.tsx").read_text(encoding="utf-8")
    assert "useEffect(() => { forgetWorkflowState(); }, []);" in login
    store = (ROOT / "web" / "components" / "workflows" / "stores" / "workflowStore.ts").read_text(encoding="utf-8")
    assert "useWorkflowStore.persist.clearStorage();" in _between(store, "export function forgetWorkflowState", "\n}")


def test_the_legacy_copy_is_removed_and_a_blocked_storage_is_harmless():
    out = _run(CACHE, "(() => { m.purgeLegacySessionCache(); return store; })()", setup=(
        "const store = {vaf_session_cache_v1: '[{...}]', other: 'kept'};"
        "globalThis.window = {localStorage: {removeItem: (k) => { delete store[k]; }}};"))
    assert out == {"other": "kept"}
    out = _run(CACHE, "(() => { m.purgeLegacySessionCache(); return 'ok'; })()", setup=(
        "globalThis.window = {get localStorage() { throw new Error('blocked'); }};"))
    assert out == "ok"


def test_a_stored_answer_still_on_record_is_drawn_once():
    """A5: between the save and turn_left_queue, the stored transcript and the replay both carry
    the turn. The replayed round lands on the stored round's bubble (same turn, same round), and
    a replayed tool start for a stored card is a duplicate start.
    MUTATION: drop turnId from the history rebuild - red."""
    rows = [_run(LIVE, f"m.historyMessage({json.dumps(w)}, {i}, 0)") for i, w in enumerate([
        {"role": "user", "content": "q", "timestamp": "2026-01-01T10:00:00"},
        {"role": "assistant", "content": "the answer", "timestamp": "2026-01-01T10:00:05",
         "round": 1, "roundAnswer": True, "turnId": "t1"}])]
    turn_rounds = ROOT / "web" / "components" / "chat" / "turnRounds.ts"
    placed = _run(turn_rounds, f"m.placeRoundUpdate({json.dumps(rows)}, "
                               "{round: 1, content: 'the answer', turnId: 't1', now: 99})")
    assert placed["created"] is False and len(placed["messages"]) == 2
    tool = _between(_page(), "else if (data.type === 'tool_update') {", "else if (data.type === 'cursor_animation')")
    assert "if (existingIdx !== -1) return prev; // Duplicate start" in tool
