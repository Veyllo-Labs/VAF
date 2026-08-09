# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The batched learn job (vaf/tools/learn_job.py) and its tool wiring.

Pinned here, each against its failure mode:
- one DB COMMIT per batch (a crash at batch 3 keeps batches 1-2 - the old shape
  rolled back 780 sections at once),
- resume skips recorded batches, refuses a changed source by checksum, and
  cuts crash orphans via delete_document_sections before re-running,
- the sync lane runs exactly ONE batch per call (an hour-long in-process learn
  on the single worker is the chat freeze the background job exists to avoid),
- cancellation lands between batches and the outcome names the resume,
- a firing learn_max_sections cap ends the run as `capped` and NAMES the key,
- the learn_state frame carries ints/strings only and never
  SubAgentStreamUpdate's typed field names (`progress` is Optional[int] there -
  a string in it is a silent ValidationError killing the run's bridge stream),
- the async tool path spawns learn_agent with the JSON spec as IPC payload and
  the per-session duplicate guard, and the child dispatcher hands the payload
  to run_learn_job.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import vaf.tools.learn_job as lj
from vaf.core.learn_ledger import LearnLedger
from vaf.tools.learn_job import LearnJobSpec, _learn_batches


@pytest.fixture(autouse=True)
def _isolated_vaf_dir(tmp_path, monkeypatch):
    # Path, not str: web_interface does `Platform.vaf_dir() / "logs"` directly.
    monkeypatch.setattr("vaf.core.platform.Platform.vaf_dir",
                        staticmethod(lambda: tmp_path / ".vaf"))
    return tmp_path


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "book.pdf"
    p.write_bytes(b"%PDF-fake-content")
    return p


class _FakeDb:
    """Counts commits: get_db commits at context exit, so exits == commits."""
    commits = 0

    async def execute(self, *a, **kw):
        class _R:
            def scalar_one_or_none(self):
                return None

            def scalars(self):
                class _S:
                    def all(self):
                        return []

                    def first(self):
                        return None  # no completed root: the learn proceeds
                return _S()
        return _R()


class _FakeGetDb:
    loops = []  # running-loop id per DB context (the cross-loop regression pin)

    def __init__(self, user_scope_id=None):
        pass

    async def __aenter__(self):
        import asyncio as _aio
        _FakeGetDb.loops.append(id(_aio.get_running_loop()))
        return _FakeDb()

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            _FakeDb.commits += 1
        return False


class _IngestLog(list):
    pipeline = None


@pytest.fixture
def rig(monkeypatch, pdf):
    """Fake extractor (100-page doc, 10 pages/batch), fake DB, fake ingest."""
    _FakeDb.commits = 0
    _FakeGetDb.loops = []
    ingests = _IngestLog()

    def _fake_extract(path, max_pages=None, ocr_fallback=True, *, first_page=1, cancel=None):
        total = 100
        pages_read = 0 if (max_pages == 0) else min(max_pages or total, total - first_page + 1)
        md = "\n\n".join(f"--- Page {first_page + i} ---\ncontent" for i in range(pages_read))
        return {"markdown": md, "total_pages": total, "num_pages": total,
                "pages_read": pages_read, "first_page": first_page,
                "truncated": pages_read < total, "used_ocr": False,
                "method": "pdfplumber", "ocr_unavailable_reason": ""}

    async def _fake_ingest(db, **kw):
        ingests.append({"section_offset": kw.get("section_offset"),
                        "update_root": kw.get("update_root")})
        return {"created": 3, "sections": 3, "doc_summary": "", "doc_tags": [],
                "sections_total": 3, "sections_dropped": 0,
                "sections_skipped_toc": 0, "toc_titles": []}

    class _FakePipeline:
        """Finalize + wipe + orphan-cut all go through RagPipeline; the fakes
        record instead of needing a database."""
        cut = {}
        wiped = []
        roots = []

        def __init__(self, db):
            pass

        async def ingest(self, content, metadata=None, user_scope_id=None, auto_connect=True):
            _FakePipeline.roots.append(metadata)

        async def delete_by_tag(self, tag, soft=True, user_scope_id=None):
            _FakePipeline.wiped.append(tag)
            return 0

        async def delete_document_sections(self, doc_tag, from_section_index,
                                           user_scope_id=None, soft=True):
            _FakePipeline.cut.update(doc_tag=doc_tag, frm=from_section_index)
            return 0

    _FakePipeline.cut = {}
    _FakePipeline.wiped = []
    _FakePipeline.roots = []
    ingests.pipeline = _FakePipeline

    monkeypatch.setattr("vaf.core.pdf_extract.extract_pdf_markdown", _fake_extract)
    monkeypatch.setattr("vaf.memory.database.get_db", _FakeGetDb)
    monkeypatch.setattr("vaf.memory.rag.RagPipeline", _FakePipeline)
    monkeypatch.setattr("vaf.tools.learn_document.ingest_document_knowledge", _fake_ingest)
    monkeypatch.setattr("vaf.core.config.Config.get",
                        classmethod(lambda cls, k, d=None:
                                    {"learn_batch_pages": 10, "learn_max_sections": 0}.get(k, d)))
    # Finalize touches _summarize_doc_from_contexts through the real module.
    monkeypatch.setattr("vaf.tools.learn_document._summarize_doc_from_contexts",
                        lambda contexts, title, fn: ("sum", []))
    return ingests


