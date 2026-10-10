# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf ftp`: the FTP servers your agent confirmed, from the terminal.

The agent's `ftp` tool keeps this account's OWN list of confirmed servers (vaf/core/ftp.py).
`servers` lists them with how their certificate is trusted (an authority, a remembered
fingerprint, or none for plain FTP); `forget` removes one, which is what a server whose
certificate changed needs - the next connection then asks again. The command runs as the
machine owner, like `vaf ssh`, behind the same terminal door.
"""
import typer

from vaf.cli.ui import UI

app = typer.Typer(help="The FTP servers your agent confirmed.")


@app.callback()
def _group():
    """The FTP servers your agent confirmed.

    Deliberate: without a callback, Typer collapses a one-command app into that command."""


def _scope():
    from vaf.core.identity_binding import resolve_owner_identity
    return resolve_owner_identity().scope


_TRUST = {"authority": "certificate authority", "pinned": "remembered certificate",
          "none": "NOT encrypted"}


@app.command("servers")
def servers():
    """The servers this account confirmed, and how each one's certificate is trusted."""
    from vaf.core import ftp
    try:
        rows = ftp.servers(_scope())
    except ftp.FtpError as e:
        UI.error(str(e))
        raise typer.Exit(1)
    if not rows:
        UI.info("No servers yet. The first connection the agent makes asks you first.")
        return
    for row in rows:
        trust = _TRUST.get(row["trust"], row["trust"])
        fingerprint = f"  {row['fingerprint']}" if row["fingerprint"] else ""
        print(f"{row['name']}  ({trust}){fingerprint}")


@app.command("forget")
def forget(name: str = typer.Argument(..., help="As `vaf ftp servers` prints it, e.g. ftps://ftp.example.org")):
    """Remove one server; the next connection to it asks again."""
    from vaf.core import ftp
    if not ftp.forget(name, _scope()):
        UI.error(f"No server named {name}.")
        raise typer.Exit(1)
    UI.success(f"Removed {name}.")
