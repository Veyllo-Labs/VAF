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

`find(name)` answers from PATH first, then from those folders. npm starts node by name
(`#!/usr/bin/env node`), so a child that runs a tool found there needs its folder on PATH:
`child_env(tool)` is that child's environment. This process's own PATH is left alone,
deliberately: everything else it starts would see the portable Node too, the JavaScript
linter included, which stays on PATH alone for the reason vaf/tools/linter.py gives.

`lock_drift(project_dir)` answers whether an npm tree on disk is the one its
``package-lock.json`` pins, for every package and not one sample: a patched package deep in
the tree is the usual security bump, and a check of only the top-level dependency let three
of those pass an installed WhatsApp bridge by. It reads the real ``package.json`` of each
package, never npm's own ``node_modules/.package-lock.json``, because `npm ci --dry-run`
rewrites that manifest without unpacking anything. The WhatsApp bridge's dependency install
and the web stage of scripts/ci_check.sh both ask it.

Not on the facade: which Node an installer brings, and whether VAF's own bundled npm trees
match their locks, is VAF's installation, not an embedder's.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Mapping, NamedTuple, Optional


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
            return hit
    return None


def child_env(tool: str, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment for a child that runs ``tool`` (a path `find` returned): ``base``, this
    process's environment by default, with the tool's folder first on PATH when it is not on
    it yet. A tool found on PATH leaves PATH exactly as it was."""
    env = dict(os.environ if base is None else base)
    folder = str(Path(tool).parent)
    path = env.get("PATH", "")
    if folder not in path.split(os.pathsep):
        # The rest stays byte for byte: an empty entry means the working directory on POSIX,
        # so dropping one (or adding one to an empty PATH) would change what the child finds.
        env["PATH"] = folder + os.pathsep + path if path else folder
    return env


class NpmDrift(NamedTuple):
    """One package where the tree on disk and the lockfile disagree. ``name`` is the lock key
    (``node_modules/a/node_modules/b``); ``installed`` is "" when nothing is on disk,
    ``locked`` is "" when the lock does not name the package at all."""
    name: str
    installed: str
    locked: str


# Entries npm may leave out on this machine: platform builds and optional dependencies.
_MAY_BE_ABSENT = ("optional", "devOptional", "os", "cpu", "libc")


def _disk_version(package_dir: Path) -> str:
    try:
        data = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("version") or "") if isinstance(data, dict) else ""


def _installed_packages(project_dir: Path) -> Dict[str, Path]:
    """Every package directory of the real tree, keyed the way the lock keys it.

    The keys are joined with "/" from directory entry names, never from str(Path): the lock
    uses "/" on every platform, and str() of a Windows path does not."""
    found: Dict[str, Path] = {}
    pending = [(project_dir / "node_modules", "node_modules/")]
    while pending:
        folder, prefix = pending.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            # .bin holds links, .package-lock.json is npm's manifest, .cache is a cache.
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            path = Path(entry.path)
            if entry.name.startswith("@"):
                pending.append((path, f"{prefix}{entry.name}/"))
                continue
            if not (path / "package.json").is_file():
                continue
            key = prefix + entry.name
            found[key] = path
            pending.append((path / "node_modules", f"{key}/node_modules/"))
    return found


def lock_drift(project_dir: Path, *, include_dev: bool) -> Optional[List[NpmDrift]]:
    """Where ``project_dir/node_modules`` differs from ``project_dir/package-lock.json``.

    ``[]`` means the tree is what the lock pins. ``None`` means this cannot be told: the lock
    is missing, unreadable, not JSON, or has no ``packages`` table (lockfile v1). Never raises.

    A package counts when its version on disk differs from the locked one (whatever its
    flags), when a required one is not on disk (an absent ``optional``, ``devOptional`` or
    os/cpu/libc-gated entry is a platform build this machine does not get; a ``peer`` without
    ``optional`` is required, npm 7 and later install it), and when a package on disk is not
    in the lock, which is how a stale nested copy shadows a bumped hoisted one. ``link``
    entries are skipped (their version is at the link target), and ``dev`` entries are
    skipped unless ``include_dev`` is set, for a tree installed with ``--omit=dev``."""
    project_dir = Path(project_dir)
    try:
        lock = json.loads((project_dir / "package-lock.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(packages, dict):
        return None
    drift: List[NpmDrift] = []
    for key, meta in packages.items():
        if not isinstance(key, str) or not key.startswith("node_modules/") or not isinstance(meta, dict):
            continue
        if meta.get("link") or (meta.get("dev") and not include_dev):
            continue
        locked = str(meta.get("version") or "")
        installed = _disk_version(project_dir / key)
        if not installed:
            if not any(meta.get(flag) for flag in _MAY_BE_ABSENT):
                drift.append(NpmDrift(key, "", locked))
        elif locked and installed != locked:
            drift.append(NpmDrift(key, installed, locked))
    for key, path in _installed_packages(project_dir).items():
        if key not in packages:
            drift.append(NpmDrift(key, _disk_version(path), ""))
    return drift
