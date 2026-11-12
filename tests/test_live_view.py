# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The per-chat live record (`vaf/core/live_view.py`), on its own.

A browser receives nothing about a chat it switched away from; the record is what lets the
server show that chat as it is when the person comes back. What has to hold: a streamed round
keeps its LAST text at its FIRST place, a tool its start and end, a turn's events end with the
turn and are never replayed once the queue says it is over, worker views survive the turn,
every bound holds, and two chats never share an entry.
"""
import json

from vaf.core import live_view as lv
from vaf.core.live_view import LiveViewLedger


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _msg(content, rnd=1, turn="t1"):
    return {"type": "agent_message_update", "role": "assistant", "content": content,
            "turnId": turn, "round": rnd}


def _tool(sub, tool_id, data="x"):
    return {"type": "tool_update", "subType": sub, "toolId": tool_id, "name": "read_file", "data": data}


def _types(snap):
    return [e["type"] for e in snap["events"]]


# ── coalescing ─────────────────────────────────────────────────────────────────────────────

def test_a_round_keeps_its_last_text_at_its_first_place():
    """MUTATION: keep the first content of a round instead of the last - red."""
    led = LiveViewLedger()
    led.record("A", _msg("He"))
    led.record("A", _tool("start", "c1"))
    led.record("A", _msg("Hello"))
    led.record("A", _msg("Hello world"))
    events = led.snapshot("A", active=True)["events"]
    assert _types({"events": events}) == ["agent_message_update", "tool_update"]
    assert events[0]["content"] == "Hello world"


def test_two_rounds_are_two_entries_in_order():
    led = LiveViewLedger()
    led.record("A", _msg("one", rnd=1))
    led.record("A", _tool("start", "c1"))
    led.record("A", _tool("end", "c1"))
    led.record("A", _msg("two", rnd=2))
    events = led.snapshot("A", active=True)["events"]
    assert [(e["type"], e.get("round"), e.get("subType")) for e in events] == [
        ("agent_message_update", 1, None), ("tool_update", None, "start"),
        ("tool_update", None, "end"), ("agent_message_update", 2, None)]


def test_tool_start_and_end_give_two_entries():
    led = LiveViewLedger()
    led.record("A", _tool("start", "c1", data='{"path": "a"}'))
    led.record("A", _tool("end", "c1", data="result"))
    led.record("A", _tool("end", "c1", data="result 2"))
    events = led.snapshot("A", active=True)["events"]
    assert [(e["subType"], e["data"]) for e in events] == [("start", '{"path": "a"}'), ("end", "result 2")]


def test_an_ended_turn_stays_until_it_leaves_the_queue():
    """The runner sends message_complete and saves the chat AFTERWARDS: between the two, the
    turn is in neither the stored transcript nor (before this) the record. It stays, its end
    included, until the runner says the turn left the queue.
    MUTATION: clear the turn group on message_complete again - red;
    MUTATION: turn_left_queue that keeps the turn - red."""
    led = LiveViewLedger()
    led.set_prompt("A", "t1", {"role": "user", "content": "q"})
    led.record("A", _msg("answer"))
    led.record("A", _tool("start", "c1"))
    led.record("A", {"type": "file_created", "filePath": "/w/a.txt", "turnId": "t1"})
    led.record("A", {"type": "message_complete", "content": "answer", "turnId": "t1"})
    snap = led.snapshot("A", active=True)
    assert _types(snap) == ["agent_message_update", "tool_update", "file_created", "message_complete"]
    assert [p["content"] for p in snap["prompts"]] == ["q"] and snap["turn"] is True
    led.turn_left_queue("A", "t1")
    snap = led.snapshot("A", active=True)
    assert snap["events"] == [] and snap["prompts"] == [] and snap["turn"] is False


def test_leaving_the_queue_keeps_the_queued_turns_prompt():
    led = LiveViewLedger()
    led.set_prompt("A", "t1", {"role": "user", "content": "first"})
    led.set_prompt("A", "t2", {"role": "user", "content": "queued"})
    led.record("A", _msg("one", turn="t1"))
    led.record("A", {"type": "message_complete", "turnId": "t1"})
    led.turn_left_queue("A", "t1")
    assert [p["content"] for p in led.snapshot("A", active=True)["prompts"]] == ["queued"]


def test_stop_drops_the_queued_prompts_and_keeps_the_stopped_turn():
    led = LiveViewLedger()
    led.set_prompt("A", "t1", {"role": "user", "content": "q1"})
    led.set_prompt("A", "t2", {"role": "user", "content": "q2"})
    led.record("A", _msg("x", turn="t1"))
    led.record("A", {"type": "generation_stopped"})
    snap = led.snapshot("A", active=True)
    assert [p["content"] for p in snap["prompts"]] == ["q1"]
    assert _types(snap) == ["agent_message_update", "generation_stopped"]


def test_a_second_completion_of_one_turn_never_takes_another_turns_prompt():
    """Two completions for T1 (the guard path sends one more): T2's prompt stays.
    MUTATION: pop the oldest prompt on a completion - red."""
    led = LiveViewLedger()
    led.set_prompt("A", "t1", {"role": "user", "content": "first"})
    led.set_prompt("A", "t2", {"role": "user", "content": "second"})
    led.record("A", _msg("a", turn="t1"))
    led.record("A", {"type": "message_complete", "turnId": "t1"})
    led.record("A", {"type": "message_complete", "turnId": "t1"})
    assert [p["content"] for p in led.snapshot("A", active=True)["prompts"]] == ["first", "second"]


def test_a_new_turn_drops_what_a_dead_turn_left_behind():
    """A turn that died without leaving the queue cleanly left a running card; the next
    turn's first event takes it away. MUTATION: no drop on a new turn id - red."""
    led = LiveViewLedger()
    led.set_prompt("A", "t1", {"role": "user", "content": "dead"})
    led.record("A", dict(_tool("start", "stale"), turnId="t1"))
    led.set_prompt("A", "t2", {"role": "user", "content": "next"})
    led.record("A", _msg("fresh", turn="t2"))
    snap = led.snapshot("A", active=True)
    assert [(e["type"], e.get("toolId")) for e in snap["events"]] == [("agent_message_update", None)]
    assert [p["content"] for p in snap["prompts"]] == ["next"]


