# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf setup` and the terminal door's offer on a fresh install.

The command must be usable by a person AND by an agent driving the CLI, so the
non-interactive path is tested by making every prompt explode: if a prompt is
ever reached in that mode, the test fails instead of hanging.

The door's half has one property that outranks the feature: a fresh install has
always been allowed through (there is no password to check against), and
offering to create an account must not turn any failure into a locked door.
"""
import json

import pytest
from typer.testing import CliRunner

from vaf.cli.cmd import setup as setup_cmd
from vaf.cli.cmd.setup import EXIT_CODES, SetupOutcome

runner = CliRunner()


def _explode(*_a, **_k):
    raise AssertionError("a prompt was reached in non-interactive mode")


@pytest.fixture
def no_db(monkeypatch):
    """The database half is not what these tests are about.

    Two seams, because they are two different jobs: starting containers is
    subprocess work, waiting for the schema is async work that must share the
    caller's event loop.
    """
    async def _ready(*_a, **_k):
        return True

    monkeypatch.setattr(setup_cmd, "_start_service_stack", lambda: None)
    monkeypatch.setattr(setup_cmd, "_wait_for_auth_db", _ready)


def test_the_password_comes_from_stdin_and_no_prompt_is_reached(monkeypatch, no_db):
    seen = {}

    def fake_engine(**kwargs):
        seen.update(kwargs)
        return SetupOutcome.CREATED

    monkeypatch.setattr(setup_cmd, "run_first_account_setup", fake_engine)
    monkeypatch.setattr(setup_cmd.getpass, "getpass", _explode)

    result = runner.invoke(setup_cmd.app, ["-u", "mert", "--password-stdin"],
                           input="hunter2secret\n")
    assert result.exit_code == 0
    assert seen["password"] == "hunter2secret", "the trailing newline belongs to the shell"
    assert seen["username"] == "mert"
    assert seen["interactive"] is False


def test_password_stdin_without_a_username_is_refused(monkeypatch, no_db):
    monkeypatch.setattr(setup_cmd, "run_first_account_setup", _explode)
    result = runner.invoke(setup_cmd.app, ["--password-stdin"], input="secret12\n")
    assert result.exit_code == EXIT_CODES[SetupOutcome.ABORTED]


@pytest.mark.parametrize("outcome", list(SetupOutcome))
def test_every_outcome_has_its_own_exit_code(monkeypatch, no_db, outcome):
    """A script must be able to branch without parsing text."""
    monkeypatch.setattr(setup_cmd, "run_first_account_setup", lambda **_k: outcome)
    result = runner.invoke(setup_cmd.app, ["-u", "mert", "--password-stdin"], input="secret12\n")
    assert result.exit_code == EXIT_CODES[outcome]
    assert len(set(EXIT_CODES.values())) == len(EXIT_CODES), "codes must stay distinct"


def test_an_existing_admin_stops_the_setup(monkeypatch, no_db):
    import vaf.auth.user_admin as user_admin

    class _Admin:
        username = "mert"

    async def _found(_db):
        return _Admin()

    monkeypatch.setattr(user_admin, "active_admin", _found)
    monkeypatch.setattr(user_admin, "create_first_admin", _explode)
    _fake_auth_db(monkeypatch)

    assert setup_cmd.run_first_account_setup(
        username="eve", password="secret12", interactive=False,
    ) == SetupOutcome.ALREADY_EXISTS


def test_a_short_password_is_refused_before_the_database_is_touched(monkeypatch, no_db):
    import vaf.auth.user_admin as user_admin
    monkeypatch.setattr(user_admin, "create_first_admin", _explode)
    _fake_auth_db(monkeypatch)

    assert setup_cmd.run_first_account_setup(
        username="mert", password="short", interactive=False,
    ) == SetupOutcome.ABORTED


def test_an_unreachable_database_says_so(monkeypatch):
    async def _never(*_a, **_k):
        return False

    monkeypatch.setattr(setup_cmd, "_start_service_stack", lambda: None)
    monkeypatch.setattr(setup_cmd, "_wait_for_auth_db", _never)
    assert setup_cmd.run_first_account_setup(
        username="mert", password="secret12", interactive=False,
    ) == SetupOutcome.DB_UNAVAILABLE


def _fake_auth_db(monkeypatch, created=None):
    """A no-op auth session, so the engine can be exercised without Postgres."""
    from contextlib import asynccontextmanager
    import vaf.auth.database as database

    @asynccontextmanager
    async def ctx(*_a, **_k):
        yield object()

    monkeypatch.setattr(database, "get_auth_db", ctx)
    return created


def test_a_created_account_unlocks_this_process(monkeypatch, no_db):
    """Choosing the password just proved who we are - asking again is noise."""
    import vaf.auth.user_admin as user_admin
    import vaf.cli.gate as gate

    async def _none(_db):
        return None

    async def _made(_db, **_k):
        class _U:
            username = "mert"
        return _U()

    monkeypatch.setattr(user_admin, "active_admin", _none)
    monkeypatch.setattr(user_admin, "create_first_admin", _made)
    _fake_auth_db(monkeypatch)
    monkeypatch.setattr(gate, "_unlocked", False)

    assert setup_cmd.run_first_account_setup(
        username="mert", password="secret12", agent_name="Jarvis", interactive=False,
    ) == SetupOutcome.CREATED
    assert gate._unlocked is True


def test_a_non_interactive_run_gets_a_suggested_agent_name(monkeypatch):
    """Nobody is there to answer, so the account must not wait for a name."""
    monkeypatch.setattr(setup_cmd, "input", _explode, raising=False)
    name = setup_cmd._ask_agent_name("", interactive=False)
    assert name.startswith("Nobel")
    assert setup_cmd._ask_agent_name("  Jarvis ", interactive=False) == "Jarvis"


def test_the_veyllo_key_writes_exactly_what_the_web_wizard_writes(monkeypatch):
    written = {}
    from vaf.core import config as config_module
    monkeypatch.setattr(config_module.Config, "set",
                        lambda k, v: written.__setitem__(k, v))
    setup_cmd._store_veyllo_key("vk-test")
    assert written == {"api_key_veyllo": "vk-test",
                       "provider": "veyllo",
                       "vision_provider": "veyllo"}


# ------------------------------------------------------------- the door's offer

def test_the_door_offers_the_setup_and_a_no_still_lets_you_in(monkeypatch, capsys):
    import vaf.cli.gate as gate
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate, "_stored_hash", lambda: ("", False))
    monkeypatch.setattr(gate, "_unlocked", False)
    monkeypatch.setattr("builtins.input", lambda *_a: "n")
    monkeypatch.setattr(gate.getpass, "getpass", _explode)

    assert gate.require_admin_password() is True
    assert "vaf setup" in capsys.readouterr().err


def test_a_yes_runs_the_setup_and_never_asks_for_a_password(monkeypatch):
    import vaf.cli.gate as gate
    calls = []
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate, "_stored_hash", lambda: ("", False))
    monkeypatch.setattr(gate, "_unlocked", False)
    monkeypatch.setattr("builtins.input", lambda *_a: "y")
    monkeypatch.setattr(gate.getpass, "getpass", _explode)
    monkeypatch.setattr(setup_cmd, "run_first_account_setup",
                        lambda **_k: (calls.append(1), SetupOutcome.CREATED)[1])

    assert gate.require_admin_password() is True
    assert calls, "the offer must actually run the setup"


def test_a_broken_setup_never_locks_a_fresh_install_out(monkeypatch, capsys):
    """The door was always open here. An offer that fails must not close it."""
    import vaf.cli.gate as gate
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate, "_stored_hash", lambda: ("", False))
    monkeypatch.setattr(gate, "_unlocked", False)
    monkeypatch.setattr("builtins.input", lambda *_a: "y")
    monkeypatch.setattr(gate.getpass, "getpass", _explode)

    def boom(**_k):
        raise RuntimeError("docker is on fire")

    monkeypatch.setattr(setup_cmd, "run_first_account_setup", boom)
    assert gate.require_admin_password() is True
    assert "vaf setup" in capsys.readouterr().err


def test_an_unreadable_answer_counts_as_no(monkeypatch):
    """Under captured output `input()` raises OSError, not EOFError - and a
    fresh start may not die on the question."""
    import vaf.cli.gate as gate
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate, "_stored_hash", lambda: ("", False))
    monkeypatch.setattr(gate, "_unlocked", False)
    monkeypatch.setattr("builtins.input",
                        lambda *_a: (_ for _ in ()).throw(OSError("captured")))
    monkeypatch.setattr(gate.getpass, "getpass", _explode)
    monkeypatch.setattr(setup_cmd, "run_first_account_setup", _explode)

    assert gate.require_admin_password() is True


# ------------------------------------------------- the agent name is the owner's

def test_the_agent_name_follows_the_configured_owner(monkeypatch, tmp_path):
    """Reading the workspace of the literal "admin" meant an owner called
    anything else never saw the name they chose."""
    from vaf.core import config as config_module
    monkeypatch.setattr(config_module.Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config_module, "get_local_admin_username", lambda: "mert")

    home = tmp_path / "users" / "mert"
    home.mkdir(parents=True)
    (home / "identity.json").write_text(json.dumps({"name": "Jarvis", "emoji": "*"}))

    from vaf.cli.tui_app import app as tui_app
    assert tui_app._agent_name() == "Jarvis"
