# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The protocol document is the one a stranger implements from, so it is checked.

A design document nobody checks becomes a description of what the code used to do, and
this one is worse than most if it rots: it is read by people who cannot see the code,
on machines that are not this one, and a wrong constant there costs them a debugging
session they have no way to win.

So the numbers and the tables in it are asserted against the runtime rather than
proof-read. What is NOT asserted here is prose - that is what review is for.
"""
from pathlib import Path

import pytest

from vaf.core.a2a import frame as frame_mod
from vaf.core.a2a import room as room_mod
from vaf.core.a2a import store as store_mod

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "agents" / "A2A_PROTOCOL.md"


@pytest.fixture(scope="module")
def text() -> str:
    return DOC.read_text(encoding="utf-8")


# ── the frame ──────────────────────────────────────────────────────────────

def test_the_version_and_protocol_name_are_the_ones_in_the_code(text):
    assert f'`protocol = "{frame_mod.PROTOCOL}"`' in text
    assert f"`VERSION = {frame_mod.VERSION}`" in text


def test_every_kind_is_listed(text):
    """MUTATION: add a kind to the code and not to the document.

    An implementer builds a receiver from this list. A kind missing from it is a kind
    the stranger's agent treats as unknown, which by rule 2 means it displays it and
    does nothing - the failure is silent on both sides.
    """
    kinds = text.split("### Kinds")[1].split("###")[0]
    for kind in frame_mod.KINDS:
        assert f"`{kind}`" in kinds, f"the document does not list the kind {kind!r}"


def test_every_report_status_is_listed(text):
    statuses = text.split("`report.body.status`")[1].split("\n\n")[0]
    for status in frame_mod.REPORT_STATUSES:
        assert f"`{status}`" in statuses, f"status {status!r} is missing"


def test_the_wire_keys_all_have_a_row(text):
    """The field table is what somebody types their parser against."""
    table = text.split("| Field | Meaning |")[1].split("\n\n")[0]
    for key in frame_mod.WIRE_KEYS:
        assert f"| `{key}` |" in table, f"the field table has no row for {key!r}"


# ── roles ──────────────────────────────────────────────────────────────────

def test_the_role_table_matches_the_enforcement_table(text):
    """MUTATION: change a capability in the code and leave the document.

    This table tells a stranger what its agent may send. If it disagrees with the
    table that REFUSES, the stranger finds out by being refused in front of everybody
    in a room it was invited into.
    """
    section = text.split("| Role | May emit | May not |")[1].split("\n\n")[0]
    for role, allowed in room_mod.CAPABILITIES.items():
        row = next((ln for ln in section.split("\n") if ln.startswith(f"| `{role}` |")), "")
        assert row, f"no row for the role {role!r}"
        may, may_not = row.split("|")[2], row.split("|")[3]
        for kind in allowed:
            assert f"`{kind}`" in may, f"{role} may emit {kind!r} and the document omits it"
        everything = set().union(*room_mod.CAPABILITIES.values())
        for kind in everything - set(allowed):
            assert f"`{kind}`" in may_not, (
                f"{role} may NOT emit {kind!r} and the document does not say so")
            # And the may-column must contain ONLY what is allowed. Checking each
            # column positively lets a row contradict itself - the same kind in both -
            # and it is the MAY column a stranger builds their peer from, so a
            # contradiction there is read as permission.
            assert f"`{kind}`" not in may, (
                f"{role} may NOT emit {kind!r} and the document offers it anyway")


def test_the_room_kinds_and_budgets_are_the_ones_in_the_code(text):
    for kind in room_mod.ROOM_KINDS:
        assert f"`{kind}`" in text
    assert f"`max_depth`\n({room_mod.DEFAULT_MAX_DEPTH})" in text or \
           f"({room_mod.DEFAULT_MAX_DEPTH})" in text
    assert f"({room_mod.DEFAULT_MAX_CHILDREN})" in text


def test_every_mode_is_documented_with_the_right_default(text):
    modes = text.split("| Mode | An arriving frame may |")[1].split("\n\n")[0]
    for mode in room_mod.ROOM_MODES:
        assert f"`{mode}`" in modes, f"mode {mode!r} is undocumented"
    assert f"`{room_mod.DEFAULT_MODE}` **(default)**" in modes


def test_every_participant_lane_is_named(text):
    lanes = text.split("Lanes are")[1].split("\n\n")[0]
    for lane in room_mod.PARTICIPANT_LANES:
        assert f"`{lane}`" in lanes


# ── storage ────────────────────────────────────────────────────────────────

def test_the_format_tag_is_the_pinned_one(text):
    assert store_mod.ROOM_FORMAT in text


def test_the_sequence_width_matches_the_layout_it_prints(text):
    assert f"<seq:{store_mod._SEQ_WIDTH:03d}d>" in text


def test_every_directory_the_store_makes_appears_in_the_layout(text):
    layout = text.split("```\n<vaf-dir>/a2a/rooms/")[1].split("```")[0]
    for name in ("room.json", "members/", "log/", "cursors/", "tickets/"):
        assert name in layout, f"the storage layout omits {name}"


# ── the CLI contract ───────────────────────────────────────────────────────

def test_every_command_is_listed_and_every_listed_one_exists(text):
    """MUTATION: add a command and leave the document.

    The block in this document is what a foreign agent is told it can run.
    """
    from typer.main import get_command

    from vaf.cli.cmd import a2a as a2a_cmd

    listed = set(text.split("```\ncreate")[1].split("```")[0].split()) | {"create"}
    real = set(get_command(a2a_cmd.app).commands)

    assert not (real - listed), f"undocumented commands: {sorted(real - listed)}"
    assert not (listed - real), f"the document names commands that do not exist: {sorted(listed - real)}"


def test_the_exit_codes_are_the_ones_the_cli_returns(text):
    from vaf.cli.cmd import a2a as a2a_cmd

    table = text.split("| Exit | Meaning |")[1].split("\n\n")[0]
    for name, code in (("EXIT_OK", 0), ("EXIT_ERROR", 1), ("EXIT_REFUSED", 2),
                       ("EXIT_NO_ROOM", 3), ("EXIT_TIMEOUT", 4), ("EXIT_CLOSED", 5)):
        assert getattr(a2a_cmd, name) == code, f"{name} moved and the document did not"
        assert f"| {code} |" in table


def test_the_scope_flag_is_absent_from_both_the_cli_and_the_promise(text):
    """Asserted against the COMMAND TABLE, not against the source text: the module
    docstring says out loud that the flag does not exist, and a grep for the string
    would fire on the very sentence that promises its absence."""
    from typer.main import get_command

    from vaf.cli.cmd import a2a as a2a_cmd

    for name, command in get_command(a2a_cmd.app).commands.items():
        options = {opt for param in command.params for opt in param.opts}
        assert "--scope" not in options, f"vaf a2a {name} grew a --scope flag"
    assert "no `--scope` flag" in text


# ── the claims that are load-bearing ───────────────────────────────────────

def test_the_remote_lane_is_documented_as_built(text):
    """This test pinned the GAP while the remote client did not exist - a document
    implying it did would have sent somebody to another machine to debug a command
    that was never built. The client exists now, so the same honesty points the
    other way: the flag must exist, the seat must be explained (it is the one piece
    of the wire a stranger cannot rediscover from an error message), and the old
    gap sentences must be gone - a doc that says both is worse than either."""
    from typer.main import get_command

    from vaf.cli.cmd import a2a as a2a_cmd

    join = get_command(a2a_cmd.app).commands["join"]
    options = {opt for param in join.params for opt in param.opts}
    assert "--url" in options, "the remote join lost its flag and the doc still promises it"

    assert "no CLI client for the socket yet" not in text
    assert "not possible through the CLI today" not in text
    assert "--url" in text
    assert "seat" in text.lower(), "the seat mechanism is undocumented"
    from vaf.core.a2a.room import Room
    assert f"`{Room.SEAT_PREFIX}" in text or f"{Room.SEAT_PREFIX}<peer>" in text, (
        "the seat credential's shape is not written down")


def test_the_ordering_rule_is_stated_and_ts_is_called_advisory(text):
    assert "`(lamport, from, seq)`" in text
    assert "ADVISORY" in text
    # and the code agrees about what the sort key is
    import inspect
    key = inspect.getsource(frame_mod.canonical_sort_key)
    assert "lamport" in key and "sender" in key and "seq" in key
    assert "ts" not in key.split("return")[1], "the sort key started reading the clock"


def test_the_conformance_list_is_numbered_without_holes(text):
    """Read off the table rather than counted into the test.

    This asserted a hardcoded range and therefore stopped one item short the moment the
    list grew: C15 was added and nothing here noticed, which is the exact failure a
    guard exists to prevent - it went on passing while covering less. Numbering the
    items 1..n with no gaps is the property that actually matters (a hole means an item
    was written and then lost) and it needs no edit when the list grows.
    """
    import re

    table = "| C1 |" + text.split("| C1 |")[1].split("\n\n")[0]
    found = sorted(int(n) for n in re.findall(r"^\| C(\d+) \|", table, re.MULTILINE))
    assert found, "the conformance table has no items at all"
    assert found == list(range(1, len(found) + 1)), (
        f"the conformance list is not 1..n without holes: {found}")
    assert len(found) >= 15, (
        f"the conformance list shrank to {len(found)} items; removing a promise is a "
        f"decision, not a tidy-up")


def test_the_document_is_reachable_from_the_index_and_the_rule_table():
    """A design document nothing points at is a document nobody reads before touching
    the subsystem, which is the exact failure the rule table exists to prevent."""
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "A2A_PROTOCOL.md" in index

    rules = ROOT / "CLAUDE.md"
    if rules.exists():                     # gitignored; absent in a fresh clone
        assert "A2A_PROTOCOL.md" in rules.read_text(encoding="utf-8")
