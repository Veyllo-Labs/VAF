# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Cross Chat Hint: the wiring, not the retrieval.

A lane that works perfectly and is never called is the failure mode this file
exists for. It pins where the hints are looked up (once per turn, in chat_step),
where they land (under the memory block, in a copy of the first message), which
turns must not get them at all, and that the push to the browser is scoped to the
owner rather than broadcast.
"""
import re
from pathlib import Path

import pytest

from vaf.core import cross_chat
from vaf.core.agent import Agent as CoreAgent
from vaf.core.cross_chat import CrossChatHint

ROOT = Path(__file__).resolve().parent.parent
AGENT_SRC = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")

OWNER = "ab12cd34-owner"

HINT = CrossChatHint(
    session_id="chat_b",
    session_name="Rechnungen",
    updated_at="2026-08-09T10:00:00",
    score=0.9,
    terms=("reisekostenabrechnung",),
    text="Die Reisekostenabrechnung liegt hier",
)


class _Agent:
    """A stand-in with only the attributes the two methods read."""

    def __init__(self, **kw):
        self._current_user_scope_id = OWNER
        self._current_username = "owner"
        self.current_session_id = "chat_d"
        self._background_run = False
        self._front_office_mode = False
        self._current_chat_source = "web"
        self.__dict__.update(kw)


def _refresh(agent, query="Reisekostenabrechnung?"):
    return CoreAgent._refresh_cross_chat_hints(agent, query)


def _block(agent, memory_context):
    return CoreAgent._memory_system_block(agent, memory_context)


@pytest.fixture
def spy(monkeypatch):
    """Record every hints_for_turn call and answer with one fixed hint."""
    calls = []

    def _fake(query, **kwargs):
        calls.append((query, kwargs))
        return [HINT]

    monkeypatch.setattr(cross_chat, "hints_for_turn", _fake)
    return calls


@pytest.fixture(autouse=True)
def _no_emit(monkeypatch):
    """Keep the real websocket out of it; patch the INSTANCE, not the factory.

    Patching `get_web_interface` itself passes alone and fails in the full suite,
    because the singleton is already built by then.
    """
    from vaf.core.web_interface import get_web_interface
    pushed = []
    monkeypatch.setattr(get_web_interface(), "push_update_to_user",
                        lambda scope, data: pushed.append((scope, data)))
    return pushed


# ── where the block lands ───────────────────────────────────────────────────────

def test_hints_sit_under_the_memories_not_among_them():
    agent = _Agent(_cross_chat_block="Cross-chat hints (from this user's OTHER chats)")

    block = _block(agent, "[Source 1] (Relevance: 90%)\nthe user likes tea")

    assert block.index("## Memory context") < block.index("[Source 1]") < block.index("Cross-chat hints")


def test_an_empty_memory_context_keeps_its_own_guidance_when_hints_exist():
    """Appending hints to the memory STRING would flip this onto the wrong branch.

    With no memories the block teaches the model how to answer "who am I" and how
    to use memory_search. That is the normal state in the CLI and whenever the
    memory database is down, so the hints must not cost those four sentences.
    """
    agent = _Agent(_cross_chat_block="Cross-chat hints (from this user's OTHER chats)")

    block = _block(agent, "")

    assert "(No memories found for this query.)" in block
    assert "Do NOT use memory_save to look up" in block
    assert "Cross-chat hints" in block


def test_without_hints_the_block_is_exactly_what_it_always_was():
    agent = _Agent()
    with_attr = _block(agent, "[Source 1] x")
    agent2 = _Agent(_cross_chat_block="")
    assert with_attr == _block(agent2, "[Source 1] x")
    assert "Cross-chat" not in with_attr


# ── which turns get them ────────────────────────────────────────────────────────

def test_a_normal_chat_turn_gets_them(spy):
    agent = _Agent()

    _refresh(agent)

    assert len(spy) == 1
    assert spy[0][1]["user_scope_id"] == OWNER
    assert spy[0][1]["current_session_id"] == "chat_d"
    assert agent._cross_chat_block.startswith("Cross-chat hints")


@pytest.mark.parametrize("attrs", [
    {"_background_run": True},        # nobody is there to be shown a hint
    {"_front_office_mode": True},     # a STRANGER is driving this turn
    {"_current_chat_source": "voice_call"},  # every voice in the room counts as the owner
    {"_current_user_scope_id": None},        # fail closed on an unknown caller
])
def test_the_gated_lanes_never_even_look(spy, attrs):
    agent = _Agent(**attrs)

    _refresh(agent)

    assert spy == []
    assert agent._cross_chat_block == ""


def test_an_empty_question_does_not_scan(spy):
    agent = _Agent()

    _refresh(agent, "   ")

    assert spy == []


def test_a_failing_lane_never_breaks_the_turn(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("session store on fire")

    monkeypatch.setattr(cross_chat, "hints_for_turn", _boom)
    agent = _Agent()

    _refresh(agent)

    assert agent._cross_chat_block == ""
    assert agent._cross_chat_hints == []


def test_the_cli_lane_follows_the_caller_not_the_open_session(spy, monkeypatch):
    """`vaf run --session <id>` binds THAT session's owner onto the agent."""
    from vaf.core import identity_binding
    monkeypatch.setattr(identity_binding, "resolve_owner_identity",
                        lambda: identity_binding.Identity(scope=OWNER, username="owner"))
    agent = _Agent(_current_user_scope_id="ef56gh78-stranger", _identity_is_owner_bound=True)

    _refresh(agent)

    assert spy[0][1]["user_scope_id"] == OWNER


