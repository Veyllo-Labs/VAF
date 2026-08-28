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
- HOME PATHS are checked by SHAPE, not by name, and that layer runs everywhere. A denylist
  can only catch the names somebody thought of: the sweep that added this layer was looking
  for one first name and found `/Users/<real-macos-login>` in `launch_vaf.scpt`, a different
  identifier entirely, which no name-based check would ever have reported. A shape check
  catches the whole class, protects a fork of this repo as much as this one, and needs no
  secret to work.
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

# A home directory in committed content names a person. The shape is generic and safe to
# publish, so unlike the literals above this layer also runs on CI and in anyone's fork.
_HOME_PATH_RE = re.compile(r"(?:[Cc]:[\\/]+Users[\\/]+|/Users/|/home/)([A-Za-z0-9._-]{2,32})")

# Accounts that are placeholders, service accounts or documentation examples rather than a
# person. Everything else in a home path is treated as a real login. Keep this list boring:
# a new entry is a claim that the name identifies nobody, and that claim is easy to get wrong
# (the login this layer first caught was initials plus a surname, which reads like a service
# account until you say it out loud - it cannot be listed here for the reason above).
_PLACEHOLDER_USERS = {
    "user", "users", "username", "youruser", "user1", "web_user", "test", "testuser",
    "alice", "bob", "example", "me", "admin", "administrator", "public", "root",
    "runner", "browser", "nobody9x", "windows10fan", "node", "app", "vaf",
    "...", "<user>", "$user", "${user}",
}

# Files that carry the author's real name ON PURPOSE - a credit line is authorship, not a
# leaked identifier, and the owner decides whether to publish their own name. Listed rather
# than pattern-matched so that adding one is a visible decision. This set may only shrink.
_AUTHOR_CREDIT_FILES = {
    "vaf/cli/cmd/settings.py",   # "Created by ..." in the CLI's about screen
    "vaf/sources/news.json",     # maintainer field of the shipped news feed
}

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


def _home_path_hits(text: str):
    return sorted({m for m in _HOME_PATH_RE.findall(text)
                   if m.lower() not in _PLACEHOLDER_USERS})


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


def test_no_real_home_paths_in_tracked_content():
    """A home directory names a person, and this layer runs everywhere - no secret needed.

    It exists because the name-based layer cannot be complete. The sweep that added it was
    hunting one first name; this shape check reported `/Users/<a real macOS login>` in the
    macOS launcher, an identifier nobody had thought to look for. It was also a product bug:
    the launcher only worked on one machine.
    """
    offenders = {}
    for rel, p in _tracked_text_files():
        text = p.read_bytes().decode("utf-8", errors="ignore")
        hits = _home_path_hits(text)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "Real home paths in committed content (public repo). Use $HOME / a placeholder, or "
        "add the account to _PLACEHOLDER_USERS if it truly identifies nobody:\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(offenders.items()))
    )


def test_no_checkout_path_in_tracked_content():
    """This repo's own absolute location must never appear in committed content.

    Not a privacy rule like the layers above - a correctness one, and it runs on
    every machine because the needle is derived from THIS checkout: whatever
    absolute path the repo happens to live at, no tracked file may name it. A
    checkout path in code works on exactly one computer.

    It exists because a test passed `cwd="<this repo's absolute path>"` to
    subprocess.run. Green on the author's laptop, broken in every clone - and
    it stayed invisible for a day because CI was down during the outage that
    would otherwise have caught it in minutes. The home-path layer above misses
    this shape entirely when the checkout lives outside a home directory.
    """
    needle = str(_REPO)
    offenders = []
    for rel, p in _tracked_text_files():
        if rel == "tests/test_public_repo_hygiene.py":
            continue                      # this file names the needle by construction
        text = p.read_bytes().decode("utf-8", errors="ignore")
        if needle in text:
            offenders.append(rel)
    assert not offenders, (
        f"The repository's own absolute path ({needle}) appears in committed content - "
        "that code runs on one machine only. Derive it instead "
        "(Path(__file__).resolve().parents[N]) or use tmp_path:\n"
        + "\n".join(f"  {o}" for o in sorted(offenders))
    )


def test_the_home_path_detector_actually_detects():
    """Both directions, because a shape check that never refuses anything reads exactly like
    a clean repository."""
    # Assembled at runtime, like the session-id example above and for the same reason: a
    # contiguous real-shaped home path written here would be flagged by this very guard.
    login = "jd" + "oe"
    for shape in (rf"C:\Users\{login}\Documents", f"/Users/{login}/VAF", f"/home/{login}/.vaf"):
        assert _home_path_hits(shape) == [login], shape
    for benign in (r"C:\Users\user\Documents", "/home/runner/work", "/Users/alice/x",
                   "/home/user/.vaf", "/Users/.../Documents"):
        assert _home_path_hits(benign) == [], benign


