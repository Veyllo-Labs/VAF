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
    room says to put a space after the name, and the hint shows the handle followed by one
    (no particle glued to it). MUTATION: drop the clause from one of the texts, or glue a
    particle to the handle in the hint - red."""
    main = json.loads((ROOT / "web" / "messages" / f"{lang}.json").read_text(
        encoding="utf-8"))["main"]
    for key in ("appRoomEmpty", "appRoomEmptyAgentOnly"):
        assert marker in main[key], (lang, key)
    assert "@{handle} " in main["appRoomPlaceholder"], lang
    agent_only = main["appRoomPlaceholderAgentOnly"]
    assert agent_only.endswith("@{handle}") or "@{handle} " in agent_only, lang


def test_wake_notes_and_the_empty_room_are_worded():
    """MUTATION: word a timer note as the general failure, or one empty text whatever the room
    can do - red."""
    assert _call("['timer','process','draft','confirm'].map(m.lineTextKey)") == [
        "appRoomWakeTimer", "appRoomWakeProcess", "appRoomWakeDraft", "appRoomConfirmed"]
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


def test_a_line_stored_after_the_give_up_counts_as_sent():
    """After a give-up the room is asked once: a line of the person that was not there before
    and carries the text means the line was taken. MUTATION: match any line with the text
    (an older one) - red; MUTATION: match another party's line - red."""
    lines = ("[{id: 'old', peer: 'person', text: 'Licht aus'},"
             " {id: 'n1', peer: 'app_agent', text: 'Licht aus'}]")
    assert _call(f"m.arrivedAfterAll(new Set(['old']), {lines}, ' Licht aus ')") is False
    more = lines[:-1] + ", {id: 'n2', peer: 'person', text: 'Licht aus'}]"
    assert _call(f"m.arrivedAfterAll(new Set(['old']), {more}, 'Licht aus')") is True
    assert _call(f"m.arrivedAfterAll(new Set(['old']), {more}, 'Licht an')") is False


def test_a_give_up_without_the_line_says_it_may_still_come():
    """MUTATION: word a give-up as the plain failure - red."""
    assert _call("m.refusalKey('timeout')") == "appRoomSendUnconfirmed"


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
