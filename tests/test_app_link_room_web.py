# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Veyllo App room's rules in the browser (web/components/chat/appRoom.ts), run as the
web's real code: transpiled with the app's own TypeScript and executed in node (the pattern of
tests/test_live_view_web.py); skipped without node. Every catalogue key a rule names exists in
all seven catalogues."""
import json
from pathlib import Path

import pytest

from test_live_view_web import _run

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "web" / "components" / "chat" / "appRoom.ts"
LANGS = ("de", "en", "ja", "ko", "th", "tr", "zh")


def _call(expr: str):
    return _run(MODULE, expr)


def test_the_open_command_names_no_room_for_the_veyllo_app_room():
    """MUTATION: return `open_room` for every room - red."""
    assert _call("m.roomOpenCommand(m.APP_ROOM_ID)") == {"type": "open_app_room"}
    assert _call("m.roomOpenCommand('r1')") == {"type": "open_room", "room_id": "r1"}


def test_a_room_view_closes_when_its_row_leaves_the_list():
    """MUTATION: keep the Veyllo App room whatever the list says - red."""
    rows = "[{kind: 'chat'}, {kind: 'room', roomId: 'r1'}]"
    assert _call(f"m.roomStillListed(m.APP_ROOM_ID, {rows})") is False
    assert _call(f"m.roomStillListed(m.APP_ROOM_ID, [{{kind: 'app_room'}}, ...{rows}])") is True
    assert _call(f"m.roomStillListed('r1', {rows})") is True
    assert _call(f"m.roomStillListed('r2', {rows})") is False


@pytest.mark.parametrize("code, key", [
    ("needs_newer_app", "appRoomNeedsNewerApp"), ("not_connected_yet", "appRoomNotConnectedYet"),
    ("channel_off", "appRoomChannelOff"), ("no_device", "appRoomNoDevice"),
    ("rate_limited", "appRoomRateLimited"), ("bad_text", "appRoomFailed"),
])
def test_each_refusal_has_its_sentence(code, key):
    """MUTATION: map `needs_newer_app` to the general failure - red."""
    assert _call(f"m.refusalKey('{code}')") == key


def test_the_composer_keeps_its_text_unless_the_line_was_taken_where_it_was_written():
    """MUTATION: clear whenever the text matches - red (the same words typed in another chat
    were lost)."""
    assert _call("m.clearAfterSend(m.APP_ROOM_ID, m.APP_ROOM_ID, 'Hallo', ' Hallo ')") is True
    assert _call("m.clearAfterSend(m.APP_ROOM_ID, null, 'Hallo', 'Hallo')") is False
    assert _call("m.clearAfterSend(m.APP_ROOM_ID, 'r1', 'Hallo', 'Hallo')") is False
    assert _call("m.clearAfterSend(m.APP_ROOM_ID, m.APP_ROOM_ID, 'Hallo', 'Hallo, und')") is False


def test_the_unread_dot_never_burns_on_the_open_room():
    """MUTATION: ignore `open` - red."""
    assert _call("m.showUnreadDot(2, false)") is True
    assert _call("m.showUnreadDot(2, true)") is False
    assert _call("m.showUnreadDot(0, false)") is False


def test_delivery_says_why_offers_a_resend_and_names_the_device():
    """MUTATION: offer the resend for a waiting line, or drop the device for two devices -
    red."""
    assert _call("m.deliveryOf('delivered', '', 1, 'Pixel 9')") == {"key": "appRoomDelivered",
                                                                    "resend": False}
    assert _call("m.deliveryOf('delivered', '', 2, 'Pixel 9')") == {
        "key": "appRoomDeliveredTo", "values": {"device": "Pixel 9"}, "resend": False}
    assert _call("m.deliveryOf('waiting')") == {"key": "appRoomWaiting", "resend": False}
    assert _call("m.deliveryOf('not_delivered', 'removed')") == {
        "key": "appRoomNotDeliveredRemoved", "resend": True}
    assert _call("m.deliveryOf('not_delivered', 'lane_off')")["resend"] is False
    assert _call("m.deliveryOf('not_delivered', 'expired')")["key"] == "appRoomNotDeliveredExpired"


def test_the_person_addressed_gets_their_own_form():
    """MUTATION: one key for every addressee - red ("Mia an Du")."""
    assert _call("m.toKey('person')") == "appRoomToYou"
    assert _call("m.toKey('app_agent')") == "appRoomTo"


def test_coded_lines_are_worded_by_the_view():
    """MUTATION: show the server's English sentence for a timeout - red."""
    assert _call("m.lineTextKey('timeout')") == "appRoomTaskTimeout"
    assert _call("m.lineTextKey('turn_failed')") == "appRoomTurnFailed"
    assert _call("m.lineTextKey('answered')") is None
    assert _call("m.lineTextKey(undefined)") is None


