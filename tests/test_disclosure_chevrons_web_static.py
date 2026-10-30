# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One chevron convention for the whole web UI (docs/web-ui/DESIGN.md 4.7).

Something that opens in place shows a chevron pointing right that turns down when open;
something that opens a list on top (a select, a dropdown menu) shows a chevron pointing down.
Two disclosures drifted apart: the "Who does what" rows pointed left, and the mail account's
"Advanced" pointed down and turned up, the menu's chevron on a disclosure.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WEB = _ROOT / "web"

# A down chevron that turns up belongs to a control that opens a list on top. Each entry
# names why that file's down chevron is a menu and not a disclosure.
_MENUS = {
    "components/settings/WorkflowCreator.tsx": "the step's tool picker opens a list on top",
}


def _sources():
    for sub in ("components", "app"):
        for path in (_WEB / sub).rglob("*.tsx"):
            yield path.relative_to(_WEB).as_posix(), path.read_text(encoding="utf-8")


def test_a_down_chevron_turns_up_only_on_a_menu():
    """MUTATION: put the mail account's "Advanced" back to ChevronDown with rotate-180."""
    found = {}
    for rel, src in _sources():
        for line in src.splitlines():
            if "ChevronDown" in line and "rotate-180" in line:
                found.setdefault(rel, []).append(line.strip())
    assert set(found) <= set(_MENUS), {k: v for k, v in found.items() if k not in _MENUS}


def test_no_disclosure_points_left():
    """A left-pointing chevron means back. MUTATION: turn a ChevronLeft on an open state, or
    draw the overview caret pointing left again."""
    for rel, src in _sources():
        for line in src.splitlines():
            assert not ("ChevronLeft" in line and re.search(r"rotate-(90|180)|-rotate-90", line)), (rel, line)
    overview = (_WEB / "components/settings/ModelRolesOverview.tsx").read_text(encoding="utf-8")
    assert 'd="M4.5 2.5 8 6l-3.5 3.5"' in overview
    css = (_WEB / "app/globals.css").read_text(encoding="utf-8")
    assert "rotate(-90deg)" not in css[css.index(".vaf-role-caret"):css.index(".vaf-role-panel")]


def test_the_mail_advanced_toggle_is_a_disclosure():
    """MUTATION: drop aria-expanded, or go back to the down chevron."""
    src = (_WEB / "components/connections/MailAccounts.tsx").read_text(encoding="utf-8")
    toggle = src[src.index("onClick={() => setAdvanced(v => !v)}"):]
    toggle = toggle[:toggle.index("</button>")]
    assert "aria-expanded={advanced}" in toggle
    assert "<ChevronRight" in toggle and "rotate-90" in toggle and "ChevronDown" not in toggle


def test_the_convention_is_written_down():
    doc = (_ROOT / "docs/web-ui/DESIGN.md").read_text(encoding="utf-8")
    section = doc[doc.index("### 4.7 Disclosure vs. Menu Chevrons"):doc.index("## 5. Layout Patterns")]
    assert "pointing **right** when closed" in section and "pointing\n  **down**" in section
