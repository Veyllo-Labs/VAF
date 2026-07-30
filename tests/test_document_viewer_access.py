# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What the document viewer is allowed to put into a session.

`document_viewer` opens a file in the Web UI's side panel. It reads the file TWICE: once
through `LibrarianTool._read_file`, which extracts text and understands PDF/Word/Excel, and
once raw, to carry the original bytes to the browser as base64.

Only the first read was checked, and the check was not honoured. `_read_file` answers a
blocked path by RETURNING the string "[ERROR] Access denied: ...", so a refusal was never a
control-flow event: the function carried on, opened the same path raw, and put the bytes into
`new_doc["data"]`, into `session.runtime_state["sidebar_documents"]`, and from there into the
browser. The panel then showed the refusal in `content` while `data` held the real file - and
the tool told the caller the document had been opened.

That defeated the STATIC blocks as well (`.ssh`, `.env`, `id_rsa`, `~/.vaf`), which exist for
everyone including the machine owner, so this was not only a tenant-isolation gap.

Measured before and after on the running code, not read off the source: with the old version
the probe found one document in the session carrying the plaintext; with the fix it finds
none and gets a refusal back.

A NOTE ON MEASURING IT, because the first attempt here proved nothing and looked like a
clearance: a stand-in session whose `runtime_state` is an EMPTY dict is falsy, so the tool
replaces it with a fresh dict on the instance - and a probe reading the class attribute then
finds nothing and reports "no leak". The fixture below hands over a session whose
`runtime_state` is already truthy, which is the only shape that observes what is written.
"""
import base64
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from vaf.tools.document_viewer import DocumentViewerTool

SCOPE = "deadbeef-0000-0000-0000-000000000000"       # synthetic; never a real scope UUID
OTHER = "cafe1234-0000-0000-0000-000000000000"       # synthetic
SECRET = "SECRET-MATERIAL-DO-NOT-LEAK"


@pytest.fixture
def session():
    """A session that OBSERVES what is written to it (see the note in the module docstring)."""
    s = MagicMock()
    s.runtime_state = {"sidebar_documents": []}
    return s


def _open(path, session, **kwargs):
    """Drive the tool the way the chat lane does, and report what reached the session."""
    with patch("vaf.core.subagent_ipc.get_current_session_id", return_value="probe-session"), \
         patch("vaf.core.session.SessionManager") as mgr, \
         patch("vaf.core.web_interface.get_web_interface"):
        mgr.return_value.load.return_value = session
        result = DocumentViewerTool().run(path=str(path), **kwargs)

    docs = [d for d in (session.runtime_state.get("sidebar_documents") or [])
            if isinstance(d, dict)]
    blob = "".join(str(d.get("data", "")) for d in docs)
    try:
        raw = base64.b64decode(blob).decode(errors="ignore") if blob else ""
    except Exception:
        raw = ""
    return result, docs, raw


@pytest.fixture
def blocked_file(tmp_path, monkeypatch):
    """A path under a statically blocked directory, without touching a real one."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    secret = home / ".ssh" / "id_rsa"
    secret.write_text(SECRET)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: home))
    return secret


# ── the leak ─────────────────────────────────────────────────────────────────

def test_a_blocked_file_never_reaches_the_session(blocked_file, session):
    """THE regression. The refusal has to stop the function, not decorate its output."""
    result, docs, raw = _open(blocked_file, session)
    assert SECRET not in raw, "the file's bytes reached the session despite the refusal"
    assert docs == [], "a document was added to the panel for a blocked path"
    assert "[ERROR]" in result or "denied" in result.lower()


def test_a_blocked_file_is_not_reported_as_opened(blocked_file, session):
    """The old version answered "Document ... has been opened", which is worse than leaking
    quietly: the caller is told the action succeeded."""
    result, _, _ = _open(blocked_file, session)
    assert "has been opened" not in result


