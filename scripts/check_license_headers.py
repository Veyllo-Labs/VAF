# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""CI guard: every first-party source file must carry the AGPL SPDX header.

VAF is dual-licensed (AGPL-3.0-or-later + Commercial; see LICENSING.md). To keep the
licensing machine-detectable and to make the Section 7 plugin permission travel with the
code, every first-party Python and web source file carries:

    SPDX-License-Identifier: AGPL-3.0-or-later

This script fails (exit 1) if any first-party source file is missing that line, so new
files can't silently land unlicensed. Vendored third-party code and scaffolding templates
(which become the user's own project) are deliberately excluded.

Run locally:  python scripts/check_license_headers.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPDX_TAG = "SPDX-License-Identifier: AGPL-3.0-or-later"

# Root files that must exist for the dual-license setup to be coherent.
REQUIRED_FILES = ["LICENSE", "LICENSING.md", "COMMERCIAL.md"]

# Directories scanned for first-party source.
PY_ROOTS = ["vaf", "tests", "scripts", "examples"]
PY_ROOT_FILES = ["setup.py"]
WEB_ROOTS = ["web/app", "web/components", "web/lib", "web/hooks"]
WEB_EXTS = {".ts", ".tsx", ".js", ".jsx"}

# Pruned anywhere in the tree.
PRUNE_DIRS = {
    "__pycache__", "node_modules", ".next", ".git", "build", "dist",
    "venv", ".venv", "vaf.egg-info", "models", "logs", "tmp",
}
# Excluded path prefixes (relative to ROOT): vendored + scaffolding templates.
EXCLUDE_PREFIXES = (
    "vaf/vendor/",
    "vaf/tools/coder_templates/",
    "vaf/whatsapp_node/",
)


def _excluded(rel: str) -> bool:
    return any(rel.startswith(p) for p in EXCLUDE_PREFIXES)


def _iter(base: str, exts: set[str]):
    p = ROOT / base
    if p.is_file():
        yield p
        return
    if not p.is_dir():
        return
    for f in p.rglob("*"):
        if f.suffix not in exts:
            continue
        if any(part in PRUNE_DIRS for part in f.relative_to(ROOT).parts):
            continue
        yield f


def main() -> int:
    missing_files = [f for f in REQUIRED_FILES if not (ROOT / f).exists()]

    targets = []
    for base in PY_ROOTS:
        targets += _iter(base, {".py"})
    for base in PY_ROOT_FILES:
        targets += _iter(base, {".py"})
    for base in WEB_ROOTS:
        targets += _iter(base, WEB_EXTS)

    missing_header = []
    checked = 0
    for path in targets:
        rel = path.relative_to(ROOT).as_posix()
        if _excluded(rel):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            continue  # empty file (e.g. empty __init__.py) — nothing to license
        checked += 1
        if SPDX_TAG not in text:
            missing_header.append(rel)

    ok = not missing_files and not missing_header
    if ok:
        print(f"License headers OK: {checked} first-party source files carry '{SPDX_TAG}'.")
        return 0

    if missing_files:
        print("ERROR: missing required license files:")
        for f in missing_files:
            print(f"  - {f}")
    if missing_header:
        print(f"ERROR: {len(missing_header)} first-party source file(s) missing '{SPDX_TAG}':")
        for f in missing_header:
            print(f"  - {f}")
        print("\nAdd this header to the top of each file (after any shebang / 'use client'):")
        print("  # SPDX-FileCopyrightText: 2026 Veyllo GmbH")
        print("  # SPDX-License-Identifier: AGPL-3.0-or-later")
        print("  (// comments for .ts/.tsx). See LICENSING.md.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
