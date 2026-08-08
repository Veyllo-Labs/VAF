# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""README badges make measurable claims. This keeps them true.

A badge is a promise rendered as an image, and a stale one is worse than none:
it is read as current by everyone who sees it and by nobody who maintains it.
Two failure shapes, handled differently here:

- EXACT claims rot on the next change. "LLM providers: 6" is wrong the day a
  seventh is registered, and nothing else in the suite would notice. Adding a
  provider already means touching several registries (see the provider row in
  the repo's cross-cutting registry rules); this test makes the badge one of
  them instead of a thing someone remembers.
- FLOOR claims ("110+", "4000+") stay true while the numbers grow, which is the
  normal direction. They are checked as floors, so they only fail if the project
  actually shrinks past them - at which point the badge really is a lie.

The telemetry badge is checked too, because it is the one claim a reader cannot
verify by looking: it must point at a section that exists and that admits the
one outbound call VAF makes.
"""
import re
import subprocess
import sys
from pathlib import Path

from vaf.core.config import PROVIDER_MODELS

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
TOOLS_CATALOG = ROOT / "docs" / "agents" / "TOOLS_CATALOG.md"


def _readme() -> str:
    return README.read_bytes().decode("utf-8")


def _badge_number(pattern: str) -> int:
    """Pull the number out of a shields.io badge label in the README."""
    m = re.search(pattern, _readme())
    assert m, f"badge not found in README: {pattern}"
    return int(m.group(1))


def test_provider_badge_matches_the_registry_exactly():
    claimed = _badge_number(r"LLM%20providers-(\d+)-")
    assert claimed == len(PROVIDER_MODELS), (
        f"README badge claims {claimed} LLM providers, PROVIDER_MODELS has "
        f"{len(PROVIDER_MODELS)} ({sorted(PROVIDER_MODELS)}). Update the badge "
        f"in README.md together with the other provider registries."
    )


def _first_party_source_lines() -> int:
    """Lines of source we wrote, counted the way the badge means it.

    Deliberately excludes vendored code, build output and generated/lock files:
    a line count inflated by a dependency lockfile measures nothing about this
    project. The definition lives here, next to the assertion, so the badge and
    its guard can never drift apart.
    """
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                             cwd=ROOT, check=True).stdout.split("\n")
    code_suffixes = {".py", ".ts", ".tsx", ".js", ".jsx", ".sh", ".ps1", ".css", ".sql"}
    excluded_dirs = ("vaf/vendor/", "web/public/", "node_modules/", "web/.next/")
    excluded_files = ("package-lock.json", "requirements.lock", "licenses_data.ts")

    total = 0
    for rel in tracked:
        if not rel or rel.startswith(excluded_dirs) or rel.endswith(excluded_files):
            continue
        path = ROOT / rel
        if path.suffix not in code_suffixes or not path.is_file():
            continue
        total += path.read_bytes().count(b"\n")
    return total


def test_lines_of_code_badge_floor_still_holds():
    """No third-party badge backs this one, so we measure it ourselves.

    Checked when this was added: shields.io has no LOC endpoint (it answers
    "404: badge not found"), tokei.rs no longer responds at all, and
    ghloc.vercel.app computes on demand without exposing an API a badge could
    read. The badge is therefore a static floor, and the link points at ghloc so
    a reader can see the live number for themselves.
    """
    claimed_floor_k = _badge_number(r"source-(\d+)k%2B%20lines-")
    lines = _first_party_source_lines()
    assert lines >= claimed_floor_k * 1000, (
        f"README badge claims {claimed_floor_k}k+ source lines, counted {lines:,}."
    )


def test_tools_badge_floor_still_holds():
    claimed_floor = _badge_number(r"tools-(\d+)%2B-")
    tools = {m for m in re.findall(r"^\| `([a-z_]+)`", TOOLS_CATALOG.read_bytes().decode("utf-8"), re.M)}
    assert len(tools) >= claimed_floor, (
        f"README badge claims {claimed_floor}+ tools, catalog documents {len(tools)}."
    )


def test_tests_badge_floor_still_holds():
    """Counted the way pytest counts, because that is what the badge claims.

    Grepping ``def test_`` undercounts badly - parametrized cases are one
    function and many tests (3450 functions against 4126 collected when this
    was written), so a grep-based floor would either be set too low to mean
    anything or fail against a badge that is in fact true. Collection costs a
    few seconds; a claim guard that checks a proxy instead of the claim is not
    a guard.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests"), "-q", "--collect-only",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=ROOT,
    )
    m = re.search(r"^(\d+) tests? collected", proc.stdout, re.M)
    assert m, f"could not read the collected count:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    collected = int(m.group(1))
    claimed_floor = _badge_number(r"tests-(\d+)%2B-")
    assert collected >= claimed_floor, (
        f"README badge claims {claimed_floor}+ tests, pytest collects {collected}."
    )


def test_telemetry_badge_points_at_a_section_that_exists():
    readme = _readme()
    assert "img.shields.io/badge/telemetry-none" in readme
    assert "#what-vaf-sends-and-where" in readme, "badge must link to the anchor"
    assert "### What VAF sends, and where" in readme, "the linked section is missing"


def test_telemetry_section_admits_the_version_check():
    """A 'no telemetry' claim next to an undisclosed outbound call is the kind of
    thing that gets found and quoted. The section must name the version check and
    the config key that turns it off."""
    section = _readme()[_readme().index("### What VAF sends, and where"):]
    section = section[:section.index("## Extending VAF")]
    assert "update_check_on_start" in section
    assert "GitHub" in section
    # It must also be honest that configured providers receive prompts.
    assert "provider" in section.lower()
