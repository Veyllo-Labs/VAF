# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The filesystem map answers about the folder you named, not the one it prefers.

`query_fast` is the librarian's ultra-fast path: it answers folder questions from a cached
scan in about a second, before any tool is dispatched. It matched intent in a fixed order,
and the file types (`pdf`, `txt`, `docx`) lived inside the *Documents* branch - so a type
word decided which folder was reported and the folder word was never reached. "How many PDFs
are in Downloads" returned the Documents count.

Measured live: Downloads held 33 PDFs, the answer said 9, and 9 is exactly the number of
PDFs in Documents. Three separate runs returned the identical string, byte for byte, and the
caller could not tell - the answer is fluent, fast, and names a folder, just not the one in
the question. The agent then repeated it as fact.

This is the second time the same fast path has hijacked a question. The first
(2026-07-13, pinned in tests/test_librarian_honesty.py) was a PATH substring-matching
'document'; the fix stripped paths but left the type-implies-Documents rule that caused
this one. Hence the fixture below: Downloads deliberately holds MORE PDFs than Documents,
so any answer that silently falls back to Documents is a wrong NUMBER, not just a wrong
label.
"""
import pytest

from vaf.core.fs_map import FilesystemMap


def _map():
    """A map shaped like a real machine: the PDFs live in Downloads, not in Documents."""
    m = FilesystemMap.__new__(FilesystemMap)
    m.map = {"locations": {
        "documents": {"file_types": {"pdf": 9, "txt": 3, "html": 3}, "total_files": 17},
        "downloads": {"file_types": {"html": 37, "pdf": 33, "md": 6}, "total_files": 90},
        "pictures": {"file_types": {"png": 44, "jpg": 16, "pdf": 1}, "total_files": 80},
        "videos": {"file_types": {"mp4": 3}, "total_files": 3},
    }}
    return m


# ── the defect ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("question", [
    "und wie viele PDF habe ich im Downloads ordner ?",
    "Zaehle alle PDF-Dateien im Downloads-Ordner.",
    "how many PDFs are in my Downloads folder?",
    "wie viele PDFs liegen in Downloads?",
])
def test_a_type_question_about_downloads_answers_about_downloads(question):
    """The exact live failure. 33 is Downloads, 9 is Documents; a fallback shows up as the
    wrong number, which is why the fixture makes them differ."""
    out = _map().query_fast(question)
    assert out is not None
    assert "33" in out, f"answered with the wrong folder's count: {out!r}"
    assert "Downloads" in out
    assert "Documents" not in out


def test_the_same_question_about_documents_still_works():
    """The counterpart, so the fix cannot be "always answer Downloads"."""
    out = _map().query_fast("Wie viele PDFs habe ich im Documents Ordner?")
    assert out is not None and "9" in out and "Documents" in out


@pytest.mark.parametrize("question,folder,count", [
    ("wie viele mp4 sind in Videos?", "Videos", "3"),
    ("how many PNGs are in Pictures?", "Pictures", "44"),
    ("how many html files are in downloads?", "Downloads", "37"),
])
def test_every_folder_answers_for_itself(question, folder, count):
    out = _map().query_fast(question)
    assert out is not None and folder in out and count in out


# ── a type with no folder named ──────────────────────────────────────────────

def test_a_type_with_no_folder_names_every_folder():
    """The old code answered "Documents" here, which on the live machine was wrong by a
    factor of three. The counts are already in the map, so naming them all costs nothing
    and cannot be quietly wrong."""
    out = _map().query_fast("wie viele PDFs habe ich?")
    assert out is not None
    for fragment in ("Downloads 33", "Documents 9", "Pictures 1", "43"):
        assert fragment in out, f"missing {fragment!r} in {out!r}"


def test_a_type_nobody_has_falls_through_to_a_real_search():
    assert _map().query_fast("wie viele zip-Archive habe ich?") is None


# ── never a count that was not taken ─────────────────────────────────────────

def test_a_folder_without_type_data_does_not_report_zero():
    """The trap found while writing this: `types.get("pdf", 0)` turns "the scan has no type
    data for this folder" into a confident "0 PDFs". Unknown is not zero - return None and
    let the slow path actually look."""
    m = FilesystemMap.__new__(FilesystemMap)
    m.map = {"locations": {"downloads": {"total_files": 5}}}          # no file_types at all

    assert m.query_fast("wie viele PDFs sind in Downloads?") is None
    # Without a type asked, the total IS known and may still be answered.
    assert m.query_fast("wie viele Dateien sind in Downloads?") == "Downloads folder: 5 files"


def test_a_folder_the_machine_does_not_have_falls_through():
    m = FilesystemMap.__new__(FilesystemMap)
    m.map = {"locations": {"documents": {"file_types": {"pdf": 1}, "total_files": 1}}}
    assert m.query_fast("how many videos do I have?") is None


def test_a_zero_the_scan_really_took_is_reported():
    """The other side of it: file_types exists and simply has no PDFs. That zero was
    counted, so it is an answer."""
    m = FilesystemMap.__new__(FilesystemMap)
    m.map = {"locations": {"videos": {"file_types": {"mp4": 3}, "total_files": 3}}}
    out = m.query_fast("how many PDFs are in my videos folder?")
    assert out is not None and "0 PDFs" in out


# ── the 2026-07-13 guarantees must survive ───────────────────────────────────

@pytest.mark.parametrize("question", [
    "Loesche die Datei /home/user/Documents/VAF/report.html",
    "Open /home/user/Documents/report.html",
    "Was steht in bericht.pdf?",
    "remove the entry from the list",
    "start the docker container",
])
def test_paths_filenames_and_lookalike_words_still_answer_nothing(question):
    """Pinned in tests/test_librarian_honesty.py as well; repeated here because this file is
    where someone will next edit the matching, and the two rules interact."""
    assert _map().query_fast(question) is None
