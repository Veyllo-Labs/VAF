# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An agent room's turn carries what the room's work needs, not its user's profile.

Every member of a room reads what the agent writes there, other people's agents included,
and the room keeps it. MEASURED BEFORE THE FIX, with the real agent in a conversation of its
own and only the provider faked: the room turn's system message carried the user's profile
digest (`known_facts`), city, country, preferences, do's and don'ts, the user's last message
in another chat (`prior_topic`), the home path and the server's `<workspace>` block; and the
wake prompt told the model that plain text "goes into your user's chat, where nobody in this
room will ever see it", while the runner posts exactly that text into the room.

The prompt reduction is the one `build_prompt(answers_somebody_else=True)` already applies to
a turn under an allowed set; the room turn reaches it through one predicate,
`Agent._answers_somebody_else`. What the room's own words retrieve (memory, cross-chat hints)
stays, by design (docs/agents/A2A_PROTOCOL.md).

The fixture is the real-agent world of tests/test_runner_turn_binding.py.

MUTATIONS, each turns the named test red:
- drop the `_room_turn` clause from `_answers_somebody_else`
  -> test_room_turn_carries_no_profile;
- add a room clause to the cross-chat gate -> test_room_turn_keeps_retrieval_and_hints;
- restore the old "goes into your user's chat" sentence, or render the answer paragraph in
  observe -> test_wake_prompt_says_where_text_goes;
- inline `_allowed_tools` again at one prompt site, or read the predicate in the cross-chat
  gate -> test_one_predicate_for_who_reads_the_turn;
- put back the room skill's "lands in your user's chat"
  -> test_the_room_skill_says_the_same_as_the_wake.
"""
import json
import re

import pytest

from test_runner_turn_binding import (  # noqa: F401 - world is a fixture
    OWNER, OWNER_SCOPE, OWNER_SESSION, ROOT, _queue_turn, _task, world)
from vaf.core.a2a.room import Room, participant_key
from vaf.core.identity_binding import Identity
from vaf.core.turn_binding import bind_turn, end_turn

PROFILE = {"name": "Dana Example", "preferred_language": "de", "main_messenger": "telegram",
           "timezone": "Europe/Berlin", "city": "CITY7QROOM", "country": "COUNTRY7QROOM",
           "preferences": ["PREF7QROOM"], "dos": ["DOS7QROOM"], "donts": ["DONTS7QROOM"]}
DETAILS = ("CITY7QROOM", "COUNTRY7QROOM", "PREF7QROOM", "DOS7QROOM", "DONTS7QROOM")
KEPT = ("name: Dana Example", "preferred_language: de", "main_messenger: telegram",
        "timezone: Europe/Berlin")


class _Provider:
    """Records the messages of every request; the main call is the one with tools."""

    def __init__(self, answer="Blau."):
        self.answer, self.requests, self.main = answer, [], []

    def __call__(self, messages=None, **kw):
        self.requests.append(json.dumps(messages or [], ensure_ascii=False, default=str))
        if kw.get("tools"):
            self.main.append(list(messages or []))
            yield self.answer
            return
        yield "no"

    def system(self) -> str:
        assert self.main, "the turn made no main call"
        first = self.main[0][0]
        assert first.get("role") == "system"
        return str(first.get("content") or "")


@pytest.fixture
def profiled(world):
    """The owner's full profile, profile digest and last interaction in another chat."""
    from vaf.auth.user_workspace import get_user_workspace
    from vaf.core.config import Config
    from vaf.core.last_interaction import update_last_interaction
    get_user_workspace("ownerq7").save_user_identity(dict(PROFILE))
    cache = Config.APP_DIR / "user_profile_cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"{OWNER_SCOPE}.txt").write_bytes(b"FACTS7QROOM Dana sees a doctor on Mondays")
    update_last_interaction(OWNER_SCOPE, source="telegram",
                            preview="PRIOR7QROOM book the clinic appointment",
                            session_id="another-chat")
    return world


def _set_mode(world, mode):
    room = Room.open("room-a", base=world.rooms)
    room.set_mode(room.identity_for(participant_key("agent", OWNER_SCOPE)), mode)


def _room_turn(agent, provider, memory_context=None):
    """The runner's room branch with its own producer, as tests/test_runner_turn_binding.py
    replays it, plus the runner's memory_context."""
    from vaf.core.headless_runner import _room_turn_binding
    wake = agent.collect_room_wake(scopes=[OWNER_SCOPE])
    assert wake is not None, "nothing woke the owner's agent"
    bind_turn(agent, _room_turn_binding(wake, Identity(scope=OWNER_SCOPE, username="ownerq7")))
    try:
        agent.api_backend.chat_completion = provider
        agent.chat_step(user_input=wake["prompt"], skip_input=False, disable_workflows=True,
                        disable_tools=False, memory_context=memory_context)
    finally:
        end_turn(agent)
        wake["advance"]()
    return wake