def _spec(pdf, **kw):
    return LearnJobSpec(path=str(pdf), document_title="Book", **kw)


def test_one_commit_per_batch_and_monotonic_offsets(pdf, rig):
    out = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                         user_scope_id=None, session_id="s1")
    assert out.status == "complete"
    assert out.batches_total == 10 and out.batches_done == 10
    assert out.sections_stored == 30
    # 10 batch commits + 1 finalize commit + 1 read-only already-learned
    # lookup at the start (it opens its own short DB context)
    assert _FakeDb.commits == 12
    assert [i["section_offset"] for i in rig] == [0, 3, 6, 9, 12, 15, 18, 21, 24, 27]
    assert all(i["update_root"] is False for i in rig), \
        "a per-batch root upsert stamps the last batch's numbers as the document's"
    # complete -> the ledger is gone (the root is the durable record)
    assert LearnLedger.load(out.doc_tag, "") is None


def test_the_whole_job_runs_on_one_event_loop(pdf, rig):
    """First live run: batch 1 committed, batch 2 died on "got Future attached
    to a different loop" - a per-batch asyncio.run gave every batch a fresh
    loop while the async DB engine stayed cached on the first one. Every DB
    context of a job must therefore see the SAME running loop."""
    out = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                         user_scope_id=None, session_id="s1")
    assert out.status == "complete"
    assert len(_FakeGetDb.loops) >= 10, "the pin needs the multi-batch run it pins"
    assert len(set(_FakeGetDb.loops)) == 1, \
        f"batches ran on {len(set(_FakeGetDb.loops))} different event loops"


def test_crash_at_batch_three_keeps_the_first_two(pdf, rig, monkeypatch):
    calls = {"n": 0}

    async def _boom(db, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("db died")
        return {"created": 3, "sections": 3, "doc_summary": "", "doc_tags": [],
                "sections_total": 3, "sections_dropped": 0,
                "sections_skipped_toc": 0, "toc_titles": []}

    monkeypatch.setattr("vaf.tools.learn_document.ingest_document_knowledge", _boom)
    out = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                         user_scope_id=None, session_id="s1")
    assert out.status == "failed"
    # batches 1-2 plus the start's read-only already-learned lookup context
    assert _FakeDb.commits == 3, "the failed batch must not commit"
    led = LearnLedger.load(out.doc_tag, "")
    assert led is not None and led.done_batch_indices() == {0, 1}
    assert led.status == "failed"
    assert "resume" in out.message()


