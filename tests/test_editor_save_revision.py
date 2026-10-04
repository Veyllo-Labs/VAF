# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An editor save never overwrites silently.

The measured gaps: no save route checked a revision, so the agent's edit of an open file -
or another tab's save - was overwritten by the next save with nothing said; the Excel,
PowerPoint and Word saves wrote back only what the editor shows, so formulas, numbers, rows
past 500, pictures and unsupported Word content were lost from the original; a Markdown file
was sniffed for HTML tags and rewritten when it merely contained a table; the native Word
editor took an EMPTY model when its load failed and kept Save enabled. Each test names the
mutation it catches.
"""
import asyncio
import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from tests.test_file_routes_account_jail import TENANT, _request, tree  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[1]


def _rev(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save(fn, body_cls, **body):
    return asyncio.run(fn(body_cls(**body), _request(TENANT, path="/api/file/save")))


def _conflict(fn, body_cls, **body):
    with pytest.raises(HTTPException) as refused:
        _save(fn, body_cls, **body)
    assert refused.value.status_code == 409
    return refused.value.detail


def test_the_file_route_names_the_revision(tree):
    from vaf.core import web_server as ws
    own = tree["own"]
    response = asyncio.run(ws.get_file(_request(TENANT), str(own)))
    assert response.headers["X-VAF-Revision"] == _rev(own)


def test_a_file_changed_since_it_was_opened_is_not_overwritten(tree):
    """MUTATION: drop the comparison in write_if_revision."""
    from vaf.core import web_server as ws
    own = tree["own"]
    opened = _rev(own)
    own.write_text("the agent's edit", encoding="utf-8")
    detail = _conflict(ws.save_file, ws.FileSaveRequest, path=str(own), content="mine",
                       base_revision=opened)
    assert detail["code"] == "conflict" and detail["current_revision"] == _rev(own)
    assert own.read_text(encoding="utf-8") == "the agent's edit"


def test_a_save_that_names_no_revision_does_not_overwrite_an_existing_file(tree):
    from vaf.core import web_server as ws
    detail = _conflict(ws.save_file, ws.FileSaveRequest, path=str(tree["own"]), content="mine")
    assert detail["code"] == "conflict"
    assert tree["own"].read_text(encoding="utf-8") == "x"


def test_the_right_revision_saves_and_answers_the_new_one(tree):
    from vaf.core import web_server as ws
    own = tree["own"]
    out = _save(ws.save_file, ws.FileSaveRequest, path=str(own), content="mine",
                base_revision=_rev(own))
    assert own.read_text(encoding="utf-8") == "mine" and out["revision"] == _rev(own)
    assert out["redirected"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_save_keeps_the_files_mode(tree):
    """A document is not a secret. MUTATION: write it with the stores' 0600 default."""
    from vaf.core import web_server as ws
    own = tree["own"]
    os.chmod(own, 0o640)
    _save(ws.save_file, ws.FileSaveRequest, path=str(own), content="mine", base_revision=_rev(own))
    assert (own.stat().st_mode & 0o777) == 0o640


def test_markdown_is_written_verbatim_unless_the_editor_says_it_sends_html(tree):
    """MUTATION: sniff the content for tags again."""
    from vaf.core import web_server as ws
    own = tree["own"]
    raw = "| a | b |\n|---|---|\n\n<table><tr><td>kept as written</td></tr></table>\n"
    _save(ws.save_file, ws.FileSaveRequest, path=str(own), content=raw, base_revision=_rev(own))
    assert own.read_text(encoding="utf-8") == raw
    _save(ws.save_file, ws.FileSaveRequest, path=str(own), content="<h1>Title</h1><p>Body</p>",
          base_revision=_rev(own), format="html")
    assert own.read_text(encoding="utf-8").startswith("# Title")


def _workbook_with_formula(path: Path) -> None:
    import openpyxl
    wb = openpyxl.Workbook()
    ws_ = wb.active
    ws_.title = "Sheet1"
    ws_["A1"], ws_["A2"], ws_["A3"] = "1", "2", "=A1+A2"
    wb.save(path)


def test_a_lossy_workbook_is_saved_to_one_copy_and_then_in_place(tree):
    """MUTATION: write a lossy office file in place (drop the loss check)."""
    pytest.importorskip("openpyxl")
    from vaf.core import web_server as ws
    original = tree["own"].with_name("budget.xlsx")
    _workbook_with_formula(original)
    before = original.read_bytes()

    loaded = asyncio.run(ws.get_file_as_html(_request(TENANT), str(original)))
    assert "formulas" in loaded.headers["X-VAF-Loss"]
    html = "<table><tr><td>1</td></tr><tr><td>2</td></tr><tr><td>3</td></tr></table>"
    first = _save(ws.save_file_as_xlsx, ws.FileSaveRequest, path=str(original), content=html,
                  base_revision=loaded.headers["X-VAF-Revision"])
    copy = original.with_name("budget (bearbeitet).xlsx")
    assert first["redirected"] is True and Path(first["path"]) == copy
    assert original.read_bytes() == before, "the original was overwritten"

    # The copy is the editor's own: nothing to lose, saved in place, no second copy.
    assert ws._office_loss_report(copy) == []
    second = _save(ws.save_file_as_xlsx, ws.FileSaveRequest, path=str(copy), content=html,
                   base_revision=first["revision"])
    assert second["redirected"] is False and Path(second["path"]) == copy
    assert sorted(p.name for p in original.parent.glob("budget*")) == [
        "budget (bearbeitet).xlsx", "budget.xlsx"]


