# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""No internal identifiers in committed content - enforced, not just written down.

This is a PUBLIC repository. The house rule (see the repo's working conventions) forbids
committed content from referencing real chat/session identifiers, real usernames or home
paths, real scope ids, or the owner's email. Incident descriptions stay neutral ("live
incident"), and fixtures use synthetic placeholders.

The rule existed in prose and was violated anyway, twice: a repo-wide sweep removed 68
occurrences once, and on 2026-07-21 a real session identifier still landed in a pushed test
docstring. Prose does not stop a hurried commit; this guard does.

Design constraints of a PUBLIC guard for PRIVATE literals:

- The session-id pattern (a colour word plus four or more digits) is generic and safe to
  publish, so it is checked here directly, minus the deliberately synthetic fixtures.
- The owner-specific literals (username, home path, scope id, email) must NOT appear in this
  file, or the guard would itself violate the rule. They live in an OPTIONAL, gitignored
  denylist file that exists only on the owner's machine: one literal per line, read at test
  time. On CI the file is absent and that layer is skipped; locally every pytest run checks
  the full set. Local git hooks (also never committed) cover commit messages, which no
  pytest can see.
"""
import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# The pattern that has actually bitten this repo, twice. Colour-word session ids are how VAF
# names chat sessions, so any such token in committed content is a leaked internal id unless
# it is one of the known synthetic fixtures below.
_SESSION_ID_RE = re.compile(r"(?:cyan|yellow|blue|green|red|purple|orange)[0-9]{4,}")

# Deliberately synthetic fixtures, allowed everywhere. Adding a NEW synthetic id to committed
# content requires adding it here - that forced, visible step is the point of the allowlist.
_SYNTHETIC_IDS = {"green123456", "red654321", "yellow012345"}

# Owner-specific literals, one per line, gitignored, owner's machine only. Never commit it.
_LOCAL_DENYLIST = _REPO / ".hygiene-deny.local"

# Tracked files we do not scan: binaries and vendored third-party code (upstream authorship
# notes legitimately contain their authors' real emails).
_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".svg", ".woff", ".woff2",
    ".ttf", ".pdf", ".gguf", ".onnx", ".bin", ".lock", ".zip", ".mp3", ".wav",
}
_SKIP_PREFIXES = ("vaf/vendor/",)


def _tracked_text_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_REPO, capture_output=True, check=True
    ).stdout.decode("utf-8", errors="ignore")
    for rel in out.split("\0"):
        if not rel or rel.startswith(_SKIP_PREFIXES):
            continue
        if Path(rel).suffix.lower() in _SKIP_SUFFIXES:
            continue
        p = _REPO / rel
        if p.is_file():
            yield rel, p


def _session_id_hits(text: str):
    return [m for m in _SESSION_ID_RE.findall(text) if m not in _SYNTHETIC_IDS]


def test_no_real_session_ids_in_tracked_content():
    offenders = {}
    for rel, p in _tracked_text_files():
        text = p.read_bytes().decode("utf-8", errors="ignore")
        hits = _session_id_hits(text)
        if hits:
            offenders[rel] = sorted(set(hits))
    assert not offenders, (
        "Real session identifiers in committed content (public repo). Describe incidents "
        "neutrally ('live incident') or use a synthetic placeholder and add it to "
        "_SYNTHETIC_IDS:\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(offenders.items()))
    )


def test_no_owner_literals_in_tracked_content():
    """Full-literal layer, owner's machine only. CI skips it (the denylist is gitignored and
    absent there); the generic pattern above still runs everywhere."""
    if not _LOCAL_DENYLIST.exists():
        return  # CI or a fresh checkout: nothing to check at this layer
    literals = [
        ln.strip() for ln in
        _LOCAL_DENYLIST.read_bytes().decode("utf-8", errors="ignore").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    offenders = {}
    for rel, p in _tracked_text_files():
        text = p.read_bytes().decode("utf-8", errors="ignore")
        hits = [lit for lit in literals if lit in text]
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "Owner-specific literals in committed content (public repo):\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(offenders.items()))
    )


def test_the_detector_actually_detects():
    """Pin the detector itself: a real-shaped id is flagged, a synthetic one is not, and the
    incident shape (an id inside a docstring sentence) is caught.

    The true-positive example is CONCATENATED at runtime so the contiguous token never
    appears in this file - otherwise the pre-commit hook (and this very guard) would flag
    the guard's own test data. The hook catching exactly that during this test's first
    commit is what proved both layers work.
    """
    real_shaped = "purple" + "123456"
    assert _session_id_hits(f"see session {real_shaped} for details") == [real_shaped]
    assert _session_id_hits("fixture uses green123456 throughout") == []
    assert _session_id_hits("no ids here, just orange juice and red54 wine") == []


def test_the_denylist_itself_is_ignored():
    """The local denylist must never be committable - it IS the secret."""
    gitignore = (_REPO / ".gitignore").read_bytes().decode("utf-8", errors="ignore")
    assert ".hygiene-deny.local" in gitignore, (
        ".hygiene-deny.local must be gitignored; it holds the literals this guard must "
        "keep OUT of the repo"
    )