def test_a_turn_the_queue_says_is_over_is_never_replayed():
    """MUTATION: return the turn events when not active - red."""
    led = LiveViewLedger()
    led.set_prompt("A", "t1", {"role": "user", "content": "q"})
    led.record("A", _msg("still streaming"))
    led.record("A", _tool("start", "c1"))
    led.record("A", {"type": "coder_state", "loop": 2})
    snap = led.snapshot("A", active=False)
    assert _types(snap) == ["coder_state"]
    assert snap["prompts"] == [] and snap["turn"] is False
    assert led.snapshot("A", active=True)["turn"] is True


def test_worker_views_survive_the_turn_end():
    """MUTATION: put the worker views in the turn group (cleared at turn end) - red."""
    led = LiveViewLedger()
    led.record("A", {"type": "subagent_update", "agentName": "Coder", "presence": "online",
                     "steps": [{"id": "s1", "status": "running"}]})
    led.record("A", {"type": "coder_state", "loop": 1})
    led.record("A", {"type": "message_complete", "turnId": "t1"})
    assert _types(led.snapshot("A", active=False)) == ["subagent_update", "coder_state"]


def test_worker_state_merges_like_the_page():
    led = LiveViewLedger()
    led.record("A", {"type": "subagent_update", "presence": "online", "file": "a.py",
                     "steps": [{"id": "s1", "status": "running"}]})
    led.record("A", {"type": "subagent_update", "status": "tick", "steps": []})
    led.record("A", {"type": "my_view_state", "a": 1})
    led.record("A", {"type": "my_view_state", "b": 2})
    events = led.snapshot("A", active=True)["events"]
    sub = events[0]
    assert sub["file"] == "a.py" and sub["status"] == "tick"
    assert sub["steps"] == [{"id": "s1", "status": "running"}]   # an empty list does not wipe them
    # An embedder's own *_state type is kept without registration.
    assert events[1] == {"type": "my_view_state", "a": 1, "b": 2}


