# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf backup`: write, check and list the encrypted backups (vaf/core/backup.py).

Behind the terminal door like `vaf secrets`: a backup holds every key of the machine,
encrypted, and this machine can open it, so it is not something the agent's own commands
write or read. A script without a terminal (a cron job) passes the door as everywhere.
"""
from pathlib import Path
from typing import Optional

import typer

from vaf.cli.ui import UI

app = typer.Typer(help="Encrypted backups of everything VAF keeps")


@app.callback()
def _group():
    """Encrypted backups of everything VAF keeps."""


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
