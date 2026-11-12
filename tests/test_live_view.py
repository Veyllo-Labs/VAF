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


# ── AC2: coalescing ──────────────────────────────────────────────────────────────

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


def test_message_complete_empties_the_turn():
    """MUTATION: do not clear the turn group on message_complete - red."""
    led = LiveViewLedger()
    led.record("A", _msg("answer"))
    led.record("A", _tool("start", "c1"))
    led.record("A", {"type": "file_created", "filePath": "/w/a.txt", "turnId": "t1"})
    led.record("A", {"type": "message_complete", "content": "answer", "turnId": "t1"})
    assert led.snapshot("A", active=True)["events"] == []
    assert led.snapshot("A", active=True)["turn"] is False


def test_stop_empties_the_turn_and_its_prompts():
    led = LiveViewLedger()
    led.set_prompt("A", "t1", {"role": "user", "content": "q1"})
    led.set_prompt("A", "t2", {"role": "user", "content": "q2"})
    led.record("A", _msg("x"))
    led.record("A", {"type": "generation_stopped"})
    snap = led.snapshot("A", active=True)
    assert snap["events"] == [] and snap["prompts"] == []


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


def test_prompts_close_with_their_turn():
    led = LiveViewLedger()
    led.set_prompt("A", "t1", {"role": "user", "content": "first"})
    led.set_prompt("A", "t2", {"role": "user", "content": "queued"})
    led.record("A", {"type": "message_complete", "turnId": "t1"})
    assert [p["content"] for p in led.snapshot("A", active=True)["prompts"]] == ["queued"]


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


# ── AC1 (pure half): isolation ───────────────────────────────────────────────────

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


# ── AC3: bounds ──────────────────────────────────────────────────────────────────

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
    """MUTATION: no entry bound - red; MUTATION: running tool starts not protected - red."""
    led = LiveViewLedger(max_entries=10)
    led.set_prompt("A", "t1", {"role": "user", "content": "q"})
    led.record("A", _tool("start", "running"))
    for i in range(30):
        led.record("A", _tool("start", f"c{i}"))
        led.record("A", _tool("end", f"c{i}"))
    snap = led.snapshot("A", active=True)
    assert len(snap["events"]) <= 10
    assert snap["events"][0]["toolId"] == "running"
    assert snap["prompts"] == [{"role": "user", "content": "q"}]
    assert snap["events"][-1]["toolId"] == "c29"


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
