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

# The REAL identity resolver, captured before any fixture replaces it: the module-wide
# `rooms` fixture pins a stand-in, and a test that wants to exercise the real one has to
# hold on to it from before that happened.
REAL_KEY = a2a_cmd._key


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    """Point every store at a temporary directory and pin the acting identity."""
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    monkeypatch.setattr(a2a_cmd, "_key", lambda room_id="": "scope-terminal")
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
    monkeypatch.setattr(a2a_cmd, "_key", lambda room_id="": "scope-guest")
    assert runner.invoke(a2a_cmd.app, ["join", room_id, "--ticket", ticket]).exit_code == 0

    monkeypatch.setattr(a2a_cmd, "_key", lambda room_id="": "scope-third")
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
    # The opener appears under the ACCOUNT's name, not under the literal "terminal" -
    # that is a lane, not a person, and it put the machine owner in the room called
    # after the thing they typed into. Whatever the account is called here, there is
    # exactly one member who is not the guest, and that one leads.
    leader = next(r for r in rows if r["display"] != "Codex")
    assert leader["role"] == "leader"
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


def test_report_carries_progress_and_names_the_shape_it_wants(rooms):
    """MUTATION: drop an unparsable --progress silently, or omit it from the body.

    The caller here is usually a machine reading the error: "3 von 5" has to come
    back as an instruction, because a silently dropped count leaves the board
    looking like the worker never reported at all.
    """
    created = runner.invoke(a2a_cmd.app, ["create", "--kind", "round"])
    room_id = json.loads(created.stdout.strip().splitlines()[-1])["room"]
    asked = runner.invoke(a2a_cmd.app, ["say", room_id, "please do the thing"])
    task_id = json.loads(asked.stdout.strip().splitlines()[-1])["id"]

    ok = runner.invoke(a2a_cmd.app, [
        "report", room_id, "on it", "--status", "working", "--reply-to", task_id,
        "--progress", "3/5", "--step", "writing the tests"])
    assert ok.exit_code == 0, ok.stdout

    board = runner.invoke(a2a_cmd.app, ["tasks", room_id])
    entry = [json.loads(line) for line in board.stdout.strip().splitlines()
             if json.loads(line)["id"] == task_id][0]
    assert entry["progress"] == {"done": 3, "total": 5, "step": "writing the tests"}

    bad = runner.invoke(a2a_cmd.app, [
        "report", room_id, "on it", "--status", "working", "--progress", "3 von 5"])
    assert bad.exit_code != 0
    # The refusal goes to stderr, like every other _fail in this CLI.
    assert "DONE/TOTAL" in (bad.stderr if bad.stderr else bad.output), (
        "the refusal must name the shape it wants")


def test_howto_reprints_the_briefing_for_a_room_you_are_already_in(rooms):
    """MUTATION: write a second, shorter reference instead of reusing the briefing.

    An invitation is read once, in a session that may be long over. An agent that
    lost it could sit in a room it is a member of and not know how to report -
    and two differently worded references would leave it deciding which is
    current. Same text, join step replaced by its own handle.
    """
    created = runner.invoke(a2a_cmd.app, ["create", "--kind", "round", "--topic", "planning"])
    room_id = json.loads(created.stdout.strip().splitlines()[-1])["room"]

    out = runner.invoke(a2a_cmd.app, ["howto", room_id])
    assert out.exit_code == 0, out.stdout
    text = out.stdout
    assert "already in" in text
    assert "VAF_A2A_PEER=" in text, "it must name the handle to act as"
    assert "--ticket" not in text, "nothing is redeemed again"
    for command in ("vaf a2a wait", "vaf a2a say", "vaf a2a report"):
        assert command in text, f"the reference lost {command}"
    assert "--progress" in text, "the reminder must teach progress too"


