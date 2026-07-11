# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Session-derived paths must key on the session, never process-global state.

Live incident (green778499, 2026-07-11): with parallel main workers, the fresh
chat's system prompt advertised ANOTHER chat's folder as "this chat's workspace"
because the prompt builder resolved it via the process-global session pointer -
the model dutifully saved the deliverable into the foreign chat's workspace.
Same class: document_writer resolved its output dir via the global, and the
session-workspace anchor (project_path) was only ever set by the SUBPROCESS
notify path, so in-process writes never armed the [SESSION WORKSPACE] note.
"""
import re
import types
from pathlib import Path

import pytest

import vaf.core.platform as platform_mod
import vaf.core.session as session_mod
from vaf.core.session import record_created_file


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


class _FakeSession:
    def __init__(self):
        self.runtime_state = None
        self.project_path = ""


class _FakeMgr:
    def __init__(self):
        self.session = _FakeSession()
        self.saved = 0

    def load(self, sid):
        return self.session

    def save(self, sess, sync_state=False):
        self.saved += 1


# ── record_created_file (shared workspace anchor) ────────────────────────────

def test_record_anchors_project_path_once(fake_home, monkeypatch):
    mgr = _FakeMgr()
    monkeypatch.setattr(session_mod, "get_manager", lambda: mgr)
    ws = fake_home / "Documents" / "VAF_Projects" / "u1" / "chat1"
    ws.mkdir(parents=True)
    record_created_file("chat1", ws / "a.png")
    assert mgr.session.runtime_state["last_project_path"] == str(ws)
    assert mgr.session.project_path == str(ws)
    # second file in a SUB-project updates last_project_path but never the anchor
    sub = ws / "Workflow"
    sub.mkdir()
    record_created_file("chat1", sub / "b.png")
    assert mgr.session.runtime_state["last_project_path"] == str(sub)
    assert mgr.session.project_path == str(ws), "project_path is set once, never overwritten"
    assert mgr.saved == 2


def test_record_ignores_unsafe_dirs(fake_home, monkeypatch):
    mgr = _FakeMgr()
    monkeypatch.setattr(session_mod, "get_manager", lambda: mgr)
    record_created_file("chat1", fake_home / "stray.txt")  # home root = unsafe
    assert mgr.session.project_path == ""
    assert mgr.saved == 0


def test_record_non_vaf_projects_paths_do_not_anchor(fake_home, monkeypatch):
    mgr = _FakeMgr()
    monkeypatch.setattr(session_mod, "get_manager", lambda: mgr)
    other = fake_home / "Documents" / "VAF_Documents" / "chat1"
    other.mkdir(parents=True)
    record_created_file("chat1", other / "doc.txt")
    assert mgr.session.runtime_state["last_project_path"] == str(other)
    assert mgr.session.project_path == "", "only VAF_Projects paths anchor the workspace"


def test_record_never_raises(monkeypatch):
    monkeypatch.setattr(session_mod, "get_manager",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    record_created_file("chat1", "/x/y.txt")  # must not raise
    record_created_file(None, None)


# ── notify_file_created wires the in-process anchor ──────────────────────────

def test_in_process_notify_calls_shared_setter(monkeypatch):
    import vaf.core.web_interface as wi_mod
    calls = []
    monkeypatch.setattr(session_mod, "record_created_file",
                        lambda sid, fp: calls.append((sid, str(fp))))
    fake_wi = types.SimpleNamespace(
        _server_loop=object(),
        _push_session_update=lambda sid, payload: None,
    )
    monkeypatch.setattr(wi_mod, "get_web_interface", lambda: fake_wi)
    wi_mod.notify_file_created("chatX", "/tmp/somefile.txt")
    assert calls and calls[0][0] == "chatX"


# ── source guards: no process-global session resolution in prompt content ────

def test_prompt_builder_never_uses_global_workspace_fallback():
    src = Path(session_mod.__file__).parent.joinpath("system_prompt.py").read_text(encoding="utf-8")
    assert not re.search(r"get_session_workspace_dir\(\s*create=", src), (
        "system_prompt.py resolves the workspace without a session id - that falls "
        "back to the PROCESS-GLOBAL session pointer and leaks another chat's "
        "workspace into this chat's prompt under parallel workers"
    )
    assert not re.search(r"get_session_workspace_dir\(\s*get_current_session_id\(\)", src), (
        "system_prompt.py resolves the workspace via the process-global session "
        "pointer - must use the session_id passed into build_prompt"
    )


def test_agent_passes_session_id_to_build_prompt():
    import vaf.core.agent as agent_mod
    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    starts = [m.end() for m in re.finditer(r"prompt_manager\.build_prompt\(", src)]
    assert starts, "build_prompt call sites not found"
    for pos in starts:
        window = src[pos:pos + 700]
        assert "session_id=" in window, (
            f"build_prompt call without explicit session_id near: {window[:120]}"
        )


def test_headless_runner_derives_workspace_from_task_session():
    import vaf.core.headless_runner as hr_mod
    src = Path(hr_mod.__file__).read_text(encoding="utf-8")
    assert "_gswd(task.session_id" in src, (
        "headless_runner lost the deterministic workspace fallback keyed on the "
        "task's own session"
    )


# ── document_writer resolves its output dir with the injected session ────────

def test_document_writer_passes_injected_session(monkeypatch, tmp_path):
    import vaf.tools.document_writer as dw_mod
    captured = {}

    def fake_resolve(default, session_id=None):
        captured["session_id"] = session_id
        return tmp_path

    monkeypatch.setattr(session_mod, "resolve_agent_output_dir", fake_resolve)
    from vaf.tools.document_writer import DocumentWriterTool
    DocumentWriterTool().run(document_type="letter", content="x",
                             filename="a.txt", _session_id="chat42")
    assert captured.get("session_id") == "chat42"
