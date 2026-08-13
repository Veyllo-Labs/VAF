# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf setup`: create this machine's admin account from the terminal.

WHY. Until now the first account could only be created in the browser, so a
fresh install answered `vaf run` with "open the web UI once to create your
account" - a detour for someone who is already sitting in a terminal, and a
dead end for an AI agent installing VAF on a box with no browser at all.

WHAT IT ASKS. Username, password (twice), a name for the agent, and optionally
a Veyllo API key. The agent name is the one thing the web wizard never asks,
even though it is the name the agent calls itself in every reply; a fresh
install otherwise ends up with a random one like "Nobel4831SkyBlue".

WHAT IT DOES NOT DO. It does not set up two-factor auth. The account is created
with `requires_2fa_setup=True`, exactly as the web bootstrap does, so the first
browser login walks into the same enrollment screen. Doing 2FA here would mean
showing a QR code in a terminal and duplicating the enrollment endpoints for a
step the web already owns.

DRIVABLE BY AGENTS. `--password-stdin` reads the password from stdin, because a
password on the command line is visible in `ps` to every user on the machine.
`--username` and `--agent-name` complete the non-interactive set; the API key
stays interactive-only (two secrets on one stdin is a footgun, and the key has
a home in Settings).

THE DATABASE. Accounts live in PostgreSQL, which lives in the Docker stack, so
this command starts the stack and waits for it - with a bounded budget, unlike
the server's own retry which waits forever on purpose. A CLI command that never
returns is worse than one that says what is wrong.
"""
from __future__ import annotations

import getpass
import os
import sys
from enum import Enum
from typing import Optional

import typer

from vaf.cli.ui import UI

app = typer.Typer(help="Create the admin account for this machine")


class SetupOutcome(str, Enum):
    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    DB_UNAVAILABLE = "db_unavailable"
    ABORTED = "aborted"
    FAILED = "failed"


# Exit codes, so a script or an agent can branch without parsing text.
EXIT_CODES = {
    SetupOutcome.CREATED: 0,
    SetupOutcome.FAILED: 1,
    SetupOutcome.ABORTED: 2,
    SetupOutcome.ALREADY_EXISTS: 3,
    SetupOutcome.DB_UNAVAILABLE: 4,
}

_MAX_PASSWORD_ROUNDS = 3


def _interactive() -> bool:
    """A human who can answer. Same rule as the terminal door: stdin decides."""
    if os.environ.get("VAF_NONINTERACTIVE") == "1":
        return False
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


async def _wait_for_auth_db(seconds: float) -> bool:
    """Create the auth schema, retrying until the budget runs out.

    Deliberately not `init_auth_db_with_retry`: that one never gives up (right
    for a server whose frontend polls forever, wrong for a command someone is
    waiting on). The schema creation is the readiness probe - on a CLI-first
    install nothing has ever created these tables, so "the DB answers" and "the
    tables exist" have to be one step.

    Async, and awaited from the SAME event loop as everything else that touches
    the database: the engine is cached process-wide and bound to the loop that
    created it, so a second `asyncio.run` would meet an engine from a loop that
    no longer exists ("attached to a different loop"). Found live.
    """
    import asyncio
    import time

    from vaf.auth.database import init_auth_db

    deadline = time.monotonic() + max(1.0, seconds)
    delay, last_error = 1.0, None
    while True:
        try:
            await init_auth_db()
            return True
        except Exception as e:
            last_error = e
            if time.monotonic() + delay >= deadline:
                UI.error(f"The database did not become ready: {type(last_error).__name__}: "
                         f"{str(last_error)[:160]}")
                return False
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 5.0)


def _start_service_stack() -> None:
    """Bring the containers up. Synchronous on purpose - it is subprocess work."""
    from vaf.core.service_stack import ensure_service_stack, find_stack_root

    if find_stack_root() is not None:
        UI.info("Starting the Docker services (database)...")
        ensure_service_stack(log=lambda m: UI.info(f"  {m}"))
    else:
        # A pip install ships no compose file. If a database is configured
        # elsewhere this still works, so try before complaining.
        UI.info("No Docker compose file found - trying the configured database directly.")


def _database_unreachable_note() -> None:
    UI.error(
        "VAF stores accounts in a PostgreSQL database that normally runs in Docker.\n"
        "  Check that Docker is running, then try again:\n"
        "    docker ps\n"
        "    vaf setup\n"
        "  If your database runs elsewhere, point `memory_db_url` at it first."
    )


def _ask_username(preset: str) -> Optional[str]:
    from vaf.auth.user_admin import UserAdminError, validate_username

    if preset:
        try:
            return validate_username(preset)
        except UserAdminError as e:
            UI.error(e.message)
            return None
    for _ in range(_MAX_PASSWORD_ROUNDS):
        try:
            raw = input("Username: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        try:
            return validate_username(raw)
        except UserAdminError as e:
            UI.warning(e.message)
    return None


def _ask_password() -> Optional[str]:
    """Twice, hidden, and checked against the shared rule before confirming.

    Validating the FIRST entry before asking for the repeat means a too-short
    password costs one prompt instead of two.
    """
    from vaf.auth.user_admin import MIN_PASSWORD_LEN, UserAdminError, validate_password

    for _ in range(_MAX_PASSWORD_ROUNDS):
        try:
            first = getpass.getpass(f"Password (at least {MIN_PASSWORD_LEN} characters): ")
            try:
                validate_password(first)
            except UserAdminError as e:
                UI.warning(e.message)
                continue
            again = getpass.getpass("Repeat password: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if first != again:
            UI.warning("Passwords do not match - try again.")
            continue
        return first
    return None


def _ask_agent_name(preset: str, interactive: bool) -> str:
    """The name the agent calls itself. Never empty - a suggestion stands in.

    Without a preset and without a terminal (an agent driving the CLI), the
    suggestion is taken silently rather than asked: the account must not hang
    on a question nobody is there to answer.
    """
    from vaf.auth.user_admin import suggest_agent_name

    if preset.strip():
        return preset.strip()
    suggestion = suggest_agent_name()
    if not interactive:
        return suggestion
    try:
        answer = input(f"What do you want to call your AI agent? [{suggestion}]: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return suggestion
    return answer.strip() or suggestion


def _ask_veyllo_key() -> Optional[str]:
    """Optional, and hidden while typing - it is a credential.

    Not validated against the API here: the endpoint that tests a key is
    first-run-gated and closes the moment an account exists, so a check would
    have to be built twice. Settings can test it.
    """
    try:
        key = getpass.getpass("Veyllo API key (press Enter to skip): ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return key.strip() or None


def _store_veyllo_key(key: str) -> None:
    """Exactly what the web wizard's API step writes, nothing more."""
    from vaf.core.config import Config

    Config.set("api_key_veyllo", key)
    Config.set("provider", "veyllo")
    Config.set("vision_provider", "veyllo")