def test_joining_answers_with_the_welcome_and_asks_on_stderr(rooms):
    """MUTATION: answer a join with the handle again, or put the nudge on stdout.

    Two promises at once. The join is where a newcomer learns the room, so the
    packet travels with it. And the ask is a SENTENCE: on stdout it would land
    in the middle of this CLI's one-JSON-object-per-line contract and break the
    machine peer that is parsing it, so it goes to stderr, where a human or an
    agent reading its own tool output still sees it.
    """
    created = runner.invoke(a2a_cmd.app, ["create", "--kind", "round", "--topic", "planning"])
    room_id = json.loads(created.stdout.strip().splitlines()[-1])["room"]
    invited = runner.invoke(a2a_cmd.app, ["invite", room_id, "--display", "Codex"])
    ticket = json.loads(invited.stdout.strip().splitlines()[-1])["ticket"]

    joined = runner.invoke(a2a_cmd.app, ["join", room_id, "--ticket", ticket])
    assert joined.exit_code == 0, joined.stdout
    line = json.loads(joined.stdout.strip().splitlines()[-1])
    # The flat fields a guest has always read stay put; the packet is beside them.
    assert line["peer"] and line["role"] and line["room"] == room_id
    packet = line["welcome"]
    assert packet["room"] == room_id and packet["you"]["peer"] == line["peer"]
    assert "say" in packet["you"]["may_send"], "the packet must say what this role may send"
    assert packet["members"], "arriving without a roster is arriving blind"
    assert packet["describe_yourself"] is True
    assert "topic" in packet and packet["kind"] == "round"

    # Every line of stdout stays parseable: the ask is not among them.
    for line in joined.stdout.strip().splitlines():
        json.loads(line)


def test_wait_asks_once_for_a_card_and_stays_quiet_with_one(rooms):
    """MUTATION: ask only at join time.

    The join happened in some earlier session, possibly days ago. `wait` is the
    command an agent runs before every turn, so it is the one place an ask
    cannot be missed - and it must stop the moment the peer has answered, or it
    becomes noise that gets filtered out.
    """
    created = runner.invoke(a2a_cmd.app, ["create", "--kind", "round"])
    room_id = json.loads(created.stdout.strip().splitlines()[-1])["room"]

    silent = runner.invoke(a2a_cmd.app, ["wait", room_id, "--timeout", "1"])
    assert "introduce" in silent.stderr, (
        "a peer that never said what it can do is never asked again")
    assert "introduce" not in silent.stdout, (
        "the ask is a sentence: on stdout it breaks the machine peer parsing "
        "this stream one JSON object per line")
    for line in silent.stdout.strip().splitlines():
        if line.strip():
            json.loads(line)

    runner.invoke(a2a_cmd.app, ["introduce", room_id, "--skills", "writes Rust"])
    described = runner.invoke(a2a_cmd.app, ["wait", room_id, "--timeout", "1"])
    assert "introduce" not in described.stderr, (
        "the ask keeps running after it was answered")


def test_the_remote_lane_answers_the_same_questions_as_the_local_one(tmp_path, monkeypatch):
    """MUTATION: leave howto, skill or tasks local-only.

    A remote peer has no room on disk, so `_room()` refuses it - which meant the
    side that needs the instructions most got "there is no room here" instead.
    They read from the handshake kept at join time, so they cost no round trip;
    only the board needs the wire, and it folds the frames with the host's own
    function rather than a second opinion.
    """
    record = {
        "url": "wss://host:8443/ws/a2a/room-far", "peer": "p-far", "role": "worker",
        "seat": "s-far", "cursor": 0,
        "welcome": {"room": "room-far", "kind": "chain", "topic": "Deploy",
                    "workspace": "/shared/room-far",
                    "you": {"peer": "p-far", "display": "Codex", "role": "worker",
                            "may_send": ["say", "report"], "card": {}},
                    "members": [], "tasks_open": 2, "describe_yourself": True},
    }
    monkeypatch.setattr(a2a_cmd, "_open_local", lambda room_id: None)
    monkeypatch.setattr(a2a_cmd, "_remote_record", lambda room_id: record)

    howto = runner.invoke(a2a_cmd.app, ["howto", "room-far"])
    assert howto.exit_code == 0, howto.stdout
    assert "VAF_A2A_PEER=p-far" in howto.stdout
    assert "vaf a2a report" in howto.stdout and "--progress 3/5" in howto.stdout

    skill = runner.invoke(a2a_cmd.app, ["skill", "room-far"])
    assert skill.exit_code == 0, skill.stdout
    assert skill.stdout.startswith("---\n") and "name: vaf_a2a_rooms" in skill.stdout
    assert "/shared/room-far" in skill.stdout, "the shared folder has to travel"


class _FakeRemoteRoom:
    """A room on another machine, standing in for the socket."""

    def __init__(self, ack, sent):
        self._ack, self._sent = ack, sent

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, payload):
        self._sent.append(payload)
        return self._ack


