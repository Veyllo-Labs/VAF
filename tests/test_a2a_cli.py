# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf a2a`: the door a foreign agent walks through.

`wait` gets the most attention here, because a foreign agent blocks on that one line
between turns. Its timeout must be its own exit code rather than an error, a closed
room must end it distinguishably, and an interruption must never swallow a message.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import vaf.core.a2a.store as store_mod
from vaf.cli.cmd import a2a as a2a_cmd
from vaf.core.a2a.room import Room

runner = CliRunner()


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    """Point every store at a temporary directory and pin the acting identity."""
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    monkeypatch.setattr(a2a_cmd, "_key", lambda: "scope-terminal")
    return tmp_path


def _lines(result):
    return [json.loads(line) for line in result.stdout.strip().splitlines() if line.strip()]


def _guest(room_id, base, display="Codex"):
    """A second participant, standing in for a foreign agent."""
    room = Room.open(room_id, base=base)
    return room, room.join(display=display, scope_id=None, peer_id="p-codex")


# ── the flag that must not exist ────────────────────────────────────────────

def test_no_command_offers_a_scope_flag():
    """MUTATION: add --scope to any command.

    Identity here is the machine owner's, because anyone who can run `vaf a2a` can run
    `vaf`. A flag would not make the lane stricter; it would only invite somebody to
    pass another tenant's scope and expect it to be honoured.
    """
    import inspect
    offenders = []
    for name, fn in vars(a2a_cmd).items():
        if not callable(fn) or name.startswith("_"):
            continue
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            continue
        for param in params:
            if "scope" in param.lower():
                offenders.append(f"{name}:{param}")
    assert not offenders, f"a2a commands must not take a scope: {offenders}"

    # The declaration form, not the word: the module docstring explains the flag's
    # absence and must be allowed to name it.
    source = Path(a2a_cmd.__file__).read_text(encoding="utf-8")
    assert '"--scope"' not in source


# ── rooms ───────────────────────────────────────────────────────────────────

def test_create_join_say_read(rooms):
    created = runner.invoke(a2a_cmd.app, ["create", "--kind", "round", "--topic", "planning"])
    assert created.exit_code == 0
    room_id = _lines(created)[0]["room"]

    room, guest = _guest(room_id, rooms)
    room.say(guest, "anyone there?")

    read = runner.invoke(a2a_cmd.app, ["read", room_id])
    assert read.exit_code == 0
    rows = _lines(read)
    assert [r["text"] for r in rows] == ["anyone there?"]
    assert rows[0]["display"] == "Codex"

    said = runner.invoke(a2a_cmd.app, ["say", room_id, "I am here"])
    assert said.exit_code == 0 and _lines(said)[0]["ok"] is True
    assert [r["text"] for r in room.transcript() if r["kind"] == "say"][-1] == "I am here"


def test_read_moves_the_position_and_keep_position_does_not(rooms):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    room, guest = _guest(room_id, rooms)
    room.say(guest, "one")

    assert len(_lines(runner.invoke(a2a_cmd.app, ["read", room_id, "--keep-position"]))) == 1
    assert len(_lines(runner.invoke(a2a_cmd.app, ["read", room_id]))) == 1
    assert _lines(runner.invoke(a2a_cmd.app, ["read", room_id])) == []


def test_an_unknown_room_has_its_own_exit_code(rooms):
    result = runner.invoke(a2a_cmd.app, ["say", "room-ghost", "hello"])
    assert result.exit_code == a2a_cmd.EXIT_NO_ROOM


def test_speaking_without_joining_has_its_own_exit_code(rooms):
    Room.create(kind="round", owner_scope=None, base=rooms, room_id="room-x")
    result = runner.invoke(a2a_cmd.app, ["say", "room-x", "hello"])
    assert result.exit_code == a2a_cmd.EXIT_NO_ROOM
    assert "vaf a2a join room-x" in result.stderr


def test_a_room_refusal_is_its_own_exit_code(rooms):
    """A round refuses a directive. The exit code separates "you may not" from
    "it broke", which is the difference a script has to branch on."""
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create", "--kind", "round"]))[0]["room"]
    result = runner.invoke(a2a_cmd.app, ["directive", room_id, "obey"])
    assert result.exit_code == a2a_cmd.EXIT_REFUSED


def test_a_traversing_room_id_is_refused(rooms):
    result = runner.invoke(a2a_cmd.app, ["read", "../../etc"])
    assert result.exit_code == a2a_cmd.EXIT_REFUSED