def _closing_note(username: str, agent_name: str, provider_set: bool) -> None:
    UI.success(f'Done. Your admin account "{username}" is ready, '
               f'and your agent is called "{agent_name}".')
    print()
    print("Next steps:")
    print("  1. Start the app:  vaf tray     (or open http://localhost:3000)")
    print(f'  2. Log in as "{username}". The first login sets up two-factor auth,')
    print("     so have an authenticator app ready.")
    if not provider_set:
        print("  3. No AI provider is configured yet. Add your Veyllo API key")
        print("     (or another provider) in the web UI under Settings.")
    print()
    print("This terminal will ask for your admin password from now on.")


def run_first_account_setup(
    *,
    username: str = "",
    agent_name: str = "",
    password: Optional[str] = None,
    interactive: Optional[bool] = None,
    wait_db_seconds: float = 90.0,
) -> SetupOutcome:
    """Create the first admin account. The engine behind `vaf setup`.

    Also called from the terminal door's offer on a fresh install, which is why
    it is a plain function rather than only a Typer command.
    """
    import asyncio

    from vaf.auth.user_admin import UserAdminError, validate_password

    if interactive is None:
        interactive = _interactive()
    if password is None and not interactive:
        UI.error("No password given and no terminal to ask on. Use --password-stdin.")
        return SetupOutcome.ABORTED

    # A password that was HANDED to us is checked before anything expensive
    # happens. Starting Docker and waiting on a database, only to reject the
    # input afterwards, wastes a minute to say something knowable at once.
    if password is not None:
        try:
            validate_password(password)
        except UserAdminError as e:
            UI.error(e.message)
            return SetupOutcome.ABORTED

    _start_service_stack()

    from vaf.auth.database import get_auth_db
    from vaf.auth.user_admin import active_admin, create_first_admin

    state = {"key": None, "name": None, "agent": None}

    async def _flow() -> SetupOutcome:
        """Every database touch, inside ONE event loop.

        The prompts sit in here too. They block the loop, which is exactly
        right - nothing else is running, and splitting the work into several
        `asyncio.run` calls is what broke this the first time: the engine is
        cached process-wide against the loop that built it.
        """
        if not await _wait_for_auth_db(wait_db_seconds):
            _database_unreachable_note()
            return SetupOutcome.DB_UNAVAILABLE

        try:
            async with get_auth_db() as db:
                found = await active_admin(db)
                existing = found.username if found is not None else None
        except Exception as e:
            UI.error(f"The database could not be queried: {type(e).__name__}: {str(e)[:160]}")
            _database_unreachable_note()
            return SetupOutcome.DB_UNAVAILABLE

        if existing:
            UI.warning(f'This machine already has an admin account ("{existing}"). '
                       "Passwords are changed in the web UI under Settings.")
            return SetupOutcome.ALREADY_EXISTS

        if interactive:
            print()
            UI.info("Welcome to VAF. Let's create your admin account.")
        name = _ask_username(username)
        if not name:
            return SetupOutcome.ABORTED
        secret = password
        if secret is None:
            secret = _ask_password()
            if secret is None:
                UI.warning("Setup cancelled - no account was created.")
                return SetupOutcome.ABORTED

        state["name"] = name
        state["agent"] = _ask_agent_name(agent_name, interactive)
        state["key"] = _ask_veyllo_key() if interactive else None

        try:
            async with get_auth_db() as db:
                await create_first_admin(
                    db, username=name, password=secret, agent_name=state["agent"],
                )
        except UserAdminError as e:
            # admin_exists can still happen here if something raced us.
            if e.code == "admin_exists":
                UI.warning(e.message)
                return SetupOutcome.ALREADY_EXISTS
            UI.error(e.message)
            return SetupOutcome.FAILED
        except Exception as e:
            UI.error(f"The account could not be created: {type(e).__name__}: {str(e)[:200]}")
            return SetupOutcome.FAILED
        return SetupOutcome.CREATED

    outcome = asyncio.run(_flow())
    if outcome is not SetupOutcome.CREATED:
        return outcome

    name, chosen_agent, veyllo_key = state["name"], state["agent"], state["key"]
    if veyllo_key:
        try:
            _store_veyllo_key(veyllo_key)
        except Exception as e:
            UI.warning(f"The account was created, but the API key was not saved: {e}")
            veyllo_key = None

    # The door does not need to ask in this process - we just proved who we are
    # by choosing the password.
    try:
        from vaf.cli import gate
        gate._unlocked = True
    except Exception:
        pass

    _closing_note(name, chosen_agent, provider_set=bool(veyllo_key))
    return SetupOutcome.CREATED


