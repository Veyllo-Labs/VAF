# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An automation that runs when a room says something, not when a clock says so.

The run half of an automation was always trigger-agnostic; what VAF lacked was the "is
it due" decision for anything but a clock, which the `schedule` package holds and cannot
express as a condition. `RoomTriggerWatch` is that decision for room events, and this file
pins the three rules the module docstring names: the owner's own agent never fires a
trigger (a loop nothing else would stop), a trigger fires only in a room the owner is a
member of, and the cursor starts at the room's newest frame, never at zero.
"""
import json
from pathlib import Path

import pytest
import schedule
from typer.testing import CliRunner

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.room import Room, derive_peer_id, participant_key
from vaf.core.automation import (AUTOMATION_FORMAT, AutomationManager, AutomationTask,
                                 Frequency)
from vaf.core.automation_triggers import (RoomTriggerWatch, matching_frames,
                                          prompt_with_trigger, read_trigger,
                                          trigger_context, trigger_label,
                                          trigger_variables)
from vaf.core.platform import Platform

ROOT = Path(__file__).resolve().parents[1]
SCOPE = "scope-a"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A scratch home, a scratch room store, and the clock scheduler's registry empty."""
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path / "rooms")
    (tmp_path / "rooms").mkdir()
    schedule.clear()
    return tmp_path


def _room(room_id="room-t", *, owner_in=True):
    """A round with the owner's person and agent in it, and a stranger on a ticket."""
    room = Room.create(kind="round", owner_scope=SCOPE, room_id=room_id)
    person = agent = None
    if owner_in:
        for lane in ("cli", "agent"):
            key = participant_key(lane, SCOPE)
            member = room.join(display=f"Alice-{lane}", scope_id=SCOPE,
                               peer_id=derive_peer_id(key, room_id), participant_key=key)
            if lane == "cli":
                person = member
            else:
                agent = member
    guest = room.join(display="Codex", scope_id=None, peer_id="p-codex")
    return room, person, agent, guest


def _task(**kw):
    base = dict(id="t1", name="Deploy watch", prompt="Look at what was said and act.",
                frequency="on_event", time="", enabled=True, user_scope_id=SCOPE,
                trigger={"kind": "room_message", "room_id": "room-t", "match": "deploy"})
    base.update(kw)
    return AutomationTask(**base)


# ── the record ─────────────────────────────────────────────────────────────

def test_a_trigger_is_read_at_the_boundary_and_never_trusted_raw():
    """MUTATION: pass the dict through.

    A room id that cannot be a path component, a kind this version does not know, a
    cursor that is not a number: each reads as no trigger, never as an exception.
    """
    assert read_trigger({"kind": "room_message", "room_id": "room-t"}) == \
        {"kind": "room_message", "room_id": "room-t"}
    assert read_trigger({"kind": "room_message", "room_id": "../etc"}) is None
    assert read_trigger({"kind": "webhook", "room_id": "room-t"}) is None
    assert read_trigger("room-t") is None
    read = read_trigger({"kind": "room_reaction", "room_id": "room-t", "emoji": " " + "x" * 40,
                         "cursor": "seven", "match": "ignored for a reaction"})
    assert read == {"kind": "room_reaction", "room_id": "room-t", "emoji": "x" * 16}
    assert read_trigger({"kind": "room_message", "room_id": "room-t", "cursor": 12})["cursor"] == 12


def test_an_event_task_has_no_clock_and_says_when_it_runs():
    """MUTATION: give ON_EVENT a clock rule, or print `on_event at ` for it."""
    task = _task()
    assert task.frequency == Frequency.ON_EVENT
    assert task.next_run_datetime is None and task.next_run_label == "-"
    assert task.schedule_label == "on message containing 'deploy' in room room-t"
    assert _task(trigger={"kind": "room_reaction", "room_id": "room-t", "emoji": "+1"}
                 ).schedule_label == "on reaction +1 in room room-t"
    assert _task(trigger=None).schedule_label == "on an event (no room)"
    assert _task(frequency="daily", time="07:15", trigger=None).schedule_label == "daily at 07:15"