def test_the_refusal_does_not_reveal_whether_the_file_exists(tmp_path, monkeypatch, session):
    """Existence is probed only after the path is allowed. Answering "File not found" for one
    blocked path and something else for another turns the tool into an existence oracle for
    directories the caller may not read."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "real").write_text(SECRET)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: home))

    existing, _, _ = _open(home / ".ssh" / "real", session)
    missing, _, _ = _open(home / ".ssh" / "absent", session)
    assert "not found" not in existing.lower()
    assert "not found" not in missing.lower()
    assert existing.split(":")[0] == missing.split(":")[0], (
        f"the two answers differ and leak existence: {existing!r} vs {missing!r}"
    )


# ── the per-user jail, which this tool never had ─────────────────────────────

def test_another_tenants_file_is_refused(tmp_path, monkeypatch, session):
    """The tool declares `user_scope_id`, so the dispatcher hands it an identity - it just
    never turned that into a boundary. `is_safe_path` answers the jail as well as the static
    blocks, so asking it once inside the jail covers both."""
    home = tmp_path / "home"
    foreign = home / "Documents" / "VAF_Projects" / "ffffffff" / "notes.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_text(SECRET)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: home))

    with patch("vaf.tools.filesystem._visible_skill_roots", return_value=[]):
        result, docs, raw = _open(foreign, session, user_scope_id=SCOPE)
    assert SECRET not in raw
    assert docs == []
    assert "denied" in result.lower() or "[ERROR]" in result


def test_without_an_identity_the_static_blocks_still_apply(blocked_file, session):
    """A direct consumer passes no scope, which means no jail - but never no rules. The
    static blocks exist for the machine owner too."""
    result, docs, raw = _open(blocked_file, session)
    assert SECRET not in raw and docs == []


# ── the control: an allowed file must still open ─────────────────────────────

def test_an_ordinary_file_still_opens(tmp_path, monkeypatch, session):
    """Without this the assertions above would also pass if the tool refused everything."""
    home = tmp_path / "home"
    doc = home / "Documents" / "report.txt"
    doc.parent.mkdir(parents=True)
    doc.write_text("hello from a perfectly ordinary file")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: home))

    result, docs, raw = _open(doc, session)
    assert len(docs) == 1, f"an allowed file did not open: {result!r}"
    assert "hello from a perfectly ordinary file" in raw


# ── the structure that keeps the guard un-steppable ──────────────────────────

def test_the_raw_read_happens_after_the_decision():
    """The bug was structural: the check and the raw read lived in one function with the
    refusal in between as a mere string. The read now sits in a method the guard has to
    return into, so a later branch cannot land past it by accident."""
    import inspect

    src = inspect.getsource(DocumentViewerTool.run)
    assert "is_safe_path" in src, "the access decision left run()"
    assert 'open(' not in src, "run() reads a file again; the decision must come first"
    assert "_open_in_viewer" in src


def test_the_downstream_re_ask_runs_under_the_same_jail(tmp_path, monkeypatch, session):
    """Why the `with` block here wraps the whole body, unlike the one in `learn_document`.

    `_open_in_viewer` does not simply read the allowed path - it goes through
    `LibrarianTool._read_file`, which asks `is_safe_path` AGAIN. Two askers, one path: if the
    jail were only installed for the first, the second would answer without it, which is the
    permissive direction. Holding it across the body is what keeps them in agreement.

    `learn_document` is narrow for the opposite, equally measured reason: nothing downstream
    re-asks there, and its ingestion runs in a separate thread the contextvar cannot reach.
    """
    from vaf.tools.filesystem import _librarian_scope_ctx, compute_user_jail
    from vaf.tools.librarian import LibrarianTool

    home = tmp_path / "home"
    # Inside this scope's OWN tree: a path outside it is refused by the guard, and then the
    # downstream reader is never reached - which would make this test silently measure nothing.
    uid8 = compute_user_jail(SCOPE, "user")["uid8"]
    doc = home / "Documents" / "VAF_Projects" / uid8 / "readable.txt"
    doc.parent.mkdir(parents=True)
    doc.write_text("content")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: home))

    seen = []
    original = LibrarianTool._read_file

    def _record(self, path, **kw):
        seen.append(_librarian_scope_ctx.get(None))
        return original(self, path, **kw)

    monkeypatch.setattr(LibrarianTool, "_read_file", _record)
    _open(doc, session, user_scope_id=SCOPE)

    assert seen, "the downstream reader was never reached"
    assert seen[0], (
        "LibrarianTool._read_file re-asks is_safe_path, and it ran with no jail installed - so "
        "it answers a different question than the guard that let the path through"
    )


def test_the_decision_is_asked_inside_the_jail():
    """`is_safe_path` answers the static blocks AND the per-user jail, but only if the jail
    is installed when it is asked - the jail is a contextvar, not an argument."""
    import inspect

    src = inspect.getsource(DocumentViewerTool.run)
    jail = src.index("user_jail(")
    check = src.index("is_safe_path(")
    assert jail < check, "is_safe_path is asked before the jail is entered; it answers unjailed"


# ── what the split nearly broke, silently ────────────────────────────────────

def test_the_scope_still_reaches_attachment_indexing(tmp_path, monkeypatch, session):
    """Splitting run() in two left this call reading a `kwargs` that no longer existed in
    its scope. It sits inside `except Exception: pass`, so the NameError would have been
    swallowed and attachment indexing would have stopped SILENTLY - the worst shape of
    breakage, because nothing reports it. Ruff's undefined-name gate caught it and none of
    the tests above did, so this one exists to close that.
    """
    from vaf.tools.filesystem import compute_user_jail

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: home))

    # INSIDE this scope's own tree. A file under ~/Documents would be refused here, which is
    # correct and is what the jail is for - but it would also mean this test never reached
    # the indexing call it exists to check.
    uid8 = compute_user_jail(SCOPE, "user")["uid8"]
    doc = home / "Documents" / "VAF_Projects" / uid8 / "note.txt"
    doc.parent.mkdir(parents=True)
    doc.write_text("indexable content")

    indexed = {}

    def _record(**kw):
        indexed.update(kw)

    with patch("vaf.core.config.Config.get",
               side_effect=lambda k, d=None: True if k == "attachment_rag_enabled" else d), \
         patch("vaf.memory.attachment_rag.index_session_attachments_sync", _record):
        _open(doc, session, user_scope_id=SCOPE)

    assert indexed, "attachment indexing was never reached - it fails silently when it breaks"
    assert indexed.get("user_scope_id") == SCOPE, (
        "indexing ran without the caller's scope, so what it learns is filed under nobody"
    )