# ── invitations ─────────────────────────────────────────────────────────────

def test_invite_prints_the_line_a_human_carries_over(rooms):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    invited = runner.invoke(a2a_cmd.app, ["invite", room_id, "--display", "Codex"])
    assert invited.exit_code == 0

    row = _lines(invited)[0]
    assert row["join"] == f"vaf a2a join {room_id} --ticket {row['ticket']}"


def test_a_spent_ticket_is_refused_the_second_time(rooms, monkeypatch):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    ticket = _lines(runner.invoke(a2a_cmd.app, ["invite", room_id]))[0]["ticket"]

    # A different participant redeems it: the terminal is already a member.
    monkeypatch.setattr(a2a_cmd, "_key", lambda: "scope-guest")
    assert runner.invoke(a2a_cmd.app, ["join", room_id, "--ticket", ticket]).exit_code == 0

    monkeypatch.setattr(a2a_cmd, "_key", lambda: "scope-third")
    again = runner.invoke(a2a_cmd.app, ["join", room_id, "--ticket", ticket])
    assert again.exit_code == a2a_cmd.EXIT_REFUSED


def test_joining_twice_is_reported_rather_than_refused(rooms):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    again = runner.invoke(a2a_cmd.app, ["join", room_id])
    assert again.exit_code == 0
    assert _lines(again)[0]["already"] is True


# ── wait: the line a foreign agent blocks on ───────────────────────────────

def test_wait_returns_its_own_code_when_nothing_arrives(rooms):
    """MUTATION: raise an error or exit 0 on timeout.

    A script has to tell "nothing was said" apart from "it broke". Collapsing the two
    is what makes a polling loop either spin or die.
    """
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    result = runner.invoke(a2a_cmd.app,
                           ["wait", room_id, "--timeout", "0.2", "--interval", "0.05"])
    assert result.exit_code == a2a_cmd.EXIT_TIMEOUT
    assert result.stdout.strip() == ""


def test_wait_prints_what_is_already_there_and_stops_at_n(rooms):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    room, guest = _guest(room_id, rooms)
    room.say(guest, "first")
    room.say(guest, "second")

    result = runner.invoke(a2a_cmd.app, ["wait", room_id, "--n", "1", "--timeout", "1"])
    assert result.exit_code == a2a_cmd.EXIT_OK
    assert [r["text"] for r in _lines(result)] == ["first"]

    # The second is still pending: the position moved by exactly what was printed.
    rest = runner.invoke(a2a_cmd.app, ["wait", room_id, "--n", "1", "--timeout", "1"])
    assert [r["text"] for r in _lines(rest)] == ["second"]


def test_wait_on_a_closed_room_ends_with_its_own_code_and_says_why(rooms):
    """MUTATION: exit 0 or block forever on a closed room.

    Blocking on a room that can never speak again is the worst of the three, because
    the foreign agent hangs with no way to learn why.
    """
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create", "--kind", "chain"]))[0]["room"]
    _guest(room_id, rooms)
    # A worker may not close; the terminal opened the room and leads it.
    assert runner.invoke(a2a_cmd.app, ["close", room_id, "--reason", "finished"]).exit_code == 0

    result = runner.invoke(a2a_cmd.app, ["wait", room_id, "--timeout", "1"])
    assert result.exit_code == a2a_cmd.EXIT_CLOSED
    assert [r["kind"] for r in _lines(result)] == ["close"]


def test_wait_advances_the_position_only_after_the_line_is_out(rooms):
    """MUTATION: move the cursor for the whole batch before printing.

    An interruption between the two must cost a repeated message, never a lost one.
    Printing per frame and advancing per frame is what makes that true even when the
    process is killed mid-batch.
    """
    source = Path(a2a_cmd.__file__).read_text(encoding="utf-8")
    body = source.split("def wait(")[1]
    emit_at = body.index("_emit(_row(entry))")
    cursor_at = body.index("room.store.set_cursor(identity.peer_id, entry[\"lamport\"])")
    assert emit_at < cursor_at, "the cursor moves before the line is written"


# ── membership and reading for humans ──────────────────────────────────────

def test_members_lists_everyone_with_their_role(rooms):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create", "--kind", "chain"]))[0]["room"]
    _guest(room_id, rooms)

    rows = _lines(runner.invoke(a2a_cmd.app, ["members", room_id]))
    by_display = {r["display"]: r for r in rows}
    assert by_display["terminal"]["role"] == "leader"
    assert by_display["Codex"]["role"] == "worker"


