# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An account's own standing grants live on the Settings Account tab.

The card sits with the rest of the account's own things (password, signed-in devices, tokens),
last on the tab: its loading placeholder is measured for two grants, so with any other count
its height differs, and only below the last card does that change nothing but the scroll
height. The settings search lists it under Account and jumps to it in every state of the card.
An admin's view of ANOTHER account's grants stays in the user editor. The security overview's
note, the docs and the search all name the same place.
"""
import json
import re
from pathlib import Path

import pytest

from test_model_roles_web_static import _tab_regions

_ROOT = Path(__file__).resolve().parent.parent
_MODAL = _ROOT / "web" / "components" / "SettingsModal.tsx"
_SECTION = _ROOT / "web" / "components" / "settings" / "StandingGrantsSection.tsx"
_LOCALES = ("en", "de", "ja", "ko", "th", "tr", "zh")
_OWN = 'endpoint="/api/security/grants"'


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _inside(pos: int, spans) -> bool:
    return any(a <= pos < b for a, b in spans)


def test_own_grants_sit_on_the_account_tab():
    """Rendered once, on the Account tab, after the signed-in devices and tokens, and nowhere
    on Connections. MUTATION: put the line back under `connections`, move it before
    `<SessionsSection />`, or delete it."""
    src = _src(_MODAL)
    assert src.count(_OWN) == 1
    pos = src.index(_OWN)
    regions = _tab_regions(src)
    assert not _inside(pos, regions.get("connections", [])), "the own grants card is on Connections"
    spans = [(a, b) for a, b in regions.get("account", []) if a <= pos < b]
    assert spans, "the own grants card is not on the Account tab"
    a, _ = spans[0]
    sessions = src.find("<SessionsSection />", a)
    assert a <= sessions < pos, "the grants card must come after the signed-in devices and tokens"


def test_the_admin_editor_keeps_its_own_view():
    """The user editor still shows another account's grants, unchanged and without a search
    anchor (the id would otherwise stand twice). MUTATION: add `sectionId=...` to the editor
    instance, or delete it."""
    src = _src(_MODAL)
    assert src.count("<StandingGrantsSection") == 2
    assert src.count(
        "<StandingGrantsSection endpoint={`/api/users/${editingUser.id}/grants`} own={false} />") == 1


def test_the_search_finds_grants_on_account():
    """One search entry under Account, and the card's anchor is built from the very same
    expression, so the slug the search jumps to and the id on the card cannot drift apart.
    MUTATION: delete the `addSecs` row, point it at `connections`, or pass a different
    expression as `sectionId`."""
    src = _src(_MODAL)
    body = src.split("const settingsSearchEntries", 1)[1].split("return out;", 1)[0]
    assert "addSecs('account', [tGrants('title')]);" in body
    assert body.count("tGrants(") == 1
    assert "const tGrants = useTranslations('grants');" in src
    assert f"<StandingGrantsSection {_OWN} own sectionId={{slugifySection(tGrants('title'))}} />" in src


def test_the_anchor_stands_in_every_state():
    """First load, failed load and loaded each carry the id, so a search jump lands while the
    card still loads. MUTATION: drop `id={sectionId}` from the loading placeholder's root."""
    src = _src(_SECTION)
    assert "sectionId?: string" in src
    root = '<div id={sectionId} className="bg-gray-50/50 p-6 rounded-xl border border-gray-100 mt-6 scroll-mt-2">'
    assert src.count(root) == 3
    assert src.count("<div className=\"bg-gray-50/50 p-6 rounded-xl") == 0


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_overview_note_names_the_account_tab(locale):
    """The security overview's note under the grants list says where to revoke them, in the
    words of that locale's tab label. MUTATION: revert one locale to its Connections label."""
    cat = json.loads((_ROOT / "web" / "messages" / f"{locale}.json").read_text(encoding="utf-8"))
    note = cat["notifications"]["ovGrStandingNote"]
    tabs = cat["settings"]["tabs"]
    assert tabs["account"] in note, (locale, note)
    assert tabs["connections"] not in note, (locale, note)


def _paragraph(doc: str, head: str) -> str:
    lines = [ln for ln in doc.splitlines() if ln.startswith(head)]
    assert len(lines) == 1, head
    return lines[0]


def test_docs_name_account_for_grants():
    """WEB_UI.md describes the card on the Account tab and no longer on Connections; the
    security docs name Settings, Account. MUTATION: leave the sentence in the Connections
    paragraph, or a security doc on "Settings, Connections"."""
    web_ui = _src(_ROOT / "docs" / "web-ui" / "WEB_UI.md")
    assert "StandingGrantsSection.tsx" in _paragraph(web_ui, "**Settings → Account:**")
    assert "StandingGrantsSection.tsx" not in _paragraph(web_ui, "**Settings → Connections:**")
    stale = re.compile(r"(grants|its own) under Settings,\s+Connections")
    for name in ("USER_ISOLATION.md", "SECURITY_DASHBOARD.md"):
        doc = _src(_ROOT / "docs" / "security" / name)
        assert not stale.search(doc), name
        assert re.search(r"(grants|its own) under Settings,\s+Account", doc), name