def test_the_hint_drops_the_app_while_nothing_reaches_it():
    """MUTATION: one hint whatever the room can do - red."""
    assert _call("m.placeholderKey(true)") == "appRoomPlaceholder"
    assert _call("m.placeholderKey(false)") == "appRoomPlaceholderAgentOnly"


def test_every_key_the_rules_name_is_in_every_catalogue():
    """MUTATION: delete one of the keys from one catalogue - red."""
    keys = set(_call(
        "[...['needs_newer_app','not_connected_yet','channel_off','no_device','rate_limited','x']"
        ".map(m.refusalKey), ...['delivered','waiting'].map(s => m.deliveryOf(s, '', 2, 'd').key),"
        " ...['removed','lane_off','expired','dropped'].map(r => m.deliveryOf('not_delivered', r).key),"
        " m.deliveryOf('delivered', '', 1, 'd').key,"
        " ...['turn_failed','declined','unavailable','timeout','failed','cancelled','disconnected',"
        "'file_refused'].map(m.lineTextKey), m.placeholderKey(true), m.placeholderKey(false),"
        " m.toKey('person'), m.toKey('vaf_agent'), m.refusalKey('text_only'),"
        " ...['timer','process','draft'].map(m.lineTextKey), m.emptyKey(true), m.emptyKey(false),"
        " m.refusalKey('already_sent_again'), m.deliveryOf('x', '', 1, '', true).key,"
        " m.refusalKey('timeout')]"))
    assert len(keys) >= 25
    for lang in LANGS:
        main = json.loads((ROOT / "web" / "messages" / f"{lang}.json").read_text(
            encoding="utf-8"))["main"]
        missing = sorted(k for k in keys if not str(main.get(k) or "").strip())
        assert not missing, (lang, missing)


def test_one_send_at_a_time():
    """MUTATION: let a send start while another is open - red (a double Enter sent twice)."""
    first = _call("m.beginSend(m.ROOM_SEND_IDLE, ' Hallo ', 'l1')")
    assert first == {"busy": True, "text": "Hallo", "resendOf": "l1"}
    assert _call("m.beginSend(m.beginSend(m.ROOM_SEND_IDLE, 'Hallo'), 'Hallo')") is None
    assert _call("m.beginSend(m.ROOM_SEND_IDLE, '   ')") is None


def test_a_line_sent_again_offers_no_second_resend_and_reads_quietly():
    """MUTATION: ignore `resent` - red; MUTATION: keep the amber "not delivered" for it - red."""
    assert _call("m.deliveryOf('not_delivered', 'removed', 1, '', true)") == {
        "key": "appRoomSentAgain", "resend": False, "quiet": True}
    assert _call("m.deliveryOf('not_delivered', 'removed', 1, '', false)")["resend"] is True
    assert _call("m.refusalKey('already_sent_again')") == "appRoomAlreadySentAgain"


@pytest.mark.parametrize("lang, marker", [("ja", "空白"), ("zh", "空格"), ("ko", "띄어"),
                                          ("th", "เว้นวรรค")])
def test_scripts_without_spaces_are_told_to_put_one_after_the_name(lang, marker):
    """"@Mia你好" is no mention of Mia: where a script is written without spaces, the empty
    room says to put a space after the name, and the agent-only hint shows the handle followed
    by one (no particle glued to it). MUTATION: drop the clause from one of the texts, or glue
    a particle to the handle in the hint - red."""
    main = json.loads((ROOT / "web" / "messages" / f"{lang}.json").read_text(
        encoding="utf-8"))["main"]
    for key in ("appRoomEmpty", "appRoomEmptyAgentOnly"):
        assert marker in main[key], (lang, key)
    agent_only = main["appRoomPlaceholderAgentOnly"]
    assert agent_only.endswith("@{handle}") or "@{handle} " in agent_only, lang


