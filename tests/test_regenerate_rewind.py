# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Asking the newest exchange again rewinds the transcript cleanly.

The rewind cuts at a USER boundary, never inside a turn. Two reasons, and both
fail silently rather than loudly if the cut moves:

- An assistant message carrying ``tool_calls`` and its matching ``role:"tool"``
  results are one unit. A cut between them leaves an orphan in the STORED
  record; the pre-send repair drops it from the wire, so nothing raises, and the
  model answers over a turn that never happened.
- The turn's persistence compares the last stored user message with the incoming
  one and treats a match as a continuation. A surviving user message therefore
  stacks the new answer under the old one instead of replacing it.

Also pinned: the live agent is forced to rebuild from the rewound file (its
in-memory history is otherwise authoritative), and the gates run before the cut.
"""
import re
from pathlib import Path

from vaf.core.session import Message, Session, truncate_to_last_user_turn

_REPO = Path(__file__).resolve().parents[1]
_WEB_SERVER = _REPO / "vaf" / "core" / "web_server.py"


def _turn_with_tools() -> Session:
    """A realistic newest exchange: a question, a tool call and its result, the
    per-turn context summary, and the answer."""
    s = Session(id="green123456", name="chat")
    s.messages = [
        Message(role="user", content="first question"),
        Message(role="assistant", content="first answer"),
        Message(role="user", content="second question"),
        Message(role="assistant", content="", tool_calls=[{"id": "call_1", "function": {"name": "web_search"}}]),
        Message(role="tool", content="search result", tool_call_id="call_1", name="web_search"),
        Message(role="system", content="[Context: searched the web]"),
        Message(role="assistant", content="second answer"),
    ]
    s.runtime_state["user_turn_count"] = 2
    return s


def test_the_whole_newest_exchange_goes_including_its_tool_scaffolding():
    s = _turn_with_tools()
    removed = truncate_to_last_user_turn(s)
    assert removed is not None and removed.content == "second question", \
        "the question to ask again was not returned"
    assert [m.role for m in s.messages] == ["user", "assistant"], \
        "the cut did not stop at the previous exchange"
    assert [m.content for m in s.messages] == ["first question", "first answer"]


def test_no_tool_result_survives_without_the_call_that_made_it():
    """The assertion that actually proves the invariant. A length check would pass
    for a cut that keeps the user message or drops only the trailing assistant."""
    s = _turn_with_tools()
    truncate_to_last_user_turn(s)
    offered = {c.get("id") for m in s.messages for c in (m.tool_calls or [])}
    for m in s.messages:
        if m.role == "tool":
            assert m.tool_call_id in offered, \
                f"orphaned tool result {m.tool_call_id} survived the cut - the stored record is broken"
    assert not any(m.role == "system" and str(m.content).startswith("[Context:") for m in s.messages), \
        "the discarded turn's context summary outlived the turn it describes"


def test_the_user_message_goes_too_or_the_new_answer_is_appended_under_the_old_one():
    s = _turn_with_tools()
    truncate_to_last_user_turn(s)
    assert not any(m.role == "user" and m.content == "second question" for m in s.messages), \
        "the re-asked question is still stored, so the re-run reads as a continuation and appends"


def test_the_turn_is_not_counted_twice():
    s = _turn_with_tools()
    truncate_to_last_user_turn(s)
    assert s.runtime_state["user_turn_count"] == 1, \
        "the discarded turn still counts, so memory compaction fires a turn early"


def test_the_whole_question_comes_back_not_only_its_words():
    """An image travels in the message's metadata. A caller handed only the text
    would re-ask about a picture the model can no longer see, because the message
    that carried it has just been deleted."""
    s = _turn_with_tools()
    s.messages[2].metadata = {"images": [{"path": "/tmp/photo.jpg", "name": "photo.jpg"}]}
    removed = truncate_to_last_user_turn(s)
    assert (removed.metadata or {}).get("images"), \
        "the removed question no longer carries its attachment, so the re-run is blind"


def test_a_session_without_a_question_is_left_alone():
    s = Session(id="green123456", name="chat")
    s.messages = [Message(role="assistant", content="a proactive note")]
    assert truncate_to_last_user_turn(s) is None
    assert len(s.messages) == 1, "a session with nothing to re-ask was modified anyway"
    empty = Session(id="red654321", name="chat")
    assert truncate_to_last_user_turn(empty) is None


def test_the_count_never_goes_negative():
    s = _turn_with_tools()
    s.runtime_state["user_turn_count"] = 0
    truncate_to_last_user_turn(s)
    assert s.runtime_state["user_turn_count"] == 0


def test_a_live_agent_can_be_forced_to_reread_the_rewound_session():
    """Without the force the agent keeps answering from the reply just discarded:
    its in-memory history is authoritative and the early return protects it."""
    src = (_REPO / "vaf" / "core" / "agent.py").read_bytes().decode("utf-8")
    sig = re.search(r"def load_session_context\(self, session_id: str([^)]*)\)", src)
    assert sig and "force" in sig.group(1), "load_session_context lost its force parameter"
    body = src.split("def load_session_context", 1)[1][:1200]
    assert "if not force and hasattr(self, 'current_session_id')" in body, \
        "the early return ignores force again - a rewind would be a no-op on the live history"
    runner = (_REPO / "vaf" / "core" / "headless_runner.py").read_bytes().decode("utf-8")
    assert 'force=bool((getattr(task, "metadata", None) or {}).get("force_reload"))' in runner, \
        "the runner no longer honours force_reload, so the rewind never reaches the agent"


def test_the_gates_run_before_anything_is_discarded():
    """Refuse, never stop-then-discard: a running turn holds a live index into the
    history it started with, and the runner writes the whole session back when it
    finishes, which would undo the cut anyway."""
    src = _WEB_SERVER.read_bytes().decode("utf-8").replace("\r\n", "\n")
    branch = src.split('elif type == "regenerate_last_reply":', 1)
    assert len(branch) == 2, "the regenerate branch is gone"
    body = branch[1].split("elif type ==", 1)[0]
    cut = body.index("truncate_to_last_user_turn(")
    for gate in ("_ws_session_owner_ok", "is_busy_for_session", "get_active_tasks"):
        assert gate in body, f"the {gate} gate is missing from the regenerate branch"
        assert body.index(gate) < cut, f"the {gate} gate runs after the transcript is already cut"
    assert "repoint=False" in body, \
        "the session is loaded with repoint, which moves what 'current' means and restores foreign state"
    assert '"force_reload": True' in body, \
        "the re-queued turn does not force the agent to reread, so it answers from the discarded reply"
    assert "get_pending_tasks" in body, \
        "a specialist whose process is still starting passes the gate - it is pending, not yet active"


def test_the_repaint_puts_the_question_back_and_reaches_every_view():
    """The question was just deleted from the record and the runner only writes it
    again when the turn ends, so the repaint has to carry it. And a second window
    on the same chat must not keep showing the reply that no longer exists."""
    src = _WEB_SERVER.read_bytes().decode("utf-8").replace("\r\n", "\n")
    body = src.split('elif type == "regenerate_last_reply":', 1)[1].split("elif type ==", 1)[0]
    assert "_regen_msgs.append(_regen_ask)" in body, \
        "the repaint no longer carries the re-asked question, so the new answer has nothing asking it"
    assert "broadcast_to_session" in body, \
        "only the tab that pressed is repainted; another window keeps the discarded reply on screen"
    assert '_regen_meta["images"] = _regen_imgs' in body, \
        "the re-run no longer carries the question's attachment, so an image turn is re-asked blind"
