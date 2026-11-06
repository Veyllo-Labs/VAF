# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf devices`: the paired Veyllo App devices of every account on this machine.

The terminal's door is the admin password, so the listing shows every account's devices.
A removal goes to the running VAF first (its link manager cuts the device off at once); with
nothing listening it is done here, and the relay member is deleted by the next running VAF.

NAMED BOUNDARY: pairing is not offered here. The one-time value K lives in the web server's
process and the "is this your device?" confirmation is a step of the signed-in account in the
web UI; a terminal pairing would need both moved out of that process.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import typer

from vaf.cli.ui import UI

app = typer.Typer(help="Paired Veyllo App devices.")


def ask_running_vaf(method: str, path: str, json: Optional[Dict[str, Any]] = None
                    ) -> Optional[Tuple[int, Any]]:
    """(status, body) from the running VAF over the loopback lane, as the machine owner, or
    None when nothing listens."""
    import requests

    from vaf.cli.cmd.users import _refused
    from vaf.core.web_interface import internal_api_base
    try:
        with requests.Session() as session:
            # Never through a proxy from the environment: this call does not leave the machine.
            session.trust_env = False
            r = session.request(method, f"{internal_api_base()}{path}", json=json, timeout=30)
    except requests.RequestException as e:
        if _refused(e):
            return None
        raise typer.BadParameter(f"VAF did not answer ({type(e).__name__}).") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body


def _when(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "-"


def _rows() -> Tuple[List[Dict[str, Any]], Optional[set]]:
    """Every device, and which are connected right now (None when VAF does not run)."""
    from vaf.core.app_link import devices
    rows = devices.all_devices()
    answer = ask_running_vaf("GET", "/api/app-link/devices?all=1")
    if answer is None or answer[0] != 200:
        return rows, None
    connected = {d["id"] for d in (answer[1] or {}).get("devices", []) if d.get("connected")}
    return rows, connected


@app.command("list")
def list_devices():
    """Every paired device: id, account, model, platform, paired, last seen, way, relay."""
    rows, connected = _rows()
    if not rows:
        UI.info("No Veyllo App is paired on this machine.")
        return
    header = f"{'ID':<10} {'USER':<14} {'MODEL':<18} {'PLATFORM':<9} {'PAIRED':<16} {'LAST SEEN':<16} {'WAY':<7} RELAY"
    print(header)
    for row in rows:
        way = row.get("last_transport") or "-"
        if connected is not None and row["device_id"] in connected:
            way += "*"
        print(f"{row['device_id'][:8]:<10} {str(row.get('username') or '-')[:14]:<14} "
              f"{str(row.get('model') or '-')[:18]:<18} {str(row.get('platform') or '-')[:9]:<9} "
              f"{_when(row.get('created_at')):<16} {_when(row.get('last_seen_at')):<16} "
              f"{way:<7} {'yes' if row.get('relay_member') else 'no'}")
    if connected is not None:
        print("* connected now")
    else:
        print("VAF is not running: the connection state is not known.")


@app.command("remove")
def remove_device(device: str = typer.Argument(..., help="The device id or its first 8 characters")):
    """Remove a device (a lost phone too): its access ends at once; it has to pair again."""
    from vaf.core.app_link import devices
    row = devices.resolve(device)
    if row is None:
        UI.error(f"No device matches '{device}' (`vaf devices list`).")
        raise typer.Exit(1)
    answer = ask_running_vaf("DELETE", f"/api/app-link/devices/{row['device_id']}")
    if answer is None:
        devices.remove_device(row["device_id"], by="", reason="removed in a terminal")
        UI.success(f"Removed {row.get('model') or 'the device'} of {row.get('username') or 'its account'}. "
                   "VAF is not running; its relay membership is deleted at the next start.")
        return
    if answer[0] != 200:
        body = answer[1] if isinstance(answer[1], dict) else {}
        detail = body.get("detail") if isinstance(body.get("detail"), str) else ""
        UI.error(detail or f"VAF refused the removal ({answer[0]}).")
        raise typer.Exit(1)
    UI.success(f"Removed {row.get('model') or 'the device'} of {row.get('username') or 'its account'}; "
               "it is cut off now.")
