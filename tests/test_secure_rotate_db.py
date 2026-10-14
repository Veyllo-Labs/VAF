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


def test_an_owner_password_changed_but_not_saved_is_not_called_shipped(monkeypatch):
    """The owner role's password changed and did not verify: saying it "still has the shipped
    password" is false and hides the connection string the operator must keep. MUTATION:
    drop the RotationUnsaved branch - red."""
    from typer.testing import CliRunner

    from vaf.cli.cmd import secure
    from vaf.core import db_credentials
    from vaf.core.config import Config

    values = {"memory_db_url": "postgresql://vaf_app:vaf_app_dev_secret@localhost/db",
              "memory_db_owner_url": "postgresql://vaf:vaf_dev_secret@localhost/db"}
    monkeypatch.setattr(Config, "get", lambda k, d=None: values.get(k, d))
    monkeypatch.setattr(Config, "set", lambda k, v: values.__setitem__(k, v))

    async def _rotate(dsn):
        if "vaf_app" in dsn:
            return "postgresql://vaf_app:NEW-app@localhost/db"
        raise db_credentials.RotationUnsaved(
            "did not verify. The new connection string is: postgresql://vaf:NEW@localhost/db",
            "postgresql://vaf:NEW@localhost/db")

    monkeypatch.setattr(db_credentials, "rotate_role_password", _rotate)
    result = CliRunner().invoke(secure.app, ["rotate-db", "--yes"])
    out = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "postgresql://vaf:NEW@localhost/db Save it as memory_db_owner_url" in out
    assert "still has the shipped password" not in out


def test_a_password_the_config_did_not_save_is_shown_for_both_roles(monkeypatch):
    """Review finding: Config.set ran bare after a verified rotation. A refused write left the
    role on a new password nobody had: a traceback for the app role, and "still has the
    shipped password" for the owner. MUTATION: call Config.set unwrapped again - red."""
    from typer.testing import CliRunner

    from vaf.cli.cmd import secure
    from vaf.core import db_credentials
    from vaf.core.config import Config

    values = {"memory_db_url": "postgresql://vaf_app:vaf_app_dev_secret@localhost/db",
              "memory_db_owner_url": "postgresql://vaf:vaf_dev_secret@localhost/db"}
    refuse = {"memory_db_url"}

    def _set(k, v):
        if k in refuse:
            raise OSError("read-only config")
        values[k] = v

    async def _rotate(dsn):
        return dsn.replace("dev_secret", "NEW")

    monkeypatch.setattr(Config, "get", lambda k, d=None: values.get(k, d))
    monkeypatch.setattr(Config, "set", _set)
    monkeypatch.setattr(db_credentials, "rotate_role_password", _rotate)

    result = CliRunner().invoke(secure.app, ["rotate-db", "--yes"])
    out = " ".join(result.output.split())
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)  # said, not a traceback
    assert "postgresql://vaf_app:vaf_app_NEW@localhost/db Save it as memory_db_url" in out
    assert "nothing was changed" not in out   # the role's password was

    refuse.clear()
    refuse.add("memory_db_owner_url")
    values["memory_db_url"] = "postgresql://vaf_app:vaf_app_dev_secret@localhost/db"
    result = CliRunner().invoke(secure.app, ["rotate-db", "--yes"])
    out = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "postgresql://vaf:vaf_NEW@localhost/db Save it as memory_db_owner_url" in out
    assert "still has the shipped password" not in out
