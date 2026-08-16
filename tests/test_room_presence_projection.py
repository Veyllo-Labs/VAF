# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Reading a room paints nobody as typing any more; it leaves a read receipt.

The old projection derived "took the newest message recently and answered
nothing" into a typing bubble with a two-minute window, so an agent that merely
monitored its room looked permanently busy - the owner watched the dots blink
for hours over an agent that was just listening. The rule now: composing is
only the turn marker (the agent is really answering) or keypresses a browser
reported; reading becomes a read position the view stacks under the last
message a peer took.
"""
import ast
from pathlib import Path

import vaf.core.web_server as ws_mod
from vaf.core.web_server import ROOM_KEYS_TYPING_WINDOW_S, derive_room_presence

MEMBERS = {
    "p-reader1": {"display": "Reader"},
    "p-typist1": {"display": "Typist"},
    "p-agent01": {"display": "Agent"},
    "p-viewer1": {"display": "Viewer"},
}
LABELS = {"p-reader1": "Reader"}


def _presence(**kw):
    args = dict(activity={}, members=MEMBERS, labels=LABELS,
                acting="p-viewer1", keys={}, busy_agent="", now=1000.0)
    args.update(kw)
    return derive_room_presence(**args)


def test_a_reader_is_a_receipt_never_a_typist():
    """MUTATION: put cursor-derived entries back into `typing`."""
    typing, receipts = _presence(activity={
        "p-reader1": {"read_to": 42, "read_at": 999.0, "last_wrote": 10}})
    assert typing == []
    assert receipts == [{"peer": "p-reader1", "label": "Reader",
                         "readTo": 42, "readAt": 999.0}]


def test_the_viewer_s_own_position_is_noise():
    typing, receipts = _presence(activity={
        "p-viewer1": {"read_to": 42, "read_at": 999.0, "last_wrote": 0}})
    assert receipts == []


def test_a_position_of_zero_is_no_receipt():
    """A member that never read anything has no place to stand a face under."""
    _, receipts = _presence(activity={
        "p-reader1": {"read_to": 0, "read_at": 0.0, "last_wrote": 0}})
    assert receipts == []


def test_keypresses_compose_and_expire():
    """MUTATION: drop the expiry - the dots would outlive the typing forever."""
    keys = {"p-typist1": 998.0}
    typing, _ = _presence(keys=keys)
    assert typing == [{"peer": "p-typist1", "label": "Typist", "kind": "keys"}]

    stale = {"p-typist1": 1000.0 - ROOM_KEYS_TYPING_WINDOW_S - 1}
    typing, _ = _presence(keys=stale)
    assert typing == []
    assert stale == {}, "an expired keypress must leave the map, not haunt it"


def test_the_turn_marker_still_composes():
    typing, _ = _presence(busy_agent="p-agent01")
    assert typing == [{"peer": "p-agent01", "label": "Agent", "kind": "turn"}]


def test_a_stranger_never_appears():
    """Neither lane may leak a peer that is not a member (a kicked peer's
    leftover cursor or keypress must vanish with its membership)."""
    typing, receipts = _presence(
        activity={"p-gone1234": {"read_to": 9, "read_at": 999.0, "last_wrote": 0}},
        keys={"p-gone1234": 999.5},
        busy_agent="p-gone1234")
    assert typing == []
    assert receipts == []


# ── wiring: a correct helper nobody calls changes nothing ──────────────────

def _source():
    return Path(ws_mod.__file__).read_text(encoding="utf-8")


def test_the_projection_actually_calls_the_helper():
    tree = ast.parse(_source())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "derive_room_presence"]
    assert calls, "derive_room_presence is never called - the projection kept the old rule"


def test_the_payload_carries_the_receipts():
    assert '"readPositions": read_positions' in _source(), (
        "the receipts are derived but never sent to the view")


def test_room_typing_is_a_handled_message():
    source = _source()
    assert '"room_typing"' in source, "the browser's keypress signal has no handler"
    assert "_ROOM_KEYS_TYPING" in source


def test_the_old_window_is_gone():
    """MUTATION TARGET: reintroducing the two-minute read-as-typing window."""
    assert "ROOM_TYPING_WINDOW_S" not in _source()
