# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf secure` - what protects the stored data, and the one rotation that needs a human.

`status` answers the question nobody could answer before: where does each key
live, is anything still lying in config.json, and are the shipped default
database passwords still in use. It prints no secret, only locations.

`renew-recovery-key` replaces the recovery key with one that also carries the
machine key, so a version 1 kit (the key store only) can be brought up to what
`recover` needs to open every encrypted store on a new machine.

`rotate-db` replaces the published default Postgres password. Deliberately a
command and not a startup step: rotating the password of a database the app is
mid-connection with, and half-succeeding, locks the user out of their own
memories. Here it is explicit, verified before it is persisted, and reversible
because the old value is printed nowhere but known to the operator.
"""

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

    # Both DSNs: the owner (DDL) role is the stronger one, and checking only
    # the app DSN reads "all clear" while the superuser still has the
    # published secret (exactly the state a half-done rotation left behind).
    from vaf.core.db_credentials import shipped_password_in_use
    if shipped_password_in_use():
        UI.warning("The memory database still uses the shipped default password. "
                   "Run `vaf secure rotate-db`.")
    else:
        UI.success("The memory database password is not the shipped default.")

    from vaf.core.recovery_kit import kit_covers_machine_key, kit_path, kit_version, recovery_wrap_path
    from vaf.core.secure_store import _get_passphrase
    if recovery_wrap_path().exists():
        if kit_version() == 2 and _get_passphrase():
            UI.success(f"Recovery key set up (file: {recovery_wrap_path().name}); the password "
                       "stores are sealed under your passphrase, which it does not replace.")
        elif kit_covers_machine_key():
            UI.success(f"Recovery key set up (file: {recovery_wrap_path().name}); it opens the "
                       "key store and every password store.")
        elif kit_version() == 2:
            UI.success(f"Recovery key set up (file: {recovery_wrap_path().name}); the password "
                       "stores are sealed under your passphrase, which it does not replace.")
        else:
            UI.warning("The recovery key opens the key store only: API keys, mail passwords and "
                       "stored credentials would be lost with this machine. Run "
                       "`vaf secure renew-recovery-key` in a terminal here.")
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
    from vaf.core.recovery_kit import recovery_wrap_path, unwrap_keys
    from vaf.core.secure_store import _machine_kek, rewrap_from_kek

    if not recovery_wrap_path().exists():
        UI.error(f"No recovery file at {recovery_wrap_path()}. Restore it from your "
                 f"backup together with data_keys.enc, then run this again.")
        raise typer.Exit(1)

    secret = key.strip() or getpass.getpass("Recovery key: ").strip()
    if not secret:
        UI.error("Nothing entered.")
        raise typer.Exit(1)

    keys = unwrap_keys(secret)
    if keys is None:
        UI.error("That key does not open the recovery file.")
        raise typer.Exit(1)
    dek = keys.dek

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

    # The other encrypted stores (API keys, mail passwords, stored credentials) were sealed
    # under the old machine's key, which a version 2 kit carries: re-wrap each one this
    # machine's key does not open yet. A version 1 kit carries only the key store's key.
    if keys.kek:
        try:
            rewrapped = rewrap_from_kek(keys.kek)
        except Exception as e:
            UI.error(f"The password stores could not be re-wrapped: {e}")
            raise typer.Exit(1)
        UI.success("Password stores opened again: " + (", ".join(rewrapped) or
                                                      "none needed it"))
        # They are sealed under THIS machine's key now: the recovery key must carry it, or
        # the next recovery opens the key store alone.
        try:
            from vaf.core.recovery_kit import refresh_recovery_wrap
            if not refresh_recovery_wrap(dek):
                raise RuntimeError("the recovery file is gone or no longer version 2")
        except Exception as e:
            UI.warning(f"The recovery key still carries the old machine's key ({e}), so it "
                       "would bring back the key store but not the password stores. Run "
                       "`vaf secure renew-recovery-key`.")
    elif keys.version == 1:
        UI.warning("This recovery key is version 1 and opens the key store only. API keys, mail "
                   "passwords and stored credentials of the old machine stay sealed unless you "
                   "also restored its machine key (secure_store.kek).")
    UI.info("Run `vaf secure status` to confirm, and keep your recovery key.")


@app.command("renew-recovery-key")
def renew_recovery_key(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
):
    """Replace the recovery key with a new one that opens every encrypted store."""
    from vaf.cli.gate import is_interactive, refuse_agent_command, require_admin_password

    # It writes a new secret in plain text, so: a person at this terminal, proved.
    if refuse_agent_command():
        raise typer.Exit(1)
    if not is_interactive():
        UI.error("A new recovery key needs a person at the terminal: it does not run from a "
                 "script or the agent's shell.")
        raise typer.Exit(1)
    if not require_admin_password(force=True):
        raise typer.Exit(1)
    if not yes and not typer.confirm(
        "Write a new recovery key? The current one stops opening the key store, so keep the "
        "new note where the old one was."
    ):
        UI.info("Cancelled.")
        raise typer.Exit(0)

    from vaf.core.data_keyring import _ring
    from vaf.core.recovery_kit import kit_covers_machine_key, renew_recovery_kit
    dek = _ring()._get_dek(create=False)
    if dek is None:
        UI.error("There is no key store on this machine yet, so there is nothing to recover.")
        raise typer.Exit(1)
    try:
        path = renew_recovery_kit(dek)
    except Exception as e:
        UI.error(str(e))
        raise typer.Exit(1)
    from vaf.core.secure_store import _get_passphrase
    covers = ("the key store and every password store"
              if kit_covers_machine_key() and not _get_passphrase()
              else "the key store; the password stores stay sealed under your passphrase")
    UI.success(f"New recovery key written to {path}. It opens {covers}.")
    UI.warning("Move that file off this machine and delete it here. The old key no longer "
               "opens the key store, but backups written before now open only with the old "
               "key or this machine: run `vaf backup create` for one the new key opens.")


@app.command("rotate-db")
def rotate_db(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
):
    """Replace the Postgres password with a fresh random one."""
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

    from vaf.core.db_credentials import RotationUnsaved, rotate_and_save, shipped_password_in

    try:
        rotate_and_save("memory_db_url", dsn)
    except RotationUnsaved as e:
        # The password changed and was not saved; the error carries the only copy.
        UI.error(str(e))
        raise typer.Exit(1)
    except Exception as e:
        UI.error(f"Rotation failed, nothing was changed in the config: {type(e).__name__}: {e}")
        raise typer.Exit(1)

    # The owner role gets the same treatment, not a warning. A default install
    # has TWO roles since the RLS cutover - vaf_app for data, vaf as owner for
    # DDL - and the owner is the more powerful of the two. Rotating only the
    # app role removes the shipped password from the weaker account and leaves
    # it on the stronger one; live run 2026-08-13 ended exactly there, with a
    # superuser still reachable by the published secret. Failure here does not
    # roll back the app rotation: each DSN is verified before it is persisted,
    # so the config never points at a password that does not work.
    owner = str(Config.get("memory_db_owner_url", "") or "")
    if owner and shipped_password_in(owner):
        try:
            rotate_and_save("memory_db_owner_url", owner)
            UI.success("Owner role rotated and verified as well.")
        except RotationUnsaved as e:
            # Not "still has the shipped password": it does not any more, and the error
            # carries the only copy of the new one.
            UI.error(f"The app role IS rotated. {e}")
            raise typer.Exit(1)
        except Exception as e:
            UI.error(f"Owner role rotation failed ({type(e).__name__}: {e}). The app "
                     "role IS rotated; the owner role still has the shipped password. "
                     "Fix the cause and run this command again.")
            raise typer.Exit(1)
    UI.success("Database password rotated and verified. Restart VAF so every worker "
               "picks up the new credentials.")
