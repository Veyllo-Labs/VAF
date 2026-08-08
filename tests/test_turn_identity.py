# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Every exchange has an identity, and everything it produces carries it.

A session says WHO is talking; a turn says WHICH exchange. Without the second
one a created file has no address, and the browser could only bind it to "the
newest answer that exists right now" - which, for the whole duration of a tool
call, is still the PREVIOUS turn's answer. An uploaded image was worse than a
race: it is announced before the turn starts, so it landed on the older answer
every single time from the second message on.

Pinned here: the id exists per task, it reaches the emitters through the same
context the session uses, it survives the fork into the coder subprocess, and
the announcement of an upload happens AFTER the turn it belongs to exists.
"""
import inspect
import os
import re
from pathlib import Path

import pytest

from vaf.core import subagent_ipc as ipc

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_turn_ctx(monkeypatch):
    """The context is per-thread and set-only; tests must not inherit it."""
    monkeypatch.delenv("VAF_TURN_ID", raising=False)
    token = ipc._turn_ctx.set(ipc._UNSET)
    yield
    ipc._turn_ctx.reset(token)


def test_every_task_gets_its_own_identity():
    from vaf.core.task_queue import AgentTask

    a = AgentTask(session_id="s1", input_text="hi")
    b = AgentTask(session_id="s1", input_text="hi")
    assert a.turn_id and b.turn_id
    assert a.turn_id != b.turn_id, "two exchanges must be distinguishable"


def test_the_queue_hands_the_identity_back_to_the_caller():
    """The upload lane needs it BEFORE the runner picks the task up."""
    from vaf.core.task_queue import TaskQueue

    tq = TaskQueue()
    task = tq.add(session_id="s-turn-test", input_text="hello", source="web")
    assert getattr(task, "turn_id", None), "tq.add must return an addressable task"
    tq.get(timeout=0.5)  # drain, so the singleton queue stays clean for others


def test_the_context_answers_told_first_and_the_fork_second():
    assert ipc.get_current_turn_id() is None       # never told, no env
    os.environ["VAF_TURN_ID"] = "from-parent"
    assert ipc.get_current_turn_id() == "from-parent"   # the child case
    ipc.set_current_turn_id("told")
    assert ipc.get_current_turn_id() == "told"          # told wins over the env
    ipc.set_current_turn_id(None)
    assert ipc.get_current_turn_id() is None, \
        "told None is a declaration, not a fallback to the parent's turn"


def test_a_created_file_carries_the_turn_it_came_from(monkeypatch):
    from vaf.core import web_interface as wi

    sent = []
    # Patch the INSTANCE, not the class: WebInterfaceManager is a singleton and a
    # neighbouring test leaves an instance attribute behind that would shadow a
    # class-level patch (green alone, red in the full suite).
    w = wi.get_web_interface()
    monkeypatch.setattr(w, "_push_session_update", lambda sid, payload: sent.append(payload))
    # In-process branch: without a server loop the notify posts over HTTP instead.
    monkeypatch.setattr(w, "_server_loop", object(), raising=False)
    monkeypatch.setattr("vaf.core.session.record_created_file", lambda *a, **kw: None)

    ipc.set_current_turn_id("turn-abc")
    wi.notify_file_created("s1", "/tmp/x.png", title="x.png")
    assert sent and sent[-1]["turnId"] == "turn-abc"

    # An explicit id wins: the upload is announced for a turn the announcer is
    # not running in.
    wi.notify_file_created("s1", "/tmp/y.png", title="y.png", turn_id="turn-xyz")
    assert sent[-1]["turnId"] == "turn-xyz"


def test_the_answer_carries_the_same_turn(monkeypatch):
    """A file can only find its answer if BOTH sides carry the address."""
    from vaf.core import web_interface as wi

    sent = []
    w = wi.get_web_interface()
    monkeypatch.setattr(w, "_push_session_update", lambda sid, payload: sent.append(payload))

    ipc.set_current_turn_id("turn-abc")
    w.emit_agent_message("assistant", "hello", session_id="s1")
    w.emit_message_complete("hello", session_id="s1")
    assert [p["turnId"] for p in sent] == ["turn-abc", "turn-abc"]


def test_the_coder_subprocess_inherits_the_turn():
    """It finishes minutes later; its files still belong to the turn that
    started it, so the id has to cross the fork like the session does."""
    src = inspect.getsource(__import__("vaf.tools.coder", fromlist=["x"]))
    assert '_sub_env["VAF_TURN_ID"]' in src, \
        "the async coder announces its files with no address again"


def test_the_upload_is_announced_after_its_turn_exists():
    """Announcing first is exactly what put an uploaded image under the
    previous answer: at that moment its own turn did not exist yet."""
    import vaf.core.web_server as web_server

    src = inspect.getsource(web_server.websocket_endpoint)
    add_at = src.find("_task = tq.add(")
    notify_at = src.find("notify_file_created(session_id, _ai")
    assert add_at != -1 and notify_at != -1, "the upload lane moved - re-point this guard"
    assert add_at < notify_at, "the upload is announced before its turn exists again"
    assert "turn_id=getattr(_task" in src, "the upload announcement lost its address"


# ---------------------------------------------------------------------------
# The browser half: the guess is gone and the address is used
# ---------------------------------------------------------------------------

def _page() -> str:
    return (_REPO / "web" / "app" / "page.tsx").read_text(encoding="utf-8")


def test_the_browser_no_longer_guesses_the_newest_answer():
    src = _page()
    guess = re.findall(r"filter\(\(\{ m \}\) => m\.role === 'assistant'\)\.pop\(\)\?\.i", src)
    # One legitimate use remains: the streaming delta comparison against the
    # previous bubble, which is about TEXT, not about ownership of a file.
    assert len(guess) <= 1, f"{len(guess)} places still guess which answer owns a file"
    assert "chipTargetIndex" in src, "the address lookup is gone"


def test_a_chip_waits_for_its_own_turn():
    src = _page()
    assert "const ready = pending.filter(f => !f.turnId || f.turnId === data.turnId)" in src, \
        "completing one turn flushes chips belonging to another turn again"
    assert "setCreatedFiles([]);" not in src, \
        "sending a message throws away chips that are still waiting for their turn"


def test_the_bubble_carries_its_turn_and_the_whole_turn_is_searched_for_chips():
    src = _page()
    assert "turnId: data.turnId" in src, "the assistant bubble no longer carries its turn"
    assert "const turnChips = " in src, "the turn-wide chip list is gone"
    # Both the CONDITION and the loop have to read it: a render gated on the
    # anchor's own list drops chips that sit on the turn's final answer, even
    # though the list itself is still computed correctly.
    assert "{isBot && turnChips.length > 0 && (" in src and "turnChips.map(" in src, \
        "a grouped turn renders only the anchor's chips again - the rest vanish"
