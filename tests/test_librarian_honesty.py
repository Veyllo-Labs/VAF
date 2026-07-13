# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Librarian honesty on deletion tasks (incident 2026-07-13 09:16-09:18).

The librarian has NO delete capability, yet four delete/verify tasks were each
"answered" with canned Documents statistics in ~1s: the fs-map fast path
keyword-matched 'document' inside the PATH '/home/mert/Documents/...'. The
canned answer neither did nor refused anything, which fueled the caller's
escalating retries. Fixtures below are the four REAL task payloads.
"""
from vaf.core.fs_map import FilesystemMap
from vaf.tools.librarian import LibrarianTool, _is_destruction_request

# The four real payloads from ~/.vaf/subagent_queue/task_payloads/ (paths as delivered).
PAYLOAD_DELETE_DE = (
    "Lösche die Datei /home/mert/Documents/VAF_Projects/a74e6e21/blue286275/Workflow/wetter_berlin.html"
)
PAYLOAD_DELETE_EN = (
    "Delete the file at this exact path: /home/mert/Documents/VAF_Projects/a74e6e21/blue286275/"
    "Workflow/wetter_berlin.html -- remove it permanently. Do NOT list directories, do NOT read "
    "anything. Just delete that specific file."
)
PAYLOAD_DELETE_RM = (
    "DELETE the file at /home/mert/Documents/VAF_Projects/a74e6e21/blue286275/Workflow/"
    "wetter_berlin.html -- remove it permanently with rm or unlink. Do NOT list directories, "
    "do NOT read the file. Just DELETE it."
)
PAYLOAD_VERIFY = (
    "Confirm whether the file /home/mert/Documents/VAF_Projects/a74e6e21/blue286275/Workflow/"
    "wetter_berlin.html still exists. Answer YES if it exists, NO if deleted."
)


# ── destructive-intent detection ──────────────────────────────────────────────

def test_real_delete_payloads_are_detected():
    assert _is_destruction_request(PAYLOAD_DELETE_DE)
    assert _is_destruction_request(PAYLOAD_DELETE_EN)
    assert _is_destruction_request(PAYLOAD_DELETE_RM)


def test_verification_and_benign_tasks_pass():
    # The real existence-check payload mentions "deleted" but in a sentence
    # without a target - it must NOT be refused (legitimate verification).
    assert not _is_destruction_request(PAYLOAD_VERIFY)
    assert not _is_destruction_request("Liste die Dateien im Ordner Documents auf")
    assert not _is_destruction_request("Read the report about data removal policies")
    assert not _is_destruction_request("How big is my Downloads folder?")


def test_refusal_is_honest_and_not_retry_bait():
    lib = LibrarianTool.__new__(LibrarianTool)
    out = LibrarianTool._try_direct_execution(lib, PAYLOAD_DELETE_DE)
    assert out is not None
    assert "cannot delete" in out.lower()
    assert "Nothing was deleted" in out
    assert "do not re-delegate" in out.lower()
    assert not out.startswith("[ERROR]")          # error-styled results invite retries
    assert "try again" not in out.lower()
    assert "Filesystem Map Answer" not in out     # the incident's canned answer


# ── fs-map fast path: intent words, not path substrings ──────────────────────

def _map_with_docs():
    m = FilesystemMap.__new__(FilesystemMap)
    m.map = {"locations": {
        "documents": {"file_types": {"pdf": 3, "txt": 3, "docx": 0}, "total_files": 16},
        "downloads": {"total_files": 5},
        "videos": {"total_files": 2},
    }}
    return m


def test_paths_and_filenames_never_trigger_folder_stats():
    m = _map_with_docs()
    # The incident class: a PATH containing /Documents/ or a filename mention.
    assert m.query_fast(PAYLOAD_VERIFY) is None
    assert m.query_fast("Open /home/user/Documents/report.html") is None
    assert m.query_fast("Was steht in bericht.pdf?") is None
    # 'mov' must not match 'remove', 'doc' must not match 'docker'.
    assert m.query_fast("remove the entry from the list") is None
    assert m.query_fast("start the docker container") is None


def test_genuine_folder_questions_still_fast_answer():
    m = _map_with_docs()
    out = m.query_fast("How many documents do I have?")
    assert out and "3 PDFs" in out
    assert m.query_fast("Wie viele Dateien sind in Downloads?")
    assert m.query_fast("how many videos are there?")


def test_tool_description_forbids_delete_delegation():
    assert "CANNOT delete" in LibrarianTool.description