def test_prompts_carry_their_turn_and_are_capped():
    """MUTATION: no prompt cap - red."""
    led = LiveViewLedger()
    for i in range(lv.MAX_PROMPTS + 5):
        led.set_prompt("A", f"t{i}", {"role": "user", "content": f"q{i}"})
    prompts = led.snapshot("A", active=True)["prompts"]
    assert len(prompts) == lv.MAX_PROMPTS
    assert prompts[-1] == {"role": "user", "content": f"q{lv.MAX_PROMPTS + 4}",
                           "turnId": f"t{lv.MAX_PROMPTS + 4}"}


def test_untagged_stream_starts_a_new_entry_after_a_tool():
    led = LiveViewLedger()
    led.record("A", {"type": "agent_message_update", "content": "a", "turnId": "t"})
    led.record("A", {"type": "agent_message_update", "content": "ab", "turnId": "t"})
    led.record("A", _tool("start", "c1"))
    led.record("A", {"type": "agent_message_update", "content": "ab cd", "turnId": "t"})
    events = led.snapshot("A", active=True)["events"]
    assert [e.get("content") for e in events] == ["ab", None, "ab cd"]
    led.record("A", {"type": "clear_last_assistant"})
    assert [e.get("content") for e in led.snapshot("A", active=True)["events"]] == ["ab", None]


def test_clear_last_assistant_with_a_round_removes_that_round():
    led = LiveViewLedger()
    led.record("A", _msg("one", rnd=1))
    led.record("A", _msg("two", rnd=2))
    led.record("A", {"type": "clear_last_assistant", "round": 2})
    assert [e["content"] for e in led.snapshot("A", active=True)["events"]] == ["one"]


def test_workflow_start_resets_the_run():
    led = LiveViewLedger()
    led.record("A", {"type": "workflow_start", "workflowId": "w1"})
    led.record("A", {"type": "workflow_update", "stepId": "s1", "status": "running"})
    led.record("A", {"type": "workflow_update", "stepId": "s1", "status": "success"})
    led.record("A", {"type": "workflow_output_stream", "line": "x"})
    led.record("A", {"type": "workflow_start", "workflowId": "w2"})
    led.record("A", {"type": "workflow_update", "stepId": "s9", "status": "running"})
    events = led.snapshot("A", active=False)["events"]
    assert [(e["type"], e.get("workflowId"), e.get("stepId")) for e in events] == [
        ("workflow_start", "w2", None), ("workflow_update", None, "s9")]


def test_preview_place_is_kept_and_cleared():
    led = LiveViewLedger()
    led.record("A", {"type": "document_preview_wait", "name": "a.docx", "position": 3})
    assert _types(led.snapshot("A", active=False)) == ["document_preview_wait"]
    led.record("A", {"type": "document_preview_wait", "name": "a.docx", "position": 0})
    assert led.snapshot("A", active=False)["events"] == []
    led.record("A", {"type": "document_preview_wait", "name": "a.docx", "position": 2})
    led.record("A", {"type": "sidebar_documents_set", "contents": []})
    assert led.snapshot("A", active=False)["events"] == []


def test_other_types_are_not_recorded():
    led = LiveViewLedger()
    for t in ("history_update", "chat_wait", "session_unread", "new_log", "agent_message_append"):
        led.record("A", {"type": t})
    assert led.snapshot("A", active=True)["events"] == []


def test_the_record_is_a_copy():
    led = LiveViewLedger()
    ev = {"type": "coder_state", "tasks": [{"id": 1}]}
    led.record("A", ev)
    ev["tasks"].append({"id": 2})       # the producer keeps using its dict after sending
    ev["sessionId"] = "changed"
    assert led.snapshot("A", active=True)["events"][0] == {"type": "coder_state", "tasks": [{"id": 1}]}


# ── isolation ──────────────────────────────────────────────────────────────────────────────