def test_the_record_carries_its_format_tag_and_reads_untagged_files(world):
    """MUTATION: drop the tag from to_dict, or refuse a record without one.

    The house rule for a persisted format that gains a field: tagged on every save,
    and every file written before the tag existed keeps loading.
    """
    store = world / "automations"
    store.mkdir()
    (store / "old.json").write_text(json.dumps({"id": "old", "name": "n", "prompt": "p",
                                                "frequency": "daily", "time": "07:00"}),
                                    encoding="utf-8")
    manager = AutomationManager(storage_dir=str(store))
    assert manager.get("old") is not None, "an untagged file stopped loading"
    manager.create(_task(id="new"))
    saved = json.loads((store / SCOPE / "new.json").read_text(encoding="utf-8"))
    assert saved["format"] == AUTOMATION_FORMAT and saved["trigger"]["kind"] == "room_message"


def test_the_scheduler_registers_no_clock_job_for_an_event_task(world):
    """MUTATION: let an event task fall through to the clock branches.

    The clock registry is module-global, and no job may be registered for a task
    with no time. The else arm already refuses an unknown frequency, so the job
    count alone cannot see the mutation; what it changes is the DIAGNOSTIC, and that
    is pinned too: a scheduler log that calls a supported frequency "unsupported"
    is the silent else arm the measurement flagged, wearing a new frequency.
    """
    manager = AutomationManager(storage_dir=str(world / "automations"))
    logged = []
    manager._log_scheduler_event = logged.append
    before = len(schedule.jobs)
    manager._schedule_task(_task())
    assert len(schedule.jobs) == before
    assert any("REGISTERED_EVENT" in line for line in logged), logged
    assert not any("unsupported" in line for line in logged), logged
    manager._schedule_task(_task(id="clock", frequency="daily", time="07:15", trigger=None))
    assert len(schedule.jobs) == before + 1
    schedule.clear()


# ── the decision ───────────────────────────────────────────────────────────

def test_matching_reads_what_was_said_and_nothing_else(world):
    """MUTATION: match on bookkeeping, or on a reaction for a message trigger."""
    room, person, agent, guest = _room()
    said = room.say(guest, "Please DEPLOY the fix")
    room.say(guest, "unrelated")
    room.react(person, said.id, "+1")
    frames = room.store.frames()

    hits = matching_frames({"kind": "room_message", "room_id": "room-t", "match": "deploy"}, frames)
    assert [f.id for f in hits] == [said.id], "folded match, and only what was said"
    hits = matching_frames({"kind": "room_message", "room_id": "room-t"}, frames)
    assert [f.kind for f in hits] == ["say", "say"], "joins and reactions are not messages"
    hits = matching_frames({"kind": "room_reaction", "room_id": "room-t", "emoji": "+1"}, frames)
    assert [f.kind for f in hits] == ["reaction"] and hits[0].sender == person.peer_id
    assert matching_frames({"kind": "room_reaction", "room_id": "room-t", "emoji": "no"}, frames) == []
    assert matching_frames({"kind": "room_message", "room_id": "room-t", "from": guest.peer_id},
                           frames, exclude_senders={guest.peer_id}) == []


def test_the_cursor_starts_now_and_a_message_fires_once(world):
    """MUTATION: start the cursor at zero, or forget to advance it.

    A trigger created today must not fire on last week's transcript, and a message
    that fired once must not fire again on the next tick.
    """
    room, _person, _agent, guest = _room()
    room.say(guest, "deploy this from last week")
    watch = RoomTriggerWatch()
    task = _task()

    assert watch.tick([task]) == [], "the first sight of a task arms it, it does not fire"
    said = room.say(guest, "please deploy now")
    hits = watch.tick([task])
    assert len(hits) == 1 and [f.id for f in hits[0].frames] == [said.id]
    assert hits[0].newest == said.lamport and hits[0].labels[guest.peer_id] == "Codex"
    assert watch.tick([task]) == [], "fired once"


