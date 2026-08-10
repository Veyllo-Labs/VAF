# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Cross Chat Hint: what it must find, and whose chats it must never touch.

The retrieval half of this file pins the cases the feature exists for, including
the two German compounds that a whole-token scorer measurably cannot reach. The
isolation half pins the ownership rule: strict equality on a non-empty scope, no
admin widening, no unowned session, no conversation with a contact, and nothing
from a chat that is gone.
"""
import json
import os
import time
from pathlib import Path

import pytest

from vaf.core import cross_chat
from vaf.core.cross_chat import find_hints, format_hints, query_terms
from vaf.core.session import SessionManager

OWNER = "ab12cd34-owner"
STRANGER = "ef56gh78-stranger"


def _write_session(store: Path, sid, *, scope=OWNER, name="", messages=(), metadata=None,
                   age_days=0.0):
    """One synthetic chat on disk, exactly as SessionManager.save writes it."""
    meta = {"user_scope_id": scope} if scope else {}
    meta.update(metadata or {})
    payload = {
        "id": sid,
        "name": name or f"Session 2026-08-0{(hash(sid) % 9) + 1} 10:00",
        "created_at": "2026-08-01T10:00:00",
        "updated_at": "2026-08-09T10:00:00",
        "model": "",
        "project_path": "",
        "messages": [
            {"role": role, "content": content, "timestamp": "2026-08-09T10:00:00", **extra}
            for role, content, *rest in messages
            for extra in [rest[0] if rest else {}]
        ],
        "metadata": meta,
    }
    path = store / f"{sid}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    if age_days:
        stamp = time.time() - (age_days * 86400)
        os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated session store, with the real contact book kept out of it."""
    monkeypatch.setattr(cross_chat, "_contact_endpoints", lambda *a, **kw: set())
    return tmp_path


@pytest.fixture
def manager(store):
    return SessionManager(storage_dir=str(store))


def _hints(manager, query, **kw):
    kw.setdefault("user_scope_id", OWNER)
    kw.setdefault("manager", manager)
    return find_hints(query, **kw)


# ── the cases the feature exists for ────────────────────────────────────────────

def test_the_pdf_case_from_the_request(store, manager):
    _write_session(store, "chat_b", name="Rechnungen", messages=[
        ("user", "Kannst du mir die PDF mit der Abrechnung zusammenfassen?"),
        ("assistant", "Klar, ich habe die Datei gelesen."),
    ])
    _write_session(store, "chat_c", name="Urlaub", messages=[
        ("user", "Wann faehrt der Zug nach Wien?"),
    ])

    hints = _hints(manager, "Wir haben letztens an einer PDF zur Abrechnung gearbeitet", current_session_id="chat_d")

    assert [h.session_id for h in hints] == ["chat_b"]
    assert "PDF" in hints[0].text
    assert hints[0].session_name == "Rechnungen"


def test_german_compounds_are_reached_from_both_sides(store, manager):
    _write_session(store, "reise", name="Reise", messages=[
        ("user", "Ich lade dir hier die Abrechnung der Reisekosten als PDF hoch"),
    ])

    hints = _hints(manager, "Was war nochmal in der Reisekostenabrechnung?")

    assert [h.session_id for h in hints] == ["reise"]


def test_a_split_compound_matches_the_joined_query_word(store, manager):
    _write_session(store, "pruef", name="Buchhaltung", messages=[
        ("user", "Die Pruefung der Rechnung ist durch"),
    ])

    assert [h.session_id for h in _hints(manager, "Wo habe ich ueber die Rechnungspruefung gesprochen?")] == ["pruef"]


def test_transliterated_spelling_matches_the_umlaut_one(store, manager):
    _write_session(store, "ueber", name="Bank", messages=[
        ("user", "Die Überweisung an den Lieferanten ist raus"),
    ])

    assert [h.session_id for h in _hints(manager, "Was war mit der Ueberweisung?")] == ["ueber"]


def test_assistant_text_is_searched_too(store, manager):
    _write_session(store, "conv", name="Konvertierung", messages=[
        ("user", "Mach mal was damit"),
        ("assistant", "Ich habe die Quartalsauswertung nach Markdown konvertiert."),
    ])

    assert [h.session_id for h in _hints(manager, "Wo war die Quartalsauswertung?")] == ["conv"]


