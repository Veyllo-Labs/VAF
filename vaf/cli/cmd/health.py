# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf health` - is this installation healthy? The terminal's voice for vaf/core/health.py.

It asks the running VAF (`/api/system/health`), because one check exists only inside that
process: whether its agent loop still lives. When nothing answers, VAF is not running, which
is itself the first finding, and the checks a terminal can run (the services, the disk) run
here. The exit code follows monitoring plugins: 0 ok, 1 degraded, 2 down.
"""
import json
import sys
from typing import Any, Dict, Optional

import typer

from vaf.cli.ui import UI

_STYLE = {"ok": "green", "degraded": "yellow", "down": "red"}
EXIT_CODES = {"ok": 0, "degraded": 1, "down": 2}


def from_running_vaf(timeout: float = 30.0) -> Optional[Dict[str, Any]]:
    """The running VAF's answer, or None only when nothing listens on this machine. A VAF that
    listens and gives no health result (an error status, no answer in time) is reported as
    that, down: telling the operator to start a process that already runs sends them the
    wrong way."""
    import requests

    from vaf.core.web_interface import internal_api_base
    path = "/api/system/health"
    try:
        response = requests.get(f"{internal_api_base()}{path}", timeout=timeout)
    except requests.Timeout:
        return _vaf_down(f"VAF does not answer {path} within {timeout:.0f} seconds.")
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return _vaf_down(f"VAF runs, but {path} answered HTTP {response.status_code}.")
    try:
        result = response.json()
    except ValueError:
        result = None
    if isinstance(result, dict) and "status" in result:
        return result
    return _vaf_down(f"VAF runs, but {path} answered without a health result.")


def _vaf_down(reason: str) -> Dict[str, Any]:
    return {"status": "down", "checks": [{"name": "vaf", "status": "down", "reason": reason}]}


def collect() -> Dict[str, Any]:
    """The running VAF's health, or this terminal's checks plus the finding that VAF is down."""
    result = from_running_vaf()
    if result is not None:
        return result
    from vaf.core import health
    result = health.collect_health()
    result["checks"].insert(0, {"name": "vaf", "status": health.DOWN,
                                "reason": "VAF is not running (`vaf start`)."})
    result["status"] = health.worst(c["status"] for c in result["checks"])
    return result


def cmd_health(
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON"),
) -> None:
    """Is this installation healthy: the services, the disk and the agent loop.

    Exit code 0 ok, 1 degraded (VAF works, a part of it does not), 2 down.
    """
    result = collect()
    status = str(result.get("status") or "down")
    if json_output:
        # Straight to stdout: the Rich console wraps long lines inside JSON strings.
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        sys.stdout.flush()
    else:
        UI.print(f"Health: [{_STYLE.get(status, 'red')}]{status}[/]")
        for check in result.get("checks") or []:
            state = str(check.get("status") or "?")
            UI.print(f"  {str(check.get('name') or ''):9} [{_STYLE.get(state, 'red')}]{state:9}[/] "
                     f"{check.get('reason') or ''}")
    raise typer.Exit(EXIT_CODES.get(status, 2))