def test_list_shows_unread_and_mode(rooms):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    room, guest = _guest(room_id, rooms)
    room.say(guest, "ping")

    row = _lines(runner.invoke(a2a_cmd.app, ["list"]))[0]
    assert row["room"] == room_id and row["unread"] == 1 and row["mode"] == "assist"


def test_log_renders_a_group_chat_for_a_human(rooms):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create", "--kind", "chain"]))[0]["room"]
    room, guest = _guest(room_id, rooms)
    room.say(guest, "on it")
    room.report(guest, "done", status="completed")

    out = runner.invoke(a2a_cmd.app, ["log", room_id])
    assert out.exit_code == 0
    assert "Codex [worker]: on it" in out.stdout
    assert "Codex [worker] (report): [completed] done" in out.stdout


def test_export_writes_markdown_with_artifacts_listed_apart(rooms, tmp_path):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create", "--kind", "chain",
                                                 "--topic", "the logs"]))[0]["room"]
    room, guest = _guest(room_id, rooms)
    room.report(guest, "here it is", artifacts=[{"name": "log.txt", "text": "..."}])

    target = tmp_path / "out.md"
    result = runner.invoke(a2a_cmd.app, ["export", room_id, "--out", str(target)])
    assert result.exit_code == 0

    text = target.read_text(encoding="utf-8")
    assert "**Topic:** the logs" in text
    assert "## Artifacts" in text and "`log.txt` from Codex" in text


# ── the chain, end to end through the CLI ──────────────────────────────────

def test_a_leader_directs_a_worker_who_hires_and_reports(rooms):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create", "--kind", "chain"]))[0]["room"]
    room, worker = _guest(room_id, rooms, display="Worker")

    assert runner.invoke(a2a_cmd.app, ["directive", room_id, "collect the logs"]).exit_code == 0

    child, _frame = room.hire(worker, purpose="log reading")
    room.report(worker, "logs collected", status="completed")

    kinds = [f.kind for f in room.store.frames()]
    assert "directive" in kinds and "hire" in kinds and "report" in kinds

    texts = [r["text"] for r in room.transcript()]
    child_lead = next(iter(child.roles()))
    from vaf.core.a2a.room import Identity
    child.say(Identity(child_lead, "Worker", None, "leader"), "inside the child")
    assert "inside the child" not in texts


# ── what the first live run found ───────────────────────────────────────────

def test_a_guest_with_a_ticket_joins_even_when_the_owner_already_did(rooms):
    """MUTATION: short-circuit on "already a member" before looking at the ticket.

    A foreign agent driving this CLI shares the machine owner's derived handle, so the
    short-circuit locked every guest out of any room the owner had already joined. The
    first live run hit it on the first invitation.
    """
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    ticket = _lines(runner.invoke(a2a_cmd.app, ["invite", room_id, "--display", "Codex"]))[0]["ticket"]

    joined = _lines(runner.invoke(a2a_cmd.app, ["join", room_id, "--ticket", ticket,
                                                "--display", "Codex"]))[0]
    assert joined.get("already") is not True
    assert joined["peer"] != _lines(runner.invoke(a2a_cmd.app, ["list"]))[0]["peer"]


def test_a_guest_can_act_as_its_own_handle(rooms):
    """MUTATION: resolve every command through the derived key only.

    Without this a guest could join and then never say anything, because every later
    command acted as the machine owner instead. Found live, one command after the one
    above.
    """
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    ticket = _lines(runner.invoke(a2a_cmd.app, ["invite", room_id]))[0]["ticket"]
    guest = _lines(runner.invoke(a2a_cmd.app, ["join", room_id, "--ticket", ticket,
                                               "--display", "Codex"]))[0]["peer"]

    assert runner.invoke(a2a_cmd.app, ["say", room_id, "hello", "--as", guest]).exit_code == 0

    room = Room.open(room_id, base=rooms)
    spoken = [r for r in room.transcript() if r["kind"] == "say"][-1]
    assert spoken["peer"] == guest and spoken["display"] == "Codex"


def test_the_env_var_names_the_acting_peer_too(rooms, monkeypatch):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    ticket = _lines(runner.invoke(a2a_cmd.app, ["invite", room_id]))[0]["ticket"]
    guest = _lines(runner.invoke(a2a_cmd.app, ["join", room_id, "--ticket", ticket,
                                               "--display", "Codex"]))[0]["peer"]

    monkeypatch.setenv("VAF_A2A_PEER", guest)
    assert runner.invoke(a2a_cmd.app, ["say", room_id, "from the env"]).exit_code == 0

    room = Room.open(room_id, base=rooms)
    assert [r for r in room.transcript() if r["kind"] == "say"][-1]["peer"] == guest