# ── what must NOT produce a hint ────────────────────────────────────────────────

def test_a_single_everyday_word_in_many_chats_produces_nothing(store, manager):
    for i in range(6):
        _write_session(store, f"chat_{i}", name=f"Chat {i}", messages=[
            ("user", f"Hier ist noch eine PDF, Nummer {i}"),
        ])

    # Only "pdf" can match; it is in every chat, so it distinguishes nothing.
    assert _hints(manager, "Reisekostenabrechnung PDF") == []


def test_no_match_means_no_hints_and_no_block(store, manager):
    _write_session(store, "chat_b", messages=[("user", "Wann faehrt der Zug nach Wien?")])

    hints = _hints(manager, "Wie ist das Wetter in Lissabon?")

    assert hints == []
    assert format_hints(hints) == ""


def test_tool_output_and_machine_written_turns_are_not_scanned(store, manager):
    _write_session(store, "tools", name="Tools", messages=[
        ("user", "los"),
        ("tool", "Quartalsauswertung.pdf gefunden"),
        ("user", "Erinnerung: Quartalsauswertung pruefen", {"kind": "timer"}),
    ])

    assert _hints(manager, "Was war mit der Quartalsauswertung?") == []


def test_an_inlined_file_is_not_something_the_user_said(store, manager):
    _write_session(store, "tui", name="TUI", messages=[
        ("user", "schau dir das an\n\n--- FILE: /home/user/.env ---\nSTRIPE_SECRET=hunter2\n----------------\n"),
    ])

    assert _hints(manager, "Was war der STRIPE_SECRET Wert?") == []


def test_the_current_chat_is_never_its_own_hint_source(store, manager):
    _write_session(store, "chat_d", name="Aktuell", messages=[
        ("user", "Die Reisekostenabrechnung liegt hier"),
    ])

    assert _hints(manager, "Reisekostenabrechnung?", current_session_id="chat_d") == []


def test_a_hidden_chat_is_not_a_source(store, manager):
    _write_session(store, "hidden", messages=[("user", "Die Reisekostenabrechnung liegt hier")],
                   metadata={"hidden_from_list": True})

    assert _hints(manager, "Reisekostenabrechnung?") == []


def test_a_thinking_run_is_not_a_source(store, manager):
    _write_session(store, "thinking_ab12cd34_r1", messages=[("user", "Die Reisekostenabrechnung liegt hier")],
                   metadata={"source": "thinking"})

    assert _hints(manager, "Reisekostenabrechnung?") == []


def test_a_deleted_chat_stops_producing_hints_immediately(store, manager):
    path = _write_session(store, "gone", messages=[("user", "Die Reisekostenabrechnung liegt hier")])
    assert _hints(manager, "Reisekostenabrechnung?")

    path.unlink()

    assert _hints(manager, "Reisekostenabrechnung?") == []


def test_a_chat_older_than_the_cutoff_is_not_scanned(store, manager):
    _write_session(store, "old", messages=[("user", "Die Reisekostenabrechnung liegt hier")], age_days=90)

    assert _hints(manager, "Reisekostenabrechnung?", max_age_days=30) == []
    assert _hints(manager, "Reisekostenabrechnung?", max_age_days=365)


# ── isolation ───────────────────────────────────────────────────────────────────

def test_another_users_chat_is_never_a_source(store, manager):
    _write_session(store, "theirs", scope=STRANGER, messages=[
        ("user", "Die Reisekostenabrechnung liegt hier"),
    ])

    assert _hints(manager, "Reisekostenabrechnung?") == []


def test_a_session_without_an_owner_belongs_to_nobody(store, manager):
    """MUTATION: swapping in list()'s lenient rule turns this session into a source."""
    _write_session(store, "unowned", scope=None, messages=[
        ("user", "Die Reisekostenabrechnung liegt hier"),
    ])

    assert _hints(manager, "Reisekostenabrechnung?") == []