def test_the_owners_own_agent_never_fires_a_trigger_but_the_person_does(world):
    """MUTATION: drop the exclusion of the owner's agent lane.

    The automation runs AS that agent. Its own words in the room re-triggering it is a
    loop nothing else in the tree would stop. The person's reaction is the whole
    point: an emoji on the agent's report is the approval button.
    """
    room, person, agent, _guest = _room()
    watch = RoomTriggerWatch()
    message_task = _task(id="m")
    reaction_task = _task(id="r", trigger={"kind": "room_reaction", "room_id": "room-t"})
    watch.tick([message_task, reaction_task])

    report = room.say(agent, "deploy done, please check")
    assert watch.tick([message_task, reaction_task]) == [], "the agent woke its own trigger"

    room.react(person, report.id, "+1")
    hits = watch.tick([message_task, reaction_task])
    assert [h.task.id for h in hits] == ["r"]
    assert hits[0].frames[0].kind == "reaction" and hits[0].frames[0].reply_to == report.id


def test_a_room_the_owner_is_not_in_is_nothing(world):
    """MUTATION: skip the membership check.

    Rule 4.4 shape: a task must not read a room its account has no seat in, and a
    root task with no account has no seat anywhere.
    """
    room, _p, _a, guest = _room(owner_in=False)
    watch = RoomTriggerWatch()
    watch.tick([_task()])
    room.say(guest, "deploy")
    assert watch.tick([_task()]) == []
    assert watch.tick([_task(user_scope_id=None)]) == []


def test_a_persisted_cursor_catches_up_what_arrived_while_down(world):
    """After a fire the position is on the task; a fresh watch (a restart) reads on from
    it rather than from now, so nothing said in between is lost."""
    room, _p, _a, guest = _room()
    first = RoomTriggerWatch()
    task = _task()
    first.tick([task])
    said = room.say(guest, "deploy one")
    hit = first.tick([task])[0]
    task.trigger = dict(task.trigger, cursor=hit.newest)

    later = room.say(guest, "deploy two")
    hits = RoomTriggerWatch().tick([task])
    assert [f.id for f in hits[0].frames] == [later.id], "from the persisted position on"
    assert said.id not in [f.id for f in hits[0].frames]


# ── the run ────────────────────────────────────────────────────────────────

def test_the_manager_fires_the_run_with_what_triggered_it_and_persists_the_cursor(world):
    """MUTATION: run without the context, or do not persist the cursor at all.

    The cursor goes to disk before the run, so a restart in the middle cannot fire
    the same frames twice; and the run is told why it runs, or the agent reads a bare
    prompt with no idea what just happened in the room. (Whether the write happens
    before or after the run is not observable here: the run is a stub.)
    """
    room, _p, _a, guest = _room()
    store = world / "automations"
    manager = AutomationManager(storage_dir=str(store))
    manager.create(_task())
    started = []
    manager._run_scheduled_task = lambda task, *, trigger=None: started.append((task, trigger))

    assert manager._fire_room_triggers() == 0, "arming"
    said = room.say(guest, "deploy it")
    assert manager._fire_room_triggers() == 1
    task, context = started[0]
    assert context["room_id"] == "room-t" and context["frames"][0]["id"] == said.id
    assert context["frames"][0]["who"] == "Codex" and "deploy it" in context["frames"][0]["text"]
    on_disk = json.loads((store / SCOPE / "t1.json").read_text(encoding="utf-8"))
    assert on_disk["trigger"]["cursor"] == said.lamport
    assert manager._fire_room_triggers() == 0


