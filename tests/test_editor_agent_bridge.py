# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
from types import SimpleNamespace

from vaf.tools.document_viewer import ReplaceEditorSelectionTool, ReplaceEditorTextTool


class FakeSessionManager:
    def __init__(self, session):
        self._session = session

    def load(self, session_id):
        return self._session


class FakeWebInterface:
    def __init__(self):
        self.calls = []

    def emit_editor_apply_edit(self, session_id, selection_index, new_text, start=None, end=None):
        self.calls.append(
            {
                "session_id": session_id,
                "selection_index": selection_index,
                "new_text": new_text,
                "start": start,
                "end": end,
            }
        )


def test_replace_editor_selection_uses_runtime_selection_offsets(monkeypatch):
    session = SimpleNamespace(runtime_state={"editor_selections": [{"start": 10, "end": 19}]})
    fake_web = FakeWebInterface()

    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_session_id", lambda: "sess-1")
    monkeypatch.setattr("vaf.core.web_interface.get_web_interface", lambda: fake_web)
    monkeypatch.setattr("vaf.core.session.SessionManager", lambda: FakeSessionManager(session))

    tool = ReplaceEditorSelectionTool()
    result = tool.run(selection_index=0, new_text="new value")

    assert result == "The marked region in the Document Editor has been updated with the new text."
    assert fake_web.calls == [
        {
            "session_id": "sess-1",
            "selection_index": 0,
            "new_text": "new value",
            "start": 10,
            "end": 19,
        }
    ]


def test_replace_editor_text_replaces_first_matching_occurrence(monkeypatch):
    session = SimpleNamespace(
        runtime_state={
            "editor_document": {
                "name": "Draft",
                "content": "Intro paragraph.\nTarget sentence.\nClosing line.",
            }
        }
    )
    fake_web = FakeWebInterface()

    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_session_id", lambda: "sess-1")
    monkeypatch.setattr("vaf.core.web_interface.get_web_interface", lambda: fake_web)
    monkeypatch.setattr("vaf.core.session.SessionManager", lambda: FakeSessionManager(session))

    tool = ReplaceEditorTextTool()
    result = tool.run(old_text="Target sentence.", new_text="Rewritten sentence.")

    assert "has been updated" in result
    assert fake_web.calls == [
        {
            "session_id": "sess-1",
            "selection_index": -1,
            "new_text": "Rewritten sentence.",
            "start": 17,
            "end": 33,
        }
    ]


def test_replace_editor_text_uses_occurrence_index(monkeypatch):
    session = SimpleNamespace(
        runtime_state={
            "editor_document": {
                "name": "Draft",
                "content": "Clause A. Clause B. Clause A.",
            }
        }
    )
    fake_web = FakeWebInterface()

    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_session_id", lambda: "sess-1")
    monkeypatch.setattr("vaf.core.web_interface.get_web_interface", lambda: fake_web)
    monkeypatch.setattr("vaf.core.session.SessionManager", lambda: FakeSessionManager(session))

    tool = ReplaceEditorTextTool()
    tool.run(old_text="Clause A.", new_text="Clause X.", occurrence_index=1)

    assert fake_web.calls[0]["start"] == 20
    assert fake_web.calls[0]["end"] == 29


def test_replace_editor_text_errors_when_text_not_found(monkeypatch):
    session = SimpleNamespace(runtime_state={"editor_document": {"name": "Draft", "content": "Alpha Beta"}})

    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_session_id", lambda: "sess-1")
    monkeypatch.setattr("vaf.core.session.SessionManager", lambda: FakeSessionManager(session))
    monkeypatch.setattr("vaf.core.web_interface.get_web_interface", lambda: FakeWebInterface())

    tool = ReplaceEditorTextTool()
    result = tool.run(old_text="Gamma", new_text="Delta")

    assert result == "Error: old_text was not found in the current editor document."
