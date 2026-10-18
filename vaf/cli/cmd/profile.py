# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf profile`: what this installation runs itself (vaf/core/inference_profile.py).

    vaf profile                  the profile, the services it runs, what contradicts it
    vaf profile api              no local models: the speech containers stop, nothing is
                                 downloaded; chat and speech go to the providers in Settings
    vaf profile api --prune      ... and their containers, images and volumes are removed
    vaf profile local            the local models may run again: the speech containers
                                 start, the chat model downloads on first use
    vaf profile --services core  the services to start, one per line (the installers)

Not behind the terminal door, like `vaf server`: what it changes is the machine's, not an
account's data, and an account that could run it from the agent's shell has `host_bash`,
which reaches the same config and the same docker directly.
"""
from pathlib import Path
from typing import Optional

import typer

from vaf.cli.ui import UI


def _gb(size: int) -> str:
    return f"{size / 1024 ** 3:.1f} GB"


def _local_models() -> int:
    """Bytes of GGUF files in the models folder: what `--prune` does not delete."""
    root = Path(__file__).resolve().parents[3] / "models"
    try:
        return sum(p.stat().st_size for p in root.rglob("*.gguf") if p.is_file())
    except OSError:
        return 0


def profile_cmd(
    name: Optional[str] = typer.Argument(None, help="local or api; empty shows the current one"),
    prune: bool = typer.Option(False, "--prune",
                               help="With api: also remove the speech containers' images and volumes"),
    no_stack: bool = typer.Option(False, "--no-stack",
                                  help="Only write the setting; leave the containers alone (installers)"),
    services: Optional[str] = typer.Option(None, "--services",
                                           help="Print the services to start: core or optional"),
):
    """What this installation runs itself: local models (local) or none (api)."""
    from vaf.core import inference_profile as ip
    from vaf.core import service_stack as ss

    if services is not None:
        if services not in ("core", "optional"):
            UI.error("--services takes core or optional.")
            raise typer.Exit(2)
        for key in (ss.needed_core_services() if services == "core" else ss.needed_optional_services()):
            print(key)
        return

    if name is None:
        current = ip.current()
        UI.info(f"Profile: {current} ("
                + ("local models and speech run on this machine" if current == ip.LOCAL
                   else "no local models; chat and speech through the providers in Settings")
                + ")")
        UI.info("Services: " + ", ".join(s.service_key for s in ss.needed_services()))
        left_out = [s.service_key for s in ss.SERVICES if s not in ss.needed_services()]
        if left_out:
            UI.info("Left out: " + ", ".join(left_out))
        for sentence in ip.conflicts():
            UI.warning(sentence)
        if current == ip.API and _local_models():
            UI.info(f"{_gb(_local_models())} of local models are still in the models folder; "
                    "delete them yourself when you need the space.")
        return

    try:
        wanted = ip.validate(name, prune)
    except ValueError as e:
        UI.error(str(e))
        raise typer.Exit(2)
    if wanted == ip.LOCAL and not no_stack:
        UI.info("Starting the speech containers (the first start downloads their images)...")
    try:
        result = ip.apply(wanted, prune=prune, stack=not no_stack)
    except Exception as e:
        # The key or its lock could not be written: nothing was switched. The installers
        # stop on this exit; a person gets the command to run again.
        retry = (f"vaf profile {wanted}" + (" --prune" if prune else "")
                 + (" --no-stack" if no_stack else ""))
        UI.error(f"The profile could not be switched ({e}). Run `{retry}` again once that "
                 "is fixed.")
        raise typer.Exit(1)
    UI.success(f"Profile set to {wanted}." + ("" if result["previous"] != wanted else " (unchanged)"))
    if no_stack:
        return
    if wanted == ip.API:
        if result["stopped"]:
            UI.info("The speech containers are stopped" + (" and removed." if prune else "."))
        for name_removed in result["removed"]:
            UI.info(f"Removed {name_removed}")
        if _local_models():
            UI.info(f"{_gb(_local_models())} of local models stay in the models folder; "
                    "delete them yourself when you need the space.")
    elif result["started"]:
        UI.success("The stack runs with the speech containers.")
    elif not result["errors"]:
        UI.info("No service stack here (no compose file), so there is nothing to start.")
    for problem in result["errors"]:
        UI.warning(problem)
    for sentence in result["conflicts"]:
        UI.warning(sentence)
    if result["errors"]:
        # The profile holds from the moment it is written; what did not happen is the
        # stop or start part of the switch (the containers, the local model server). The
        # retry names the same request, --prune included: a plain retry would stop the
        # containers and leave the images on the disk. A non-zero exit says so to the
        # installers, which warn on it.
        retry = f"vaf profile {wanted}" + (" --prune" if prune else "")
        UI.error(f"The switch is not complete: run `{retry}` again once the problem above "
                 "is fixed.")
        raise typer.Exit(1)