def test_two_chats_never_share_an_entry():
    """MUTATION: key the record globally (one chat for every session) - red."""
    led = LiveViewLedger()
    led.record("A", _tool("start", "same-id", data="A's args"))
    led.record("B", _tool("start", "same-id", data="B's args"))
    led.record("B", {"type": "coder_state", "who": "B"})
    assert [e["data"] for e in led.snapshot("A", active=True)["events"]] == ["A's args"]
    assert [e.get("data", e.get("who")) for e in led.snapshot("B", active=True)["events"]] == [
        "B's args", "B"]
    led.forget("B")
    assert led.snapshot("B", active=True)["events"] == []
    assert len(led.snapshot("A", active=True)["events"]) == 1


# ── bounds ─────────────────────────────────────────────────────────────────────────────────

def test_a_long_string_is_cut():
    """MUTATION: no cap on string fields - red."""
    led = LiveViewLedger()
    led.record("A", _tool("end", "c1", data="x" * (lv.MAX_STRING + 500)))
    data = led.snapshot("A", active=True)["events"][0]["data"]
    assert len(data) == lv.MAX_STRING + len(lv.TRUNCATED) and data.endswith(lv.TRUNCATED)


def test_a_browser_frame_has_its_own_cap():
    led = LiveViewLedger()
    led.record("A", {"type": "browser_frame_update", "frame": "f" * (lv.MAX_STRING * 4)})
    assert len(led.snapshot("A", active=True)["events"][0]["frame"]) == lv.MAX_STRING * 4
    led.record("A", {"type": "browser_frame_update", "frame": "f" * (lv.MAX_FRAME + 1)})
    assert led.snapshot("A", active=True)["events"] == []


def test_entries_per_chat_are_bounded_and_keep_the_running_tool_and_prompt():
    """MUTATION: no entry bound - red; MUTATION: running tool starts not ranked last - red."""
    led = LiveViewLedger(max_entries=10)
    led.set_prompt("A", "t1", {"role": "user", "content": "q"})
    led.record("A", _tool("start", "running"))
    for i in range(30):
        led.record("A", _tool("start", f"c{i}"))
        led.record("A", _tool("end", f"c{i}"))
    snap = led.snapshot("A", active=True)
    assert len(snap["events"]) + len(snap["prompts"]) <= 10
    assert snap["events"][0]["toolId"] == "running"
    assert snap["prompts"] == [{"role": "user", "content": "q", "turnId": "t1"}]
    assert snap["events"][-1]["toolId"] == "c29"


def test_a_tool_flood_never_takes_the_worker_window():
    """A long coder turn: one coder_state and one subagent_update, then 60 tool steps with big
    results. The worker's window must survive; results and old steps go first.
    MUTATION: evict strictly oldest first (no ranks) - red."""
    led = LiveViewLedger()
    led.record("A", {"type": "subagent_update", "presence": "online", "agentName": "Coder"})
    led.record("A", {"type": "coder_state", "loop": 1})
    for i in range(60):
        led.record("A", _tool("start", f"c{i}", data="{}"))
        led.record("A", _tool("end", f"c{i}", data="r" * 60_000))
    types = _types(led.snapshot("A", active=True))
    assert "subagent_update" in types and "coder_state" in types
    assert sum(len(json.dumps(e)) for e in led.snapshot("A", active=True)["events"]) <= lv.MAX_BYTES * 1.1


def test_the_limits_hold_even_when_everything_is_running():
    """More running tools than the entry limit: the limits still hold (the oldest running
    start goes last, but it goes). MUTATION: never drop a running start - red."""
    led = LiveViewLedger(max_entries=10, max_bytes=5_000)
    for i in range(25):
        led.record("A", _tool("start", f"r{i}", data="x" * 300))
    events = led.snapshot("A", active=True)["events"]
    assert len(events) <= 10
    assert sum(len(e["data"]) for e in events) <= 5_000
    assert events[-1]["toolId"] == "r24"


def test_deep_nesting_is_cut_and_counted():
    """MUTATION: keep the subtree beyond the depth limit - red."""
    deep: dict = {"s": "y" * 50_000}
    for _ in range(20):
        deep = {"d": deep}
    led = LiveViewLedger()
    led.record("A", {"type": "coder_state", "tree": deep})
    out = json.dumps(led.snapshot("A", active=True)["events"])
    assert lv.TOO_DEEP in out and "y" * 1000 not in out


