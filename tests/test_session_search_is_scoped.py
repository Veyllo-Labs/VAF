# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The session store gets ONE strict-ownership walker, and search runs on it.

`SessionManager.search()` used to glob the whole store with no scope at all: on a
multi-user installation it answered with other people's chat text, and its single
caller is a CLI command nobody thought of as multi-user. It is now a thin wrapper
over `iter_owned_sessions`, whose ownership rule is deliberately stricter than
`list()`'s - a session with no owner is shown to everyone in a sidebar, but its
CONTENT belongs to nobody.
"""
import inspect
import json
import os
import time

import pytest

from vaf.core.session import SessionManager

OWNER = "ab12cd34-owner"
STRANGER = "ef56gh78-stranger"


def _write(store, sid, *, scope=OWNER, text="the quarterly report", metadata=None, age_days=0.0):
    meta = {"user_scope_id": scope} if scope else {}
    meta.update(metadata or {})
    (store / f"{sid}.json").write_text(json.dumps({
        "id": sid,
        "name": sid,
        "created_at": "2026-08-01T10:00:00",
        "updated_at": "2026-08-09T10:00:00",
        "messages": [{"role": "user", "content": text}],
        "metadata": meta,
    }), encoding="utf-8")
    if age_days:
        stamp = time.time() - (age_days * 86400)
        os.utime(store / f"{sid}.json", (stamp, stamp))


@pytest.fixture
def manager(tmp_path):
    return SessionManager(storage_dir=str(tmp_path))


def test_search_requires_a_scope(manager):
    """Mutation guard: making it optional again restores the unscoped glob."""
    with pytest.raises(TypeError):
        manager.search("quarterly")  # no user_scope_id
    assert "user_scope_id" in inspect.signature(SessionManager.search).parameters


def test_search_never_returns_another_users_session(tmp_path, manager):
    _write(tmp_path, "mine")
    _write(tmp_path, "theirs", scope=STRANGER)

    assert [r["session_id"] for r in manager.search("quarterly", user_scope_id=OWNER)] == ["mine"]


def test_search_never_returns_an_unowned_session(tmp_path, manager):
    _write(tmp_path, "orphan", scope=None)

    assert manager.search("quarterly", user_scope_id=OWNER) == []


def test_an_empty_scope_yields_nothing_rather_than_everything(tmp_path, manager):
    _write(tmp_path, "mine")

    assert manager.search("quarterly", user_scope_id="") == []
    assert manager.search("quarterly", user_scope_id=None) == []
    assert list(manager.iter_owned_sessions(None)) == []


def test_iter_examines_files_and_yields_candidates_under_separate_bounds(tmp_path, manager):
    """The newest files can all belong to someone else; one bound cannot serve both."""
    for i in range(30):
        _write(tmp_path, f"theirs_{i:02d}", scope=STRANGER)
    time.sleep(0.01)
    _write(tmp_path, "mine")
    # Oldest first would never reach "mine"; the walk is newest-first, so it is examined first.
    got = list(manager.iter_owned_sessions(OWNER, max_files=5, max_candidates=50))
    assert [d.get("id") for _p, d in got] == ["mine"]


def test_iter_stops_at_the_candidate_bound(tmp_path, manager):
    for i in range(10):
        _write(tmp_path, f"mine_{i:02d}")

    assert len(list(manager.iter_owned_sessions(OWNER, max_candidates=3))) == 3


def test_iter_skips_hidden_thinking_and_oversized_sessions(tmp_path, manager):
    _write(tmp_path, "hidden", metadata={"hidden_from_list": True})
    _write(tmp_path, "thinking_x", metadata={"source": "thinking"})
    _write(tmp_path, "huge", text="x" * 5000)
    _write(tmp_path, "fine")

    ids = {d.get("id") for _p, d in manager.iter_owned_sessions(OWNER, max_bytes=2000)}

    assert ids == {"fine"}


def test_iter_honours_the_age_cutoff(tmp_path, manager):
    _write(tmp_path, "recent")
    _write(tmp_path, "ancient", age_days=90)

    ids = {d.get("id") for _p, d in manager.iter_owned_sessions(OWNER, max_age_days=30)}

    assert ids == {"recent"}


def test_a_corrupt_or_empty_file_is_skipped_not_raised(tmp_path, manager):
    (tmp_path / "broken.json").write_text("", encoding="utf-8")
    (tmp_path / "garbage.json").write_text("{not json", encoding="utf-8")
    _write(tmp_path, "fine")

    assert {d.get("id") for _p, d in manager.iter_owned_sessions(OWNER)} == {"fine"}


def test_list_owned_rows_carry_what_list_rows_carry(tmp_path, manager):
    _write(tmp_path, "mine")
    _write(tmp_path, "theirs", scope=STRANGER)

    rows = manager.list_owned(user_scope_id=OWNER)

    assert [r["id"] for r in rows] == ["mine"]
    assert set(rows[0]) >= {"id", "name", "created_at", "updated_at", "message_count", "summary", "metadata"}