def test_the_original_is_not_saved_over_an_existing_copy(tree):
    """A second session on the original: the copy exists, and it is opened, not replaced.
    MUTATION: write the copy whether or not it exists."""
    pytest.importorskip("openpyxl")
    from vaf.core import web_server as ws
    original = tree["own"].with_name("budget.xlsx")
    _workbook_with_formula(original)
    copy = original.with_name("budget (bearbeitet).xlsx")
    copy.write_bytes(b"earlier edits")
    loaded = asyncio.run(ws.get_file_as_html(_request(TENANT), str(original)))
    from urllib.parse import unquote
    assert Path(unquote(loaded.headers["X-VAF-Edit-Copy"])) == copy
    detail = _conflict(ws.save_file_as_xlsx, ws.FileSaveRequest, path=str(original),
                       content="<table><tr><td>x</td></tr></table>",
                       base_revision=loaded.headers["X-VAF-Revision"])
    assert detail["code"] == "copy_exists" and Path(detail["path"]) == copy
    assert copy.read_bytes() == b"earlier edits"


def test_a_word_document_with_content_the_editor_cannot_hold_goes_to_a_copy(tree):
    pytest.importorskip("docx")
    from docx import Document

    from vaf.core import web_server as ws
    original = tree["own"].with_name("letter.docx")
    plain = io.BytesIO()
    doc = Document()
    doc.add_paragraph("Dear Alice")
    doc.save(plain)
    # The same document plus a comments part: the native model has no place for comments.
    with zipfile.ZipFile(io.BytesIO(plain.getvalue())) as src, zipfile.ZipFile(original, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("word/comments.xml", "<w:comments xmlns:w='x'/>")
    before = original.read_bytes()
    loaded = asyncio.run(ws.get_file_as_docx_model(_request(TENANT), str(original)))
    assert "comments" in loaded.headers["X-VAF-Loss"]
    out = _save(ws.save_file_as_docx_native, ws.FileSaveDocxNativeRequest, path=str(original),
                document={"title": "letter", "sections": []},
                base_revision=loaded.headers["X-VAF-Revision"])
    assert out["redirected"] is True and Path(out["path"]).name == "letter (bearbeitet).docx"
    assert original.read_bytes() == before


def test_the_dead_html_to_docx_route_is_gone():
    from vaf.core import web_server as ws
    assert not any(getattr(r, "path", "") == "/api/file/save-docx" for r in ws.app.routes)


# ── the editors ──────────────────────────────────────────────────────────────

def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_a_failed_word_load_never_becomes_an_empty_document():
    """MUTATION: adopt createEmptyNativeDocx in the load's catch again, or enable Save."""
    src = _src("web/components/NativeDocxEditor.tsx")
    load = src[src.index("const loadModel = useCallback"):src.index("useEffect(() => {\n    if (documentModel || !filePath) return;")]
    assert "createEmptyNativeDocx" not in load and "setLoadFailed(true)" in load
    assert "disabled={isSaving || loadFailed || !documentModel}" in src


def test_an_agent_rewrite_keeps_a_dirty_draft():
    """MUTATION: reset the editor state on document_ready whatever it holds."""
    src = _src("web/app/page.tsx")
    ready = src[src.index("data.type === 'document_ready'"):src.index("data.type === 'editor_apply_edit'")]
    assert "prev.isOpen && prev.filePath === fp && prev.dirty" in ready
    assert "externalChange: true" in ready


def test_every_editor_save_names_its_revision():
    for rel in ("web/components/DocumentEditor.tsx", "web/components/NativeDocxEditor.tsx",
                "web/components/CodeViewer.tsx"):
        src = _src(rel)
        assert "saveEditorFile(" in src and "base_revision:" in src, rel


def test_the_code_viewer_follows_a_save_into_the_copy():
    """After "keep mine as a copy" the viewer showed the original and called the edits saved:
    the next save went against the original's revision. MUTATION: drop the retarget."""
    viewer = _src("web/components/CodeViewer.tsx")
    redirect = viewer[viewer.index("if (outcome.redirected) {"):viewer.index("revisionRef.current = outcome.revision;")]
    assert "onRetarget(outcome.path)" in redirect and "setIsDirty(false)" not in redirect
    assert "onRetarget={(path) => setCodeViewerState(" in _src("web/app/page.tsx")
