# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The version lives in four places, and a release is only as true as the slowest one.

`vaf/version.py` is the source of truth and the release workflow already compares the tag
and `web/package.json` against it, remotely, after the tag is pushed. The other two places
are not checked anywhere: the lock file that must move with the manifest, and the two
places that TALK about the release, the CHANGELOG section the workflow publishes verbatim
as the release body and the in-app "what's new" entry.

Why that matters, measured on this repo: the version was bumped to a29 on 2026-09-14 and
the tag was never pushed, so twenty-one later commits kept landing under `[Unreleased]`
while a closed `## [0.1.0a29]` section and an in-app entry of the same version described
only the older work. Tagging would have shipped all of it with release notes that named
none of it, and the announcement modal would have told every user about the wrong release.
Nothing failed; the notes were simply wrong.

These tests do not check WHAT the entries say. They check that a release cannot describe a
different release than the one it ships.
"""
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _version() -> str:
    text = (REPO / "vaf" / "version.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    assert m, "vaf/version.py carries no __version__"
    return m.group(1)


def _npm_spelling(version: str) -> str:
    """`0.1.0a29` the way npm spells it: `0.1.0-alpha.29`. The release workflow compares the
    two through `packaging.version.parse`, so the shapes have to be convertible."""
    m = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", version)
    assert m, f"unexpected PEP 440 shape: {version}"
    if not m.group(2):
        return m.group(1)
    word = {"a": "alpha", "b": "beta", "rc": "rc"}[m.group(2)]
    return f"{m.group(1)}-{word}.{m.group(3)}"


def test_the_web_manifest_and_its_lock_carry_the_python_version():
    """Both lock fields, not just the manifest: the release workflow only reads the manifest,
    and a lock left behind is what deadlocked the updater in 0.1.0a7 to a13."""
    want = _npm_spelling(_version())
    pkg = json.loads((REPO / "web" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((REPO / "web" / "package-lock.json").read_text(encoding="utf-8"))
    assert pkg.get("version") == want
    assert lock.get("version") == want
    assert (lock.get("packages") or {}).get("", {}).get("version") == want


def test_the_changelog_has_a_section_for_the_version_being_shipped():
    """`.github/workflows/release.yml` publishes the section matching the tag VERBATIM as the
    release body, and falls back to "See CHANGELOG.md for details." when there is none. A
    release whose notes are that sentence says nothing at all."""
    version = _version()
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.search(rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})\s*$",
                        text, re.MULTILINE)
    assert heading, f"CHANGELOG.md has no dated section for {version}"
    body = text[heading.end():].split("\n## [", 1)[0]
    assert body.strip(), f"the {version} section is empty, so the release body would be too"


def test_the_in_app_announcement_describes_the_version_being_shipped():
    """The modal shows the NEWEST entry only, and fires when it is newer than what the user
    acknowledged. An entry left on the previous version announces the previous release to
    somebody who just installed this one."""
    version = _version()
    text = (REPO / "web" / "lib" / "changelog.ts").read_text(encoding="utf-8")
    versions = re.findall(r"^\s*version:\s*'([^']+)',", text, re.MULTILINE)
    assert versions, "web/lib/changelog.ts carries no entries"
    assert versions[0] == version, (
        f"the newest in-app entry is {versions[0]}, the release ships {version}")
    # Newest first is what the modal assumes; an out-of-order list shows the wrong entry.
    from packaging.version import parse
    assert versions == sorted(versions, key=parse, reverse=True), versions


@pytest.mark.parametrize("spelled,want", [
    ("0.1.0a29", "0.1.0-alpha.29"), ("1.2.3", "1.2.3"),
    ("2.0.0b4", "2.0.0-beta.4"), ("0.9.0rc1", "0.9.0-rc.1"),
])
def test_the_npm_spelling_rule(spelled, want):
    assert _npm_spelling(spelled) == want