def test_resume_skips_done_batches_and_cuts_orphans(pdf, rig):
    from vaf.core.learn_ledger import LearnBatch, file_sha256
    led = LearnLedger(doc_tag="doc-book", source_path=str(pdf),
                      content_sha256=file_sha256(pdf), user_scope_id="",
                      session_id="s1", total_pages=100, batch_pages=10, total_batches=10)
    led.record_batch(LearnBatch(index=0, first_page=1, pages=10, section_start=0, sections=3))
    led.record_batch(LearnBatch(index=1, first_page=11, pages=10, section_start=3, sections=3))

    out = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                         user_scope_id=None, session_id="s1")
    assert out.status == "complete"
    assert rig.pipeline.cut == {"doc_tag": "doc-book", "frm": 6}, \
        "crash orphans past the cut line were not deleted before the re-run"
    # 1 orphan-cut + 8 remaining batches + 1 finalize (the cut is its own
    # transaction: the deletions must be durable BEFORE the re-run re-ingests)
    assert _FakeDb.commits == 10
    assert len(rig) == 8


def test_changed_source_is_refused_by_checksum(pdf, rig):
    led = LearnLedger(doc_tag="doc-book", source_path=str(pdf),
                      content_sha256="not-the-real-sha", user_scope_id="",
                      session_id="s1", total_pages=100, batch_pages=10, total_batches=10)
    led.save()
    out = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                         user_scope_id=None, session_id="s1")
    assert out.status == "refused"
    assert "checksum mismatch" in out.error
    assert "force_relearn" in out.error
    assert _FakeDb.commits == 0, "a refused run must not touch the database"


def test_sync_lane_runs_exactly_one_batch(pdf, rig):
    out = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                         user_scope_id=None, session_id="s1", max_batches=1)
    assert out.status == "partial"
    assert out.batches_done == 1 and out.batches_total == 10
    # one batch commit + the start's read-only already-learned lookup context
    assert _FakeDb.commits == 2
    # Second call continues at batch 2
    out2 = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                          user_scope_id=None, session_id="s1", max_batches=1)
    assert out2.batches_done == 2
    assert [i["section_offset"] for i in rig] == [0, 3]


def test_cancel_lands_between_batches_and_names_the_resume(pdf, rig):
    polls = {"n": 0}

    def _cancel():
        polls["n"] += 1
        return polls["n"] > 2

    out = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                         user_scope_id=None, session_id="s1", cancel_cb=_cancel)
    assert out.status == "stopped"
    assert 0 < out.batches_done < 10
    msg = out.message()
    assert "Stopped by you" in msg and "resume" in msg
    led = LearnLedger.load(out.doc_tag, "")
    assert led is not None and led.status == "stopped"


def test_section_cap_ends_as_capped_and_names_the_key(pdf, rig, monkeypatch):
    monkeypatch.setattr("vaf.core.config.Config.get",
                        classmethod(lambda cls, k, d=None:
                                    {"learn_batch_pages": 10, "learn_max_sections": 5}.get(k, d)))
    out = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                         user_scope_id=None, session_id="s1")
    assert out.status == "capped"
    assert out.sections_stored >= 5
    assert "learn_max_sections" in out.message()


def test_empty_batches_are_listed_not_absorbed(pdf, rig, monkeypatch):
    async def _empty(db, **kw):
        return {"created": 0, "sections": 0, "doc_summary": "", "doc_tags": [],
                "sections_total": 0, "sections_dropped": 0,
                "sections_skipped_toc": 0, "toc_titles": []}

    monkeypatch.setattr("vaf.tools.learn_document.ingest_document_knowledge", _empty)
    out = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                         user_scope_id=None, session_id="s1")
    assert out.status == "complete"
    assert out.empty_page_ranges, "pages without text vanished silently"
    assert "had no extractable text" in out.message()


# ---------------------------------------------------------------------------
# The learn_state frame contract (the bridge landmine)
# ---------------------------------------------------------------------------

