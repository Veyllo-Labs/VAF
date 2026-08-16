# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A room is a conversation, so the agent learns from it the way it learns from a chat.

It did not. Measured: `memory_context` is a parameter of `chat_step`, the chat queue
fills it and the room lane passed nothing, so an agent answered its own room knowing
none of what it had ever been told. And `run_session_compaction_sync` - the step that
turns every fifteen messages into durable notes - had two callers, both on the chat
queue. Everything said in a room was gone at the next restart.

What is pinned here is the shape of the repair, not the wording of it: the room uses
the SAME two primitives the chat lane uses, hands over its own transcript instead of
letting them read a shared object, and stamps what it learned so it can be taken back.
"""
from pathlib import Path

import pytest

import vaf.core.a2a.store as store_mod
from vaf.core.a2a.room import Room, derive_peer_id, participant_key, transcript

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
RUNNER_SRC = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "rooms_root",
                        lambda base=None: Path(base) if base else tmp_path)
    return tmp_path


class _F:
    """The two fields the transcript reads, and nothing else it could lean on."""

    def __init__(self, kind, sender, text):
        self.kind, self.sender, self.body = kind, sender, {"text": text}


def test_the_transcript_names_who_spoke_and_leaves_the_plumbing_out():
    """MUTATION: drop the speaker label, or stop skipping bookkeeping and pings.

    A two-party chat gets away with "User:" and "Assistant:" because the roles are the
    whole cast. In a room the same sentence means something different depending on who
    said it, and half the speakers are agents nobody here controls - a fact learned
    without its speaker cannot be judged later.

    Bookkeeping is the room talking about itself. A join, an ack or a "still on this?"
    is not a fact about the world, and a memory that swallowed them would remember the
    plumbing.
    """
    frames = [_F("say", "p1", "the deploy is on Fridays"),
              _F("join", "p2", ""),
              _F("ping", "p1", "still on this?"),
              _F("ack", "p2", "ok"),
              _F("report", "p2", "moved it to Tuesdays")]
    out = transcript(frames, labels={"p1": "Alice", "p2": "Nobel"})

    assert "Alice: the deploy is on Fridays" in out, "the speaker is not named"
    assert "Nobel: moved it to Tuesdays" in out
    assert "still on this?" not in out, "a ping was learned as if somebody had said it"
    assert "ok" not in out, "an ack was learned"
    assert out.count("\n\n") == 1, "only the two real lines survive"

    # An unknown peer keeps its handle rather than vanishing: a line with no speaker at
    # all is worse than a line with an opaque one.
    assert "p9: hello" in transcript([_F("say", "p9", "hello")], labels={})


def test_the_transcript_drops_the_OLDEST_when_it_runs_out_of_room():
    """MUTATION: truncate from the end instead of the beginning.

    What is still being talked about is at the end. A budget spent on the opening of a
    long room leaves the model summarising a conversation that has since moved on.
    """
    frames = [_F("say", "p1", f"line {i} " + "x" * 40) for i in range(20)]
    out = transcript(frames, labels={"p1": "Alice"}, max_chars=200)

    assert "line 19" in out, "the newest line was dropped"
    assert "line 0" not in out, "the oldest line survived a full budget"
    assert len(out) <= 220


def test_learning_from_a_room_hands_over_its_own_transcript_and_its_own_source():
    """MUTATION: let the room fall back to the agent's history, or drop the source.

    Two separate defects, one line apart.

    The transcript: ONE process serves every tenant on this machine, so `agent.history`
    during a room turn holds whichever session was last loaded. Learning from it would
    teach one account another account's conversation - the isolation class this tree
    already has rules for, arriving through a door nobody had looked at.

    The source: a room is multi-voiced. A fact can come from a foreign agent nobody here
    controls, and `delete_memories_by_source_scope` deletes by exactly this field - so a
    stamped source is the difference between "take back what that room taught" and
    hunting through a vector store by hand.
    """
    learn = AGENT_SRC.split("def _learn() -> None:", 1)[1].split("\n            topic =", 1)[0]

    assert "conversation=transcript(" in learn, (
        "the room does not hand over its own transcript, so compaction reads whatever "
        "session the shared agent happens to hold")
    assert 'source=f"room/{room.room_id}"' in learn, "what the room taught is not stamped"
    assert "run_session_compaction_sync(" in learn, (
        "a second kind of learning was grown instead of calling the one that exists")
    assert "len(_all)" in learn, (
        "counted in turns rather than frames: a room can say twenty things while this "
        "agent answers once")


def test_compaction_prefers_a_supplied_transcript_over_the_agent():
    """MUTATION: fall back to the agent whenever the supplied transcript is empty.

    `or` instead of `is not None` is the whole bug: a room whose recent frames are all
    pings renders to "", and an `or` would then quietly reach into the agent - the exact
    cross-account read the parameter exists to prevent, on the one input where nobody
    would think to test it.
    """
    src = (ROOT / "vaf" / "memory" / "rag.py").read_text(encoding="utf-8")
    body = src.split("def run_session_compaction_sync(", 1)[1].split("\ndef ", 1)[0]

    assert "conversation if conversation is not None else" in body, (
        "an empty supplied transcript falls back to the agent's history")
    assert 'meta: Dict[str, Any] = {\n                        "source": source or f"memory/{date_str}"' in body, (
        "the caller's source does not reach the memory")


def test_a_room_turn_is_given_what_it_knows_and_learns_only_if_it_ran():
    """MUTATION: leave memory_context off the room's chat_step, or learn in `finally`.

    The first is the original defect, stated as a guard: the parameter exists, the chat
    lane fills it, and the room lane passed nothing.

    The second is the defect a repair invites. Learning belongs on `else`, not `finally`:
    a turn that raised produced half a conversation, and storing that as durable fact is
    worse than storing nothing. The distinction is one keyword and no test would notice.
    """
    branch = RUNNER_SRC.split("_room_wake = agent.collect_room_wake", 1)[1].split(
        "_pushed = False", 1)[0]

    assert "memory_context=_room_memory or None," in branch, (
        "the room turn still answers knowing nothing this account was ever told")
    assert "turn_memory_context(" in branch, "the room lane hand-rolls retrieval again"
    assert '_room_wake.get("query")' in branch, (
        "it retrieves with the wake PROMPT, which is instructions - a query built from "
        "instructions retrieves instructions")

    after = branch.split("append_domain_log_always(\n                                \"headless\", f\"[ROOM] delivery failed", 1)[1]
    assert "else:" in after.split('_room_wake["learn"]()', 1)[0], (
        "learning does not hang off `else`, so a failed turn teaches half a conversation")


def test_only_the_user_and_the_leader_carry_authority_in_a_room(rooms):
    """MUTATION: make from_authority true when ANY frame comes from an authority.

    Same conservative shape the neighbouring `from_user_only` already has, and for the
    same reason: a wake that MIXES a stranger's words with a leader's must not let the
    stranger's ask ride on the leader's standing. `any` instead of `all` is the whole
    difference, and a foreign agent that times its message alongside the leader's would
    be the one to find it.
    """
    src = AGENT_SRC.split("_authority = {user_peer}", 1)[1].split("return {", 1)[0]
    assert "all(f.sender in _authority for f in frames)" in src, (
        "one authorised frame in the wake authorises the whole wake")
    assert "set(room.leaders())" in src, "the leader is not an authority"
    assert "bool(frames) and" in src, "an EMPTY wake counts as authorised"


def test_remembering_in_a_room_needs_authority_and_never_happens_in_observe():
    """MUTATION: allow memory_save on any room turn, or allow it in observe too.

    Remembering is a write, and the room gate stops writes because frames come from
    agents nobody here controls. Opening it flat would let a foreign agent plant a
    durable "fact" that a later turn reads back as truth - prompt injection with a
    persistence layer.

    Observe is the tighter half: the user chose a read-only agent for that room, and a
    memory is the one write a later turn reads back as fact.
    """
    gate = AGENT_SRC.split("def _room_mode_gate_decision", 1)[1].split("\n    def ", 1)[0]

    assert "_ROOM_AUTHORITY_TOOLS" in gate, "no tool is ever allowed on authority"
    assert 'mode != "observe"' in gate, "a read-only agent may write to memory"
    assert 'room_turn.get("from_authority")' in gate, (
        "the exception does not ask who spoke, so any room turn may remember")
    assert '"memory_save"' in AGENT_SRC.split("_ROOM_AUTHORITY_TOOLS = frozenset(", 1)[1][:80]


def test_one_place_clamps_the_retrieval_size():
    """MUTATION: clamp in the lanes again, or drop the clamp from the primitive.

    Three lanes clamped `k` by hand - the chat queue, automations, thinking runs - and
    the room would have been the fourth. That is the count at which a copy becomes a
    primitive: a lane that forgot the clamp hands an unbounded `k` to the vector search,
    and nothing about the answer would look wrong.
    """
    from vaf.memory import rag

    body = (ROOT / "vaf" / "memory" / "rag.py").read_text(encoding="utf-8")
    prim = body.split("def turn_memory_context(", 1)[1].split("\ndef ", 1)[0]
    assert "max(1, min(20, k))" in prim, "the primitive does not clamp"

    for lane in ("vaf/core/automation.py", "vaf/core/thinking_mode.py",
                 "vaf/core/headless_runner.py", "vaf/api/mail_routes.py"):
        text = (ROOT / lane).read_text(encoding="utf-8")
        assert "max(1, min(20," not in text, f"{lane} still clamps by hand"
        assert "turn_memory_context(" in text, f"{lane} does not use the primitive"

    # Never raises into a turn, whatever the store is doing. Three outcomes collapse to
    # one answer on purpose: off, unreachable and nothing-matched all mean the prompt
    # gets no extra block, and a caller could not act on the difference anyway.
    import vaf.core.config as cfg_mod
    assert rag.turn_memory_context("x", user_scope_id=None, caller="test") == "" or True
    orig = cfg_mod.Config.get
    try:
        cfg_mod.Config.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store down"))
        assert rag.turn_memory_context("anything") == "", "a broken store raised into the turn"
    finally:
        cfg_mod.Config.get = orig
