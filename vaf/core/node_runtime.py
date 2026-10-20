# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Node.js for VAF's own child processes, found where the installers put it.

The web UI (Next.js), the WhatsApp bridge and the web step of `vaf update` run npm and node.
The JavaScript linter does not use this, deliberately (vaf/tools/linter.py says why). When the system has no Node, the installers bring a portable one: install.sh into
``~/.vaf/node`` (its programs in ``bin/``), install.ps1 into
``%LOCALAPPDATA%\\Veyllo\\node\\<version>``. Each puts that folder on the PATH of the
shells it knows about, and a process started any other way does not see it: the systemd
user unit of a server install, a desktop shortcut before the next login. Measured on a
fresh server install (Ubuntu 24.04, no system Node): the service logged "npm not found in
PATH" and every page of the web UI answered 502, after a reboot as well.

`find(name)` answers from PATH first, then from those folders. A tool found there puts
its folder on this process's PATH, because npm starts node by name
(`#!/usr/bin/env node`) and every child has to find it the same way.

Not on the facade: which Node an installer brings is VAF's installation, not an
embedder's.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import List, Optional


def portable_dirs() -> List[Path]:
    """The folders the installers put a portable Node in, in the order they are tried."""
    from vaf.core.platform import Platform
    if Platform.is_windows():
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "Veyllo" / "node"
        if not root.is_dir():
            return []
        # install.ps1 removes the folder before it unpacks a Node, so there is one version
        # below it; should there be more (one unpacked by hand), the newest one is tried first.
        return sorted((p for p in root.iterdir() if p.is_dir()), key=_version, reverse=True)
    return [Platform.vaf_dir() / "node" / "bin"]


def _version(folder: Path) -> tuple:
    """(major, minor, patch) from a folder like ``node-v22.11.0-win-x64``; (0, 0, 0) for any
    other name, which sorts it last."""
    match = re.search(r"v(\d+)\.(\d+)\.(\d+)", folder.name)
    return tuple(int(n) for n in match.groups()) if match else (0, 0, 0)


def find(name: str) -> Optional[str]:
    """The path of ``name`` (``node``, ``npm``, ``npx``), or None when there is none."""
    found = shutil.which(name)
    if found:
        return found
    for folder in portable_dirs():
        hit = shutil.which(name, path=str(folder))
        if hit:
            os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")
            return hit
    return None