# ── the emit ────────────────────────────────────────────────────────────────────

def test_the_push_goes_to_the_owner_only(spy, _no_emit):
    agent = _Agent()

    _refresh(agent)

    assert len(_no_emit) == 1
    scope, payload = _no_emit[0]
    assert scope == OWNER
    assert payload["type"] == "cross_chat_hints"
    assert payload["hints"][0]["session_id"] == "chat_b"


def test_an_empty_result_still_pushes_so_the_panel_clears(monkeypatch, _no_emit):
    monkeypatch.setattr(cross_chat, "hints_for_turn", lambda *a, **kw: [])
    agent = _Agent()

    _refresh(agent)

    assert _no_emit[0][1]["hints"] == []


def test_the_emit_is_user_scoped_in_the_source():
    """Mutation guard: a global push_update here is the leak class of the RAG panel."""
    body = re.search(r'"type": "cross_chat_hints"', AGENT_SRC)
    assert body, "the cross-chat push moved - re-point this guard"
    window = AGENT_SRC[max(0, body.start() - 600):body.start()]
    assert "push_update_to_user(scope" in window
    assert "get_web_interface().push_update(" not in window


# ── the wiring itself ───────────────────────────────────────────────────────────

def test_chat_step_refreshes_the_hints_once_per_turn():
    """MUTATION: delete the call and this goes red; the lane would never run."""
    assert "self._refresh_cross_chat_hints(raw_user_input or user_input)" in AGENT_SRC
    # Inside chat_step, and skipped on the retry re-entry so one turn scans once.
    call = AGENT_SRC.index("self._refresh_cross_chat_hints(raw_user_input or user_input)")
    assert "if not auto_retry:" in AGENT_SRC[call - 200:call]


def test_all_generation_paths_build_the_block_through_the_one_builder():
    # Three, not two: the in-process local lane used to send the raw history and
    # therefore dropped the memory block entirely (see
    # tests/test_generation_branch_parity.py).
    assert AGENT_SRC.count("self._memory_system_block(memory_context)") == 3
    # And the literal exists exactly once, in that builder.
    assert AGENT_SRC.count('"## Memory context (relevant to this query)\\n\\n"') == 2  # populated + empty branch