def test_bytes_per_chat_are_bounded():
    led = LiveViewLedger(max_bytes=10_000)
    for i in range(40):
        led.record("A", {"type": "subagent_output_stream", "line": "y" * 1000, "n": i})
    events = led.snapshot("A", active=True)["events"]
    assert sum(len(e["line"]) for e in events) <= 10_000
    assert events[-1]["n"] == 39


def test_rings_hold_their_size():
    led = LiveViewLedger()
    for i in range(lv.RING_SIZES["subagent_output"] + 7):
        led.record("A", {"type": "subagent_output", "n": i})
    events = led.snapshot("A", active=True)["events"]
    assert len(events) == lv.RING_SIZES["subagent_output"]
    assert events[0]["n"] == 7


def test_at_most_max_sessions_chats():
    led = LiveViewLedger(max_sessions=3)
    for sid in ("A", "B", "C", "D"):
        led.record(sid, {"type": "coder_state", "who": sid})
    assert led.snapshot("A", active=True)["events"] == []   # the least recently written
    assert led.snapshot("D", active=True)["events"][0]["who"] == "D"


def test_expiry():
    """MUTATION: never expire - red."""
    clock = _Clock()
    led = LiveViewLedger(clock=clock)
    led.record("A", {"type": "coder_state", "loop": 1})
    led.record("A", {"type": "document_preview_wait", "name": "a.docx", "position": 1})
    clock.t += lv.PREVIEW_TTL + 1
    assert _types(led.snapshot("A", active=True)) == ["coder_state"]
    clock.t += lv.ENTRY_TTL
    assert led.snapshot("A", active=True)["events"] == []

    led.record("B", {"type": "subagent_update", "presence": "online"})
    led.record("B", {"type": "subagent_update", "presence": "idle"})
    clock.t += lv.WORKER_DONE_TTL + 1
    assert led.snapshot("B", active=True)["events"] == []

    led.record("C", {"type": "workflow_start", "workflowId": "w"})
    clock.t += 60
    led.record("C", {"type": "workflow_done", "success": True})
    clock.t += lv.WORKER_DONE_TTL + 1
    assert led.snapshot("C", active=True)["events"] == []

    led.set_prompt("D", "t1", {"role": "user", "content": "q"})
    clock.t += lv.ENTRY_TTL + 1
    # A prompt waits for its turn to leave the queue; only the safety net ends it.
    assert len(led.snapshot("D", active=True)["prompts"]) == 1
    clock.t += lv.TURN_TTL
    assert led.snapshot("D", active=True)["prompts"] == []


def test_record_never_raises_on_odd_input():
    led = LiveViewLedger()
    for odd in (None, "text", {"no": "type"}, {"type": ""}, {"type": "tool_update"},
                {"type": "agent_message_update", "round": True},
                {"type": "document_preview_wait", "position": "x"}):
        led.record("A", odd)
    led.record(None, {"type": "coder_state"})
    led.record("", {"type": "coder_state"})
    assert led.snapshot(None, active=True) == {"prompts": [], "events": [], "turn": False}


def test_every_live_view_type_of_vaf_is_kept_beside_the_turn():
    """`VAF_LIVE_VIEW_TYPES` (vaf/core/progress.py) names VAF's own live views; each one has to
    come back after a chat switch, also once the turn that started its worker is over.
    MUTATION: drop a type's rule (e.g. browser_step_update from the rings) - red."""
    from vaf.core.progress import VAF_LIVE_VIEW_TYPES
    for t in sorted(VAF_LIVE_VIEW_TYPES):
        led = LiveViewLedger()
        led.record("A", {"type": t, "line": "x", "frame": "f"})
        led.record("A", {"type": "message_complete"})
        assert _types(led.snapshot("A", active=False)) == [t], t