_FORBIDDEN_FRAME_KEYS = {"progress", "status", "name", "line", "steps", "file", "code"}


def test_learn_state_frames_carry_safe_keys_only(pdf, rig, monkeypatch):
    """SubAgentStreamUpdate types `progress` as Optional[int] and the bridge
    swallows ValidationErrors - one bad frame kills the run's whole stream."""
    frames = []
    monkeypatch.setenv("VAF_TASK_ID", "t1")
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    monkeypatch.setattr("vaf.core.progress.StatePublisher.publish",
                        lambda self, state, *, session_id="", force=False:
                        frames.append(state) or True)
    monkeypatch.setattr("vaf.core.progress.resolve_ui_session_id", lambda: "s1")

    spec_json = _spec(pdf).to_json()
    result = lj.run_learn_job(spec_json)
    assert "Learned" in result
    assert frames, "the job published no learn_state frames"
    for f in frames:
        assert not (_FORBIDDEN_FRAME_KEYS & set(f.keys())), \
            f"frame carries a SubAgentStreamUpdate-typed key: {f}"
        for k, v in f.items():
            assert isinstance(v, (int, str)), f"non int/str frame value {k}={v!r}"
    assert frames[-1]["phase"] == "done"


def test_cancel_flag_file_stops_the_child(pdf, rig, monkeypatch, tmp_path):
    monkeypatch.setenv("VAF_TASK_ID", "t-cancel")
    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    monkeypatch.setattr("vaf.core.progress.resolve_ui_session_id", lambda: "s1")
    flag = lj.cancel_flag_path("t-cancel")
    flag.write_text("")
    result = lj.run_learn_job(_spec(pdf).to_json())
    assert "Stopped by you" in result
    assert not flag.exists(), "the child must clean up its cancel flag"


# ---------------------------------------------------------------------------
# Tool wiring: async spawn path + duplicate guard + dispatcher branch
# ---------------------------------------------------------------------------

def test_async_path_spawns_learn_agent_with_json_payload(pdf, monkeypatch):
    from vaf.tools.learn_document import LearnDocumentTool

    spawned = {}

    def _fake_spawn(agent_type, task, **kw):
        spawned.update(agent_type=agent_type, task=task, **kw)
        from vaf.core.subagent_spawn import SpawnedSubagent
        return SpawnedSubagent(task_id="t1", marker=f"[SUBAGENT_ASYNC:t1:{agent_type}] x")

    monkeypatch.setattr("vaf.core.subagent_spawn.spawn_subagent", _fake_spawn)
    monkeypatch.setattr("vaf.tools.filesystem.is_safe_path", lambda p: (True, str(p)))
    monkeypatch.setattr("vaf.core.config.Config.get",
                        classmethod(lambda cls, k, d=None:
                                    True if k == "sub_agents_in_separate_terminals" else d))
    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_session_id", lambda: "sess-1")
    monkeypatch.setattr("vaf.core.subagent_ipc.SubAgentIPC.has_live_task",
                        lambda self, at, sid, pending_grace_s=120: False)
    monkeypatch.setattr("vaf.core.pdf_extract.extract_pdf_markdown",
                        lambda path, max_pages=None, ocr_fallback=True, first_page=1, cancel=None:
                        {"markdown": "--- Page 1 ---\nreal text here", "total_pages": 100,
                         "pages_read": 3, "first_page": 1, "truncated": True,
                         "used_ocr": False, "method": "pdfplumber",
                         "num_pages": 100, "ocr_unavailable_reason": ""})

    out = LearnDocumentTool().run(path=str(pdf), _agent=MagicMock())
    assert out.startswith("[SUBAGENT_ASYNC:t1:learn_agent]")
    assert spawned["agent_type"] == "learn_agent"
    assert spawned["include_task_arg"] is False
    payload = json.loads(spawned["payload"])
    assert payload["path"] == str(pdf)
    assert payload["resume"] is True and payload["force_relearn"] is False