@pytest.mark.parametrize("lang", LANGS)
def test_the_mention_is_still_taught(lang):
    """The composer's hint names no handle, so the empty room and the two notices that say what
    is still possible are where the person learns the "@" form. MUTATION: delete `@{handle}`
    from one of those texts in one language - red."""
    main = json.loads((ROOT / "web" / "messages" / f"{lang}.json").read_text(
        encoding="utf-8"))["main"]
    for key in ("appRoomEmpty", "appRoomEmptyAgentOnly", "appRoomNotConnectedYet",
                "appRoomNeedsNewerApp"):
        assert "@{handle}" in main[key], (lang, key)


def test_the_hint_shows_a_handle_that_reaches_the_agent_and_fits_one_line():
    """While no line can reach the app's agent the hint shows "@" and the handle, the one form
    the server takes. A handle wider than 12 Latin letters is not cut (a cut handle is no
    mention) but becomes "VAF", which always reaches the VAF agent. Width counts what a reader
    sees as one character (a family emoji, a Thai consonant with its marks) and counts a wide
    one (Han, kana, Hangul, emoji) double and a wide Latin letter (M, W) as 1.6. MUTATION: cut
    the handle instead of "VAF", count code points instead of graphemes, count wide characters
    single, or count M and W as ordinary letters - red."""
    assert _call("m.HINT_HANDLE_MAX") == 12
    assert _call("m.hintHandle('Nobel')") == "Nobel"
    assert _call("m.hintHandle(' Nobel ')") == "Nobel"
    assert _call("m.hintHandle('Bartholom\u00e4us')") == "Bartholom\u00e4us"
    assert _call("m.hintHandle('Bartholom\u00e4uss')") == "VAF"
    # Wide Latin letters weigh more than one: twelve M or ten W do not fit one line at 390 px
    # (203 and 181 px against about 159), an ordinary twelve-letter name does.
    assert _call("m.hintHandle('MMMMMMMMMMMM')") == "VAF"
    assert _call("m.hintHandle('WWWWWWWWWW')") == "VAF"
    assert _call("m.hintHandle('Wiedenbr\u00fccks')") == "Wiedenbr\u00fccks"
    assert abs(_call("m.hintWidth('Bartholom\u00e4us')") - 11.65) < 1e-9
    # An accented capital weighs as its base letter, decomposed or not.
    assert _call("m.hintWidth('\u00c4')") == _call("m.hintWidth('A\u0308')") == 1.25
    # The lowercase w is wide too, and the capital I is narrow, not a capital's 1.25.
    assert abs(_call("m.hintWidth('www')") - 4.8) < 1e-9
    assert abs(_call("m.hintWidth('III')") - 1.8) < 1e-9
    assert _call("m.hintHandle('')") == "VAF"
    assert _call("m.hintHandle('   ')") == "VAF"
    # Han: two letters wide each; six fit, seven do not.
    assert _call("m.hintWidth('\u7f8e\u54b2')") == 4
    assert _call("m.hintHandle('\u7f8e\u54b2'.repeat(3))") == "\u7f8e\u54b2" * 3
    assert _call("m.hintHandle('\u7f8e\u54b2'.repeat(3) + '\u7f8e')") == "VAF"
    # One family emoji (four people joined by ZWJ) is one wide grapheme; a Thai consonant
    # with its vowel and tone marks is one.
    family = "'\\u{1F468}\\u200d\\u{1F469}\\u200d\\u{1F467}\\u200d\\u{1F466}'"
    assert _call(f"m.hintWidth({family})") == 2
    assert _call("m.hintWidth('\u0e19\u0e49\u0e33')") == 1
    assert _call("m.hintWidth('\u0e2a\u0e21\u0e28\u0e31\u0e01\u0e14\u0e34\u0e4c')") == 5
    # Without Intl.Segmenter the width falls back to code points instead of failing.
    assert _run(MODULE, f"m.hintWidth({family})", setup="delete Intl.Segmenter;") == 11


def test_wake_notes_and_the_empty_room_are_worded():
    """MUTATION: word a timer note as the general failure, or one empty text whatever the room
    can do - red."""
    assert _call("['timer','process','draft','confirm','card_waiting'].map(m.lineTextKey)") == [
        "appRoomWakeTimer", "appRoomWakeProcess", "appRoomWakeDraft", "appRoomConfirmed",
        "appRoomCardWaiting"]
    assert _call("m.emptyKey(true)") == "appRoomEmpty"
    assert _call("m.emptyKey(false)") == "appRoomEmptyAgentOnly"
    assert _call("m.refusalKey('text_only')") == "appRoomTextOnly"


