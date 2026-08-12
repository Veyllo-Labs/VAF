# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf secure rotate-db` and the default-password warning, per the live run.

Both cases here are regression tests for defects found in the FIRST live run
of the command (which had shipped without any test):

1. The bare `postgresql://` scheme from config resolves to SQLAlchemy's sync
   default driver (psycopg2), which is not installed; the command died with
   ModuleNotFoundError before touching anything. Every engine the command
   opens must go through `normalize_async_dsn`.

2. The default-password check read only `memory_db_url`. A default install
   has TWO roles since the RLS cutover, and rotating just the app role left
   the OWNER (DDL) superuser on the published secret while the warning said
   all clear.
"""
from vaf.memory.database import normalize_async_dsn


def test_normalize_forces_the_async_driver():
    assert normalize_async_dsn("postgresql://u:p@localhost:5432/db") == \
        "postgresql+asyncpg://u:p@localhost:5432/db"
    # Already-normalized input passes through untouched.
    assert normalize_async_dsn("postgresql+asyncpg://u:p@h/db") == \
        "postgresql+asyncpg://u:p@h/db"
    # A DSN without any scheme gets one rather than reaching SQLAlchemy bare.
    assert normalize_async_dsn("u:p@h/db").startswith("postgresql+asyncpg://")


def test_memory_lane_and_rotate_share_one_normalizer(monkeypatch):
    """get_database_url must ride the same helper the CLI uses.

    Mutation check for the fix: if someone re-inlines the scheme replacement
    in one place and edits it, the two lanes drift apart again - this pins
    them to a single implementation.
    """
    from vaf.memory import database

    seen = {}

    def spy(url):
        seen["url"] = url
        return "postgresql+asyncpg://spy"

    monkeypatch.setattr(database, "normalize_async_dsn", spy)
    monkeypatch.setattr(database.Config, "get",
                        lambda k, d=None: "postgresql://u:p@h/db")
    assert database.get_database_url() == "postgresql+asyncpg://spy"
    assert seen["url"] == "postgresql://u:p@h/db"


def _warn_with(monkeypatch, app_dsn, owner_dsn):
    from vaf.core import service_stack
    from vaf.core.config import Config

    values = {"memory_db_url": app_dsn, "memory_db_owner_url": owner_dsn}
    monkeypatch.setattr(Config, "get", lambda k, d=None: values.get(k, d))
    # The security-event write is not under test and needs no store.
    monkeypatch.setattr(service_stack, "_say", lambda log, msg: None)
    return service_stack._warn_about_default_db_password()


def test_warning_fires_when_only_the_owner_dsn_is_default(monkeypatch):
    """The half-rotated state the live run actually produced."""
    assert _warn_with(monkeypatch,
                      "postgresql://vaf_app:rotated123@localhost/db",
                      "postgresql://vaf:vaf_dev_secret@localhost/db") is True


def test_warning_still_fires_for_the_app_dsn(monkeypatch):
    assert _warn_with(monkeypatch,
                      "postgresql://vaf:vaf_dev_secret@localhost/db",
                      "") is True


def test_warning_is_silent_once_both_are_rotated(monkeypatch):
    assert _warn_with(monkeypatch,
                      "postgresql://vaf_app:rotated123@localhost/db",
                      "postgresql://vaf:alsorotated456@localhost/db") is False
