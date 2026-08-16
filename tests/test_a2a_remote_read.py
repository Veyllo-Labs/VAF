# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A remote peer can HEAR: read, members and log work for rooms on other machines.

The field incident this exists for: a peer joined over the wire, spoke into the
room for an hour, and never saw that the same urgent question had been put to it
three times - `read` answered "there is no room on this machine" while
`_remote_frames()` sat in the file, written for exactly this case and unused.
A group of agents that can talk but not listen does not coordinate, it
broadcasts.

The frames come from the session daemon's mirror when one holds the room, and
from a one-shot wire connection otherwise; these tests scripts both, so the
verbs' behaviour is pinned without a server.
"""
import json

import pytest
from typer.testing import CliRunner

import vaf.cli.cmd.a2a as a2a


ALICE = "p-aaaaaaaaaa"
ME = "p-mmmmmmmmmm"


def _frame(kind, lamport, sender=ALICE, text=None, display=None):
    from vaf.core.a2a.frame import Frame
    body = {"text": text if text is not None else f"line {lamport}"}
    if display:
        body["display"] = display
    return Frame.from_dict({
        "v": 1, "id": f"m-{lamport}", "room": "room-far", "seq": lamport,
        "lamport": lamport, "ts": 0.0, "from": sender, "role": "peer",
        "to": {"room": True}, "kind": kind, "body": body,
    })


@pytest.fixture
def far_room(monkeypatch, tmp_path):
    """A remote record for room-far, with the record store under tmp."""
    record = {"url": "wss://192.0.2.9:8443/ws/a2a/room-far", "seat": "s-x",
              "peer": ME, "cursor": 0}
    saves = []
    monkeypatch.setattr(a2a, "_remote_record",
                        lambda room_id: dict(record) if room_id == "room-far" else None)
    monkeypatch.setattr(a2a, "_remote_save",
                        lambda room_id, rec: saves.append((room_id, dict(rec))))
    monkeypatch.setattr(a2a, "_open_local", lambda room_id: None)
    import vaf.core.a2a.session as sess
    monkeypatch.setattr(sess, "session_pid", lambda room_id: 0)
    return {"record": record, "saves": saves}


def _wire(monkeypatch, frames):
    calls = []

    def fake(record):
        calls.append(1)
        return list(frames)

    monkeypatch.setattr(a2a, "_remote_frames", fake)
    return calls


def test_read_prints_a_remote_room_instead_of_denying_it(far_room, monkeypatch):
    _wire(monkeypatch, [_frame("join", 1, display="Alice"),
                        _frame("say", 2), _frame("say", 3)])
    result = CliRunner().invoke(a2a.app, ["read", "room-far"])
    assert result.exit_code == 0
    rows = [json.loads(l) for l in result.stdout.splitlines() if l.startswith("{")]
    assert [r["lamport"] for r in rows] == [2, 3]
    assert rows[0]["display"] == "Alice"
    assert all(r["remote"] for r in rows)


def test_read_advances_the_seat_cursor_after_printing(far_room, monkeypatch):
    _wire(monkeypatch, [_frame("say", 2), _frame("say", 3)])
    CliRunner().invoke(a2a.app, ["read", "room-far"])
    assert far_room["saves"], "the cursor must be saved"
    assert far_room["saves"][-1][1]["cursor"] == 3


def test_read_keep_position_leaves_the_cursor_alone(far_room, monkeypatch):
    _wire(monkeypatch, [_frame("say", 2)])
    CliRunner().invoke(a2a.app, ["read", "room-far", "--keep-position"])
    assert far_room["saves"] == []


def test_read_skips_what_the_cursor_already_covered(far_room, monkeypatch):
    far_room["record"]["cursor"] = 2
    _wire(monkeypatch, [_frame("say", 2), _frame("say", 3)])
    result = CliRunner().invoke(a2a.app, ["read", "room-far"])
    rows = [json.loads(l) for l in result.stdout.splitlines() if l.startswith("{")]
    assert [r["lamport"] for r in rows] == [3]


def test_read_hides_the_readers_own_echo(far_room, monkeypatch):
    _wire(monkeypatch, [_frame("say", 2, sender=ME), _frame("say", 3)])
    result = CliRunner().invoke(a2a.app, ["read", "room-far"])
    rows = [json.loads(l) for l in result.stdout.splitlines() if l.startswith("{")]
    assert [r["lamport"] for r in rows] == [3]


def test_a_live_session_mirror_is_preferred_over_the_wire(far_room, monkeypatch):
    """The mirror is free - the daemon already paid for the frames. Dialling
    out despite a running session would burn the lease the daemon holds."""
    wire_calls = _wire(monkeypatch, [])
    import vaf.core.a2a.session as sess
    monkeypatch.setattr(sess, "session_pid", lambda room_id: 4242)
    monkeypatch.setattr(sess, "mirror_frames",
                        lambda room_id, since_lamport=0: [_frame("say", 5)])
    result = CliRunner().invoke(a2a.app, ["read", "room-far"])
    rows = [json.loads(l) for l in result.stdout.splitlines() if l.startswith("{")]
    assert [r["lamport"] for r in rows] == [5]
    assert wire_calls == [], "a live session means no new wire connection"


def test_members_folds_the_roster_from_the_frames(far_room, monkeypatch):
    _wire(monkeypatch, [
        _frame("join", 1, sender=ALICE, display="Alice"),
        _frame("join", 2, sender="p-bbbbbbbbbb", display="Bob"),
        _frame("leave", 3, sender="p-bbbbbbbbbb"),
        _frame("say", 4, sender=ALICE),
    ])
    result = CliRunner().invoke(a2a.app, ["members", "room-far"])
    rows = [json.loads(l) for l in result.stdout.splitlines() if l.startswith("{")]
    assert [r["peer"] for r in rows] == [ALICE]
    assert rows[0]["display"] == "Alice"
    assert rows[0]["stale"] is None, (
        "the wire cannot know liveness; inventing 'stale' for people who are "
        "merely far away is how a roster reads 'everyone absent'"
    )


def test_log_renders_the_conversation_for_a_human(far_room, monkeypatch):
    _wire(monkeypatch, [_frame("join", 1, display="Alice"),
                        _frame("say", 2, text="hello there"),
                        _frame("report", 3, text="done")])
    result = CliRunner().invoke(a2a.app, ["log", "room-far"])
    assert result.exit_code == 0
    assert "Alice [peer]: hello there" in result.stdout
    assert "(report)" in result.stdout
    assert "join" not in result.stdout.lower()


def test_log_follow_names_the_live_lanes_instead_of_pretending(far_room, monkeypatch):
    _wire(monkeypatch, [])
    result = CliRunner().invoke(a2a.app, ["log", "room-far", "--follow"])
    assert result.exit_code != 0
    err = getattr(result, "stderr", "") or str(result.exception or "")
    assert "session" in err or "wait" in err


def test_an_unknown_room_still_fails_honestly(monkeypatch):
    monkeypatch.setattr(a2a, "_open_local", lambda room_id: None)
    monkeypatch.setattr(a2a, "_remote_record", lambda room_id: None)
    for verb in (["read", "room-none"], ["members", "room-none"], ["log", "room-none"]):
        result = CliRunner().invoke(a2a.app, verb)
        assert result.exit_code != 0
        err = (getattr(result, "stderr", "") or "").lower()
        assert "no room" in err
