# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf users`: the accounts that can sign in to this VAF, from the terminal.

`list` shows them with their role, whether their second factor is set up, and whether their
record still verifies (vaf/auth/account_seal.py). It sits behind the terminal door like
`vaf secrets`.

`reset-password NAME` is the way back in when an admin's password is gone, which no web
page can be: the admin is the one who resets the others. It needs a PERSON at the terminal
(stdin a tty), never a script or the agent's shell, and a proof: the admin password, or
the recovery key from VAF-BackThisUp.md (`--recovery-key`), which is what an owner who
forgot the password still has. The terminal door alone is not used here on purpose: it
opens for a caller without a terminal, and it asks for exactly the password that is gone.
The reset sets a new password, makes the second factor be set up again at the next login,
and re-seals the record as it stands now; a record that was changed outside VAF is shown
and asked about first. Every reset is the security event `password_changed`.
"""
import asyncio
import getpass

import typer

from vaf.cli.ui import UI

app = typer.Typer(help="The accounts that can sign in to this VAF.")

_MAX_ATTEMPTS = 3


@app.callback()
def _group():
    """The accounts that can sign in to this VAF."""


def _accounts() -> list:
    from sqlalchemy import select

    from vaf.auth import account_seal
    from vaf.auth.database import get_auth_db
    from vaf.auth.models import LocalUser

    async def _load():
        async with get_auth_db() as db:
            users = (await db.execute(select(LocalUser).order_by(LocalUser.username))).scalars().all()
            return [{"username": u.username, "role": u.role, "active": bool(u.is_active),
                     "twofa": bool(u.totp_secret) and not u.requires_2fa_setup,
                     "sealed": account_seal.is_sealed(u)} for u in users]

    return asyncio.run(_load())


@app.command("list")
def list_users():
    """The accounts, their role and whether their record still verifies."""
    from vaf.cli.gate import require_admin_password
    if not require_admin_password():
        raise typer.Exit(1)
    try:
        rows = _accounts()
    except Exception as e:
        UI.error(f"The accounts could not be read: {e}")
        raise typer.Exit(1)
    if not rows:
        UI.info("No accounts yet. `vaf setup` creates the admin.")
        return
    for r in rows:
        state = "" if r["active"] else "  (deactivated)"
        twofa = "2FA on" if r["twofa"] else "2FA not set up"
        record = "" if r["sealed"] else "  RECORD CHANGED OUTSIDE VAF"
        print(f"{r['username']}: {r['role']}, {twofa}{state}{record}")


def _proof(recovery_key: bool) -> str:
    """What proved the caller, or '' when nothing did. A person at the terminal only."""
    from vaf.cli.gate import _stored_hash, is_interactive, refuse_agent_command
    if refuse_agent_command():
        # Before the terminal check: the agent can give its command a terminal of its own.
        return ""
    if not is_interactive():
        UI.error("A password reset needs a person at the terminal: it does not run from a "
                 "script or the agent's shell.")
        return ""
    if recovery_key:
        from vaf.core.recovery_kit import recovery_wrap_path, unwrap_with_secret
        if not recovery_wrap_path().exists():
            UI.error(f"No recovery file at {recovery_wrap_path()}; restore it from your backup first.")
            return ""
        secret = getpass.getpass("Recovery key (from VAF-BackThisUp.md): ").strip()
        if secret and unwrap_with_secret(secret) is not None:
            return "recovery key"
        UI.error("That key does not open the recovery file.")
        _log_failed_proof("recovery key")
        return ""
    stored, unreadable = _stored_hash()
    if unreadable:
        UI.error("The key store cannot be read, so the password cannot be checked.")
        return ""
    if not stored:
        UI.error("No admin password is recorded on this machine to check against. "
                 "Use --recovery-key.")
        return ""
    from vaf.auth.crypto import verify_password
    for remaining in range(_MAX_ATTEMPTS, 0, -1):
        try:
            entered = getpass.getpass("VAF admin password (or Ctrl+C, then --recovery-key): ")
        except (EOFError, KeyboardInterrupt):
            print()
            return ""
        if entered and verify_password(stored, entered):
            return "admin password"
        UI.error(f"Wrong password. {remaining - 1} attempt(s) left.")
    _log_failed_proof("admin password")
    return ""


def _log_failed_proof(what: str) -> None:
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("cli_password_gate_failed",
                           detail=f"Password reset refused at the terminal: wrong {what}")
    except Exception:
        pass


def _new_password() -> str:
    from vaf.auth.user_admin import MIN_PASSWORD_LEN
    for _ in range(_MAX_ATTEMPTS):
        first = getpass.getpass(f"New password (at least {MIN_PASSWORD_LEN} characters): ")
        if len(first) < MIN_PASSWORD_LEN:
            UI.error(f"At least {MIN_PASSWORD_LEN} characters.")
            continue
        if getpass.getpass("Again: ") != first:
            UI.error("The two did not match.")
            continue
        return first
    return ""


@app.command("reset-password")
def reset_password(
    username: str = typer.Argument(..., help="The account to reset"),
    recovery_key: bool = typer.Option(False, "--recovery-key",
                                      help="Prove it with the recovery key from VAF-BackThisUp.md "
                                           "instead of the admin password"),
):
    """A new password for an account; its second factor is set up again at the next login."""
    proof = _proof(recovery_key)
    if not proof:
        raise typer.Exit(1)

    from vaf.auth import account_seal
    from vaf.auth.database import get_auth_db
    from vaf.auth.user_admin import find_user, reset_password_from_terminal

    async def _check():
        async with get_auth_db() as db:
            user = await find_user(db, username)
            if user is None:
                return None
            return {"role": user.role, "sealed": account_seal.is_sealed(user)}

    try:
        state = asyncio.run(_check())
    except Exception as e:
        UI.error(f"The accounts could not be read: {e}")
        raise typer.Exit(1)
    if state is None:
        UI.error(f"No account named {username!r}. `vaf users list` shows them.")
        raise typer.Exit(1)
    if not state["sealed"]:
        UI.warning(f"This account's record was changed outside VAF (its role reads "
                   f"{state['role']!r} now). Resetting it accepts the record as it stands.")
        if not typer.confirm("Reset it anyway?", default=False):
            raise typer.Exit(1)

    password = _new_password()
    if not password:
        raise typer.Exit(1)

    async def _reset():
        async with get_auth_db() as db:
            user = await find_user(db, username)
            if user is None:
                return False
            await reset_password_from_terminal(db, user, password)
            return True

    try:
        done = asyncio.run(_reset())
    except Exception as e:
        UI.error(f"The password could not be reset: {e}")
        raise typer.Exit(1)
    if not done:
        UI.error(f"No account named {username!r}.")
        raise typer.Exit(1)
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("password_changed", username=username,
                           detail=f"reset in a terminal on this machine ({proof})")
    except Exception:
        pass
    UI.success(f"New password set for {username}. Every device signed in to it is signed out "
               "(within a few seconds); the second factor is set up again at the next login.")
