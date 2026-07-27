# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: the mail store could not be created at all on older SQLite.

The message index was declared as a contentless FTS5 table with ``contentless_delete=1``.
That option needs SQLite 3.43 (2023); older builds reject it while the table is being
CREATEd, and because the index is part of schema creation, the failure takes the entire
mail store with it - not one degraded feature, the whole thing.

It reaches real installs: ``requires-python`` allows 3.10, whose bundled SQLite on Windows
and macOS predates 3.43. The per-commit CI matrix only runs those two platforms on 3.12,
which is why this stayed invisible until the nightly full matrix, where it surfaced as 81
mail tests erroring on one line of DDL.

The half-fix that does not work, pinned below so nobody tries it: dropping the option is
not enough. A contentless table without it refuses DELETE outright, and the store deletes
from this index on every message removal, purge and re-index. The fallback therefore has to
be an ordinary FTS5 table, which stores its own copy of the indexed text - more disk, same
INSERT / DELETE / MATCH / bm25 surface, so no call site changes.

This machine's SQLite supports the option, so the interesting path is the one it never
takes. Both are exercised here by forcing the probe.
"""
import sqlite3
from unittest.mock import patch

import pytest

from vaf.mail import store as store_mod

COLUMNS = ("subject", "from_addr", "to_addrs", "body_text")


def _make(sql):
    conn = sqlite3.connect(":memory:")
    conn.execute(sql)
    return conn


def _exercise(conn):
    """Everything the store does to this index, in order."""
    conn.execute(
        "INSERT INTO messages_fts(rowid, subject, from_addr, to_addrs, body_text) "
        "VALUES(1, 'Rechnung', 'a@example.invalid', 'b@example.invalid', 'Betrag faellig')")
    conn.execute(
        "INSERT INTO messages_fts(rowid, subject, from_addr, to_addrs, body_text) "
        "VALUES(2, 'Urlaub', 'c@example.invalid', 'd@example.invalid', 'Strand und Meer')")
    assert conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'Rechnung'").fetchone()[0] == 1
    assert conn.execute(
        "SELECT bm25(messages_fts) FROM messages_fts WHERE messages_fts MATCH 'Strand'"
    ).fetchone() is not None
    conn.execute("DELETE FROM messages_fts WHERE rowid=?", (1,))
    assert conn.execute(
        "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'Rechnung'").fetchone()[0] == 0


@pytest.mark.parametrize("supported", [True, False])
def test_the_index_works_whether_or_not_this_sqlite_has_the_option(supported):
    """THE regression, both branches: create the index and do every operation the store
    performs on it. The False branch is what an older SQLite gets."""
    if supported and not store_mod._fts_supports_contentless_delete():
        pytest.skip("this SQLite cannot take the contentless branch")
    with patch.object(store_mod, "_fts_supports_contentless_delete", return_value=supported):
        sql = store_mod._fts_create_sql()
    _exercise(_make(sql))


def test_dropping_the_option_alone_would_not_have_worked():
    """Pins WHY the fallback stores content instead of just omitting the option - the
    cheap-looking fix leaves an index that cannot delete."""
    conn = _make("CREATE VIRTUAL TABLE messages_fts USING fts5("
                 "subject, from_addr, to_addrs, body_text, content='')")
    conn.execute("INSERT INTO messages_fts(rowid, subject) VALUES(1, 'x')")
    with pytest.raises(sqlite3.OperationalError, match="contentless"):
        conn.execute("DELETE FROM messages_fts WHERE rowid=1")


def test_both_variants_index_the_same_columns():
    """A fallback that silently indexed less would make search quietly worse on the
    platforms that need it most."""
    with patch.object(store_mod, "_fts_supports_contentless_delete", return_value=True):
        modern = store_mod._fts_create_sql()
    with patch.object(store_mod, "_fts_supports_contentless_delete", return_value=False):
        legacy = store_mod._fts_create_sql()
    for col in COLUMNS:
        assert col in modern and col in legacy, col
    assert 'unicode61 remove_diacritics 2' in modern
    assert 'unicode61 remove_diacritics 2' in legacy, "tokenizer must match or ranking differs"
    assert "content=''" in modern and "content=''" not in legacy


def test_the_probe_does_not_leave_a_connection_behind():
    """It runs on every schema creation; a leaked in-memory connection per call would be a
    slow leak in a long-lived process."""
    store_mod._fts_supports_contentless_delete.cache_clear()
    opened = []
    real_connect = sqlite3.connect

    def _tracking(*a, **kw):
        conn = real_connect(*a, **kw)
        opened.append(conn)
        return conn

    with patch("sqlite3.connect", _tracking):
        store_mod._fts_supports_contentless_delete()
    store_mod._fts_supports_contentless_delete.cache_clear()
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_the_store_records_which_variant_it_built(tmp_path):
    """Which index a database got is a property OF that database - a later migration and a
    support question both need to read it back rather than re-probe the current machine."""
    db = store_mod.MailStore(tmp_path / "mail.db")
    db.ensure_schema()
    conn = db._conn()
    row = conn.execute("SELECT value FROM schema_meta WHERE key='fts_variant'").fetchone()
    assert row is not None, "schema_meta has no fts_variant row"
    assert row["value"] in ("contentless", "stored")
    _exercise(conn)
