# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A chat's created files are part of its transcript.

Before, a file chip lived only in the browser that received the `file_created` event: a chat
switch to an idle chat or a reload rebuilt the messages from the server, which had no files,
and the chips were gone. Now both notify paths record the file on the session
(`runtime_state["created_files"]`, with the `purpose` and `toolId` fields of the file event), the stored answer
carries its turn id, and `_history_projection` puts each file on the last entry of its turn.
"""
import asyncio
from pathlib import Path

import pytest

import vaf.core.session as session_mod
from vaf.core.session import CREATED_FILES_KEY, TURN_ID_KEY, Message, Session

SID = "web-chat-0041"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(session_mod, "default_sessions_dir", lambda: sessions)
    mgr = session_mod.SessionManager(storage_dir=str(sessions))
    monkeypatch.setattr(session_mod, "_manager", mgr)
    s = mgr.new(name="Chat", user_scope_id="ab12cd34-0000-4000-8000-000000000001")
    s.id = SID
    mgr.save(s, sync_state=False)
    return mgr


def _files(mgr):
    return mgr.load(SID).runtime_state.get(CREATED_FILES_KEY) or []


# ── AC6: both notify paths record ────────────────────────────────────────────────

def test_the_in_process_notify_records_the_chip(store, monkeypatch, tmp_path):
    """MUTATION: drop toolId from the stored entry - red."""
    from vaf.core import web_interface as wi_mod
    wi = wi_mod.get_web_interface()
    pushed = []
    monkeypatch.setattr(wi, "_server_loop", object())
    monkeypatch.setattr(wi, "_push_session_update", lambda sid, data: pushed.append(data))
    monkeypatch.setattr(wi_mod, "_current_tool_call_id", lambda: "call_7")
    shot = tmp_path / "work" / "check.png"
    shot.parent.mkdir()
    shot.write_bytes(b"png")
    wi_mod.notify_file_created(SID, shot, title="check.png", turn_id="t1", purpose="check")
    [entry] = _files(store)
    assert entry["path"] == shot.resolve().as_posix()
    assert (entry["name"], entry["turnId"], entry["purpose"], entry["toolId"]) == (
        "check.png", "t1", "check", "call_7")
    assert pushed and pushed[0]["filePath"] == entry["path"]


def test_an_unsafe_folder_is_recorded_but_not_anchored(store, monkeypatch, tmp_path):
    """MUTATION: append after the unsafe-dir early return - red."""
    import vaf.core.workspace_guard as guard
    monkeypatch.setattr(guard, "is_unsafe_project_dir", lambda d: True)
    session_mod.record_created_file(SID, tmp_path / "notes.txt", entry={"turnId": "t2"})
    loaded = store.load(SID)
    assert [f["name"] for f in loaded.runtime_state[CREATED_FILES_KEY]] == ["notes.txt"]
    assert "last_project_path" not in loaded.runtime_state


def test_the_subprocess_route_records_the_chip(store, monkeypatch, tmp_path):
    import vaf.core.web_server as server

    async def _to_session(sid, data):
        return None
    monkeypatch.setattr(server.manager, "broadcast_to_session", _to_session)
    body = {"type": "file_created", "sessionId": SID, "filePath": (tmp_path / "out.csv").as_posix(),
            "title": "out.csv", "turnId": "t3", "toolId": "call_9"}
    asyncio.run(server.receive_workflow_update(server.WorkflowUpdate(**body), _caller={}))
    [entry] = _files(store)
    assert (entry["name"], entry["turnId"], entry["toolId"]) == ("out.csv", "t3", "call_9")
    assert "purpose" not in entry


def test_the_list_is_deduplicated_and_bounded(store, tmp_path):
    for _ in range(3):
        session_mod.record_created_file(SID, tmp_path / "a.txt", entry={"turnId": "t1"})
    session_mod.record_created_file(SID, tmp_path / "a.txt", entry={"turnId": "t2"})
    assert [f["turnId"] for f in _files(store)] == ["t1", "t2"]
    for i in range(session_mod.CREATED_FILES_MAX + 5):
        session_mod.record_created_file(SID, tmp_path / f"f{i}.txt")
    files = _files(store)
    assert len(files) == session_mod.CREATED_FILES_MAX
    assert files[-1]["name"] == f"f{session_mod.CREATED_FILES_MAX + 4}.txt"


# ── AC6: the projection ──────────────────────────────────────────────────────────

def _session(created, *, rounds=True):
    meta = {TURN_ID_KEY: "t1"}
    if rounds:
        meta["rounds"] = [{"round": 1, "text": "Looking.", "tool_call_ids": ["c1"], "answer": False},
                          {"round": 2, "text": "Done, here it is.", "tool_call_ids": [], "answer": True}]
    s = Session(id=SID, name="Chat")
    s.messages = [
        Message(role="user", content="old question", timestamp="2026-01-01T10:00:00"),
        Message(role="assistant", content="old answer", timestamp="2026-01-01T10:00:05"),
        Message(role="user", content="make a report", timestamp="2026-01-01T11:00:00"),
        Message(role="assistant", content="", timestamp="2026-01-01T11:00:01",
                tool_calls=[{"id": "c1", "type": "function",
                             "function": {"name": "write_file", "arguments": "{}"}}]),
        Message(role="tool", content="written", timestamp="2026-01-01T11:00:02",
                tool_call_id="c1", name="write_file"),
        Message(role="assistant", content="Looking. Done, here it is.",
                timestamp="2026-01-01T11:00:09", metadata=meta),
    ]
    s.runtime_state = {CREATED_FILES_KEY: created}
    return s


def test_files_go_on_the_last_entry_of_their_turn():
    """MUTATION: put the files on the FIRST entry of the turn - red."""
    from vaf.core.web_server import _history_projection
    created = [{"path": "/w/report.md", "name": "report.md", "turnId": "t1"},
               {"path": "/w/shot.png", "name": "shot.png", "turnId": "t1",
                "purpose": "check", "toolId": "c1"}]
    out = _history_projection(_session(created), SID)
    rounds = [e for e in out if e.get("round")]
    assert [e["round"] for e in rounds] == [1, 2]
    assert all(e.get("turnId") == "t1" for e in rounds)
    assert "files" not in rounds[0]
    assert rounds[1]["files"] == [
        {"path": "/w/report.md", "name": "report.md"},
        {"path": "/w/shot.png", "name": "shot.png", "purpose": "check", "toolId": "c1"}]


def test_a_turn_without_rounds_gets_turn_id_and_files():
    from vaf.core.web_server import _history_projection
    out = _history_projection(_session([{"path": "/w/a.txt", "name": "a.txt", "turnId": "t1"}],
                                       rounds=False), SID)
    answer = out[-1]
    assert answer["turnId"] == "t1" and answer["files"] == [{"path": "/w/a.txt", "name": "a.txt"}]


def test_a_file_whose_turn_is_gone_is_not_projected():
    """A regenerate discarded the answer the file belonged to (or its turn still runs)."""
    from vaf.core.web_server import _history_projection
    out = _history_projection(_session([{"path": "/w/old.txt", "name": "old.txt",
                                         "turnId": "discarded"}]), SID)
    assert not any("files" in e for e in out)


def test_an_unaddressed_file_goes_on_the_last_answer_not_newer_than_it():
    from vaf.core.web_server import _history_projection
    created = [{"path": "/w/x.txt", "name": "x.txt", "ts": "2026-01-01T10:30:00"},
               {"path": "/w/y.txt", "name": "y.txt", "ts": "2026-01-01T12:00:00"}]
    out = _history_projection(_session(created), SID)
    old_answer = next(e for e in out if e.get("content") == "old answer")
    assert old_answer["files"] == [{"path": "/w/x.txt", "name": "x.txt"}]
    assert out[-1]["files"] == [{"path": "/w/y.txt", "name": "y.txt"}]


def test_a_broken_list_never_costs_the_transcript():
    from vaf.core.web_server import _history_projection
    out = _history_projection(_session(["junk", {"no": "path"}, {"path": "/w/a", "turnId": 5}]), SID)
    assert out[-1]["content"] == "Done, here it is."

