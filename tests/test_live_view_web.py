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
    return PAGE.read_bytes().decode()


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
    """MUTATION: re-add kept tool cards (or bubbles, or prompts) - red."""
    out = _run(LIVE, "['system', 'user', 'tool', 'assistant', 'workflow'].map(r => m.keepCachedOrphan({role: r}))")
    assert out == [True, False, False, False, False]
    merge = _between(_page(), "const orphans = cachedMsgs", ";")
    assert merge.strip() == "const orphans = cachedMsgs.filter(keepCachedOrphan)\n" \
        "                        .map((m, idx) => ({ ...m, _order: 100000 + idx }))"


def test_the_worker_window_comes_back_idle_and_closed():
    switch = _between(_page(), "const handleSessionSwitch = (id: string) => {", "const switchAfterDelete")
    assert "isOpen: false," in switch
    assert "presence: restoredSubAgent.presence === 'error' ? 'error' : 'idle'," in switch


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
    # Never a replay inside a replay, and never a terminal event (the server sends none:
    # the turn group is cleared by message_complete), so no sound can come from a replay.
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
    branch = "if (data.autoLoad && prevSessionId && data.sessionId !== prevSessionId) {\n" \
             "                        requestHistory(wsSocketRef.current, prevSessionId, false);\n" \
             "                        return;"
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
    cache = CACHE.read_bytes().decode()
    assert "setItem" not in cache
    assert "loadSessionCache" not in cache and "saveSessionCache" not in cache
    src = _page()
    assert "loadSessionCache" not in src and "saveSessionCache" not in src
    assert "vaf_session_cache" not in src
    assert "purgeLegacySessionCache();" in src
    live = LIVE.read_bytes().decode()
    assert "localStorage" not in live.split("*/", 1)[1] and "setItem" not in live


def test_the_legacy_copy_is_removed_and_a_blocked_storage_is_harmless():
    out = _run(CACHE, "(() => { m.purgeLegacySessionCache(); return store; })()", setup=(
        "const store = {vaf_session_cache_v1: '[{...}]', other: 'kept'};"
        "globalThis.window = {localStorage: {removeItem: (k) => { delete store[k]; }}};"))
    assert out == {"other": "kept"}
    out = _run(CACHE, "(() => { m.purgeLegacySessionCache(); return 'ok'; })()", setup=(
        "globalThis.window = {get localStorage() { throw new Error('blocked'); }};"))
    assert out == "ok"
