# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Chat-attached documents are persisted to real files.

Web uploads carried the literal path "Hochgeladen über Web-UI (kein lokaler
Pfad)", so `learn_document(path=...)` - the exact advice the runner prints for
oversized attachments - was unfollowable for the very documents it was printed
about, and the learn button would have had nothing to point at. Pinned here:
the entry's `path` is a real file inside the user-siloed session attachments
dir, a failed persist keeps the honest literal (never a fake path), the learn
delegation prefers the persisted file over the preview content, and the
runner's advice line skips pathless documents instead of emitting an
uncallable tool call.
"""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def rig(tmp_path, monkeypatch):
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    monkeypatch.setattr("vaf.core.session.get_session_attachments_dir",
                        lambda session_id, user_scope_id=None, create=True: attach_dir)
    # The librarian preview is irrelevant here; keep it cheap and offline.
    monkeypatch.setattr("vaf.tools.librarian.LibrarianTool._read_file",
                        lambda self, path, chunking=True, first_page=None, last_page=None:
                        "### PDF: x\n**Pages:** 1-1 of 1\ncontent")
    return attach_dir


def _process(files, session="sess-1", scope="deadbeef-0000"):
    from vaf.core.web_server import process_files_to_sidebar_list
    return asyncio.run(process_files_to_sidebar_list(files, session_id=session,
                                                     user_scope_id=scope))


def test_uploaded_document_gets_a_real_path(rig):
    import base64
    payload = base64.b64encode(b"%PDF-fake-bytes").decode()
    out = _process([{"name": "report.pdf", "data": payload, "mimeType": "application/pdf"}])
    assert len(out) == 1
    p = Path(out[0]["path"])
    assert p.is_file(), f"no persisted file behind {out[0]['path']!r}"
    assert p.parent == rig
    assert p.read_bytes() == b"%PDF-fake-bytes"
    assert "Hochgeladen" not in out[0]["path"]


def test_failed_persist_keeps_the_honest_literal(rig, monkeypatch):
    import base64
    monkeypatch.setattr("vaf.core.session.get_session_attachments_dir",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    payload = base64.b64encode(b"%PDF-fake").decode()
    out = _process([{"name": "report.pdf", "data": payload, "mimeType": "application/pdf"}])
    assert out[0]["path"] == "Hochgeladen über Web-UI (kein lokaler Pfad)", \
        "a failed persist must fall back to the honest literal, never a fake path"


def test_no_session_keeps_the_literal(rig):
    import base64
    payload = base64.b64encode(b"%PDF-fake").decode()
    out = _process([{"name": "r.pdf", "data": payload, "mimeType": "application/pdf"}],
                   session=None, scope=None)
    assert out[0]["path"] == "Hochgeladen über Web-UI (kein lokaler Pfad)"


def test_learn_delegation_prefers_the_persisted_file(tmp_path, monkeypatch):
    """learn_attached_knowledge routes a persisted attachment through the SAME
    learn_document lane (batched job, resume, honest numbers) - the sidebar
    `content` is the librarian PREVIEW and structurally wrong for learning."""
    from vaf.tools.learn_attached_knowledge import LearnAttachedKnowledgeTool

    real = tmp_path / "stored.pdf"
    real.write_bytes(b"%PDF-real")

    class _Sess:
        runtime_state = {"sidebar_documents": [
            {"name": "stored.pdf", "path": str(real), "content": "preview text"},
        ]}

    monkeypatch.setattr("vaf.core.session.SessionManager.load",
                        lambda self, sid: _Sess())
    seen = {}

    def _fake_learn_run(self, **kw):
        seen.update(kw)
        return "[SUBAGENT_ASYNC:t1:learn_agent] learning"

    monkeypatch.setattr("vaf.tools.learn_document.LearnDocumentTool.run", _fake_learn_run)

    out = LearnAttachedKnowledgeTool().run(confirm_learn=True, session_id="s1",
                                           _agent=MagicMock())
    assert seen.get("path") == str(real), "delegation did not use the persisted file"
    assert "[SUBAGENT_ASYNC:t1:learn_agent]" in out


def test_runner_advice_skips_pathless_documents():
    """An unfollowable learn_document(path="Hochgeladen ...") helps nobody."""
    import inspect

    import vaf.core.headless_runner as hr

    src = inspect.getsource(hr)
    i = src.index("ATTACHMENT SIZE WARNING")
    window = src[i:i + 1200]
    assert 'startswith("Hochgeladen")' in window, \
        "the advice line emits the no-path literal as a path again"


def test_attachment_context_names_the_persisted_path():
    """The model spent three failed calls and a find_files discovering the
    attachment's path - the context header now says it, and only for REAL
    paths (the honest no-path literal is never dressed up as one)."""
    import inspect

    import vaf.core.headless_runner as hr

    src = inspect.getsource(hr)
    i = src.index("DOCUMENT CONTEXT ACTIVE")
    window = src[i - 2200:i + 1200]
    assert '(path: ' in window, "the attachment context lost the persisted path"
    assert 'startswith("Hochgeladen")' in window, \
        "the no-path literal would be presented as a real path"
    assert "first_page/last_page" in window, \
        "the context stopped telling the model how to read a page range"
