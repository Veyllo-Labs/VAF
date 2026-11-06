# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf relay`: the Veyllo relay that carries the Veyllo App link from anywhere.

Each command asks the running VAF first and works on this machine's files when nothing
listens. `dissolve` runs here in either case: the terminal's door (the admin password)
stands where the web route asks for the admin's 2FA code; a running VAF sees the secret gone
within a second and closes its relay connection.
"""
import asyncio

import typer

from vaf.cli.cmd.devices import ask_running_vaf
from vaf.cli.ui import UI

app = typer.Typer(help="The Veyllo relay for the Veyllo App.")

_NEVER = ("The web UI, the API and the sign-in are never reachable through the relay: only the "
          "paired devices' end-to-end encrypted link passes it.")


def _detail(answer, fallback: str) -> str:
    """The reason the running VAF gave (`detail`), or the fallback with the status code."""
    body = answer[1] if isinstance(answer[1], dict) else {}
    detail = body.get("detail")
    return str(detail) if isinstance(detail, str) and detail else f"{fallback} ({answer[0]})."


def _local_status():
    from vaf.core.app_link import relay
    status = relay.connector().status()
    status["state"] = "off" if not status["enabled"] else "VAF not running"
    return status


@app.command("status")
def relay_status():
    """On or off, connected or not, the instance, the devices on the relay."""
    from vaf.core.health import app_link_check
    answer = ask_running_vaf("GET", "/api/app-link/relay")
    running = answer is not None and answer[0] == 200
    status = answer[1] if running else _local_status()
    on = "on" if status.get("enabled") else "off"
    print(f"Relay:            {on}{'' if status.get('instance') else ' (not set up yet)'}")
    # The same sentence `vaf health` shows for this state, not a second wording of it.
    row = app_link_check(status) if running else None
    print(f"State:            {row['reason'] if row else status.get('state')}")
    print(f"Relay secret:     {'stored' if status.get('secret') else 'none'}")
    print(f"Devices on relay: {status.get('members', 0)}")
    print(_NEVER)


def _switch(enabled: bool) -> None:
    answer = ask_running_vaf("PUT", "/api/app-link/relay", {"enabled": enabled})
    if answer is None:
        from vaf.core.app_link import relay
        relay.set_relay_enabled(enabled, username="")
    elif answer[0] != 200:
        UI.error(_detail(answer, "VAF refused the change"))
        raise typer.Exit(1)
    if enabled:
        UI.success("The relay is on. VAF connects to it only while a device needs it.")
    else:
        UI.success("The relay is off: no relay connection, no relay QR codes, no push notices. "
                   "Devices keep the direct path; switching it on again needs no new pairing.")


@app.command("on")
def relay_on():
    """Switch the relay on."""
    _switch(True)


@app.command("off")
def relay_off():
    """Switch the relay off (the instance and the devices stay)."""
    _switch(False)


@app.command("rotate")
def relay_rotate():
    """Replace the relay secret; the old one stops at once, devices stay paired."""
    answer = ask_running_vaf("POST", "/api/app-link/relay/rotate")
    if answer is None:
        from vaf.core.app_link import relay
        connector = relay.connector()
        if not asyncio.run(connector.rotate(username="")):
            UI.error(relay.admin_failure(connector.last_error))
            raise typer.Exit(1)
    elif answer[0] != 200:
        UI.error(_detail(answer, "The relay did not rotate the secret"))
        raise typer.Exit(1)
    UI.success("The relay secret is replaced.")


@app.command("dissolve")
def relay_dissolve(yes: bool = typer.Option(False, "--yes", help="Do not ask")):
    """Delete the relay instance; devices keep the direct path."""
    from vaf.core.app_link import relay
    connector = relay.connector()
    if not connector.set_up():
        UI.error(relay.admin_failure("not_set_up"))
        raise typer.Exit(1)
    UI.warning("This deletes the relay instance: every device loses the relay and has to pair "
               "again to use it. The direct path keeps working.")
    if not yes and not typer.confirm("Dissolve the relay connection now?"):
        UI.info("Nothing was changed.")
        raise typer.Exit(0)
    if not asyncio.run(connector.dissolve(username="")):
        UI.error(relay.admin_failure(connector.last_error))
        raise typer.Exit(1)
    UI.success("The relay connection is dissolved.")
