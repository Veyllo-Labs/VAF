# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The document preview waits its turn for Gotenberg in the `gotenberg` fair queue.

Gotenberg converts one document at a time (one LibreOffice). Conversions now take turns
by person, a viewer that waits longer than half a second hears its place
(`document_preview_wait`), and a conversion the queue refuses shows the document in the
simpler layout at once (`document_preview_busy`) instead of starting the host's own
LibreOffice next to it. Only the converter URL is faked; any other POST goes to the
real `requests.post` untouched (other tests' heartbeat threads post too).
"""
import asyncio
import base64
import contextlib
import io
import json
import re
import threading
import time
from pathlib import Path

import pytest

import vaf.core.fair_queue as fair_queue
from vaf.core.config import Config

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "web" / "app" / "page.tsx"
VIEWER = ROOT / "web" / "components" / "DocumentViewer.tsx"
URL = "http://gotenberg.invalid:3000"
CONVERT = URL + "/forms/libreoffice/convert"
SCOPE = "ab12cd34-0000-4000-8000-000000000001"
OTHER = "ab12cd34-0000-4000-8000-000000000002"
SID = "web-chat-0031"


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    monkeypatch.setattr(fair_queue, "_registry", {})


class _Converter:
    """A fake Gotenberg at CONVERT only: counts concurrency and answers a PDF (or the
    status set in `status`)."""

    def __init__(self, monkeypatch, *, delay=0.1, status=200):
        import requests

        self.delay, self.status = delay, status
        self.now = self.peak = self.calls = 0
        self.lock = threading.Lock()
        real_post = requests.post

        def post(url, *args, **kwargs):
            if url != CONVERT:
                return real_post(url, *args, **kwargs)
            with self.lock:
                self.calls += 1
                self.now += 1
                self.peak = max(self.peak, self.now)
            try:
                time.sleep(self.delay)
            finally:
                with self.lock:
                    self.now -= 1
            ok = self.status == 200
            return type("R", (), {"status_code": self.status, "content": b"%PDF-1.4 fake",
                                  "headers": {"content-type": "application/pdf" if ok
                                              else "text/plain"}})()

        monkeypatch.setattr(requests, "post", post)


def _config(monkeypatch, url=URL):
    real_get = Config.get.__func__

    def get(cls, key, default=None):
        if key == "document_conversion_docker_url":
            return url
        if key == "attachment_rag_enabled":
            return False
        return real_get(cls, key, default)

    monkeypatch.setattr(Config, "get", classmethod(get))


@contextlib.contextmanager
def _held(key=OTHER):
    """Another person's conversion holds the preview's one slot until the block ends."""
    from vaf.core import web_server as ws

    holding, let_go = threading.Event(), threading.Event()

    def hold():
        with ws._gotenberg_queue().slot(key):
            holding.set()
            let_go.wait(10)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert holding.wait(5)
    try:
        yield let_go
    finally:
        let_go.set()
        t.join(5)


def _docx_bytes():
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_paragraph("Quarterly numbers in a real document.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.fixture
def upload(tmp_path, monkeypatch):
    """process_files_to_sidebar_list with the attachment folder in tmp_path, a cheap
    librarian, and the host LibreOffice replaced by a spy."""
    from vaf.core import web_server as ws

    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    monkeypatch.setattr("vaf.core.session.get_session_attachments_dir",
                        lambda session_id, user_scope_id=None, create=True: attach_dir)
    monkeypatch.setattr("vaf.tools.librarian.LibrarianTool._read_file",
                        lambda self, path, chunking=True, first_page=None, last_page=None:
                        "extracted text of the document")
    host = []
    monkeypatch.setattr(ws, "_docx_to_pdf_via_libreoffice",
                        lambda path: host.append(path) or None)
    _config(monkeypatch)
    events = []

    async def notify(payload):
        events.append(dict(payload))

    def run(name, data, mime, *, with_notify=True, scope=SCOPE):
        files = [{"name": name, "data": base64.b64encode(data).decode(), "mimeType": mime}]
        return asyncio.run(ws.process_files_to_sidebar_list(
            files, session_id=SID, user_scope_id=scope,
            notify=notify if with_notify else None))

    return {"run": run, "host": host, "events": events}


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ── one at a time ─────────────────────────────────────────────────────────────

def test_one_preview_at_a_time(monkeypatch, tmp_path):
    """Three conversions at once reach the converter one after another.
    MUTATION: remove the slot around the POST - red (peak 3)."""
    from vaf.core import web_server as ws

    _config(monkeypatch)
    fake = _Converter(monkeypatch, delay=0.15)
    doc = tmp_path / "a.docx"
    doc.write_bytes(b"x")
    results = []
    threads = [threading.Thread(
        target=lambda k=k: results.append(ws._office_to_pdf_via_gotenberg(
            str(doc), "a.docx", user_key=k)), daemon=True)
        for k in ("scope-a", "scope-b", "scope-c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert fake.calls == 3 and fake.peak == 1, (fake.calls, fake.peak)
    assert results == [b"%PDF-1.4 fake"] * 3
    assert ws._gotenberg_queue().metrics()["served"] == 3


def test_no_url_takes_no_slot(monkeypatch, tmp_path):
    """Without a converter configured nothing waits: the answer is None at once, even
    while somebody holds the slot. MUTATION: take the slot before the URL check - red."""
    from vaf.core import web_server as ws

    _config(monkeypatch, url="")
    monkeypatch.setattr(ws, "GOTENBERG_WAIT_S", 0.3)
    doc = tmp_path / "a.docx"
    doc.write_bytes(b"x")
    with _held():
        started = time.monotonic()
        assert ws._office_to_pdf_via_gotenberg(str(doc), "a.docx", user_key=SCOPE) is None
        assert time.monotonic() - started < 0.2
    assert ws._gotenberg_queue().metrics()["refused"]["timeout"] == 0


# ── busy keeps the upload ─────────────────────────────────────────────────────

def test_busy_keeps_the_upload_and_skips_the_host_converter(upload, monkeypatch):
    """A DOCX whose preview cannot get the converter in time keeps its text, its own
    bytes and the simple HTML layout; the host LibreOffice is not started next to the
    busy one. MUTATIONS: fall through to the host converter on busy (red); drop the
    entry on busy (red)."""
    from vaf.core import web_server as ws

    monkeypatch.setattr(ws, "GOTENBERG_WAIT_S", 0.3)
    _Converter(monkeypatch)
    data = _docx_bytes()
    with _held():
        out = upload["run"]("report.docx", data, DOCX_MIME)
    assert len(out) == 1, out
    entry = out[0]
    assert entry["name"] == "report.docx"
    assert entry["content"].startswith("extracted text of the document")
    assert entry["data"] == base64.b64encode(data).decode()
    assert entry["mimeType"] == DOCX_MIME
    assert "Quarterly numbers" in entry["htmlContent"]
    assert upload["host"] == [], "the host LibreOffice ran next to the busy converter"
    busy = [e for e in upload["events"] if e["type"] == "document_preview_busy"]
    assert busy == [{"type": "document_preview_busy", "name": "report.docx",
                     "sessionId": SID, "reason": "timeout"}], upload["events"]


def test_busy_odt_shows_its_text(upload, monkeypatch):
    """An ODT has no HTML converter of its own: on busy it carries no htmlContent, so
    the viewer shows its extracted text, and never the "not reachable" literal, which
    would not be true. MUTATION: keep the German literal on busy - red."""
    from vaf.core import web_server as ws

    monkeypatch.setattr(ws, "GOTENBERG_WAIT_S", 0.3)
    _Converter(monkeypatch)
    with _held():
        out = upload["run"]("notes.odt", b"PK\x03\x04 not really an odt",
                            "application/vnd.oasis.opendocument.text")
    entry = out[0]
    assert "htmlContent" not in entry, entry.get("htmlContent")
    assert entry["content"].startswith("extracted text of the document")
    assert entry["mimeType"] == "application/vnd.oasis.opendocument.text"


def test_no_place_is_shown_after_busy(tmp_path, monkeypatch):
    """A place held back in its grace is never shown once the queue refused that file,
    also while the next file waits (each file asks its own outcome). Two DOCX behind a
    held slot, each refused at 0.3 s, inside the 0.5 s grace; the loop runs on past
    every timer. MUTATIONS: no still_waiting (red); still_waiting reading one flag shared
    by the files of the loop (red)."""
    from vaf.core import web_server as ws

    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    monkeypatch.setattr("vaf.core.session.get_session_attachments_dir",
                        lambda session_id, user_scope_id=None, create=True: attach_dir)
    monkeypatch.setattr("vaf.tools.librarian.LibrarianTool._read_file",
                        lambda self, path, chunking=True, first_page=None, last_page=None:
                        "extracted text of the document")
    monkeypatch.setattr(ws, "_docx_to_pdf_via_libreoffice", lambda path: None)
    monkeypatch.setattr(ws, "GOTENBERG_WAIT_S", 0.3)
    monkeypatch.setattr(ws, "PREVIEW_WAIT_GRACE_S", 0.5)
    _config(monkeypatch)
    _Converter(monkeypatch)
    events = []

    async def notify(payload):
        events.append(dict(payload))

    data = base64.b64encode(_docx_bytes()).decode()
    files = [{"name": f"{n}.docx", "data": data, "mimeType": DOCX_MIME} for n in ("one", "two")]

    async def main():
        out = await ws.process_files_to_sidebar_list(files, session_id=SID,
                                                     user_scope_id=SCOPE, notify=notify)
        await asyncio.sleep(1.0)        # every held-back timer has fired by now
        return out

    with _held():
        out = asyncio.run(main())
    assert len(out) == 2
    kinds = [(e["type"], e["name"]) for e in events]
    assert kinds == [("document_preview_busy", "one.docx"),
                     ("document_preview_busy", "two.docx")], kinds


def test_a_failure_still_tries_the_host_converter(upload, monkeypatch):
    """A converter that answers an error is not a busy queue: the host LibreOffice is
    still tried, as before."""
    _Converter(monkeypatch, status=500)
    out = upload["run"]("report.docx", _docx_bytes(), DOCX_MIME)
    assert len(upload["host"]) == 1
    assert "htmlContent" in out[0]
    assert not [e for e in upload["events"] if e["type"] == "document_preview_busy"]


# ── the event over the real socket ────────────────────────────────────────────

def _wait_until(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@contextlib.contextmanager
def _bob(monkeypatch, tmp_path):
    """The real WebSocket handler, signed in as Bob, with his chat on disk."""
    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    import vaf.auth.permissions as permissions
    import vaf.core.session as session_mod
    import vaf.core.web_server as real_server

    sessions_dir = tmp_path / "ws-sessions"
    sessions_dir.mkdir()
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    monkeypatch.setattr(session_mod, "default_sessions_dir", lambda: sessions_dir)
    monkeypatch.setattr(session_mod, "get_session_attachments_dir",
                        lambda session_id, user_scope_id=None, create=True: attach_dir)
    monkeypatch.setattr("vaf.tools.librarian.LibrarianTool._read_file",
                        lambda self, path, chunking=True, first_page=None, last_page=None:
                        "extracted text of the document")
    mgr = session_mod.SessionManager(storage_dir=str(sessions_dir))
    monkeypatch.setattr(real_server, "session_mgr", mgr)
    s = mgr.new(name="Bobs Chat", user_scope_id=SCOPE)
    s.id = SID
    mgr.save(s)

    async def _unknown(scope):
        return ("unknown", "")
    monkeypatch.setattr(permissions, "account_standing_async", _unknown)
    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    _config(monkeypatch)
    token = jwt.encode({"sub": "2", "user_scope_id": SCOPE, "username": "bob", "role": "user"},
                       "s" * 32, algorithm="HS256")
    with TestClient(real_server.app, client=("127.0.0.1", 40001)).websocket_connect(
            f"/ws?token={token}") as ws:
        yield ws, real_server


def _until(ws, kind, timeout=15.0):
    """Frames up to and including the first of `kind`, read in a thread so a missing
    frame is a red test and not a hang."""
    seen = []

    def read():
        try:
            while True:
                msg = ws.receive_json()
                seen.append(msg)
                if msg.get("type") == kind:
                    return
        except Exception:
            return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    assert seen and seen[-1].get("type") == kind, f"no {kind}: {[m.get('type') for m in seen]}"
    return seen


def _upload_frame():
    data = base64.b64encode(_docx_bytes()).decode()
    return json.dumps({"type": "set_sidebar_documents", "sessionId": SID,
                       "documents": [{"name": "report.docx", "data": data,
                                      "mimeType": DOCX_MIME}]})


def test_the_upload_hears_its_place(monkeypatch, tmp_path):
    """Waiting longer than the grace, the uploading socket hears the place, then the
    grant, then the documents. MUTATION: no on_wait at the conversion - red."""
    _Converter(monkeypatch, delay=0.05)
    with _held() as let_go, _bob(monkeypatch, tmp_path) as (ws, _server):
        sent_at = time.monotonic()
        ws.send_text(_upload_frame())
        first = _until(ws, "document_preview_wait")[-1]
        assert time.monotonic() - sent_at >= 0.4, "the place came before the grace"
        assert first["sessionId"] == SID and first["name"] == "report.docx"
        assert first["position"] >= 1 and isinstance(first["waited_ms"], int)
        assert "eta_ms" in first
        let_go.set()
        rest = _until(ws, "sidebar_documents_set")
    waits = [m for m in rest if m.get("type") == "document_preview_wait"]
    assert waits and waits[-1]["position"] == 0, [m.get("type") for m in rest]
    doc = rest[-1]["contents"][0]
    assert doc["mimeType"] == "application/pdf"


def test_a_quick_preview_shows_no_place(monkeypatch, tmp_path):
    """A conversion granted within the grace never flashes a place.
    MUTATION: grace_s=0 at the conversion - red."""
    from vaf.core import web_server as ws_mod

    _Converter(monkeypatch, delay=0.05)
    queued = []

    def let_go_soon(let_go):
        # Granted 0.15 s after the conversion queued, well inside the 0.5 s grace.
        queued.append(_wait_until(lambda: ws_mod._gotenberg_queue().metrics()["waiting"] == 1))
        time.sleep(0.15)
        let_go.set()

    with _held() as let_go, _bob(monkeypatch, tmp_path) as (ws, _server):
        threading.Thread(target=let_go_soon, args=(let_go,), daemon=True).start()
        ws.send_text(_upload_frame())
        frames = _until(ws, "sidebar_documents_set")
    assert queued == [True], "the conversion never waited: the test proves nothing"
    assert not [m for m in frames if m.get("type") == "document_preview_wait"], frames


def test_busy_is_announced_before_the_documents(monkeypatch, tmp_path):
    """A refused preview says so before the documents arrive, so the viewer can tell
    why it shows the simpler layout. MUTATIONS: no document_preview_busy (red); send it
    after sidebar_documents_set (red)."""
    from vaf.core import web_server as ws_mod

    monkeypatch.setattr(ws_mod, "GOTENBERG_WAIT_S", 0.3)
    _Converter(monkeypatch)
    with _held(), _bob(monkeypatch, tmp_path) as (ws, _server):
        ws.send_text(_upload_frame())
        frames = _until(ws, "sidebar_documents_set")
    kinds = [m.get("type") for m in frames]
    assert "document_preview_busy" in kinds, kinds
    busy = frames[kinds.index("document_preview_busy")]
    assert busy == {"type": "document_preview_busy", "sessionId": SID,
                    "name": "report.docx", "reason": "timeout"}
    assert frames[-1]["contents"][0]["mimeType"] == DOCX_MIME


# ── whose turn ────────────────────────────────────────────────────────────────

def _spy_slot_keys(monkeypatch):
    keys = []
    real_slot = fair_queue.ResourceQueue.slot

    def slot(self, key=None, **kwargs):
        if self.name == "gotenberg":
            keys.append(key)
        return real_slot(self, key, **kwargs)

    monkeypatch.setattr(fair_queue.ResourceQueue, "slot", slot)
    return keys


def test_the_preview_waits_under_the_callers_scope(monkeypatch, tmp_path):
    """The socket's upload waits as the connection's account, not in one shared
    bucket. MUTATION: omit user_key (everybody "default") - red."""
    _Converter(monkeypatch, delay=0.01)
    keys = _spy_slot_keys(monkeypatch)
    with _bob(monkeypatch, tmp_path) as (ws, _server):
        ws.send_text(_upload_frame())
        _until(ws, "sidebar_documents_set")
    assert keys == [SCOPE], keys


def test_the_report_viewer_sends_to_its_session_only(monkeypatch, tmp_path):
    """A report opened in the viewer waits as the session's recorded owner, and its
    preview events go to that session's viewers only: never a broadcast to every
    connection, never the room reroute. MUTATIONS: omit user_key (red); send through
    _push_session_update (red)."""
    import vaf.core.session as session_mod
    from vaf.core import web_server as ws

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    mgr = session_mod.SessionManager(storage_dir=str(sessions_dir))
    monkeypatch.setattr(ws, "session_mgr", mgr)
    s = mgr.new(name="Report chat", user_scope_id=OTHER)
    s.id = SID
    mgr.save(s)
    monkeypatch.setattr("vaf.tools.librarian.LibrarianTool._read_file",
                        lambda self, path, chunking=True, first_page=None, last_page=None:
                        "extracted text of the document")
    monkeypatch.setattr(ws, "_docx_to_pdf_via_libreoffice", lambda path: None)
    monkeypatch.setattr(ws, "GOTENBERG_WAIT_S", 0.3)
    # A grace far beyond the test: the exact event list cannot depend on how fast the
    # busy path runs against the held-back place.
    monkeypatch.setattr(ws, "PREVIEW_WAIT_GRACE_S", 60.0)
    _config(monkeypatch)
    _Converter(monkeypatch)
    keys = _spy_slot_keys(monkeypatch)
    sent, wide = [], []

    async def to_session(session_id, message):
        sent.append((session_id, dict(message)))

    async def broadcast(message):
        wide.append(("broadcast", dict(message)))

    def rerouted(session_id, data):
        wide.append(("_push_session_update", dict(data)))

    monkeypatch.setattr(ws.manager, "broadcast_to_session", to_session)
    monkeypatch.setattr(ws.manager, "broadcast", broadcast)
    monkeypatch.setattr(ws.manager, "_push_session_update", rerouted)
    report = tmp_path / "report.docx"
    report.write_bytes(_docx_bytes())
    with _held(key="someone-else"):
        asyncio.run(ws._open_report_in_viewer(SID, str(report), "report.docx"))
    assert keys == ["someone-else", OTHER], keys
    assert wide == [], wide
    kinds = [m["type"] for _, m in sent]
    assert kinds == ["document_preview_busy", "sidebar_documents_set"], kinds
    assert all(sid == SID for sid, _ in sent)


def test_the_report_index_takes_the_owner_recorded_after_the_wait(monkeypatch, tmp_path):
    """The attachment index is keyed on the owner the session records after the wait: a
    session that only appears while the preview waits (the first read finds nothing)
    still indexes under its owner, never in the scope-less bucket.
    MUTATION: index under the first read's scope - red."""
    import vaf.core.session as session_mod
    from vaf.core import web_server as ws

    class _Sessions:
        def __init__(self):
            self.reads = 0

        def load(self, session_id):
            self.reads += 1
            if self.reads == 1:
                raise FileNotFoundError(session_id)
            s = session_mod.Session(id=session_id, name="Report chat")
            s.metadata = {"user_scope_id": OTHER}
            return s

        def save(self, session, sync_state=False):
            pass

    monkeypatch.setattr(ws, "session_mgr", _Sessions())
    monkeypatch.setattr("vaf.tools.librarian.LibrarianTool._read_file",
                        lambda self, path, chunking=True, first_page=None, last_page=None:
                        "extracted text of the document")
    _config(monkeypatch, url="")
    real_get = Config.get.__func__

    def get(cls, key, default=None):
        if key == "attachment_rag_enabled":
            return True
        return real_get(cls, key, default)

    monkeypatch.setattr(Config, "get", classmethod(get))
    indexed = []

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(ws, "_notify_attachment_index", quiet)
    monkeypatch.setattr(ws, "_spawn_attachment_index",
                        lambda manager, session_id, scope, contents: indexed.append(scope))
    monkeypatch.setattr(ws.manager, "broadcast_to_session", quiet)
    report = tmp_path / "report.md"
    report.write_bytes(b"# Report")
    asyncio.run(ws._open_report_in_viewer(SID, str(report), "report.md"))
    assert indexed == [OTHER], indexed


# ── the page ──────────────────────────────────────────────────────────────────

def test_page_forwards_and_shows_the_preview_place():
    """The page rebuilds every event field by field (an unforwarded field is silently
    dropped): pin each read, the clears and the prop. The place is kept per chat: the
    server sends it only when it changes, so a chat switch must not drop it, and the
    grant or busy of a chat not on screen must still reach the bookkeeping.
    MUTATIONS: drop the eta_ms read (red); drop the clear in sidebar_documents_set (red);
    clear every place on a chat switch again (red); drop the events from
    crossSessionAllowed (red); the red toast again (red)."""
    page = PAGE.read_text(encoding="utf-8")
    start = page.index("else if (data.type === 'document_preview_wait')")
    branch = page[start:page.index("else if (data.type === 'stt_wait')", start)]
    wait, busy = branch.split("else if (data.type === 'document_preview_busy')")
    for field in ("data.sessionId", "data.name", "data.position", "data.eta_ms"):
        assert field in wait, field
    assert "[previewSessionId]:" in wait and "clearDocPreviewWait(previewSessionId)" in wait
    assert "data.name" in busy and "tMain('docPreviewBusy'" in busy
    assert "clearDocPreviewWait(busySessionId)" in busy
    assert "busySessionId === activeSessionId" in busy
    assert "notice: true" in busy
    docs = page.index("data.type === 'sidebar_documents_set'")
    assert "clearDocPreviewWait(sid)" in page[docs:docs + 400]
    assert not re.search(r"setDocPreviewWaits?\((null|\{\})\)", page), "a switch drops every place"
    assert "[currentSessionId]);" not in page[page.index("const [docPreviewWaits"):
                                              page.index("const docPreviewNotice")]
    assert "docPreviewWaits[currentSessionId]" in page
    allowed = page[page.index("const crossSessionAllowed = ("):]
    allowed = allowed[:allowed.index(");")]
    assert "'document_preview_wait'" in allowed and "'document_preview_busy'" in allowed
    assert "previewNotice={docPreviewNotice}" in page
    toast = page[page.index("{downloadToast.show && ("):]
    toast = toast[:toast.index("{downloadToast.message}")]
    assert "max-md:w-[calc(100vw-2rem)] max-md:max-w-none" in toast
    assert 'downloadToast.notice ? "bg-white border-gray-200 text-gray-800"' in toast
    for key in ("docPreviewWaiting", "docPreviewWaitingNoEta"):
        assert f"tMain('{key}'" in page, key
    viewer = VIEWER.read_text(encoding="utf-8")
    assert "previewNotice?: string" in viewer
    assert re.search(r'role="status"[^>]*title=\{previewNotice\}>\{previewNotice\}', viewer)
    # The h-12 header clips a wrapped row: with a notice it stays on one line, and on a phone
    # only the dot and the notice remain; without one the row keeps its classes.
    assert "previewNotice ? 'flex items-center gap-2 text-[10px] text-gray-500 flex-nowrap'" in viewer
    assert ": 'flex items-center gap-2 text-[10px] text-gray-500 flex-wrap'" in viewer
    assert viewer.count("previewNotice && 'max-md:hidden'") == 3