def test_duplicate_learn_for_the_session_is_refused(pdf, monkeypatch):
    from vaf.tools.learn_document import LearnDocumentTool

    monkeypatch.setattr("vaf.tools.filesystem.is_safe_path", lambda p: (True, str(p)))
    monkeypatch.setattr("vaf.core.config.Config.get",
                        classmethod(lambda cls, k, d=None:
                                    True if k == "sub_agents_in_separate_terminals" else d))
    monkeypatch.setattr("vaf.core.subagent_ipc.get_current_session_id", lambda: "sess-1")
    monkeypatch.setattr("vaf.core.subagent_ipc.SubAgentIPC.has_live_task",
                        lambda self, at, sid, pending_grace_s=120: True)
    monkeypatch.setattr("vaf.core.pdf_extract.extract_pdf_markdown",
                        lambda path, max_pages=None, ocr_fallback=True, first_page=1, cancel=None:
                        {"markdown": "--- Page 1 ---\nreal text here", "total_pages": 100,
                         "pages_read": 3, "first_page": 1, "truncated": True,
                         "used_ocr": False, "method": "pdfplumber",
                         "num_pages": 100, "ocr_unavailable_reason": ""})
    spawn_called = []
    monkeypatch.setattr("vaf.core.subagent_spawn.spawn_subagent",
                        lambda *a, **kw: spawn_called.append(1))

    out = LearnDocumentTool().run(path=str(pdf), _agent=MagicMock())
    assert "already being learned" in out
    assert not spawn_called


def test_scanned_pdf_without_ocr_is_refused_before_any_spawn(pdf, monkeypatch):
    from vaf.tools.learn_document import LearnDocumentTool

    monkeypatch.setattr("vaf.tools.filesystem.is_safe_path", lambda p: (True, str(p)))
    monkeypatch.setattr("vaf.core.pdf_extract.extract_pdf_markdown",
                        lambda path, max_pages=None, ocr_fallback=True, first_page=1, cancel=None:
                        {"markdown": "--- Page 1 ---\n\n--- Page 2 ---",
                         "total_pages": 500, "pages_read": 3, "first_page": 1,
                         "truncated": True, "used_ocr": False, "method": "pdfplumber",
                         "num_pages": 500,
                         "ocr_unavailable_reason": "Tesseract binary not found"})
    spawn_called = []
    monkeypatch.setattr("vaf.core.subagent_spawn.spawn_subagent",
                        lambda *a, **kw: spawn_called.append(1))

    out = LearnDocumentTool().run(path=str(pdf), _agent=MagicMock())
    assert out.startswith("Error:")
    assert "Tesseract binary not found" in out
    # Both ways out are named: the free local engine AND the vision lane.
    assert "install Tesseract" in out and "vision" in out.lower()
    assert not spawn_called, "a hopeless scan spawned a job that would store nothing"


def test_dispatcher_branch_prefers_the_payload():
    """The child reads the JSON spec from the IPC sidecar, not argv."""
    src = Path("vaf/cli/cmd/subagent.py").read_text(encoding="utf-8")
    assert 'agent_type == "learn_agent"' in src
    block = src.split('agent_type == "learn_agent"')[1][:1200]
    assert "get_task_payload" in block
    assert "run_learn_job" in block


# ---------------------------------------------------------------------------
# The Web UI wiring (button handler + banner + cancel)
# ---------------------------------------------------------------------------

