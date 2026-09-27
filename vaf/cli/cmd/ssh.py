# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf ssh`: the SSH key and servers your agent uses, from the terminal.

The agent's `ssh` tool works with this account's OWN key and list of servers
(vaf/core/ssh.py), never your `~/.ssh`. `key` prints the public key (creating it the first
time) so you can put it on a server or into a hoster's panel; `hosts` lists the servers this
account confirmed, with their fingerprints; `forget` removes one, which is what a server that
was reinstalled needs. The command runs as the machine owner, like `vaf secrets`, and sits
behind the same terminal door. A key is never replaced here: a new one would lock the
account out of every server the old one is installed on.
"""
import typer

from vaf.cli.ui import UI

app = typer.Typer(help="The SSH key and servers your agent uses (never your ~/.ssh).")


@app.callback()
def _group():
    """The SSH key and servers your agent uses (never your ~/.ssh).

    Deliberate: without a callback, Typer collapses a one-command app into that command."""


def _scope():
    from vaf.core.identity_binding import resolve_owner_identity
    return resolve_owner_identity().scope


@app.command("key")
def key():
    """Print this account's public key (created the first time)."""
    from vaf.core import ssh
    scope = _scope()
    try:
        ssh.ensure_identity(scope)
    except ssh.SshError as e:
        UI.error(str(e))
        raise typer.Exit(1)
    print(ssh.public_key(scope))


@app.command("hosts")
def hosts():
    """The servers this account confirmed, with their key fingerprints."""
    from vaf.core import ssh
    try:
        rows = ssh.known_hosts(_scope())
    except ssh.SshError as e:
        UI.error(str(e))
        raise typer.Exit(1)
    if not rows:
        UI.info("No servers yet. The first connection the agent makes asks you first.")
        return
    for row in rows:
        print(f"{row['host']}  {row['fingerprint']}  ({row['type']})")


@app.command("forget")
def forget(host: str = typer.Argument(..., help="As `vaf ssh hosts` prints it, e.g. [203.0.113.7]:2222")):
    """Remove one server; the next connection to it asks again."""
    from vaf.core import ssh
    if not ssh.forget_host(host, _scope()):
        UI.error(f"No server named {host}.")
        raise typer.Exit(1)
    UI.success(f"Removed {host}.")