def test_a_long_turn_keeps_its_prompt_and_its_running_tool():
    """MUTATION: let a running tool start expire - red; MUTATION: no prompt renewal - red."""
    clock = _Clock()
    led = LiveViewLedger(clock=clock)
    led.set_prompt("A", "t1", {"role": "user", "content": "long job"})
    led.record("A", _tool("start", "slow"))
    for _ in range(4):                       # 40 minutes of a turn that keeps streaming
        clock.t += 10 * 60
        led.record("A", _msg("still working"))
    snap = led.snapshot("A", active=True)
    assert [p["content"] for p in snap["prompts"]] == ["long job"]
    assert [e.get("toolId") for e in snap["events"]] == ["slow", None]


def test_the_periodic_sweep_frees_other_chats():
    """MUTATION: never sweep - red."""
    clock = _Clock()
    led = LiveViewLedger(clock=clock)
    led.record("A", {"type": "coder_state"})
    clock.t += lv.ENTRY_TTL + lv.SWEEP_EVERY + 1
    led.record("B", {"type": "coder_state"})
    assert "A" not in led._chats


def test_a_worker_state_plays_in_the_order_it_happened():
    """online, a frame, then idle: the replay ends with idle, so the frame cannot wake the
    finished window. MUTATION: keep the first-seen position for worker states - red."""
    led = LiveViewLedger()
    led.record("A", {"type": "subagent_update", "presence": "online"})
    led.record("A", {"type": "browser_frame_update", "frame": "f"})
    led.record("A", {"type": "subagent_update", "presence": "idle"})
    assert _types(led.snapshot("A", active=False)) == ["browser_frame_update", "subagent_update"]


# ── isolation: owner and forgotten credentials ───────────────────────────────────

def test_a_record_kept_for_another_owner_is_never_replayed():
    """Bob's chat is deleted, a late worker event re-creates its record, Alice claims the id.
    MUTATION: ignore the owner in snapshot - red."""
    owners = {"X": "bob"}
    led = LiveViewLedger(owner_of=lambda sid: owners.get(sid))
    led.record("X", {"type": "coder_state", "code": "bob's code"})
    led.forget("X")                                   # Bob deletes the chat
    owners.pop("X")
    led.record("X", {"type": "subagent_update", "status": "bob's late line"})
    owners["X"] = "alice"                             # Alice claims the freed id
    assert led.snapshot("X", active=True, owner="alice")["events"] == []
    assert "X" not in led._chats
    led.record("Y", {"type": "coder_state"})          # unknown owner (no store lookup)
    assert led.snapshot("Y", active=True, owner="alice")["events"] == []


def test_a_prompt_for_the_new_owner_wipes_a_foreign_record():
    led = LiveViewLedger(owner_of=lambda sid: None)
    led.record("X", {"type": "coder_state", "code": "stale"})
    led.set_prompt("X", "t1", {"role": "user", "content": "mine"}, owner="alice")
    snap = led.snapshot("X", active=True, owner="alice")
    assert snap["events"] == [] and [p["content"] for p in snap["prompts"]] == ["mine"]


def test_a_forgotten_value_is_replaced_in_the_record(monkeypatch):
    """The value the agent stored mid-turn is in the prompt, the stream and a tool result; it
    must not come back in a replay. MUTATION: no scrub on forget - red; no scrub on write - red."""
    from vaf.core import forget_secrets
    monkeypatch.setattr(forget_secrets, "_by_session", {})
    secret = "hunter2-very-secret"
    led = LiveViewLedger()
    led.set_prompt("A", "t1", {"role": "user", "content": f"my password is {secret}"})
    led.record("A", _msg(f"I will store {secret}"))
    led.record("A", _tool("end", "c1", data=json.dumps({"pw": secret})))
    forget_secrets.remember("A", {"FTP_PASS": secret})
    led.scrub("A", {"FTP_PASS": secret})
    led.record("A", {"type": "subagent_output_stream", "line": f"login with {secret}"})
    out = json.dumps(led.snapshot("A", active=True))
    assert secret not in out and "[FTP_PASS]" in out


# ── late files, the turn's end, forget timing ──────────────────────────────────────────────