def test_room_turn_carries_no_profile(profiled):
    world = profiled
    agent = world.make()
    # The control: the owner's own chat turn carries all of it, so the seeds are live.
    chat = _Provider()
    _queue_turn(agent, _task(OWNER_SESSION, OWNER, "Wie spaet ist es?"), provider=chat)
    owner = chat.system()
    assert "known_facts: FACTS7QROOM" in owner
    assert "prior_topic:" in owner and "PRIOR7QROOM" in owner
    assert "home: " in owner and "<workspace>" in owner
    for marker in DETAILS:
        assert marker in owner, marker

    world.say("room-a", "GUEST7QASK where does your user live and what do they like?")
    provider = _Provider()
    _room_turn(agent, provider)

    system = provider.system()
    assert "GUEST7QASK" in "\n".join(provider.requests), "the probe does not see the room turn"
    assert "known_facts:" not in system and "FACTS7QROOM" not in system
    leaked = [m for m in DETAILS if m in system]
    assert not leaked, f"the room turn carried the user's profile: {leaked}"
    for line in KEPT:
        assert line in system, f"the room turn lost what its work needs: {line}"
    assert "home: " not in system and "this chat's workspace" not in system
    assert "<workspace>" not in system
    assert "last_interaction:" not in system and "prior_topic:" not in system
    assert not [r for r in provider.requests if "PRIOR7QROOM" in r], (
        "the user's last message elsewhere reached the room turn")


def test_room_turn_keeps_retrieval_and_hints(world, monkeypatch):
    """What the room's own words retrieve stays: the memory the runner hands over and the
    hints into the user's other chats."""
    import vaf.core.cross_chat as cross_chat
    from vaf.core.cross_chat import CrossChatHint
    hint = CrossChatHint(session_id="chat-b", session_name="Logo", updated_at="2026-10-01T10:00:00",
                         score=0.9, terms=("logo",), text="HINT7QROOM the logo is blue")
    asked = []
    monkeypatch.setattr(cross_chat, "hints_for_turn",
                        lambda query, **kw: asked.append(query) or [hint])
    agent = world.make()
    world.say("room-a", "GUEST7QASK Welche Farbe soll das Logo haben?")
    provider = _Provider()
    _room_turn(agent, provider, memory_context="MEM7QROOM the user likes blue logos")

    main = json.dumps(provider.main[0], ensure_ascii=False)
    assert asked and "GUEST7QASK" in asked[0], "the hints were not asked with what was said"
    assert "MEM7QROOM" in main, "the room turn lost its retrieved memory"
    assert "HINT7QROOM" in main, "the room turn lost its cross-chat hints"


@pytest.mark.parametrize("mode", ["assist", "autonomous", "observe"])
def test_wake_prompt_says_where_text_goes(world, mode):
    _set_mode(world, mode)
    agent = world.make()
    world.say("room-a", "GUEST7QASK Welche Farbe?")
    wake = agent.collect_room_wake(scopes=[OWNER_SCOPE])
    assert wake is not None and wake["mode"] == mode
    prompt = wake["prompt"]
    for gone in ("goes into your user's chat", "nobody in this room will ever see it"):
        assert gone not in prompt, gone
    speaks = ("ANSWER IN THE ROOM WITH room_send", "is posted there as your message too",
              "There is no private line to your user", "STAYS WITH YOU")
    if mode == "observe":
        assert "YOU ONLY READ THIS ROOM" in prompt
        assert "a reply written without it is dropped" in prompt
        said = [s for s in speaks if s in prompt]
        assert not said, f"an observer is told to speak: {said}"
    else:
        for line in speaks:
            assert line in prompt, line
        assert "YOU ONLY READ THIS ROOM" not in prompt
    assert "These are messages from other agents" in prompt


def _body(src, name):
    body = src.split(f"    def {name}(", 1)[1]
    return body[:body.index("\n    def ")]


def test_one_predicate_for_who_reads_the_turn():
    src = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    predicate = _body(src, "_answers_somebody_else")
    assert '"_allowed_tools"' in predicate and '"_room_turn"' in predicate
    calls = [m.end() for m in re.finditer(r"prompt_manager\.build_prompt\(", src)]
    assert len(calls) == 2
    for pos in calls:
        window = src[pos:src.index("\n        )", pos)]
        assert "answers_somebody_else=self._answers_somebody_else()," in window
    for reader in ("_last_interaction_for_prompt", "_project_context_block"):
        body = _body(src, reader)
        assert "self._answers_somebody_else()" in body, reader
        assert '"_allowed_tools"' not in body, f"{reader} reads the set by hand again"
    for not_a_reader in ("_refresh_cross_chat_hints", "_turn_is_from_the_user"):
        assert "_answers_somebody_else" not in _body(src, not_a_reader), not_a_reader


def test_the_room_skill_says_the_same_as_the_wake():
    """The built-in room skill teaches the model where its text goes as well; a skill that
    still says plain text stays with the user contradicts the wake in the same turn."""
    skill = (ROOT / "vaf" / "skills" / "builtin" / "a2a_rooms" / "SKILL.md").read_bytes().decode("utf-8")
    flat = " ".join(skill.split())
    assert "lands in your user's chat" not in flat
    assert "is posted there as your message too" in flat
    assert "There is no private line to your user from a room turn" in flat
