# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The room learning interval counts what was SAID, not what was framed.

"Every fifteen messages" was implemented as every fifteen FRAMES, and a room
produces bookkeeping alongside every visible contribution - pings to quiet
members, joins, tallies. Measured on a live three-voice room, the owner saw the
agent learn every ~2.5 of their messages. The bookkeeping was not even the main
driver (29 of 286 frames): three voices simply say three times as much. The unit
was wrong either way, and with thirty agents a frame counter would fire inside a
single exchange.
"""
import ast
from pathlib import Path

from vaf.core.a2a.frame import Frame
from vaf.core.a2a.room import contribution_count

REPO_ROOT = Path(__file__).resolve().parents[1]


def _frame(kind: str, sender: str = "p-a", lamport: int = 1) -> Frame:
    return Frame.from_dict({
        "v": 1, "id": f"id-{kind}-{lamport}", "room": "room-x", "seq": lamport,
        "lamport": lamport, "ts": 0.0, "from": sender, "role": "peer",
        "to": {"room": True}, "kind": kind, "body": {"text": "hello"},
    })


def test_speech_counts_and_bookkeeping_does_not():
    frames = [
        _frame("join", lamport=1),
        _frame("say", lamport=2),
        _frame("ping", lamport=3),
        _frame("ask", lamport=4),
        _frame("ack", lamport=5),
        _frame("answer", lamport=6),
        _frame("tally", lamport=7),
        _frame("report", lamport=8),
        _frame("role", lamport=9),
        _frame("leave", lamport=10),
    ]
    assert contribution_count(frames) == 4


def test_thirty_quiet_members_do_not_advance_the_clock():
    """The scaling failure this exists for: pings and joins grow with the member
    count, speech does not. A room full of check-ins must count as silence."""
    frames = [_frame("ping", sender=f"p-{i}", lamport=i + 1) for i in range(30)]
    frames += [_frame("join", sender=f"p-{i}", lamport=100 + i) for i in range(30)]
    frames.append(_frame("say", lamport=999))
    assert contribution_count(frames) == 1


def test_the_room_learn_path_counts_contributions_not_frames():
    """The wiring, source-checked: the compaction call in the room-learn closure
    must pass contribution_count(...), never len(...). A correct helper that
    nobody calls would leave the owner exactly where they started."""
    src = (REPO_ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "run_session_compaction_sync"):
            calls.append(node)
    assert calls, "the room-learn compaction call disappeared - rewire this test"
    for call in calls:
        count_arg = call.args[3]
        assert isinstance(count_arg, ast.Call) and \
            getattr(count_arg.func, "id", "") == "contribution_count", (
                f"agent.py line {call.lineno}: the compaction count must be "
                f"contribution_count(frames), not a raw frame count"
            )
