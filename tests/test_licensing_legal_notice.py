# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Section 7(b) legal notice stays intact and stays valid.

LICENSING.md carries a notice whose preservation is required under AGPL Section
7(b). Two things can quietly destroy it, and neither shows up as a broken build:

- A docs sweep reformats or drops the block. The notice is prose among prose, so
  nothing else would notice its absence.
- Someone "tightens" the wording and, without meaning to, turns it into a term
  that Section 7(b) does not authorize. Section 7 is explicit about the
  consequence: anything outside 7(a)-(f) counts as a "further restriction" under
  Section 10, and then any recipient MAY SIMPLY REMOVE IT. A clause that can be
  deleted at will protects nothing.

What makes a notice valid under 7(b) is narrow. The FSF reads "legal notice" as
a notice advising a person of their RIGHTS OR OBLIGATIONS, and holds that the
term cannot be stretched to cover unrelated items. So the test does not merely
check that some text exists: it checks that each obligation the notice claims to
state is actually stated, and that the notice stays preservation-only. A bare
identifier would not qualify, which is why the reference rides inside a notice
that would be complete without it.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LICENSING = ROOT / "LICENSING.md"
README = ROOT / "README.md"

NOTICE_REFERENCE = "17f46056-1744-4574-9564-0770a6ba799b"


@pytest.fixture(scope="module")
def licensing_text() -> str:
    # read_bytes: LICENSING.md may be CRLF on a Windows checkout and universal
    # newlines would rewrite it invisibly on any later write.
    return LICENSING.read_bytes().decode("utf-8")


def test_both_language_sections_exist(licensing_text):
    assert "### Additional Term under Section 7(b)" in licensing_text
    assert "### Zusätzliche Bedingung nach Abschnitt 7(b)" in licensing_text


def test_notice_reference_appears_in_both_languages(licensing_text):
    assert licensing_text.count(NOTICE_REFERENCE) == 2, (
        "The notice reference must appear exactly once per language section. "
        "If you moved or duplicated the notice, fix the count deliberately."
    )


@pytest.mark.parametrize("obligation", [
    # Each entry is a right or obligation the notice must actually state -
    # that is what makes it a "legal notice" in the 7(b) sense.
    "GNU Affero General Public License",
    "Section 13",
    "complete corresponding source code",
    "commercial license",
])
def test_english_notice_states_its_obligations(licensing_text, obligation):
    start = licensing_text.index("### Additional Term under Section 7(b)")
    end = licensing_text.index("### Trademarks and Brand Assets")
    section = licensing_text[start:end]
    assert obligation.lower() in section.lower()


@pytest.mark.parametrize("obligation", [
    "GNU Affero General Public License",
    "Abschnitt 13",
    "vollständigen zugehörigen Quelltext",
    "kommerzielle",
])
def test_german_notice_states_its_obligations(licensing_text, obligation):
    start = licensing_text.index("### Zusätzliche Bedingung nach Abschnitt 7(b)")
    end = licensing_text.index("### Marken und Markenwerte")
    section = licensing_text[start:end]
    assert obligation.lower() in section.lower()


def test_notice_stays_preservation_only(licensing_text):
    """No display requirement, in either language.

    Requiring a notice to be SHOWN in the interface is the badgeware pattern that
    got such licenses widely treated as non-free. Preservation is uncontroversial;
    display is where it breaks.
    """
    for marker, disclaimer in [
        ("### Additional Term under Section 7(b)", "does **not** require the notice to be"),
        ("### Zusätzliche Bedingung nach Abschnitt 7(b)", "**nicht**, ihn in einer Benutzeroberfläche anzuzeigen"),
    ]:
        assert marker in licensing_text
        section = licensing_text[licensing_text.index(marker):]
        assert disclaimer in section[:2000], f"missing the no-display disclaimer after {marker}"


def test_notice_disclaims_telemetry(licensing_text):
    """Without this sentence the reference reads as a hidden serial number that
    phones home - the worst possible reading for an agent with mail and file access."""
    assert "no telemetry" in licensing_text
    assert "keinerlei Telemetrie" in licensing_text


def test_readme_points_at_the_notice():
    readme = README.read_bytes().decode("utf-8")
    assert "Section 7(b)" in readme
    assert "Preservation of Legal Notices" in readme


def test_reference_is_not_hardcoded_in_the_codebase():
    """The reference belongs in the license text, not in a constant.

    As license prose its removal is a licensing question. As a code constant it
    is just an unexplained literal, and the next cleanup pass deletes it without
    a second thought.
    """
    offenders = []
    for path in ROOT.rglob("*.py"):
        if any(p in path.parts for p in ("venv", ".git", "node_modules", "build", "dist")):
            continue
        if path.name == Path(__file__).name:
            continue
        if NOTICE_REFERENCE in path.read_bytes().decode("utf-8", "replace"):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, f"notice reference must not live in code: {offenders}"


def test_section_7_terms_are_labelled_with_their_subsection(licensing_text):
    """Every added term names the subsection that authorizes it.

    Section 7 only permits terms under (a) to (f); a term added without naming
    one invites the reading that it is a further restriction under Section 10,
    which any recipient may remove.
    """
    headings = re.findall(r"^### (?:Additional Term under Section|Zusätzliche Bedingung nach Abschnitt) (7\([a-f]\))",
                          licensing_text, re.M)
    assert headings, "no Section 7 terms found - did the headings change?"
    assert set(headings) == {"7(a)", "7(b)"}, f"unexpected set of Section 7 terms: {headings}"