def test_the_author_credit_allowlist_stays_a_decision():
    """The two files that may carry the author's real name are named, not matched. If the set
    grows silently the literal layer below stops meaning anything."""
    assert len(_AUTHOR_CREDIT_FILES) <= 2, (
        "a new file was allowed to carry the owner's real name - that is a publishing "
        "decision and belongs in a commit message, not in a quietly grown set"
    )
    for rel in _AUTHOR_CREDIT_FILES:
        assert (_REPO / rel).is_file(), f"{rel} is allowlisted but gone - drop the entry"


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
        if rel in _AUTHOR_CREDIT_FILES:
            continue        # the owner's own credit line, published on purpose
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


# ── decisions are justified by reasons, never attributed to process ──────────────────
#
# INCIDENT 2026-08-01: a comment shipped reading "NAMED EXCEPTION (owner decision, ...)".
# A stranger reading public code cannot do anything with WHO decided - that is what git
# history is for - and process attribution reads as noise where a REASON should stand. A
# sweep found twenty-nine such sites across four rounds, so this is a class, not a slip.
# The rule: write "Deliberate: <reason>", and let the reason carry the weight.
#
# The phrases are ATTRIBUTION shapes only. Domain uses of "owner" stay untouched - the
# machine owner, the voice call's owner, chmod owner-only, GitHub's owner parameter - which
# is why this is a phrase list and not a word ban. Assembled from parts so this guard does
# not flag its own source.
_ATTRIBUTION_PHRASES = tuple(
    a + b for a, b in (
        ("owner ", "decision"), ("owner-", "decided"), ("owner ", "mandate"),
        ("owner-", "mandated"), ("owner ", "request"), ("owner ", "mandated"),
        ("owner product ", "decision"), ("owner's design ", "rule"),
        ("Owner-", "Entscheidung"), ("Owner-", "Auflage"),
        ("owner ", "ruled out"),
    )
)


def _attribution_hits(text: str):
    low = text.lower()
    return sorted({p for p in _ATTRIBUTION_PHRASES if p.lower() in low})


def test_no_process_attribution_in_tracked_content():
    offenders = {}
    for rel, p in _tracked_text_files():
        if rel == "tests/test_public_repo_hygiene.py":
            continue
        try:
            text = p.read_bytes().decode("utf-8", errors="ignore")
        except Exception:
            continue
        hits = _attribution_hits(text)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "process attribution in committed content - state the technical reason instead "
        f"('Deliberate: <reason>'); git history already records who and when: {offenders}"
    )


def test_the_attribution_detector_actually_detects():
    """The detector's own floor, so an empty result means clean rather than blind."""
    assert _attribution_hits("kept broad (owner " + "decision, 2026-08-01)")
    assert _attribution_hits("Per Owner-" + "Auflage bleibt das so")
    assert _attribution_hits("a jail here is the crippling the owner " + "ruled out")
    assert not _attribution_hits("the machine owner's key collapses to the ownerless form")
    assert not _attribution_hits("chmod: restrict permissions (owner only)")


# ---------------------------------------------------------------------------
# Swept files stay dash-free. The dash ban is repo-wide by convention, but most
# of the tree predates the rule and cannot be pinned wholesale; this is the
# ratchet's clean end: a file joins the list in the same change that sweeps it.
# The seed entry is the one file where the characters do active damage - the
# system prompt teaches the model its typography, so a dash there propagates
# into every reply. U+FFFD rides in the same net because the sweep found two of
# them corrupting a German example sentence the model received verbatim.
# ---------------------------------------------------------------------------

_DASH_FREE_FILES = ("vaf/core/system_prompt.py",)
_BANNED_TYPOGRAPHY = {"—": "em dash", "–": "en dash",
                      "―": "horizontal bar", "�": "replacement character"}


def test_a_swept_file_stays_dash_free():
    offenders = []
    for rel in _DASH_FREE_FILES:
        text = (_REPO / rel).read_bytes().decode("utf-8")
        for char, name in _BANNED_TYPOGRAPHY.items():
            count = text.count(char)
            if count:
                offenders.append(f"{rel}: {count}x {name} (U+{ord(char):04X})")
    assert not offenders, (
        "banned typography crept back into a swept file; write ' - ' (or a comma):\n"
        + "\n".join(offenders)
    )


def test_the_typography_detector_actually_detects():
    """The detector's own floor, so an empty result means clean rather than blind."""
    assert "—" in _BANNED_TYPOGRAPHY and "�" in _BANNED_TYPOGRAPHY
    probe = "a–b — c�"
    assert sum(probe.count(c) for c in _BANNED_TYPOGRAPHY) == 3