def test_the_remote_vote_lane_carries_the_deadline_and_admits_a_refusal(monkeypatch):
    """MUTATION: drop closes_at from the remote body, or report the ack as ok.

    Both defects were measured in the shipped lane and neither had a test, which
    is how they survived: `--closes-in` was silently dropped, so a remote vote had
    no end on the host, and a REFUSED submission printed {"ok": true} with exit 0,
    which tells an agent it voted when the room turned it away. With a deadline
    that abstains for you, believing a false success is the expensive half.
    """
    import time

    import vaf.core.a2a.client as client_mod

    record = {"url": "wss://host:8443/ws/a2a/room-far", "seat": "s-far",
              "peer": "p-far", "role": "peer", "cursor": 0, "welcome": {}}
    monkeypatch.setattr(a2a_cmd, "_open_local", lambda room_id: None)
    monkeypatch.setattr(a2a_cmd, "_remote_record", lambda room_id: record)

    sent: list = []
    committed = {"kind": "ack", "status": "committed", "frame": "f-remote-1",
                 "lamport": 7, "seq": 2}
    monkeypatch.setattr(client_mod, "RemoteRoom", type(
        "_Conn", (), {"connect": staticmethod(
            lambda url, seat, **kw: _FakeRemoteRoom(committed, sent))}))

    opened = runner.invoke(a2a_cmd.app,
                           ["vote", "room-far", "Pizza?", "-o", "ja", "-o", "nein",
                            "--closes-in", "3"])
    assert opened.exit_code == 0, opened.stdout
    body = sent[-1]["body"]
    assert body["options"] == ["ja", "nein"]
    assert body["closes_at"] == pytest.approx(time.time() + 180.0, abs=5.0), (
        "the deadline never reached the host")
    assert _lines(opened)[-1]["id"] == "f-remote-1", (
        "the frame id comes back under 'frame', and a ballot needs it")

    refused = {"kind": "ack", "status": "refused",
               "reason": "'vielleicht' is not one of this vote's options"}
    monkeypatch.setattr(client_mod, "RemoteRoom", type(
        "_Conn", (), {"connect": staticmethod(
            lambda url, seat, **kw: _FakeRemoteRoom(refused, sent))}))
    cast = runner.invoke(a2a_cmd.app,
                         ["ballot", "room-far", "00000000-0000-4000-8000-000000000001",
                          "vielleicht"])
    assert cast.exit_code != 0, "a refused ballot must not look like a cast one"
    assert '"ok": true' not in cast.stdout
    assert "not one of this vote's options" in (cast.stdout + cast.stderr)


def test_a_remote_peer_can_read_the_tally_it_voted_in(rooms, monkeypatch):
    """MUTATION: leave `votes` local-only.

    A remote peer could open a vote and cast a ballot but never see the count -
    `_room()` refuses a room that is not on this disk. Harmless while a vote just
    sat there; with a deadline that counts silence as abstention, an agent would be
    abstained from a question it had no way to look up.
    """
    room = Room.create(kind="round", owner_scope="scope-terminal", base=rooms,
                       room_id="room-far")
    host = room.join(display="Nobel", scope_id="scope-terminal", peer_id="p-host")
    guest = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    vote = room.open_vote(host, "Pizza?", options=["ja", "nein"])
    room.cast(guest, vote.id, "ja")

    monkeypatch.setattr(a2a_cmd, "_open_local", lambda room_id: None)
    monkeypatch.setattr(a2a_cmd, "_remote_record",
                        lambda room_id: {"url": "wss://host/ws/a2a/room-far",
                                         "seat": "s-far", "peer": "p-codex"})
    monkeypatch.setattr(a2a_cmd, "_remote_frames", lambda record: room.store.frames())

    seen = runner.invoke(a2a_cmd.app, ["votes", "room-far"])
    assert seen.exit_code == 0, seen.stdout
    entry = _lines(seen)[-1]
    assert entry["question"] == "Pizza?"
    assert entry["tally"] == {"ja": 1}
    assert entry["ballots"][0]["label"] == "Codex", "names come from the join frames"
    assert entry["deadline"] > entry["ts"], "and it knows when it ends"