def test_learn_ws_handler_gates_ownership_and_path():
    """The button's WS handler must gate on session ownership, resolve the
    persisted file itself (a client-supplied path is never trusted), refuse a
    running duplicate, and answer deny frames instead of silence."""
    src = Path("vaf/core/web_server.py").read_text(encoding="utf-8")
    i = src.index('elif type == "learn_document_start"')
    block = src[i:i + 6000]
    assert "_ws_session_owner_ok(websocket, session_id)" in block
    assert "learn_denied" in block
    assert "realpath(_doc_path).startswith" in block, \
        "the handler stopped pinning the path to the session attachments dir"
    assert 'has_live_task("learn_agent"' in block
    assert "spawn_subagent(" in block
    # Already-learned pre-check: the frame answers BEFORE a child is spawned.
    assert block.index("find_completed_learn") < block.index("spawn_subagent("), \
        "the checksum pre-check must run before the spawn"
    assert '"already_learned"' in block

    j = src.index('elif type == "learn_document_cancel"')
    cancel_block = src[j:j + 1600]
    assert "_ws_session_owner_ok(websocket, session_id)" in cancel_block
    assert "get_active_tasks(session_id=session_id)" in cancel_block, \
        "cancel no longer verifies the task belongs to the caller's session"
    assert "cancel_flag_path(task_id).touch()" in cancel_block


def test_frontend_wires_button_banner_and_cancel():
    page = Path("web/app/page.tsx").read_text(encoding="utf-8")
    assert "learn_document_start" in page
    assert "learn_document_cancel" in page
    assert "learningDocument" in page, "the banner lost its i18n label"
    # The viewer's page-walk animation rides the learn_state frames (the old
    # chat lane's agent-cursor event) - losing it was the first thing the
    # owner noticed in the live run.
    assert page.count("agent-cursor") >= 2, \
        "learn_state no longer drives the Document Viewer page-walk animation"
    assert "learnStates={learnDocStates}" in page, "the viewer no longer gets button states"
    viewer = Path("web/components/DocumentViewer.tsx").read_text(encoding="utf-8")
    assert "onLearnDocument" in viewer
    assert viewer.count("!(learnReady || indexStatus === 'ready')") >= 2, \
        "the learn button is clickable before indexing is done (both rows must gate)"
    # The DURABLE flag, not the transient header status: that one auto-clears
    # after 4s and re-locked the button right after it unlocked (live run).
    assert "attachmentIndexedDone" in page, \
        "the button keys on the transient index status again - it locks after 4s"
    assert "learnReady={" in page


def test_learn_status_route_is_declared_before_the_catchall():
    """FastAPI matches in declaration order: /{memory_id} before /learn-status
    would swallow the path as a memory id."""
    src = Path("vaf/memory/routes.py").read_text(encoding="utf-8")
    assert src.index('get("/learn-status/{doc_tag}")') < src.index('get("/{memory_id}"'), \
        "learn-status is unreachable behind the /{memory_id} catch-all"
    i = src.index('get("/learn-status/{doc_tag}")')
    block = src[i:i + 2400]
    assert "get_current_user_scope" in block, "learn-status lost its user scoping"


def test_resume_resets_the_ledger_status_to_running(pdf, rig):
    """The previous run's terminal status must not survive the resume - the
    live run reported "failed" for half an hour of healthy batching."""
    from vaf.core.learn_ledger import LearnBatch, file_sha256

    led = LearnLedger(doc_tag="doc-book", source_path=str(pdf),
                      content_sha256=file_sha256(pdf), user_scope_id="",
                      session_id="s1", total_pages=100, batch_pages=10,
                      total_batches=10, status="failed", last_error="db died")
    led.record_batch(LearnBatch(index=0, first_page=1, pages=10, section_start=0, sections=3))

    states = []
    real_record = LearnLedger.record_batch

    def _spy(self, batch):
        states.append(self.status)
        return real_record(self, batch)

    import vaf.core.learn_ledger as ll
    orig = ll.LearnLedger.record_batch
    ll.LearnLedger.record_batch = _spy
    try:
        out = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                             user_scope_id=None, session_id="s1")
    finally:
        ll.LearnLedger.record_batch = orig
    assert out.status == "complete"
    assert states and all(s == "running" for s in states), \
        f"batches recorded under a terminal status: {set(states)}"