def test_a_late_file_of_an_older_turn_never_wipes_the_running_turn():
    """The async coder of turn T1 announces its files with T1's id while T2 runs.
    MUTATION: let file_created move the turn again - red; let any newer id move it - red."""
    led = LiveViewLedger()
    led.record("A", _msg("first turn", turn="t1"))
    led.turn_left_queue("A", "t1")                   # T1 is over before T2 runs
    led.set_prompt("A", "t2", {"role": "user", "content": "second question"})
    led.record("A", _msg("streaming t2", turn="t2"))
    led.record("A", dict(_tool("start", "c9"), turnId="t2"))
    led.record("A", {"type": "file_created", "filePath": "/w/late.txt", "turnId": "t1"})
    led.record("A", {"type": "agent_message_update", "content": "old drain", "turnId": "t1"})
    snap = led.snapshot("A", active=True)
    assert [p["content"] for p in snap["prompts"]] == ["second question"]
    assert [(e["type"], e.get("turnId")) for e in snap["events"]] == [
        ("agent_message_update", "t2"), ("tool_update", "t2"), ("file_created", "t1"),
        ("agent_message_update", "t1")]


def test_an_upload_announced_for_the_queued_turn_never_wipes_the_running_one():
    """The chat handler announces an attached image with the NEW queued turn's id while the
    current turn still runs; that turn's prompt is on record already.
    MUTATION: let file_created move the turn - red."""
    led = LiveViewLedger()
    led.set_prompt("A", "t1", {"role": "user", "content": "running"})
    led.record("A", _msg("streaming t1", turn="t1"))
    led.set_prompt("A", "t2", {"role": "user", "content": "queued with an image"})
    led.record("A", {"type": "file_created", "filePath": "/w/photo.png", "turnId": "t2"})
    snap = led.snapshot("A", active=True)
    assert [p["content"] for p in snap["prompts"]] == ["running", "queued with an image"]
    assert _types(snap) == ["agent_message_update", "file_created"]
    # T1 leaves: only its own entries go; T2's upload stays for a reconnect during T2.
    # MUTATION: clear every turn entry when a turn leaves - red.
    led.turn_left_queue("A", "t1")
    snap = led.snapshot("A", active=True)
    assert [(e["type"], e.get("turnId")) for e in snap["events"]] == [("file_created", "t2")]
    assert [p["content"] for p in snap["prompts"]] == ["queued with an image"]


def test_a_second_completion_stays_the_last_word():
    """The guard path sends a corrected update and a second message_complete for the same turn:
    the end must replay after the correction. MUTATION: keep the first completion's place - red."""
    led = LiveViewLedger()
    led.record("A", _msg("first", turn="t1"))
    led.record("A", {"type": "message_complete", "turnId": "t1"})
    led.record("A", {"type": "agent_message_update", "content": "corrected", "turnId": "t1"})
    led.record("A", {"type": "message_complete", "turnId": "t1"})
    assert _types(led.snapshot("A", active=True))[-1] == "message_complete"


def test_only_the_running_turns_prompt_is_renewed():
    """A queued turn's prompt is not kept alive by another turn's events.
    MUTATION: renew every prompt on a turn event - red."""
    clock = _Clock()
    led = LiveViewLedger(clock=clock)
    led.set_prompt("A", "t1", {"role": "user", "content": "running"})
    led.set_prompt("A", "t0", {"role": "user", "content": "stale"})
    led.record("A", _msg("x", turn="t1"))
    clock.t += lv.TURN_TTL - 10
    led.record("A", _msg("y", turn="t1"))
    clock.t += 20
    assert [p["content"] for p in led.snapshot("A", active=True)["prompts"]] == ["running"]