def test_a_shared_room_names_the_accounts_it_takes(rooms, monkeypatch):
    """MUTATION: let --shared alone open the room to anybody who knows the id.

    Everything said in a room shared across accounts is readable by every member, so
    admission is a decision about a conversation and not a convenience. The id is no
    protection and was never meant to be one - it travels in invitations, in prompts
    and in log lines, and an agent can be told one inside a room message.
    """
    from vaf.core.a2a.room import Room, participant_key

    # The real lane key: the module fixture pins a stand-in, and a stand-in never
    # matches a host handle - which is derived from the account and the lane.
    monkeypatch.setattr(a2a_cmd, "_key",
                        lambda room_id="": participant_key("cli", a2a_cmd._scope()))

    opened = runner.invoke(a2a_cmd.app, ["create", "--shared", "--id", "room-shared-cli"])
    assert opened.exit_code == 0, opened.stdout
    assert _lines(opened)[-1]["shared"] is True

    owner = a2a_cmd._scope()
    room = Room.open("room-shared-cli", base=rooms)
    assert room.manifest.get("multi_scope") is True
    assert room.tenants() == [owner], "only its owner, until somebody is let in"

    admitted = runner.invoke(a2a_cmd.app, ["share", "room-shared-cli", "other-account"])
    assert admitted.exit_code == 0, admitted.stdout
    assert _lines(admitted)[-1]["accounts"] == [owner, "other-account"]
    assert Room.open("room-shared-cli", base=rooms).tenants() == [owner, "other-account"]

    # A room that holds one account refuses instead of quietly becoming a shared one.
    runner.invoke(a2a_cmd.app, ["create", "--id", "room-private-cli"])
    refused = runner.invoke(a2a_cmd.app, ["share", "room-private-cli", "other-account"])
    assert refused.exit_code != 0
    assert "shared room" in (refused.stdout + refused.stderr)


def test_members_says_who_belongs_to_whom(rooms, monkeypatch):
    """MUTATION: print the roster without the pairing.

    A foreign agent reads this instead of our surfaces. In a room with several
    households "who speaks for whom" cannot be guessed from the names, and guessing it
    is how an agent ends up answering for somebody it does not work for.
    """
    from vaf.core.a2a.room import Room, derive_peer_id, participant_key

    monkeypatch.setattr(a2a_cmd, "_key",
                        lambda room_id="": participant_key("cli", a2a_cmd._scope()))
    runner.invoke(a2a_cmd.app, ["create", "--id", "room-who"])
    room = Room.open("room-who", base=rooms)
    owner = a2a_cmd._scope()
    agent = room.join(display="Nobel", scope_id=owner,
                      peer_id=derive_peer_id(participant_key("agent", owner), "room-who"))
    guest = room.join(display="Codex", scope_id=None, peer_id="p-codex")

    rows = {r["display"]: r for r in _lines(runner.invoke(a2a_cmd.app, ["members", "room-who"]))}
    assert rows["Nobel"]["kind"] == "agent"
    assert rows["Nobel"]["partner"] == room.identity_for(a2a_cmd._key()).peer_id
    assert rows["Nobel"]["partner_display"], "the partner is named, not just pointed at"
    # The person on this terminal is the other half.
    person = next(r for r in rows.values() if r["kind"] == "human")
    assert person["partner"] == agent.peer_id
    # And a guest that named no account is left unanswered rather than guessed at.
    assert rows["Codex"]["kind"] == "unknown" and rows["Codex"]["partner"] == ""
    assert guest.peer_id in rows["Codex"]["peer"]


def test_letting_an_account_in_is_written_down_for_the_administrator(rooms, monkeypatch):
    """MUTATION: admit an account and log nothing, or key the throttle without the room.

    Admission decides who reads a conversation from then on, which is exactly the kind
    of act an administrator has to be able to reconstruct afterwards. And two rooms
    opened seconds apart are two events: the throttle used to key on kind, ip, user and
    channel alone, so the second one silently vanished - an audit that drops entries is
    worse than none, because it reads as complete.
    """
    from vaf.core.a2a.room import participant_key

    monkeypatch.setattr(a2a_cmd, "_key",
                        lambda room_id="": participant_key("cli", a2a_cmd._scope()))
    events = []
    import vaf.core.security_events as sec
    monkeypatch.setattr(sec, "log_security_event",
                        lambda kind, **fields: events.append((kind, fields)))

    for room_id in ("room-audit-1", "room-audit-2"):
        runner.invoke(a2a_cmd.app, ["create", "--shared", "--id", room_id])
        assert runner.invoke(a2a_cmd.app, ["share", room_id, "other-account"]).exit_code == 0

    assert [k for k, _f in events] == ["room_account_admitted"] * 2, (
        "an admission went unrecorded")
    assert [f["path"] for _k, f in events] == ["room-audit-1", "room-audit-2"], (
        "the room has to be in the record, and in the throttle key with it")
    assert all("other-account" in f["detail"] for _k, f in events)

    # And the throttle really keys on it, or the second line above never survives.
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "security_events.py").read_text(encoding="utf-8")
    assert 'key = f"{kind}|{ip}|{username}|{channel}|{path}"' in src