def test_acting_as_a_stranger_is_refused(rooms):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    result = runner.invoke(a2a_cmd.app, ["say", room_id, "hi", "--as", "p-nobody"])
    assert result.exit_code == a2a_cmd.EXIT_NO_ROOM


def test_bookkeeping_frames_read_as_sentences_not_empty_lines(rooms):
    """MUTATION: print entry["text"] directly in any of the renderers.

    A join carries no text - it says WHO, not what - so printing the body alone gives
    "Worker (join):" and nothing after it. The live run showed exactly that.
    """
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create", "--kind", "chain"]))[0]["room"]
    room, worker = _guest(room_id, rooms, display="Worker")
    room.hire(worker, purpose="log reading")

    out = runner.invoke(a2a_cmd.app, ["log", room_id]).stdout
    assert "Worker [worker] (join): joined" in out
    assert "(hire): opened room-" in out and "for log reading" in out
    assert not any(line.rstrip().endswith(":") for line in out.splitlines()), out


# ── addressing ─────────────────────────────────────────────────────────────

def test_a_leading_mention_addresses_one_member(rooms):
    """MUTATION: resolve the name in the CLI instead of asking the room.

    Only the room knows who is in it, and a lookup here would be a second copy of the
    member table that drifts the moment somebody joins.
    """
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    room, bob = _guest(room_id, rooms, display="Bob")

    assert runner.invoke(a2a_cmd.app, ["say", room_id, "@Bob can you look"]).exit_code == 0

    said = [f for f in room.store.frames() if f.kind == "say"][-1]
    assert said.to == {"peer": bob.peer_id}


def test_a_mention_in_the_middle_stays_a_message_to_everyone(rooms):
    """MUTATION: match a mention anywhere.

    "ask @Bob about it" is a sentence ABOUT Bob said to the room. Turning it into a
    private aside would hide it from everyone the writer meant to tell.
    """
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    room, _bob = _guest(room_id, rooms, display="Bob")

    runner.invoke(a2a_cmd.app, ["say", room_id, "ask @Bob about it"])

    said = [f for f in room.store.frames() if f.kind == "say"][-1]
    assert said.to == {"room": True}


def test_an_explicit_to_wins_over_a_mention(rooms):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    room, bob = _guest(room_id, rooms, display="Bob")

    runner.invoke(a2a_cmd.app, ["say", room_id, "@Bob hi", "--to", "p-codex"])
    said = [f for f in room.store.frames() if f.kind == "say"][-1]
    assert said.to == {"peer": "p-codex"}


def test_the_log_marks_who_a_line_was_aimed_at(rooms):
    """MUTATION: drop the arrow from the human view.

    The transcript shows everything to everyone - that is deliberate - so the only way
    to tell an aside from a broadcast is to say so.
    """
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    room, bob = _guest(room_id, rooms, display="Bob")
    runner.invoke(a2a_cmd.app, ["say", room_id, "@Bob the logs"])

    out = runner.invoke(a2a_cmd.app, ["log", room_id]).stdout
    assert "-> Bob" in out
    assert "the logs" in out


# ── the audit view ──────────────────────────────────────────────────────────

def test_audit_lists_the_acts_without_the_words(rooms):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create", "--kind", "round"]))[0]["room"]
    runner.invoke(a2a_cmd.app, ["say", room_id, "the password is hunter2"])

    out = runner.invoke(a2a_cmd.app, ["audit", room_id])
    assert out.exit_code == 0
    assert "joined" in out.stdout and "message sent" in out.stdout
    assert "hunter2" not in out.stdout, "the audit printed what was said"


def test_audit_json_is_one_object_per_line(rooms):
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    rows = _lines(runner.invoke(a2a_cmd.app, ["audit", room_id, "--json"]))

    assert rows and all("event" in row and "lamport" in row for row in rows)
    assert all("text" not in row for row in rows)


def test_an_empty_room_audits_to_a_sentence_not_an_error(rooms):
    """A room with nothing in it is a normal state, not a failure. An exit code here
    would make a script treat "nothing happened" as "something broke"."""
    room_id = _lines(runner.invoke(a2a_cmd.app, ["create"]))[0]["room"]
    runner.invoke(a2a_cmd.app, ["leave", room_id])

    out = runner.invoke(a2a_cmd.app, ["audit", room_id])
    assert out.exit_code == 0
