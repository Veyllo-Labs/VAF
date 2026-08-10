# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf secure` - what protects the stored data, and the one rotation that needs a human.

`status` answers the question nobody could answer before: where does each key
live, is anything still lying in config.json, and are the shipped default
database passwords still in use. It prints no secret, only locations.

`rotate-db` replaces the published default Postgres password. Deliberately a
command and not a startup step: rotating the password of a database the app is
mid-connection with, and half-succeeding, locks the user out of their own
memories. Here it is explicit, verified before it is persisted, and reversible
because the old value is printed nowhere but known to the operator.
"""
import secrets

import typer

from vaf.cli.ui import UI

app = typer.Typer(help="Encryption, keys and database credentials")


@app.callback()
def _group():
    """Encryption, keys and database credentials.

    Deliberate: without a callback, Typer collapses a one-command app into that
    command."""


@app.command()
def status():
    """Show where the keys live and what is still unprotected."""
    from vaf.core.config import Config
    from vaf.core.data_keyring import ring_status

    info = ring_status()

    backend = {
        "keyring": "OS keyring (protected by your login password)",
        "file": "file, owner-only (protected by disk encryption underneath)",
        "config": "config.json - NOT yet moved",
        "none": "not created yet",
    }.get(info["kek_backend"], info["kek_backend"])
    UI.info(f"Master key (KEK): {backend}")
    UI.info(f"Key store:        {info['store_path']}")

    if info["unreadable"]:
        UI.error("The key store exists but cannot be opened. Encrypted data stays "
                 "locked until the master key is reachable again.")
    else:
        UI.info(f"Keys inside:      {', '.join(info['entries']) or '(none yet)'}")

    if info["legacy_in_config"]:
        UI.warning("Still in plaintext in config.json (they move on next use): "
                   + ", ".join(info["legacy_in_config"]))
    else:
        UI.success("No key material left in config.json.")

    encrypted = bool(Config.get("file_encryption_enabled", True))
    UI.info(f"Chats and files:  {'encrypted at rest' if encrypted else 'PLAINTEXT (switched off)'}")

    dsn = str(Config.get("memory_db_url", "") or "")
    if "vaf_dev_secret" in dsn or "vaf_app_dev_secret" in dsn:
        UI.warning("The memory database still uses the shipped default password. "
                   "Run `vaf secure rotate-db`.")
    else:
        UI.success("The memory database password is not the shipped default.")

    from vaf.core.recovery_kit import kit_path, recovery_wrap_path
    if recovery_wrap_path().exists():
        UI.success(f"Recovery key set up (file: {recovery_wrap_path().name}).")
        if kit_path().exists():
            UI.warning(f"The recovery note is still on this machine: {kit_path()} - "
                       f"it is a key in plain text. Move it somewhere else and delete it here.")
    else:
        UI.warning("No recovery key yet - a reinstall would make the encrypted data "
                   "unreadable. It is created with the first key.")

    # Name the master key's ACTUAL location. Listing both places would send half
    # the readers to back up a file that is not there, and a backup that misses
    # the master key is indistinguishable from no backup at all.
    from vaf.core.secure_store import _CONFIG_KEK_NAME, _kek_file_path
    where = {
        "keyring": f"OS keyring entry 'vaf/{_CONFIG_KEK_NAME}'",
        "file": str(_kek_file_path()),
        "config": "config.json - it moves out on next use",
    }.get(info["kek_backend"], "not created yet")
    UI.info("")
    UI.info(f"Back up together, or the data is unrecoverable: {info['store_path']}, "
            f"its .key.json sibling, and the master key ({where}).")


@app.command()
def recover(
    key: str = typer.Option("", "--key", help="The recovery key from VAF-BackThisUp.md"),
):
    """Put the data key back after a reinstall, using the recovery key."""
    import getpass

    from vaf.core.data_keyring import _ring
    from vaf.core.recovery_kit import recovery_wrap_path, unwrap_with_secret
    from vaf.core.secure_store import _machine_kek

    if not recovery_wrap_path().exists():
        UI.error(f"No recovery file at {recovery_wrap_path()}. Restore it from your "
                 f"backup together with data_keys.enc, then run this again.")
        raise typer.Exit(1)

    secret = key.strip() or getpass.getpass("Recovery key: ").strip()
    if not secret:
        UI.error("Nothing entered.")
        raise typer.Exit(1)

    dek = unwrap_with_secret(secret)
    if dek is None:
        UI.error("That key does not open the recovery file.")
        raise typer.Exit(1)

    # Re-wrap the recovered DEK under THIS machine's key, so normal operation
    # resumes without the recovery key.
    if _machine_kek(create=True) is None:
        UI.error("The machine key is not reachable, so the recovered key cannot be "
                 "stored. Run inside your desktop session and try again.")
        raise typer.Exit(1)
    ring = _ring()
    ring._wrap_and_store_dek(dek)
    ring._dek_cache = dek
    try:
        entries = sorted(ring.load_strict().keys())
    except Exception as e:
        UI.error(f"The key was restored but the store still does not open: {e}")
        raise typer.Exit(1)

    UI.success(f"Recovered. Keys available again: {', '.join(entries) or '(none)'}")
    UI.info("Run `vaf secure status` to confirm, and keep your recovery key.")


@app.command("rotate-db")
def rotate_db(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
):
    """Replace the Postgres password with a fresh random one."""
    import asyncio

    from vaf.core.config import Config

    dsn = str(Config.get("memory_db_url", "") or "")
    if not dsn:
        UI.error("No memory_db_url configured.")
        raise typer.Exit(1)

    if not yes and not typer.confirm(
        "Rotate the memory database password? VAF must be able to reach the "
        "database right now, and any other client using the old password will "
        "stop working."
    ):
        UI.info("Cancelled.")
        raise typer.Exit(0)

    new_password = secrets.token_urlsafe(24)

    async def _rotate() -> str:
        from urllib.parse import urlsplit, urlunsplit

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        parts = urlsplit(dsn)
        role = parts.username or "vaf"
        engine = create_async_engine(dsn, poolclass=None)
        try:
            async with engine.begin() as conn:
                # Parameter binding is not allowed in ALTER ROLE; the password is
                # generated here from token_urlsafe, so it carries no quotes.
                await conn.execute(text(f"ALTER ROLE {role} WITH PASSWORD '{new_password}'"))
        finally:
            await engine.dispose()

        netloc = f"{role}:{new_password}@{parts.hostname or 'localhost'}"
        if parts.port:
            netloc += f":{parts.port}"
        rotated = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

        # Verify BEFORE persisting: a config pointing at a password that does not
        # work is the lockout this command exists to avoid.
        verify = create_async_engine(rotated, poolclass=None)
        try:
            async with verify.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await verify.dispose()
        return rotated

    try:
        rotated = asyncio.run(_rotate())
    except Exception as e:
        UI.error(f"Rotation failed, nothing was changed in the config: {type(e).__name__}: {e}")
        raise typer.Exit(1)

    Config.set("memory_db_url", rotated)
    owner = str(Config.get("memory_db_owner_url", "") or "")
    if owner and "vaf_dev_secret" in owner:
        UI.warning("memory_db_owner_url still carries the old default password - "
                   "update it by hand if you use a separate owner role.")
    UI.success("Database password rotated and verified. Restart VAF so every worker "
               "picks up the new credentials.")
