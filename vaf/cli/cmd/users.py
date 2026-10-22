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

`sessions NAME` lists the account's live sessions (browsers, this computer, tokens for
programs) and `sign-out NAME` ends every one of them, both behind the terminal door; the
running VAF is asked to close the account's open chats, as after a reset.

`token NAME --label TEXT` makes a token for programs: a script or a program on another
machine sends it as `Authorization: Bearer <token>` and acts as the account. It needs what a
reset needs (a person at the terminal, the admin password or the recovery key), because the
terminal door alone opens for a caller without a terminal. The token is printed once; it is
listed with the sessions and revoked there, in Settings, Account, or by `sign-out`. Every
token is the security event `program_token_created`, which names the proof.

Output is ASCII only: a Windows console in cp1252 cannot print more.
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


def _proof(recovery_key: bool, action: str = "password reset") -> str:
    """What proved the caller, or '' when nothing did. A person at the terminal only.
    `action` names what is being proved for, in the refusals and the security log."""
    from vaf.cli.gate import _stored_hash, is_interactive, refuse_agent_command
    if refuse_agent_command():
        # Before the terminal check: the agent can give its command a terminal of its own.
        return ""
    if not is_interactive():
        UI.error(f"A {action} needs a person at the terminal: it does not run from a "
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
        _log_failed_proof("recovery key", action)
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
    _log_failed_proof("admin password", action)
    return ""


def _log_failed_proof(what: str, action: str = "password reset") -> None:
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("cli_password_gate_failed",
                           detail=f"{action[:1].upper()}{action[1:]} refused at the terminal: wrong {what}")
    except Exception:
        pass


def _new_password() -> str:
    """Asked twice; judged by the one rule every door uses (length, and not a common one)."""
    from vaf.auth.user_admin import MIN_PASSWORD_LEN, UserAdminError, validate_password
    for _ in range(_MAX_ATTEMPTS):
        first = getpass.getpass(f"New password (at least {MIN_PASSWORD_LEN} characters): ")
        try:
            validate_password(first)
        except UserAdminError as e:
            UI.error(e.message)
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
                return None
            await reset_password_from_terminal(db, user, password)
            return str(user.id)

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
    why_not = _close_open_connections(done)
    if why_not is None:
        UI.success(f"New password set for {username}. Every device signed in to it is signed out, "
                   "and the second factor is set up again at the next login.")
    else:
        UI.success(f"New password set for {username}. Its sessions ended, and the second factor is "
                   "set up again at the next login.")
        UI.warning(f"VAF did not confirm that the account's open connections were closed ({why_not}). "
                   "A device with a chat still open keeps it until VAF restarts: restart VAF now.")


def _close_open_connections(user_id: str):
    """Ask the running VAF to close the account's open connections: this command ended the
    sessions in the database, but only the service holds the sockets, and a socket is checked
    only at its handshake. Through the same loopback lane the other commands use
    (`internal_api_base`), as the machine owner. None when done, or when no VAF runs (nothing
    is open then); otherwise why not."""
    import requests

    from vaf.core.web_interface import internal_api_base
    try:
        # Never through a proxy from the environment: this call does not leave the machine,
        # and with HTTP_PROXY set and no loopback exception it went to the proxy and failed
        # like a VAF that is not running.
        with requests.Session() as session:
            session.trust_env = False
            r = session.post(f"{internal_api_base()}/api/users/{user_id}/sign-out", timeout=10)
    except requests.RequestException as e:
        # Only a refused connection means nothing listens. A timeout, a TLS or proxy failure
        # are also ConnectionErrors in requests, and they mean a VAF may run and hold the
        # sockets: those get the restart warning.
        return None if _refused(e) else f"no answer: {type(e).__name__}"
    if r.status_code == 200:
        return None
    return f"it answered {r.status_code}"


def _refused(exc: BaseException) -> bool:
    """Whether a requests error is a refused connection: a ConnectionRefusedError somewhere
    in its chain (requests wraps urllib3's MaxRetryError, which wraps NewConnectionError)."""
    seen, stack = set(), [exc]
    while stack:
        e = stack.pop()
        if not isinstance(e, BaseException) or id(e) in seen:
            continue                 # `reason` can be a string on some errors
        seen.add(id(e))
        if isinstance(e, ConnectionRefusedError):
            return True
        stack += [getattr(e, "reason", None), e.__cause__, e.__context__]
        stack += [a for a in getattr(e, "args", ()) if isinstance(a, BaseException)]
    return False


# ── sessions, signing out, tokens for programs ───────────────────────────────────────

_KIND_WORDS = {"browser": "browser", "this_machine": "this computer", "program": "program"}


def _ascii(text) -> str:
    """For a console that may be cp1252: anything outside ASCII becomes '?'."""
    return str(text or "").encode("ascii", "replace").decode("ascii")


def _day(iso) -> str:
    return str(iso or "")[:16].replace("T", " ") or "-"


@app.command("sessions")
def list_sessions(username: str = typer.Argument(..., help="The account whose sessions to show")):
    """The account's live sessions: browsers, this computer and tokens for programs."""
    from vaf.cli.gate import require_admin_password
    if not require_admin_password():
        raise typer.Exit(1)
    from vaf.auth import sessions
    from vaf.auth.database import get_auth_db

    async def _load():
        found = None
        async with get_auth_db() as db:
            from vaf.auth.user_admin import find_user
            user = await find_user(db, username)
            if user is not None:
                found = await sessions.list_account_sessions(db, user.id)
        return found

    try:
        rows = asyncio.run(_load())
    except Exception as e:
        UI.error(f"The sessions could not be read: {e}")
        raise typer.Exit(1)
    if rows is None:
        UI.error(f"No account named {username!r}. `vaf users list` shows them.")
        raise typer.Exit(1)
    if not rows:
        print(f"{_ascii(username)} has no live session.")
        return
    for s in rows:
        print(_ascii("\n".join(_session_lines(s))))


def _session_lines(s: dict) -> list:
    """One session as a heading and its facts on indented lines, the id last: on one line the
    row ran past 120 columns and wrapped mid-date in an 80-column terminal."""
    what = s["name"] if s["kind"] == "program" else s["device"]
    until = (f"ends {_day(s['idle_until'])} unless used, at the latest {_day(s['expires_at'])}"
             if s["idle_until"] else f"valid until {_day(s['expires_at'])}")
    return [f"{_KIND_WORDS.get(s['kind'], s['kind'])}: {what}",
            f"    {s['ip'] or 'no address'}, last used {_day(s['last_used_at'])}",
            f"    {until}",
            f"    id {s['id']}"]


@app.command("sign-out")
def sign_out(username: str = typer.Argument(..., help="The account to sign out everywhere")):
    """End every session of an account: its devices, this computer and its tokens for programs."""
    from vaf.cli.gate import require_admin_password
    if not require_admin_password():
        raise typer.Exit(1)
    from vaf.auth.database import get_auth_db
    from vaf.auth.sessions import end_account_sessions
    from vaf.auth.user_admin import find_user

    async def _end():
        async with get_auth_db() as db:
            user = await find_user(db, username)
            if user is None:
                return None
            return str(user.id), await end_account_sessions(db, user.id)

    try:
        done = asyncio.run(_end())
    except Exception as e:
        UI.error(f"The sessions could not be ended: {e}")
        raise typer.Exit(1)
    if done is None:
        UI.error(f"No account named {username!r}. `vaf users list` shows them.")
        raise typer.Exit(1)
    user_id, ended = done
    why_not = _close_open_connections(user_id)
    UI.success(f"{ended} session(s) of {_ascii(username)} ended.")
    if why_not is None:
        print("Its open chats are disconnected; every device signs in again.")
    else:
        UI.warning(f"VAF did not confirm that the account's open connections were closed ({why_not}). "
                   "A device with a chat still open keeps it until VAF restarts: restart VAF now.")


def _access_hint() -> tuple:
    """The address another machine reaches VAF by, for the example call (a placeholder when
    none is known), and which certificate it is served with there: ``automatic`` (VAF's own),
    ``own`` or ``none`` (TLS off, plain HTTP), as ``served_certificate`` says."""
    source = "automatic"
    try:
        from vaf.network.ssl_utils import served_certificate
        source = served_certificate().source
    except Exception:
        pass
    scheme, default_port = ("http", 80) if source == "none" else ("https", 443)
    try:
        from vaf.network.binding import access_addresses, resolve_lan_access_ports
        port, _ = resolve_lan_access_ports(wait_for_proxy=False)
        addresses = access_addresses()
        if addresses:
            return (f"{scheme}://{addresses[0].ip}" + ("" if port == default_port else f":{port}"),
                    source)
    except Exception:
        pass
    return f"{scheme}://<VAF's address>", source


def _token_use_lines(url: str, source: str) -> list:
    """How a program on another machine sends the token. Under VAF's own certificate curl must
    be given VAF's authority: without it curl refuses the self-signed chain, and the shortcut
    -k would send the token to whoever answers at that address."""
    call = f'-H "Authorization: Bearer <token>" {url}/api/auth/me'
    if source != "automatic":
        return [f"Use it as: curl {call}"]
    try:
        from vaf.network.ssl_utils import ca_certificate_path
        here = ca_certificate_path()
    except Exception:
        here = None
    return [f"Use it as: curl --cacert vaf-authority.pem {call}",
            "vaf-authority.pem is VAF's certificate authority: Settings, Local Network, "
            "Certificate, Download" + (f" (on this machine: {here})" if here else "") + ".",
            "Compare its fingerprint with the one `vaf server ca` prints. Not -k instead: "
            "it sends the token to whoever answers."]


@app.command("token")
def make_token(
    username: str = typer.Argument(..., help="The account the token acts as"),
    label: str = typer.Option(..., "--label", help="A name to recognise the token by (1-60 characters)"),
    days: int = typer.Option(30, "--days", help="How long it lives: 7, 30, 90 or 365"),
    recovery_key: bool = typer.Option(False, "--recovery-key",
                                      help="Prove it with the recovery key from VAF-BackThisUp.md "
                                           "instead of the admin password"),
):
    """A token for programs: a script sends it as Authorization: Bearer and acts as the account."""
    from vaf.auth import sessions
    try:
        name = sessions.program_token_name(label)
        days = sessions.program_token_days(days)
    except sessions.ProgramTokenRefused as e:
        UI.error(e.message)
        raise typer.Exit(1)
    proof = _proof(recovery_key, "token for programs")
    if not proof:
        raise typer.Exit(1)
    from vaf.auth.database import get_auth_db
    from vaf.auth.user_admin import find_user

    async def _mint():
        async with get_auth_db() as db:
            user = await find_user(db, username)
            if user is None or not user.is_active:
                return None
            return await sessions.mint_program_token(
                db, user, name=name, days=days, ip="127.0.0.1",
                where=f"a terminal on this machine ({proof})")

    try:
        minted = asyncio.run(_mint())
    except sessions.ProgramTokenRefused as e:
        UI.error(e.message)
        raise typer.Exit(1)
    except Exception as e:
        UI.error(f"The token could not be made: {e}")
        raise typer.Exit(1)
    if minted is None:
        UI.error(f"No active account named {username!r}. `vaf users list` shows them.")
        raise typer.Exit(1)
    token, _session = minted
    print(f"Token for programs '{_ascii(name)}', valid {days} days. It is shown only now:")
    print()
    print(token)
    print()
    for line in _token_use_lines(*_access_hint()):
        print(_ascii(line))
    print(f"The token acts as {_ascii(username)}. Revoke it in Settings, Account, or with "
          f"`vaf users sign-out {_ascii(username)}`.")