def test_an_agents_shell_reports_under_the_agent_and_only_in_its_own_room(rooms, monkeypatch):
    """MUTATION: honour the handed-down actor without checking the room, or drop it.

    Measured live: an agent closed eight tasks with `vaf a2a report` instead of its own
    tool, and every one of them landed under the MACHINE OWNER's handle - because the
    CLI acts as the owner by design, which is also why it has no `--scope` flag. The
    room then recorded the agent's work as the person's, and with a board that now
    names who did what that stopped being a cosmetic difference.

    The lane is handed down bound to ONE room. Without that binding a call that outlives
    its turn - a coder subprocess, say - would keep speaking as the agent in rooms it
    has nothing to do with.
    """
    import os

    from vaf.core.a2a.room import Room, derive_peer_id, participant_key

    owner = a2a_cmd._scope()
    monkeypatch.setattr(a2a_cmd, "_key", REAL_KEY)
    room = Room.create(kind="round", owner_scope=owner, base=rooms, room_id="room-actor")
    agent = room.join(display="Nobel", scope_id=owner,
                      peer_id=derive_peer_id(participant_key("agent", owner), "room-actor"))
    person = room.join(display="Alice", scope_id=owner,
                       peer_id=derive_peer_id(participant_key("cli", owner), "room-actor"))
    asked = room.ask(person, "close this")

    # No hand-down: the shell is the person, exactly as documented.
    monkeypatch.delenv(a2a_cmd.ACTOR_ENV, raising=False)
    runner.invoke(a2a_cmd.app, ["report", "room-actor", "plain", "--status", "working",
                                "--reply-to", asked.id])
    assert [f.sender for f in room.store.frames() if f.kind == "report"][-1] == person.peer_id

    # Handed down for THIS room: the shell is the agent.
    monkeypatch.setenv(a2a_cmd.ACTOR_ENV,
                       f"room-actor|{participant_key('agent', owner)}")
    runner.invoke(a2a_cmd.app, ["report", "room-actor", "as the agent",
                                "--status", "completed", "--reply-to", asked.id])
    assert [f.sender for f in room.store.frames() if f.kind == "report"][-1] == agent.peer_id, (
        "the agent's own shell still reports under its user's name")

    # Handed down for ANOTHER room: not honoured here.
    monkeypatch.setenv(a2a_cmd.ACTOR_ENV,
                       f"room-somewhere-else|{participant_key('agent', owner)}")
    runner.invoke(a2a_cmd.app, ["report", "room-actor", "stale hand-down",
                                "--status", "working", "--reply-to", asked.id])
    assert [f.sender for f in room.store.frames() if f.kind == "report"][-1] == person.peer_id, (
        "a hand-down from another room was honoured, so it outlives its turn")


def test_the_runner_hands_the_lane_down_and_takes_it_back():
    """MUTATION: set the actor and never clear it.

    The agent process is ONE process for every account on the machine, and an
    environment variable is process-wide. Left standing, the next shell - another
    tenant's, or a coder run long after the turn - would speak as this agent in this
    room. The house has a rule for exactly this shape, written after a marker left
    standing pushed every later coder run into the wrong mode.
    """
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    branch = src.split("_room_wake = agent.collect_room_wake", 1)[1].split("_pushed = False", 1)[0]
    assert "os.environ[ROOM_ACTOR_ENV] = room_actor_value(" in branch, (
        "the lane is never handed down")
    assert '_room_wake["room_id"]' in branch.split("room_actor_value(", 1)[1][:200], (
        "the hand-down is not bound to the room, so it outlives the turn")
    restore = branch.split("finally:", 1)[1]
    assert "os.environ.pop(ROOM_ACTOR_ENV, None)" in restore, "it is never cleared"
    assert "os.environ[ROOM_ACTOR_ENV] = _prev_actor" in restore, (
        "a previous value is not put back")
    # One home for the name and for the format. Both ends of this string live in
    # different processes, so a separator changed on one side alone is a silence.
    assert 'ROOM_ACTOR_ENV = "VAF_A2A_ROOM_ACTOR"' in (
        (Path(__file__).resolve().parents[1] / "vaf" / "core" / "a2a" / "room.py")
        .read_text(encoding="utf-8")), "the contract does not live in the framework"
    assert '"VAF_A2A_ROOM_ACTOR"' not in (
        (Path(__file__).resolve().parents[1] / "vaf" / "core" / "headless_runner.py")
        .read_text(encoding="utf-8")), "the runner still spells the name by hand"
