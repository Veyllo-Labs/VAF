# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Rebuild the list of common passwords a new password may not be.

    venv/bin/python scripts/build_common_passwords.py            # downloads the pinned file
    venv/bin/python scripts/build_common_passwords.py --source xato.txt

Writes ``vaf/auth/data/common_passwords.txt``, which ``validate_password`` in
``vaf/auth/user_admin.py`` reads. The source is one file of SecLists (MIT, Copyright 2018
Daniel Miessler) at a pinned commit; its SHA-256 is checked before anything is written, so
a moved or replaced upstream file never changes the list silently. CI never runs this: the
script exists so the shipped file can be reproduced byte for byte.

How the list is made, and why:

1. Only entries of at least ``MIN_PASSWORD_LEN`` characters. A shorter password is refused
   by length already; ASVS 5.0 6.2.4 asks for the most common passwords "which match the
   application's password policy", and the 100,000 most common passwords of the NCSC list
   hold only 1,212 of that length, while this source holds 46,296.
2. Lowercased, duplicates removed, in the source's order (most common first): the check
   compares ``password.lower()``.
3. Entries that the public-repo hygiene guard (tests/test_public_repo_hygiene.py) would
   flag are dropped, so the shipped file cannot turn that guard red: three entries of the
   first 10,003 have the shape of an internal session id.
4. The first ``KEEP`` of what is left.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "vaf" / "auth" / "data" / "common_passwords.txt"

REPOSITORY = "https://github.com/danielmiessler/SecLists"
SOURCE_PATH = "Passwords/Common-Credentials/xato-net-10-million-passwords-1000000.txt"
COMMIT = "c205c36a445bff37f8e58a9ec829105cd4975c58"
SHA256 = "424a3e03a17df0a2bc2b3ca749d81b04e79d59cb7aeec8876a5a3f308d0caf51"
URL = f"https://raw.githubusercontent.com/danielmiessler/SecLists/{COMMIT}/{SOURCE_PATH}"
KEEP = 10_000


def _min_length() -> int:
    sys.path.insert(0, str(ROOT))
    from vaf.auth.user_admin import MIN_PASSWORD_LEN
    return MIN_PASSWORD_LEN


def _hygiene_guard():
    """The guard module itself, so the list is filtered by exactly the patterns it checks."""
    path = ROOT / "tests" / "test_public_repo_hygiene.py"
    spec = importlib.util.spec_from_file_location("_hygiene_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _flagged(guard, entry: str) -> bool:
    return bool(guard._session_id_hits(entry) or guard._home_path_hits(entry)
                or guard._attribution_hits(entry))


def header(minimum: int) -> str:
    return "\n".join((
        f"# The most common passwords of at least {minimum} characters, lowercased, one per line.",
        "# vaf/auth/user_admin.py refuses a new password that is one of them, in any case.",
        f"# Source: {REPOSITORY}",
        f"#   file {SOURCE_PATH}",
        f"#   commit {COMMIT}",
        f"#   sha256 {SHA256}",
        "# License: MIT, Copyright 2018 Daniel Miessler (full text: licenses/SecLists-MIT.txt).",
        f"# Made by scripts/build_common_passwords.py: the entries of at least {minimum} characters in",
        "# the source's order (most common first), lowercased, duplicates removed, entries the",
        "# public-repo hygiene guard would flag dropped, the first 10000 kept.",
    )) + "\n"


def build(raw: bytes, *, expected: str = SHA256) -> str:
    """The file's content from the source's bytes; `expected` is the digest they must have."""
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected:
        raise SystemExit(f"The source's SHA-256 is {digest}, not the pinned {expected}; nothing written.")
    minimum = _min_length()
    guard = _hygiene_guard()
    seen: set = set()
    kept: list = []
    for line in raw.decode("ascii").split("\n"):
        entry = line.rstrip("\r").lower()
        if len(entry) < minimum or entry in seen or _flagged(guard, entry):
            continue
        seen.add(entry)
        kept.append(entry)
        if len(kept) == KEEP:
            break
    if len(kept) < KEEP:
        raise SystemExit(f"Only {len(kept)} entries qualify; nothing written.")
    return header(minimum) + "\n".join(kept) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", help="a local copy of the pinned file instead of the download")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)
    if args.source:
        raw = Path(args.source).read_bytes()
    else:
        with urllib.request.urlopen(URL, timeout=120) as response:   # noqa: S310 - fixed https URL
            raw = response.read()
    Path(args.out).write_bytes(build(raw).encode("ascii"))
    print(f"Wrote {KEEP} entries to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