@app.callback(invoke_without_command=True)
def setup_command(
    ctx: typer.Context,
    username: str = typer.Option("", "--username", "-u", help="Account name (asked when omitted)"),
    agent_name: str = typer.Option("", "--agent-name", help="What your AI agent is called"),
    password_stdin: bool = typer.Option(
        False, "--password-stdin",
        help="Read the password from stdin. For scripts and agents - never pass a "
             "password as an argument, it is visible in the process list.",
    ),
    wait_db: float = typer.Option(90.0, "--wait-db", help="Seconds to wait for the database"),
):
    """Create the admin account for this machine.

    Exit codes: 0 created, 1 failed, 2 cancelled or bad input,
    3 an admin already exists, 4 the database is unavailable.
    """
    if ctx.invoked_subcommand is not None:
        return

    password = None
    if password_stdin:
        if not username:
            UI.error("--password-stdin needs --username as well.")
            raise typer.Exit(EXIT_CODES[SetupOutcome.ABORTED])
        password = sys.stdin.read()
        if password.endswith("\n"):
            password = password[:-1]

    outcome = run_first_account_setup(
        username=username,
        agent_name=agent_name,
        password=password,
        interactive=False if password_stdin else None,
        wait_db_seconds=wait_db,
    )
    raise typer.Exit(EXIT_CODES[outcome])