def test_toc_skips_survive_resume_and_reach_the_message(pdf, rig, monkeypatch):
    """The batch-1 ToC skip must survive a crash + resume via the ledger and
    end up as a named line in the completion message."""
    calls = {"n": 0}

    async def _first_batch_skips_then_dies(db, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"created": 3, "sections": 4, "doc_summary": "", "doc_tags": [],
                    "sections_total": 4, "sections_dropped": 0,
                    "sections_skipped_toc": 1, "toc_titles": ["Table of Contents"]}
        raise RuntimeError("db died")

    monkeypatch.setattr("vaf.tools.learn_document.ingest_document_knowledge",
                        _first_batch_skips_then_dies)
    out1 = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                          user_scope_id=None, session_id="s1")
    assert out1.status == "failed"

    async def _clean(db, **kw):
        return {"created": 3, "sections": 3, "doc_summary": "", "doc_tags": [],
                "sections_total": 3, "sections_dropped": 0,
                "sections_skipped_toc": 0, "toc_titles": []}

    monkeypatch.setattr("vaf.tools.learn_document.ingest_document_knowledge", _clean)
    out2 = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                          user_scope_id=None, session_id="s1")
    assert out2.status == "complete"
    assert out2.sections_skipped_toc == 1, \
        "the batch-1 skip must survive the resume via the ledger"
    assert "Skipped 1 table-of-contents section(s)." in out2.message()


def test_already_learned_by_checksum_is_refused(pdf, rig, monkeypatch):
    """The ledger dies on completion, so the durable guard is the root's
    stored checksum - a byte-identical re-learn must refuse, not duplicate."""
    calls = {"n": 0}

    async def _found(sha, scope):
        calls["n"] += 1
        return {"doc_title": "Book", "doc_tag": "doc-book",
                "sections": 30, "total_pages": 100}

    monkeypatch.setattr(lj, "find_completed_learn", _found)
    out = _learn_batches(_spec(pdf), generate_fn=lambda p: "x",
                         user_scope_id=None, session_id="s1")
    assert out.status == "refused"
    assert "already learned" in out.message()
    assert "force_relearn" in out.message()
    assert calls["n"] == 1
    assert len(rig) == 0, "no batch may run for an already-learned document"
    assert LearnLedger.load(out.doc_tag, "") is None, "a refused start leaves no ledger"


def test_force_relearn_bypasses_the_checksum_refusal(pdf, rig, monkeypatch):
    async def _found(sha, scope):
        raise AssertionError("force_relearn must not consult the already-learned lookup")

    monkeypatch.setattr(lj, "find_completed_learn", _found)
    out = _learn_batches(_spec(pdf, force_relearn=True), generate_fn=lambda p: "x",
                         user_scope_id=None, session_id="s1")
    assert out.status == "complete"


def test_find_completed_learn_query_filters_checksum_status_and_scope(monkeypatch):
    import asyncio
    import uuid as _uuid
    from contextlib import asynccontextmanager

    from sqlalchemy.dialects import postgresql

    sqls = []

    class _R:
        def scalars(self):
            return self

        def first(self):
            return None

    class _Db:
        async def execute(self, stmt):
            sqls.append(str(stmt.compile(dialect=postgresql.dialect())))
            return _R()

    @asynccontextmanager
    async def _gdb(user_scope_id=None):
        yield _Db()

    params = []

    class _Db2(_Db):
        async def execute(self, stmt):
            comp = stmt.compile(dialect=postgresql.dialect())
            sqls.append(str(comp))
            params.append(set(map(str, comp.params.values())))
            return _R()

    @asynccontextmanager
    async def _gdb2(user_scope_id=None):
        yield _Db2()

    monkeypatch.setattr("vaf.memory.database.get_db", _gdb2)
    assert asyncio.run(lj.find_completed_learn("abc123", _uuid.uuid4())) is None
    # JSON accessor keys and comparison values travel as bind params.
    for needle in ("content_sha256", "learn_status", "document_index", "complete", "abc123"):
        assert needle in params[0], f"the already-learned lookup lost its {needle} filter"
    assert "user_scope_id" in sqls[0].split("WHERE", 1)[1]