# ── a room turn is a turn here too ─────────────────────────────────────────────

def test_a_room_turn_asks_with_what_was_said_and_excludes_its_own_room(spy):
    """MUTATION: let the room turn ask with the wake prompt, or forget the exclusion.

    The wake prompt is instructions - roster, board, reminders - and a query built
    from instructions matches instruction words in every chat: the filler-word
    failure the retrieval module documents, rebuilt at the call site. What was SAID
    travels in _room_turn["query"], and the room itself is excluded exactly as the
    current session is: a conversation must not hint into itself.
    """
    agent = _Agent(_room_turn={"room_id": "room-w", "mode": "assist",
                               "query": "der Matrix Regen in team.html"})
    _refresh(agent, "WAKE PROMPT full of roster and board instructions")
    assert len(spy) == 1
    query, kwargs = spy[0]
    assert query == "der Matrix Regen in team.html", "it asked with the prompt, not the words"
    assert kwargs.get("current_room_id") == "room-w", "the room would hint into itself"
    assert kwargs.get("current_session_id") is None, (
        "the stale session is a legitimate OTHER chat during a room turn - excluding "
        "it hides a real conversation for no reason")
    assert agent._cross_chat_block, "the block never reached the room turn's prompt"


def test_a_room_wake_of_pure_pings_asks_nothing(spy):
    """A wake whose frames carried no words has no question to ask - scanning every
    chat with an empty query would be pure cost with a guaranteed empty answer."""
    agent = _Agent(_room_turn={"room_id": "room-w", "mode": "assist", "query": "  "})
    _refresh(agent, "WAKE PROMPT")
    assert spy == []


def test_a_room_turn_never_repaints_the_browser_panel(spy, _no_emit):
    """MUTATION: push during room turns too.

    The push replaces the hint panel in EVERY window this user has open - empty list
    included, so panels clear. A background turn nobody typed must not wipe what the
    user is looking at. The room turn's hints live in its prompt and nowhere else.
    """
    agent = _Agent(_room_turn={"room_id": "room-w", "mode": "assist",
                               "query": "der Matrix Regen"})
    _refresh(agent, "WAKE")
    assert spy, "the lookup itself must still run"
    assert _no_emit == [], "a room turn painted the user's panel"
    # And the ordinary turn still pushes - the guard must not widen.
    agent2 = _Agent()
    _refresh(agent2)
    assert len(_no_emit) == 1


def test_the_runner_hands_over_every_key_a_reader_consumes():
    """MUTATION: read a new key off _room_turn without copying it in the runner.

    Measured failure, one day old: the mode gate read room_turn.get("from_authority")
    while the runner copied four other keys and not that one. The gate fails CLOSED,
    so nothing crashed - the feature simply never fired, and the source-scanning
    tests were green because the wake computed the value and the gate read the name.
    The hand-off between them was the untested step. This asserts the runner's dict
    literal carries EVERY key any agent.py reader consumes, so the next reader added
    without its copy fails here by name.
    """
    runner_src = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    literal = runner_src.split("agent._room_turn = {", 1)[1].split("}", 1)[0]
    handed = set(re.findall(r'"([a-z_]+)":', literal))

    consumed = set(re.findall(r'room_turn\.get\("([a-z_]+)"\)', AGENT_SRC))
    consumed |= set(re.findall(r'room_turn\["([a-z_]+)"\]', AGENT_SRC))
    consumed.discard("room_id")  # present, just also indexed via variables elsewhere
    missing = consumed - handed - {"mode"}
    assert not missing, (
        f"agent.py reads {sorted(missing)} off _room_turn and the runner never "
        f"hands it over - the gate fails closed, so this is invisible at runtime")
    for key in ("room_id", "mode", "query", "from_user", "from_user_only",
                "from_authority"):
        assert key in handed, f"the runner stopped copying {key}"
