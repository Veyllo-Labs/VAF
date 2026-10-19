# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf backup`: write, check and list the encrypted backups (vaf/core/backup.py).

Behind the terminal door like `vaf secrets`: a backup holds every key of the machine,
encrypted, and this machine can open it, so it is not something the agent's own commands
write or read. A script without a terminal (a cron job) passes the door as everywhere.

`restore` is the exception: it proves the person itself, by the admin password OR the
recovery key. The door would ask for exactly that password - the one that may be gone,
which is when the recovery key is the way in - and on a new machine without an account it
would offer to create one, which the restore then replaces. On a machine that has an admin
account, the recovery key counts only when it opens THIS machine's recovery file.
"""
from pathlib import Path
from typing import Optional

import typer

from vaf.cli.ui import UI

app = typer.Typer(help="Encrypted backups of everything VAF keeps")


@app.callback()
def _group(ctx: typer.Context):
    """Encrypted backups of everything VAF keeps."""
    if ctx.invoked_subcommand == "restore":
        return                  # its own checks: the agent, a terminal, then its proof
    from vaf.cli.gate import require_admin_password
    if not require_admin_password():
        raise typer.Exit(1)


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


@app.command("create")
def create(
    to: Optional[Path] = typer.Option(None, "--to", help="Folder to write it to (default: the backup folder)"),
    without_projects: bool = typer.Option(False, "--without-projects",
                                          help="Leave the working folders (VAF_Projects) out"),
    without_volumes: bool = typer.Option(False, "--without-volumes",
                                         help="Leave the sandbox and browser volumes out"),
):
    """Write a backup, verify it, and say who can open it."""
    from vaf.core.backup import BackupError, create_backup
    try:
        result = create_backup(to, include_projects=not without_projects,
                               include_volumes=not without_volumes,
                               progress=lambda msg: UI.info(msg + " ..."))
    except BackupError as e:
        UI.error(str(e))
        raise typer.Exit(1)
    UI.success(f"Backup written and verified: {result.path} ({_mb(result.size)}, "
               f"{result.members} files).")
    UI.info("It opens with: " + ", ".join(result.recipients) + ".")
    for warning in result.warnings:
        UI.warning(warning)
    if result.skipped:
        UI.info(f"Not VAF's, so not taken ({len(result.skipped)}):")
        for line in result.skipped[:20]:
            print(f"  {line}")
        if len(result.skipped) > 20:
            print(f"  ... and {len(result.skipped) - 20} more")


@app.command("verify")
def verify(
    path: Path = typer.Argument(..., help="The backup file"),
    recovery_key: bool = typer.Option(False, "--recovery-key",
                                      help="Open it with the recovery key instead of this machine's key"),
):
    """Open a backup and check every file in it against its checksum."""
    import getpass

    from vaf.core.backup import verify_backup
    identity = None
    if recovery_key:
        identity = getpass.getpass("Recovery key (from VAF-BackThisUp.md): ").strip()
    result = verify_backup(path, identity)
    if not result.ok:
        for problem in result.problems:
            UI.error(problem)
        raise typer.Exit(1)
    made = result.manifest.get("created_at", "?")
    version = result.manifest.get("vaf_version", "?")
    UI.success(f"Intact: {result.members} files, written {made} by VAF {version}.")


@app.command("list")
def list_cmd(
    where: Optional[Path] = typer.Option(None, "--in", help="Folder to look in (default: the backup folder)"),
):
    """The backups in the backup folder, newest first."""
    from vaf.core.backup import backup_dir, list_backups
    folder = where or backup_dir()
    rows = list_backups(folder)
    if not rows:
        UI.info(f"No backups in {folder}. `vaf backup create` writes one.")
        return
    for row in rows:
        print(f"{row.created}  {_mb(row.size):>10}  {row.path.name}")


@app.command("schedule")
def schedule_cmd(
    when: Optional[str] = typer.Argument(None, help="daily, weekly (on Sunday) or off; empty shows the schedule"),
    at: Optional[str] = typer.Option(None, "--at", help="Time of day, HH:MM in your own timezone"),
):
    """Backups that run by themselves while VAF runs: the schedule, the last run, the retention."""
    from vaf.core import backup_schedule as bs
    from vaf.core.config import Config
    if when is not None:
        wanted = when.strip().lower()
        if wanted not in (*bs.SCHEDULES, "off"):
            UI.error("Use daily, weekly or off.")
            raise typer.Exit(2)
        Config.set("backup_schedule", wanted)
    if at is not None:
        try:
            hour, minute = (int(part) for part in at.strip().split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError(at)
        except ValueError:
            UI.error("--at takes a time of day like 03:00.")
            raise typer.Exit(2)
        Config.set("backup_time", f"{hour:02d}:{minute:02d}")
    kind = bs.schedule()
    hour, minute = bs.slot_time()
    if kind is None:
        UI.info("No backups are scheduled. `vaf backup schedule daily` turns them on.")
    else:
        day = " on Sundays" if kind == "weekly" else ""
        UI.info(f"Scheduled {kind}{day} at {hour:02d}:{minute:02d} (your timezone), while VAF runs.")
        keep = bs.keep_counts()
        UI.info(f"Kept: the newest backup of each of the last {keep['daily']} days, "
                f"{keep['weekly']} weeks and {keep['monthly']} months.")
    state = bs.read_state()
    if state.get("last_success_at"):
        UI.info(f"Last scheduled backup: {state['last_success_at']} ({state.get('last_file', '')}).")
    if state.get("last_error"):
        UI.warning(f"Last scheduled run failed at {state.get('last_attempt_at')}: {state['last_error']}")
    check = bs.backup_check()
    if check["status"] != "ok":
        UI.warning(check["reason"])


@app.command("prune")
def prune_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Name what would be deleted, delete nothing"),
):
    """Delete the backups the retention does not keep (backup_keep_daily/weekly/monthly)."""
    from vaf.core import backup_schedule as bs
    from vaf.core.backup import BackupError, prune_backups
    try:
        gone = prune_backups(dry_run=dry_run, **bs.keep_counts())
    except BackupError as e:
        UI.error(str(e))
        raise typer.Exit(1)
    if not gone:
        UI.info("Nothing to delete: every backup is one the retention keeps.")
        return
    verb = "Would delete" if dry_run else "Deleted"
    for path in gone:
        print(f"{verb} {path.name}")


@app.command("restore")
def restore(
    path: Path = typer.Argument(..., help="The backup file"),
    recovery_key: bool = typer.Option(False, "--recovery-key",
                                      help="Open it with the recovery key (on a new machine)"),
    no_safety_backup: bool = typer.Option(False, "--no-safety-backup",
                                          help="Do not write a backup of the current state first"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
):
    """Replace everything VAF keeps on this machine with a backup's."""
    import getpass

    from vaf.cli.gate import _stored_hash, is_interactive, refuse_agent_command, require_admin_password
    from vaf.core.backup import BackupError, read_manifest, restore_backup
    from vaf.core.instance import find_service

    # It replaces every account, key and chat: a person at this terminal, never a script.
    if refuse_agent_command():
        raise typer.Exit(1)
    if not is_interactive():
        UI.error("A restore needs a person at the terminal: it does not run from a script or "
                 "the agent's shell.")
        raise typer.Exit(1)
    if find_service() is not None:
        UI.error("VAF is running. Stop it first (`vaf stop`): a running VAF would write into the "
                 "folders while they are replaced.")
        raise typer.Exit(1)
    identity = None
    stored, unreadable = _stored_hash()
    if recovery_key:
        identity = getpass.getpass("Recovery key (from VAF-BackThisUp.md): ").strip()
        # A key that opens the backup proves nothing about THIS machine: anyone can write a
        # backup to a key of their own. Where an admin account exists (or may: an unreadable
        # keyring counts), the key must open this machine's recovery file, or the admin
        # password proves the person instead. A new machine has neither to check against.
        if stored or unreadable:
            from vaf.core.recovery_kit import unwrap_with_secret
            if not identity or unwrap_with_secret(identity) is None:
                UI.warning("That is not this machine's recovery key, and this machine has an "
                           "admin account: prove it is you with the admin password.")
                if not require_admin_password(force=True):
                    raise typer.Exit(1)
    else:
        if unreadable or not stored:
            UI.error("This machine has no admin password to prove who you are. Restore with "
                     "--recovery-key.")
            raise typer.Exit(1)
        if not require_admin_password():
            raise typer.Exit(1)
    try:
        manifest = read_manifest(path, identity)
    except BackupError as e:
        UI.error(str(e))
        raise typer.Exit(1)
    includes = manifest.get("includes") or {}
    UI.info(f"Backup of {manifest.get('host', '?')}, written {manifest.get('created_at', '?')} by "
            f"VAF {manifest.get('vaf_version', '?')}; working folders "
            f"{'included' if includes.get('projects', True) else 'left out'}, volumes "
            f"{'included' if includes.get('volumes', True) else 'left out'}.")
    if not yes and not typer.confirm(
        "Replace everything VAF keeps on this machine with this backup?"
        + ("" if no_safety_backup else " A backup of the current state is written first.")
    ):
        UI.info("Cancelled.")
        raise typer.Exit(0)
    try:
        result = restore_backup(path, identity=identity, pre_restore=not no_safety_backup,
                                progress=lambda msg: UI.info(msg + " ..."))
    except BackupError as e:
        UI.error(str(e))
        raise typer.Exit(1)
    UI.success(f"Restored: {result.files} files, the database, {result.volumes} volumes.")
    if result.rewrapped:
        UI.info("Opened with this machine's key: " + ", ".join(result.rewrapped) + ".")
    if result.pre_restore:
        UI.info(f"The state before the restore is in {result.pre_restore}.")
    for warning in result.warnings:
        UI.warning(warning)
    UI.info("Start VAF again. `vaf secure status` shows the keys.")