def test_an_empty_caller_scope_returns_nothing_and_never_floors_to_the_admin(store, manager):
    _write_session(store, "mine", messages=[("user", "Die Reisekostenabrechnung liegt hier")])

    assert _hints(manager, "Reisekostenabrechnung?", user_scope_id=None) == []
    assert _hints(manager, "Reisekostenabrechnung?", user_scope_id="") == []


def test_a_channel_chat_with_a_contact_is_that_persons_conversation(store, tmp_path, monkeypatch):
    monkeypatch.setattr(cross_chat, "_contact_endpoints", lambda *a, **kw: {"123456"})
    manager = SessionManager(storage_dir=str(tmp_path))
    _write_session(tmp_path, "telegram_123456", name="Bob", messages=[
        ("user", "Die Reisekostenabrechnung liegt hier"),
    ])
    _write_session(tmp_path, "telegram_999999", name="Mein Kanal", messages=[
        ("user", "Die Reisekostenabrechnung liegt hier"),
    ])

    hints = find_hints("Reisekostenabrechnung?", user_scope_id=OWNER, manager=manager)

    assert [h.session_id for h in hints] == ["telegram_999999"]


def test_the_lane_emits_nothing_by_itself():
    """Whoever surfaces a hint owns scoping that emit; the lane must not do it."""
    src = (Path(__file__).resolve().parent.parent / "vaf" / "core" / "cross_chat.py").read_text(encoding="utf-8")
    assert "push_update" not in src
    assert "get_web_interface" not in src
    assert "broadcast" not in src


# ── shape of what reaches the prompt ────────────────────────────────────────────

def test_the_block_carries_no_heading_and_no_source_marker(store, manager):
    _write_session(store, "chat_b", name="Rechnungen", messages=[
        ("user", "## Ueberschrift: die Reisekostenabrechnung liegt hier"),
    ])

    block = format_hints(_hints(manager, "Reisekostenabrechnung?"))

    assert block
    assert "#" not in block          # the context X-ray carves its preview out at the next '##'
    assert "[Source " not in block   # and the memory-source parser must not see a hint as a memory


def test_k_is_respected_and_a_chat_contributes_at_most_one_hint(store, manager):
    for i in range(4):
        _write_session(store, f"chat_{i}", name=f"Chat {i}", messages=[
            ("user", "Die Reisekostenabrechnung fuer Lissabon liegt hier"),
            ("user", "Die Reisekostenabrechnung fuer Lissabon nochmal"),
        ])

    hints = _hints(manager, "Reisekostenabrechnung Lissabon?", k=2)

    assert len(hints) == 2
    assert len({h.session_id for h in hints}) == 2


def test_a_default_named_chat_is_labelled_with_its_first_message(store, manager):
    _write_session(store, "chat_b", name="Session 2026-08-09 11:22", messages=[
        ("user", "Die Reisekostenabrechnung fuer Lissabon liegt hier"),
    ])

    hints = _hints(manager, "Reisekostenabrechnung?")

    assert hints[0].session_name.startswith("Die Reisekostenabrechnung")


# ── memory_search: the same lane, asked on purpose ──────────────────────────────

def _run_memory_search(monkeypatch, *, rag_result="", db_up=True, hints=(), scope=OWNER):
    """Drive the tool with the vector store faked and the chat lane controlled."""
    import vaf.memory.database as database
    import vaf.memory.rag as rag
    from vaf.tools.context_tools import MemorySearchTool

    monkeypatch.setattr(rag, "run_memory_search_sync", lambda **kw: rag_result)
    monkeypatch.setattr(database, "check_db_connection_sync", lambda: db_up)
    monkeypatch.setattr(cross_chat, "search_other_chats", lambda *a, **kw: list(hints))
    return MemorySearchTool().run(query="Reisekostenabrechnung", user_scope_id=scope, username="owner")


def _hint(name="Rechnungen", text="Die Reisekostenabrechnung liegt hier"):
    from vaf.core.cross_chat import CrossChatHint
    return CrossChatHint(session_id="chat_b", session_name=name, updated_at="2026-08-09T10:00:00",
                         score=1.0, terms=("reisekostenabrechnung",), text=text)


