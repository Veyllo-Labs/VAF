# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Archiving a chat: a move, not a second format - and never across accounts.

Deleting a chat used to be the only option, so a conversation the agent would
later have needed was gone for tidiness. Archiving keeps the session FILE, which
is already the whole conversation, already encrypted at rest and already carries
its owner - so anything that can read a session can read an archived one, which
is what makes it usable by the memory lane later. The isolation tests are the
point of the file: an archive that leaks is worse than none.
"""
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

from vaf.core.session import SessionManager


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return SessionManager(storage_dir=str(tmp_path / "sessions"))


def _make(mgr, name, scope):
    s = mgr.new(name=name, user_scope_id=scope)
    s.add_message("user", "the quick brown fox")
    s.add_message("assistant", "jumped over the lazy dog")
    mgr.save(s)
    return s


def test_archiving_moves_the_chat_out_of_the_list_but_keeps_it(mgr):
    s = _make(mgr, "kept", "ab12cd34")
    assert mgr.archive(s.id, user_scope_id="ab12cd34") is True

    assert not (mgr.storage_dir / f"{s.id}.json").exists(), "it must leave the sidebar"
    assert [r["id"] for r in mgr.list(user_scope_id="ab12cd34")] == []
    archived = mgr.list_archived(user_scope_id="ab12cd34")
    assert [r["id"] for r in archived] == [s.id]
    assert archived[0]["message_count"] == 2


def test_an_archived_chat_is_still_a_readable_session(mgr):
    """The reason it is a move: every existing reader keeps working on it."""
    s = _make(mgr, "readable", "ab12cd34")
    mgr.archive(s.id, user_scope_id="ab12cd34")

    path = mgr.archive_dir("ab12cd34") / f"{s.id}.json"
    data = mgr._read_session_file(path)
    assert [m["content"] for m in data["messages"]][0] == "the quick brown fox"
    assert data["metadata"]["user_scope_id"] == "ab12cd34"


def test_one_account_never_sees_anothers_archive(mgr):
    mine = _make(mgr, "mine", "ab12cd34")
    theirs = _make(mgr, "theirs", "ef56ab78")
    mgr.archive(mine.id, user_scope_id="ab12cd34")
    mgr.archive(theirs.id, user_scope_id="ef56ab78")

    assert [r["id"] for r in mgr.list_archived(user_scope_id="ab12cd34")] == [mine.id]
    assert [r["id"] for r in mgr.list_archived(user_scope_id="ef56ab78")] == [theirs.id]
    assert mgr.archive_dir("ab12cd34") != mgr.archive_dir("ef56ab78")


def test_a_file_in_the_wrong_folder_still_does_not_leak(mgr):
    """Isolation must not rest on the directory alone: the owner is re-checked
    from inside the file, so a stray copy cannot be read by the wrong account."""
    theirs = _make(mgr, "theirs", "ef56ab78")
    mgr.archive(theirs.id, user_scope_id="ef56ab78")

    stray = mgr.archive_dir("ef56ab78") / f"{theirs.id}.json"
    mine = mgr.archive_dir("ab12cd34")
    mine.mkdir(parents=True, exist_ok=True)
    (mine / stray.name).write_bytes(stray.read_bytes())

    assert mgr.list_archived(user_scope_id="ab12cd34") == [], (
        "a file carrying another account's scope must be ignored, not listed"
    )


def test_archiving_a_missing_chat_reports_failure(mgr):
    """The caller deletes when the move fails, so a False must mean False."""
    assert mgr.archive("nope123456", user_scope_id="ab12cd34") is False


def test_the_delete_handler_archives_before_deleting():
    """Order matters: archiving MOVES the file, so a delete afterwards would
    have nothing to remove - and a failed move must still delete, or a chat the
    user asked to be gone quietly stays."""
    import pathlib
    import re

    src = pathlib.Path("vaf/core/web_server.py").read_text(encoding="utf-8")
    block = src[src.index('elif type == "delete_session"'):]
    block = block[:block.index('elif type == "hide_session"')]
    assert 'cmd.get("archive")' in block, "the handler ignores the dialog's checkbox"
    assert re.search(r"if not session_mgr\.archive\([^)]*\):\s*\n\s*session_mgr\.delete", block), (
        "a failed archive must fall through to a real delete"
    )
    # And ownership is still checked before either path runs.
    assert "_ws_session_owner_ok" in block


def test_the_archive_routes_scope_on_the_caller_not_a_parameter():
    """An archive that can be asked for by id is one request from the wrong reader."""
    import inspect

    from vaf.api import config_routes

    for fn in (config_routes.list_archived_chats, config_routes.read_archived_chat):
        src = inspect.getsource(fn)
        assert "get_current_user_or_local_admin(request)" in src, (
            "the scope must come from the authenticated caller"
        )
        assert 'user.get("user_scope_id")' in src
    # Reading re-checks membership of the caller's OWN listing before touching a
    # file, so a guessed id cannot reach another account's archive.
    read_src = inspect.getsource(config_routes.read_archived_chat)
    assert "list_archived(user_scope_id=scope)" in read_src
    assert "404" in read_src


def test_search_finds_a_phrase_without_opening_the_chat_first(mgr):
    """The defect this replaced: the panel could only search a chat already open,
    so it confirmed what the reader had found instead of finding it."""
    a = _make(mgr, "first", "ab12cd34")
    b = mgr.new(name="second", user_scope_id="ab12cd34")
    b.add_message("user", "where did we discuss the invoice numbering")
    mgr.save(b)
    mgr.archive(a.id, user_scope_id="ab12cd34")
    mgr.archive(b.id, user_scope_id="ab12cd34")

    hits = mgr.search_archived("invoice", user_scope_id="ab12cd34")

    assert [h["chat_id"] for h in hits] == [b.id]
    assert hits[0]["index"] == 0, "the hit must name the message, not just the chat"
    assert "invoice" in hits[0]["line"]
    # The other chat is searched too, and simply does not match.
    assert mgr.search_archived("lazy dog", user_scope_id="ab12cd34")[0]["chat_id"] == a.id


def test_search_never_crosses_accounts(mgr):
    theirs = _make(mgr, "theirs", "ef56ab78")
    mgr.archive(theirs.id, user_scope_id="ef56ab78")
    assert mgr.search_archived("lazy dog", user_scope_id="ab12cd34") == []
    assert mgr.search_archived("lazy dog", user_scope_id="ef56ab78")


def test_empty_query_returns_nothing_rather_than_everything(mgr):
    s = _make(mgr, "kept", "ab12cd34")
    mgr.archive(s.id, user_scope_id="ab12cd34")
    assert mgr.search_archived("", user_scope_id="ab12cd34") == []
    assert mgr.search_archived("   ", user_scope_id="ab12cd34") == []


def test_the_agent_can_still_remember_an_archived_chat(mgr):
    """The promise the checkbox makes: "keep it so the agent can still remember".

    Cross Chat Hints read `iter_owned_sessions`, which globbed the sessions
    directory non-recursively - so archiving a chat used to hide it from the
    agent's memory, which is the opposite of what the user asked for.
    """
    s = _make(mgr, "kept", "ab12cd34")
    mgr.archive(s.id, user_scope_id="ab12cd34")

    seen = [d.get("id") for _, d in mgr.iter_owned_sessions("ab12cd34")]
    assert s.id in seen, "an archived chat must stay readable by the memory lane"


def test_remembering_still_stops_at_the_account_boundary(mgr):
    theirs = _make(mgr, "theirs", "ef56ab78")
    mgr.archive(theirs.id, user_scope_id="ef56ab78")
    seen = [d.get("id") for _, d in mgr.iter_owned_sessions("ab12cd34")]
    assert theirs.id not in seen


def test_the_archive_search_matches_exactly_like_the_agent_does(mgr):
    """One matcher for both, so no phrase is findable by the agent and not by
    the user in the same archive: folded (umlauts) and reaching into compounds
    from both sides - the cases cross_chat.py exists for."""
    s = mgr.new(name="Buchhaltung", user_scope_id="ab12cd34")
    s.add_message("user", "die Reisekostenabrechnung liegt bei")
    s.add_message("assistant", "die Prüfung der Rechnung folgt")
    mgr.save(s)
    mgr.archive(s.id, user_scope_id="ab12cd34")

    # Compound, from the short side - a substring search would find this too...
    assert mgr.search_archived("Reisekosten", user_scope_id="ab12cd34")
    # ...but these two are exactly what a substring search gets WRONG:
    assert mgr.search_archived("Pruefung", user_scope_id="ab12cd34"), (
        "folding must find Prüfung when the user types Pruefung"
    )
    assert mgr.search_archived("Reisekostenabrechnungen", user_scope_id="ab12cd34"), (
        "a compound must match from the long side too"
    )


def test_the_archive_search_and_the_hint_lane_share_one_matcher():
    """Pinned by source: two matchers would drift, and the drift would show up
    as a phrase the agent can find and the user cannot."""
    import pathlib

    src = pathlib.Path("vaf/core/session.py").read_text(encoding="utf-8")
    block = src[src.index("def search_archived"):src.index("def delete(self, session_id")]
    assert "from vaf.core.cross_chat import _excerpt, _match_text, query_terms" in block
    assert "in content.lower()" not in block, "the hand-rolled substring search is back"


def test_a_search_box_shows_single_common_words(mgr):
    """The hint lane's selection rules (min terms, min score, filler filter)
    decide what is worth prompt space. A search box must not inherit them, or a
    one-word search would answer nothing."""
    s = mgr.new(name="notes", user_scope_id="ab12cd34")
    s.add_message("user", "der Termin steht")
    mgr.save(s)
    mgr.archive(s.id, user_scope_id="ab12cd34")

    assert mgr.search_archived("Termin", user_scope_id="ab12cd34"), (
        "one informative word must be enough for a user-typed search"
    )


def test_a_hit_names_the_words_it_actually_matched(mgr):
    """A highlight cannot draw the folded query term: a reader who typed
    "Pruefung" matched "Prüfung", and marking what they typed would mark
    nothing - leaving them to guess what the search found."""
    s = mgr.new(name="Buchhaltung", user_scope_id="ab12cd34")
    s.add_message("user", "die Prüfung der Reisekostenabrechnung laeuft")
    mgr.save(s)
    mgr.archive(s.id, user_scope_id="ab12cd34")

    hits = [h for h in mgr.search_archived("Pruefung Reisekosten", user_scope_id="ab12cd34")
            if h["index"] >= 0]
    assert hits, "the folded/compound search must still find it"
    words = hits[0]["words"]
    assert "prüfung" in [w.lower() for w in words], words
    assert any("reisekosten" in w.lower() for w in words), words


def test_a_hit_carries_the_exact_passage_the_agent_would_get(mgr):
    """The viewer marks what the MODEL sees, so the range has to address the raw
    message - `_excerpt` computes its window on a collapsed, folded copy, whose
    offsets point somewhere else entirely."""
    from vaf.core.cross_chat import _SNIPPET_CHARS

    filler = "x" * 500
    body = f"{filler} die Prüfung der Rechnung {filler}"
    s = mgr.new(name="lang", user_scope_id="ab12cd34")
    s.add_message("user", body)
    mgr.save(s)
    mgr.archive(s.id, user_scope_id="ab12cd34")

    hit = [h for h in mgr.search_archived("Pruefung", user_scope_id="ab12cd34")
           if h["index"] >= 0][0]
    start, end = hit["span"]
    passage = body[start:end]
    assert "Prüfung" in passage, "the passage must contain what matched"
    assert end - start <= _SNIPPET_CHARS
    assert len(passage) < len(body), "a passage is a window, not the whole message"


def test_deleting_an_archived_chat_removes_the_last_copy(mgr):
    """The end of the line: it leaves the archive AND the memory lane that reads
    the archive, which is why the dialog in front of it has to say so."""
    s = _make(mgr, "kept", "ab12cd34")
    mgr.archive(s.id, user_scope_id="ab12cd34")
    assert mgr.list_archived(user_scope_id="ab12cd34")

    assert mgr.delete_archived(s.id, user_scope_id="ab12cd34") is True

    assert mgr.list_archived(user_scope_id="ab12cd34") == []
    assert [d.get("id") for _, d in mgr.iter_owned_sessions("ab12cd34")] == [], (
        "the agent must stop being able to recall it"
    )
    assert mgr.delete_archived(s.id, user_scope_id="ab12cd34") is False, "gone stays gone"


def test_deleting_never_reaches_another_accounts_archive(mgr):
    """The owner is re-read from INSIDE the file before the unlink. A deleter
    that trusted the path could remove what list_archived refuses to show."""
    theirs = _make(mgr, "theirs", "ef56ab78")
    mgr.archive(theirs.id, user_scope_id="ef56ab78")

    # A stray copy sitting in the wrong folder: invisible to the listing, and
    # it must be undeletable through it too.
    stray = mgr.archive_dir("ef56ab78") / f"{theirs.id}.json"
    mine = mgr.archive_dir("ab12cd34")
    mine.mkdir(parents=True, exist_ok=True)
    (mine / stray.name).write_bytes(stray.read_bytes())

    assert mgr.delete_archived(theirs.id, user_scope_id="ab12cd34") is False
    assert (mine / stray.name).exists(), "a foreign file must not be unlinked"
    assert mgr.list_archived(user_scope_id="ef56ab78"), "the owner still has theirs"


def test_the_delete_route_answers_404_for_absent_and_for_foreign():
    """Same answer either way, so a guessed id cannot be probed for existence."""
    import inspect

    from vaf.api import config_routes

    src = inspect.getsource(config_routes.delete_archived_chat)
    assert "get_current_user_or_local_admin(request)" in src, "scope must come from the caller"
    assert "list_archived(user_scope_id=scope)" in src, "membership is checked before deleting"
    assert src.count("404") >= 2, "absent and foreign must be indistinguishable"