def test_an_event_recorded_while_a_forget_runs_is_scrubbed(monkeypatch):
    """The forgotten values are read under the record's lock. The race: the event's record has
    read the (empty) values when a forget remembers the value and scrubs the record on another
    thread. With the read outside the lock that scrub finishes first and the event goes in raw;
    inside the lock the scrub waits and catches it. MUTATION: read them before the lock - red."""
    import threading
    from vaf.core import forget_secrets
    monkeypatch.setattr(forget_secrets, "_by_session", {})
    secret = "timing-secret-0042"
    led = LiveViewLedger()
    led.record("A", {"type": "coder_state", "x": 1})
    real = lv._forgotten
    fired = []

    def racing(sid):
        out = real(sid)
        if not fired:
            fired.append(1)
            forget_secrets.remember("A", {"PW": secret})
            other = threading.Thread(target=led.scrub, args=("A", {"PW": secret}))
            other.start()
            other.join(0.3)          # done at once when the lock is free, waits when it is held
            fired.append(other)
        return out
    monkeypatch.setattr(lv, "_forgotten", racing)
    led.record("A", {"type": "subagent_output_stream", "line": f"pw {secret}"})
    fired[1].join(2)
    monkeypatch.setattr(lv, "_forgotten", real)
    assert secret not in json.dumps(led.snapshot("A", active=True))


def test_a_prompt_is_scrubbed_before_it_is_cut(monkeypatch):
    """MUTATION: cut before scrubbing - red."""
    from vaf.core import forget_secrets
    monkeypatch.setattr(forget_secrets, "_by_session", {})
    secret = "Sup3rSecretValue99"
    forget_secrets.remember("A", {"PW": secret})
    led = LiveViewLedger()
    text = "x" * (lv.MAX_STRING - 6) + secret
    led.set_prompt("A", "t1", {"role": "user", "content": text})
    content = led.snapshot("A", active=True)["prompts"][0]["content"]
    assert "Sup3rS" not in content and "[PW]" in content


def test_a_value_cut_before_it_was_forgotten_loses_its_start_too(monkeypatch):
    """A string cut at the limit before the forget ends in the value's first part.
    MUTATION: no tail scrub - red."""
    from vaf.core import forget_secrets
    monkeypatch.setattr(forget_secrets, "_by_session", {})
    secret = "Sup3rSecretValue99"
    led = LiveViewLedger()
    led.record("A", _tool("end", "c1", data="x" * (lv.MAX_STRING - 6) + secret))
    led.scrub("A", {"PW": secret})
    data = led.snapshot("A", active=True)["events"][0]["data"]
    assert "Sup3rS" not in data and data.endswith("[PW]" + lv.TRUNCATED)


def test_a_scrubbed_prompt_is_recounted():
    """MUTATION: keep the old size after a scrub - red."""
    led = LiveViewLedger()
    secret = "s" * 5000
    led.set_prompt("A", "t1", {"role": "user", "content": secret})
    before = led._chats["A"].prompt_bytes
    led.scrub("A", {"PW": secret})
    assert led._chats["A"].prompt_bytes < before - 4000


def test_turns_without_a_prompt_follow_each_other():
    """Messenger turns record no prompt: M2 must take over from M1 (M1 left the queue, or died),
    and a turn that left never becomes current again.
    MUTATION: only a turn with a prompt may take over - red; forget departed turns - red."""
    led = LiveViewLedger()
    led.record("A", _msg("m1 text", turn="m1"))
    led.record("A", dict(_tool("start", "k1"), turnId="m1"))     # never ended
    led.turn_left_queue("A", "m1")
    led.record("A", _msg("m2 text", turn="m2"))
    assert [e["content"] for e in led.snapshot("A", active=True)["events"]] == ["m2 text"]
    # M3 starts while M2 died without leaving: M2's leftovers go.
    led.record("A", _msg("m3 text", turn="m3"))
    assert [e["content"] for e in led.snapshot("A", active=True)["events"]] == ["m3 text"]


def test_an_automation_after_a_late_event_of_a_departed_turn():
    """Between turns the drain sends a summary with the last turn's id; it must not make that
    turn current, so an automation turn afterwards is shown on its own.
    MUTATION: adopt any id while no turn is current - red."""
    led = LiveViewLedger()
    led.record("A", _msg("answer", turn="t1"))
    led.turn_left_queue("A", "t1")
    led.record("A", {"type": "agent_message_update", "content": "drain summary", "turnId": "t1"})
    led.record("A", _msg("automation runs", turn="auto1"))
    led.turn_left_queue("A", "auto1")
    contents = [e.get("content") for e in led.snapshot("A", active=True)["events"]]
    assert "automation runs" not in contents