def test_the_prompt_and_the_workflow_lane_are_told_why_they_run():
    """MUTATION: append nothing. The two forms of the same context."""
    class F:
        def __init__(self, **kw): self.__dict__.update(kw)
    frames = [F(id="f-1", sender="p-codex", kind="say", body={"text": "deploy it"}, reply_to=None),
              F(id="f-2", sender="p-ann", kind="reaction", body={"text": "+1"}, reply_to="f-0")]
    trigger = {"kind": "room_message", "room_id": "room-t", "match": "deploy"}
    context = trigger_context(trigger, frames, {"p-codex": "Codex"})
    assert context["label"] == trigger_label(trigger)

    text = prompt_with_trigger("Do the thing.", context)
    assert text.startswith("Do the thing.")
    assert "TRIGGERED BY AN EVENT in room room-t" in text
    assert "- Codex [say] [id f-1]: deploy it" in text
    assert "- p-ann [reaction] [id f-2] on f-0: +1" in text
    assert "room_send" in text, "it says how to answer in the room"

    variables = trigger_variables(context)
    assert variables["trigger_room"] == "room-t" and variables["trigger_frame_id"] == "f-1"
    assert variables["trigger_text"] == "Codex: deploy it\np-ann: +1"


def test_run_task_consumes_the_context_on_both_lanes():
    """Source guards: the two lanes are deep inside run_task, which starts a real agent."""
    source = (ROOT / "vaf" / "core" / "automation.py").read_text(encoding="utf-8")
    body = source.split("    def run_task(", 1)[1].split("\n    def ", 1)[0]
    assert "prompt_with_trigger(prompt, trigger)" in body
    assert "variables.update(trigger_variables(trigger))" in body
    assert "new_terminal = False" in body.split("if trigger is not None:", 1)[1][:60], (
        "a spawned terminal would run the task without its trigger")
    loop = source.split("def scheduler_loop():", 1)[1].split("time.sleep(30)", 1)[0]
    assert "self._fire_room_triggers()" in loop, "the scheduler loop does not ask"


# ── the lanes that create one ──────────────────────────────────────────────

def test_the_terminal_creates_an_event_task(world):
    from vaf.core.automation import automation_app

    result = CliRunner().invoke(automation_app, [
        "create", "--name", "Deploy watch", "--prompt", "act on it",
        "--on-room", "room-t", "--on-match", "deploy"])
    assert result.exit_code == 0, result.output
    assert "on message containing 'deploy' in room room-t" in result.output
    manager = AutomationManager(storage_dir=str(world / "automations"))
    task = next(t for t in manager.list() if t.name == "Deploy watch")
    assert task.frequency == "on_event" and task.time == ""
    assert task.trigger == {"kind": "room_message", "room_id": "room-t", "match": "deploy"}

    bad = CliRunner().invoke(automation_app, ["create", "--name", "x", "--prompt", "y",
                                              "--on-room", "../etc"])
    assert bad.exit_code != 0


def test_the_agents_tool_creates_an_event_task(world, monkeypatch):
    """MUTATION: let the time validation run for an event task, or drop the trigger
    from the record."""
    import vaf.core.automation as automation_mod
    from vaf.tools.automation import AutomationTool

    monkeypatch.setattr(automation_mod, "ensure_scheduler_started",
                        lambda origin="": (None, False))
    out = AutomationTool().run(
        name="Approval", prompt="Deploy when approved.", frequency="on_event", time="",
        trigger_room="room-t", trigger_emoji="+1", user_scope_id=SCOPE)
    assert "Error" not in out, out
    assert "on reaction +1 in room room-t" in out
    manager = AutomationManager(user_scope_id=SCOPE)
    task = next(t for t in manager.list() if t.name == "Approval")
    assert task.trigger == {"kind": "room_reaction", "room_id": "room-t", "emoji": "+1"}
    assert task.next_run_datetime is None

    refused = AutomationTool().run(
        name="x", prompt="y", frequency="on_event", time="", user_scope_id=SCOPE)
    assert refused.startswith("Error") and "trigger_room" in refused