def _run_async(body: str):
    """Like `_call`, for a body that awaits: `body` is the inside of an async function over
    the module's exports `m` that returns a JSON value."""
    import shutil
    import subprocess

    from test_live_view_web import _TS
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
        "const sleep = (ms) => new Promise(r => setTimeout(r, ms));"
        "(async () => {" + body + "})().then(out => process.stdout.write(JSON.stringify(out)),"
        " err => { console.error(err); process.exit(1); });"
    )
    res = subprocess.run(["node", "-e", script, str(_TS), str(MODULE)],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


# A fake fetch: `head` ms until the headers, then `body` ms until the body (or a body that
# fails), honouring the abort signal like the browser's fetch does on both.
_FAKE_FETCH = """
const fake = (head, body, ok = true, payload = {id: 'l1'}) => (url, init) => {
    seen.push(init.signal);
    return new Promise((resolve, reject) => {
        const aborted = () => reject(Object.assign(new Error('aborted'), {name: 'AbortError'}));
        if (init.signal.aborted) return aborted();
        init.signal.addEventListener('abort', aborted);
        setTimeout(() => {
            init.signal.removeEventListener('abort', aborted);
            resolve({ok, status: ok ? 200 : 409, json: () => new Promise((res2, rej2) => {
                if (init.signal.aborted) return rej2(new Error('aborted'));
                init.signal.addEventListener('abort', () => rej2(new Error('aborted')));
                setTimeout(() => res2(payload), body);
            })});
        }, head);
    });
};
const seen = [];
"""


@pytest.mark.parametrize("head, body, ok, status", [
    (10, 10, True, "ok"),
    (10, 10, False, "refused"),
    (400, 10, True, "timeout"),
    (10, 400, True, "timeout"),
])
def test_a_room_send_gives_up_on_slow_headers_and_on_a_slow_body(head, body, ok, status):
    """The time limit covers the whole answer. MUTATION: swallow an aborted body as an empty
    answer (the former `.catch(() => ({}))`) - red for the slow body: a success was reported
    and the composer cleared without the line; MUTATION: no abort signal - red for the slow
    headers."""
    out = _run_async(_FAKE_FETCH + f"""
        const r = await m.sendWithTimeout(fake({head}, {body}, {str(ok).lower()}, {{code: 'x'}}),
                                          '/api/app-link/room', {{method: 'POST'}}, 150);
        return r.status;
    """)
    assert out == status


def test_a_room_send_that_answered_leaves_no_timer_behind():
    """MUTATION: never clear the timer (or clear it right after setting it, so nothing ever
    aborts) - red: a send that answered was aborted afterwards, or a slow one never was."""
    out = _run_async(_FAKE_FETCH + """
        const r = await m.sendWithTimeout(fake(10, 10), '/x', {method: 'POST'}, 120);
        await sleep(200);
        const late = await m.sendWithTimeout(fake(300, 10), '/x', {method: 'POST'}, 120);
        return [r.status, seen[0].aborted, late.status, seen[1].aborted];
    """)
    assert out == ["ok", False, "timeout", True]


def test_a_send_that_failed_outright_is_an_error_not_a_give_up():
    """A request that fails before any time ran out (the network is gone) is `error`, not
    `timeout`. MUTATION: report every failure as a give-up - red."""
    out = _run_async("""
        const fail = () => Promise.reject(new TypeError('network'));
        return (await m.sendWithTimeout(fail, '/x', {method: 'POST'}, 1000)).status;
    """)
    assert out == "error"


def test_only_the_line_the_page_sent_counts_as_arrived():
    """The line is recognised by the id the page sent with it: as its `id` (to the app's agent)
    or `msg` (to the VAF agent, waiting or stored). An earlier line with the same words, another
    party's line, or no id at all is not it. MUTATION: match on the text - red; MUTATION:
    accept another party's line - red; MUTATION: ignore `msg` - red."""
    lines = ("[{id: 'chat:4', peer: 'person', msg: 'old-id', text: '@Mia ok'},"
             " {id: 'new-id', peer: 'app_agent', text: '@Mia ok'}]")
    assert _call(f"m.holdsLine({lines}, 'new-id')") is False
    assert _call(f"m.holdsLine({lines}, '')") is False
    assert _call("m.holdsLine([{id: 'turn:t1', peer: 'person', msg: 'new-id'}], 'new-id')") is True
    assert _call("m.holdsLine([{id: 'new-id', peer: 'person'}], 'new-id')") is True


# A fake socket and fake timers for `confirmAfterGiveUp`: the test decides when transcripts
# arrive and when the wait ends.
_CHECK = """
const sent = [];
let listener = null, listening = 0;
const listen = cb => { listener = cb; listening += 1; return () => { listener = null; listening -= 1; }; };
let fire = null, cleared = 0, waited = null;
const timers = { set: (fn, ms) => { fire = fn; waited = ms; return 7; }, clear: h => { if (h === 7) cleared += 1; } };
const open = { readyState: 1, send: d => sent.push(JSON.parse(d)) };
"""


@pytest.mark.parametrize("script, expected", [
    # A poll answer from before the line was stored ends nothing; the next one with the line does.
    ("""const p = m.confirmAfterGiveUp(open, listen, 'L1', 5000, timers);
        listener([{id: 'x', peer: 'person'}]); listener([{id: 'turn:9', peer: 'person', msg: 'L1'}]);
        return [await p, sent, listening, cleared, waited];""",
     [True, [{"type": "open_app_room", "seen": False}], 0, 1, 5000]),
    # Nothing in time: not confirmed, listener and timer gone.
    ("""const p = m.confirmAfterGiveUp(open, listen, 'L1', 5000, timers);
        listener([{id: 'x', peer: 'person'}]); fire();
        return [await p, sent.length, listening, cleared];""",
     [False, 1, 0, 1]),
    # A socket that is reconnecting: nothing is sent, not confirmed, nothing left behind.
    ("""const p = m.confirmAfterGiveUp({readyState: 0, send: () => { throw new Error('x'); }},
                                       listen, 'L1', 5000, timers);
        return [await p, listening, fire === null];""",
     [False, 0, True]),
    # Sending throws: not confirmed, listener and timer gone.
    ("""const p = m.confirmAfterGiveUp({readyState: 1, send: () => { throw new Error('x'); }},
                                       listen, 'L1', 5000, timers);
        return [await p, listening, cleared];""",
     [False, 0, 1]),
    # No id: the room cannot tell the line, so it is not asked.
    ("""const p = m.confirmAfterGiveUp(open, listen, '', 5000, timers);
        return [await p, sent.length, listening];""",
     [False, 0, 0]),
])
def test_the_give_up_check(script, expected):
    """Each branch of `confirmAfterGiveUp`, against a fake socket and fake timers. MUTATIONS,
    each red: end the check on the first transcript; never clear the timer; leave the
    listener; send while the socket is not open; mark the room read (no `seen: false`); wait
    0 ms instead of the given time."""
    assert _run_async(_CHECK + script) == expected


@pytest.mark.parametrize("on_screen, box, verdict", [
    ("app:veyllo_app", "Licht aus", "clear"), ("app:veyllo_app", " Licht aus ", "clear"),
    ("app:veyllo_app", "Licht an", "drop"),
    ("green123456", "Licht aus", "wait"),
])
def test_a_line_that_turns_up_after_the_notice_clears_the_box(on_screen, box, verdict):
    """After the notice the page keeps watching: the line turning up while the room still owns
    the box and the box holds exactly its text clears both (a second Enter would send it twice);
    with other text it stops watching. Another chat on screen keeps its draft, even with the
    same words: the page waits. MUTATION: clear whatever the box holds - red; MUTATION: drop the
    view guard (`clearAfterSend`) - red (another chat's draft was deleted)."""
    lines = "[{id: 'L1', peer: 'person'}]"
    assert _call(f"m.lateLine({lines}, 'L1', m.APP_ROOM_ID, {json.dumps(on_screen)}, 'Licht aus', "
                 f"{json.dumps(box)})") == verdict
    assert _call("m.lateLine([{id: 'x', peer: 'person'}], 'L1', m.APP_ROOM_ID, m.APP_ROOM_ID, "
                 "'Licht aus', 'Licht aus')") == "wait"


def test_a_line_id_is_a_uuid_also_without_random_uuid():
    """`randomUUID` exists only in a secure context; on a plain-HTTP address in the local network
    the id comes from `getRandomValues`. Never empty, a version 4 UUID either way. MUTATION:
    return "" without `randomUUID` (the previous rule) - red."""
    out = _run_async("""
        const real = globalThis.crypto;
        const a = m.newLineId();
        // An insecure context: a crypto object without randomUUID.
        Object.defineProperty(globalThis, 'crypto', {configurable: true,
            value: {getRandomValues: arr => real.getRandomValues(arr)}});
        const lacking = typeof globalThis.crypto.randomUUID;
        const b = m.newLineId(), c = m.newLineId();
        Object.defineProperty(globalThis, 'crypto', {configurable: true, value: real});
        if (lacking !== 'undefined') throw new Error('randomUUID still there');
        return [a, b, c];
    """)
    import re
    uuid4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    assert all(uuid4.match(x) for x in out), out
    assert out[1] != out[2]


def test_the_give_up_waits_five_seconds_by_default():
    """The page calls the check without a wait of its own. MUTATION: `CONFIRM_WAIT_MS` 0 - red."""
    assert _run_async(_CHECK + """
        const p = m.confirmAfterGiveUp(open, listen, 'L1', undefined, timers);
        fire();
        await p;
        return [m.CONFIRM_WAIT_MS, waited];
    """) == [5000, 5000]


@pytest.mark.parametrize("lang", ["de", "en", "ko", "tr"])
def test_a_sentence_in_the_room_texts_is_followed_by_a_space(lang):
    """In the languages that put a space between sentences, no room text glues a sentence to
    the next one ("...시작하세요.이름"). MUTATION: take the space out again - red."""
    import re
    main = json.loads((ROOT / "web" / "messages" / f"{lang}.json").read_text(
        encoding="utf-8"))["main"]
    glued = {key: text for key, text in main.items()
             if key.startswith("appRoom") and re.search(r"[.!?:](?=[^\s.!?…)\]\"'{}0-9])", text)}
    assert not glued, glued


@pytest.mark.parametrize("lang, marker", [("de", "Leerzeichen"), ("en", "a space"), ("tr", "boşluk")])
def test_the_empty_room_says_to_put_a_space_after_the_name(lang, marker):
    """An emoji or a symbol glued to the name continues the word (`@Mia😊` names nobody), so the
    empty room says to put a space after the name in the Latin-script languages too. MUTATION:
    drop the words from one of the texts - red."""
    main = json.loads((ROOT / "web" / "messages" / f"{lang}.json").read_text(
        encoding="utf-8"))["main"]
    for key in ("appRoomEmpty", "appRoomEmptyAgentOnly"):
        assert marker in main[key], (lang, key)


@pytest.mark.parametrize("verdict, effect", [("clear", {"box": True, "notice": True}),
                                             ("drop", {"box": False, "notice": True}),
                                             ("wait", {"box": False, "notice": False})])
def test_a_line_that_turned_up_takes_the_notice_away_also_when_the_box_changed(verdict, effect):
    """The line is in the room for `clear` and `drop`, so "send it again if it does not appear"
    is no longer true: the notice goes in both, the box only when it still holds the line.
    MUTATION: keep the notice on `drop` (the previous page) - red."""
    assert _call(f"m.lateEffect('{verdict}')") == effect


@pytest.mark.parametrize("on_screen, at_bottom, go", [
    ("app:veyllo_app", True, True), ("app:veyllo_app", False, False),
    ("green123456", True, False), (None, True, False),
])
def test_the_view_goes_to_a_taken_line_only_for_a_reader_at_the_bottom(on_screen, at_bottom, go):
    """A reader who scrolled up while the line was on its way stays where they are, as in every
    chat on the page; a view at the bottom goes to the line; another chat never jumps.
    MUTATION: go whenever the room is on screen (the previous rule) - red."""
    assert _call(f"m.goToTakenLine({json.dumps(on_screen)}, {json.dumps(at_bottom)})") is go


def test_a_give_up_without_the_line_says_it_may_still_come():
    """The notice after a give-up is its own sentence ("No answer came in time. If the message
    does not appear in the room, send it again."), not the plain failure. MUTATION: drop the
    `timeout` case - red."""
    assert _call("m.refusalKey('timeout')") == "appRoomSendUnconfirmed"
    assert _call("m.refusalKey('failed')") == "appRoomFailed"
