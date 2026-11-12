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
        " ...['timer','process','draft'].map(m.lineTextKey), m.emptyKey(true), m.emptyKey(false)]"))
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


def test_a_line_sent_again_offers_no_second_resend():
    """MUTATION: ignore `resent` - red."""
    assert _call("m.deliveryOf('not_delivered', 'removed', 1, '', true)")["resend"] is False
    assert _call("m.deliveryOf('not_delivered', 'removed', 1, '', false)")["resend"] is True


def test_wake_notes_and_the_empty_room_are_worded():
    """MUTATION: word a timer note as the general failure, or one empty text whatever the room
    can do - red."""
    assert _call("['timer','process','draft','confirm'].map(m.lineTextKey)") == [
        "appRoomWakeTimer", "appRoomWakeProcess", "appRoomWakeDraft", "appRoomConfirmed"]
    assert _call("m.emptyKey(true)") == "appRoomEmpty"
    assert _call("m.emptyKey(false)") == "appRoomEmptyAgentOnly"
    assert _call("m.refusalKey('text_only')") == "appRoomTextOnly"