def test_the_browser_lane_accepts_a_trigger_and_shows_the_rule():
    """Source guards for the rebuild that has dropped fields twice."""
    server = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert server.count('"schedule": task.schedule_label') >= 1
    assert 'read_trigger(cmd.get("trigger"))' in server
    settings = (ROOT / "web" / "components" / "SettingsModal.tsx").read_text(encoding="utf-8")
    assert "auto.schedule" in settings, "the list shows a clock where an event task has a rule"


def test_two_event_tasks_do_not_share_an_empty_time_slot(world, monkeypatch):
    """MUTATION: compare times for an event task.

    Two event tasks both have no clock; treating the empty time as a shared slot
    refused the second one as a duplicate of the first. The slot of an event task is
    its trigger, and only the same room with the same rule is one.
    """
    import vaf.core.automation as automation_mod
    from vaf.tools.automation import AutomationTool

    monkeypatch.setattr(automation_mod, "ensure_scheduler_started", lambda origin="": (None, False))
    first = AutomationTool().run(name="Deploy watch", prompt="Run the deployment checklist.",
                                 frequency="on_event", time="", trigger_room="room-t",
                                 trigger_match="deploy", user_scope_id=SCOPE)
    assert "Created Successfully" in first, first
    # A different rule in the same room, with a prompt that shares no words: the only
    # thing the two have in common is the empty time.
    second = AutomationTool().run(name="Rollback watch", prompt="Revert yesterday's release notes.",
                                  frequency="on_event", time="", trigger_room="room-t",
                                  trigger_match="rollback", user_scope_id=SCOPE)
    assert "Created Successfully" in second, second
    # The same rule twice IS a duplicate, and the slot named in the refusal is the trigger.
    twin = AutomationTool().run(name="Deploy twin", prompt="Compile the deployment checklist again.",
                                frequency="on_event", time="", trigger_room="room-t",
                                trigger_match="deploy", user_scope_id=SCOPE)
    assert "Created Successfully" not in twin and "already" in twin.lower(), twin


def test_update_carries_a_trigger_and_refuses_an_event_task_with_none(world, monkeypatch):
    """MUTATION: accept frequency on_event on update without a room to watch."""
    import vaf.core.automation as automation_mod
    from vaf.tools.automation import UpdateAutomationTool

    monkeypatch.setattr(automation_mod, "ensure_scheduler_started", lambda origin="": (None, False))
    manager = AutomationManager(user_scope_id=SCOPE)
    manager.create(_task(id="clock", frequency="daily", time="07:15", trigger=None, user_scope_id=SCOPE))

    inert = UpdateAutomationTool().run(task_id="clock", frequency="on_event", user_scope_id=SCOPE)
    assert inert.startswith("Error") and "trigger_room" in inert
    assert AutomationManager(user_scope_id=SCOPE).get("clock").frequency == "daily"

    out = UpdateAutomationTool().run(task_id="clock", trigger_room="room-t", trigger_emoji="+1",
                                     time="09:00", user_scope_id=SCOPE)
    assert "Error" not in out, out
    task = AutomationManager(user_scope_id=SCOPE).get("clock")
    assert task.frequency == "on_event" and task.time == "", "a time sent beside the trigger put a clock back"
    assert task.trigger == {"kind": "room_reaction", "room_id": "room-t", "emoji": "+1"}


def test_the_browser_applies_a_trigger_after_the_clock_fields():
    """Source guard: the trigger block sits after the field loop, so a frequency or
    time sent beside it cannot put a clock back on the task."""
    server = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    update = server.split('"type": "update_automation_result", "ok": False, "error": "Automation not found"', 1)[1]
    loop = update.index('for key in ("name", "description", "prompt", "frequency", "time", "weekday", "day", "enabled")')
    trigger = update.index('update_params.update(trigger=_trigger, frequency="on_event", time="")')
    assert loop < trigger
