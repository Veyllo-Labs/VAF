# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf secrets`: the credentials your agent's commands may use, from the terminal.

A value stored here reaches a host_bash command or python_exec code as `$VAF_SECRET_<NAME>`
and never the model (vaf/core/user_secrets.py). The command runs as the machine owner, so
there is no `--scope` (the rule of `vaf memory`, `vaf inbox` and `vaf outbox`), and the group
sits behind the same terminal door. `set` reads the value without echo, so it lands in no
shell history; `list` prints names, never a value.
"""
import getpass
import sys

import typer

from vaf.cli.ui import UI

app = typer.Typer(help="Credentials your agent's commands may use, never shown to the model.")


@app.callback()
def _group():
    """Credentials your agent's commands may use, never shown to the model.

    Deliberate: without a callback, Typer collapses a one-command app into that command."""


def _identity():
    from vaf.core.identity_binding import resolve_owner_identity
    ident = resolve_owner_identity()
    return ident.username, ident.scope


@app.command("list")
def list_names():
    """The stored names, as the agent uses them."""
    from vaf.core.user_secrets import ENV_PREFIX, names
    username, scope = _identity()
    stored = names(user_scope_id=scope, username=username)
    if not stored:
        UI.info("No credentials stored. Add one with: vaf secrets set NAME")
        return
    for name in stored:
        print(f"{ENV_PREFIX}{name}")


@app.command("set")
def set_value(name: str = typer.Argument(..., help="e.g. GPORTAL_FTP_PASSWORD")):
    """Store a value under NAME (asked for without echo; piped input works too)."""
    from vaf.core.user_secrets import ENV_PREFIX, set_secret
    if sys.stdin.isatty():
        value = getpass.getpass(f"Value for {name}: ")
    else:
        value = sys.stdin.readline().rstrip("\r\n")
    username, scope = _identity()
    try:
        stored = set_secret(name, value, user_scope_id=scope, username=username)
    except ValueError as e:
        UI.error(str(e))
        raise typer.Exit(1)
    UI.success(f"Stored. Your agent uses it as ${ENV_PREFIX}{stored}")


@app.command("rm")
def remove(name: str = typer.Argument(...)):
    """Delete the value stored under NAME."""
    from vaf.core.user_secrets import delete_secret
    username, scope = _identity()
    if not delete_secret(name, user_scope_id=scope, username=username):
        UI.error(f"No credential named {name}.")
        raise typer.Exit(1)
    UI.success(f"Deleted {name}.")
