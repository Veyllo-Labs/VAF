# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Surfaces that render a real-world artefact must stay light on a dark page.

VAF's dark mode is a FOLDING palette swap: `bg-white`, `bg-gray-50..400`, borders and
`text-gray-400..900` are re-pointed at CSS variables that the `.dark` block overrides, so
ordinary Tailwind utilities flip for free. That is the right default for app chrome and the
wrong one for a sheet of paper.

Reported live 2026-07-20: the DOCX editor's page turned dark in dark mode. It is a rendering
of paper, and Print/PDF show exactly what is on screen, so it must stay light. The dark-mode
conventions already listed the document paper as a protected surface; the DOCX editor's own
page had simply never been wired up.

The fix has to be structural rather than per-utility, because the swap folds text and
surfaces in OPPOSITE directions: restoring only the background would leave the text folded
light on white, i.e. unreadable. Attaching `.vaf-doc-paper` to the SAME declaration that
defines the light ramp makes the whole subtree fold back, and makes drift impossible - the
protected surface IS the light theme, not a copy of it.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_CSS = _REPO / "web/app/globals.css"
_PAPER_CLASS = "vaf-doc-paper"


def _css() -> str:
    return _CSS.read_bytes().decode("utf-8")


def test_the_protected_surface_shares_the_light_declaration():
    """Not a copy of the light values - the same block. A copy would silently drift the day
    someone tunes the light palette."""
    css = _css()
    match = re.search(r":root,\s*\.dark\s+\.%s\s*\{" % re.escape(_PAPER_CLASS), css)
    assert match, (
        f".dark .{_PAPER_CLASS} must be declared together with :root, so the protected "
        f"surface always carries the exact light palette."
    )
    block = css[match.end():css.index("\n}", match.end())]
    # The three ramps that decide readability must all be in there.
    for var in ("--sfc-white", "--txt-gray-900", "--lin-gray-200"):
        assert var in block, f"{var} missing from the light ramp shared with the paper"


def test_the_dark_override_cannot_outrank_the_protected_surface():
    """`.dark .vaf-doc-paper` (0,2,0) beats `.dark` (0,1,0), so source order does not matter.
    If someone ever moves the protection into a bare `.vaf-doc-paper` rule, it silently stops
    working - specificity would tie and the later rule would win."""
    css = _css()
    assert re.search(r"\.dark\s+\.%s" % re.escape(_PAPER_CLASS), css)
    assert not re.search(r"(?<!\.dark )\.%s\s*\{" % re.escape(_PAPER_CLASS), css), (
        "the protection must stay scoped under .dark to keep its specificity advantage"
    )


def test_the_docx_page_actually_uses_it():
    """The rule is worthless if the paper does not carry the class."""
    src = (_REPO / "web/components/NativeDocxEditor.tsx").read_bytes().decode("utf-8")
    page_lines = [ln for ln in src.splitlines() if "pdf-page" in ln and "className" in ln]
    assert page_lines, "could not find the page element in NativeDocxEditor"
    for ln in page_lines:
        assert _PAPER_CLASS in ln, f"the document page is not protected: {ln.strip()[:120]}"