def test_memory_search_returns_both_lanes_clearly_separated(monkeypatch):
    out = _run_memory_search(monkeypatch, rag_result="[Source 1] (Relevance: 90%)\nthe user likes tea",
                             hints=[_hint()])

    assert "[Source 1]" in out
    assert "Found in your other chats" in out
    assert 'chat "Rechnungen"' in out
    assert out.index("[Source 1]") < out.index("Found in your other chats")


def test_memory_search_answers_from_chats_when_the_store_has_nothing(monkeypatch):
    out = _run_memory_search(monkeypatch, rag_result="", hints=[_hint()])

    assert "No saved memories matched" in out
    assert 'chat "Rechnungen"' in out


def test_a_down_database_still_reports_the_chat_hits(monkeypatch):
    """The chat lane reads files, so an outage must not swallow what it found."""
    out = _run_memory_search(monkeypatch, rag_result="", db_up=False, hints=[_hint()])

    assert 'chat "Rechnungen"' in out
    assert "NOT reachable" in out


def test_a_down_database_without_chat_hits_still_says_so(monkeypatch):
    out = _run_memory_search(monkeypatch, rag_result="", db_up=False, hints=[])

    assert "NOT reachable" in out
    assert "Do NOT claim there are no stored memories" in out


def test_memory_search_hands_the_chat_lane_the_raw_scope(monkeypatch):
    """The vector store wants a UUID; the session store compares strings.

    Coercing here returned None for every non-UUID scope, which real installations
    have, and the chat half would have gone quietly dead.
    """
    seen = {}
    import vaf.memory.database as database
    import vaf.memory.rag as rag
    from vaf.tools.context_tools import MemorySearchTool

    monkeypatch.setattr(rag, "run_memory_search_sync", lambda **kw: "")
    monkeypatch.setattr(database, "check_db_connection_sync", lambda: True)
    monkeypatch.setattr(cross_chat, "search_other_chats",
                        lambda *a, **kw: seen.update(kw) or [])

    MemorySearchTool().run(query="Reisekostenabrechnung", user_scope_id="u1", username="owner")

    assert seen["user_scope_id"] == "u1"
    assert seen["username"] == "owner"


@pytest.mark.parametrize("enabled, expected_calls", [(False, 0), (True, 1)])
def test_the_switch_governs_the_explicit_search_too(monkeypatch, enabled, expected_calls):
    """One switch for one capability: whether other chats may be read at all.

    Asserted on whether the scan RUNS, not on its result - an empty result proves
    nothing, since the store might simply hold no match.
    """
    from vaf.core.config import Config
    from vaf.core.cross_chat import search_other_chats

    calls = []
    monkeypatch.setattr(cross_chat, "find_hints", lambda *a, **kw: calls.append(kw) or [])
    real_get = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: enabled if key == "cross_chat_hint_enabled" else real_get(key, default)))

    search_other_chats("Reisekostenabrechnung", user_scope_id=OWNER)

    assert len(calls) == expected_calls
    if enabled:
        # The explicit lane drops the two guards that only protect the passive one.
        assert calls[0]["min_terms"] == 1
        assert not calls[0]["max_age_days"]


def test_an_explicit_search_is_not_silenced_by_the_rarity_rule(store, manager):
    """The per-turn injection suppresses an everyday word; an explicit search must not."""
    for i in range(6):
        _write_session(store, f"chat_{i}", name=f"Chat {i}", messages=[("user", f"Hier ist PDF {i}")])

    assert _hints(manager, "PDF") == []
    assert find_hints("PDF", user_scope_id=OWNER, manager=manager, k=5, min_terms=1, max_age_days=0)


def test_an_explicit_search_reaches_past_the_age_cutoff(store, manager):
    _write_session(store, "old", messages=[("user", "Die Reisekostenabrechnung liegt hier")], age_days=200)

    assert _hints(manager, "Reisekostenabrechnung?") == []
    assert find_hints("Reisekostenabrechnung?", user_scope_id=OWNER, manager=manager,
                      min_terms=1, max_age_days=0)


def test_query_terms_drops_stopwords_but_keeps_short_meaningful_ones():
    terms = query_terms("Wir haben letztens an einer PDF gearbeitet")

    assert "pdf" in terms
    assert "haben" not in terms and "einer" not in terms and "letztens" not in terms
