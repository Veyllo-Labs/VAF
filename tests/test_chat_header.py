# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The chat header: one name, one rename lane, one bar per layout.

Three things here fail quietly rather than loudly, which is why each is pinned:

- The rename. `editingId` answered two questions at once: WHICH row is being
  renamed, and WHERE the rename started. The sidebar pins itself open at 288px
  over the chat for any non-null value, so a rename begun in the header would
  fling the sidebar over the field being typed in, and both the sidebar row and
  the header would mount an `autoFocus` input at the same time, with the caret
  landing in whichever the browser picked.
- The fade. Its endpoint has to be the surface tone the column actually paints.
  A hardcoded white is invisible on a light theme and a glowing band across the
  top of a dark one, and nothing in the tree notices that.
- The small layout. It already had a bar; a second one is two stacked bars on
  the shortest viewport in the product.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PAGE = _REPO / "web" / "app" / "page.tsx"
_CSS = _REPO / "web" / "app" / "globals.css"


def _page() -> str:
    # CRLF-normalised: git can check this out with CRLF on the Windows CI runner.
    return _PAGE.read_bytes().decode("utf-8").replace("\r\n", "\n")


def test_the_header_is_the_band_the_globe_used_to_borrow():
    """The globe was centred in an implied h-16 band so it met the sidebar logo's
    optical line. The header IS that band now, so the two really move together."""
    page = _page()
    assert 'className="shrink-0 h-16 flex items-center gap-3 pl-6' in page, \
        "the chat header is gone or is no longer the h-16 band"
    assert "absolute top-0 right-[19px]" not in page, \
        "the globe is back on the vertical rail, floating over the header"
    header = page.split('className="shrink-0 h-16 flex items-center gap-3 pl-6', 1)[1]
    for control in ("toggleBrowserWindow", "visibleHotbarPicks.map", "setSubAgentPaletteOpen(true)"):
        assert control in header[:9000], f"{control} did not travel into the header"


def test_the_header_carries_no_edge_of_its_own():
    """The separation is the fade, not a rule: the header paints the column's own
    surface so there is nothing to see where it ends."""
    page = _page()
    header_open = page.split('className="shrink-0 h-16 flex items-center gap-3 pl-6', 1)[1].split(">", 1)[0]
    assert "bg-white" in header_open, "the header stopped painting the column's surface tone"
    assert "border-b" not in header_open and "border-t" not in header_open, \
        "the header grew a hard edge again"


def test_a_rename_started_in_the_header_does_not_pin_the_sidebar():
    page = _page()
    assert "const [editingWhere, setEditingWhere]" in page, \
        "the surface a rename started on is no longer tracked"
    assert "data-editing={editingId && editingWhere === 'sidebar' ? 'true' : undefined}" in page, \
        "any rename pins the sidebar open again, including one begun in the header"
    assert 'editingId && editingWhere === \'sidebar\' ? "md:w-72 md:z-50"' in page, \
        "the sidebar's 288px pin is back on every rename"
    assert page.count("startEditing(s, 'sidebar')") == 2, \
        "a sidebar pencil no longer names its surface"
    assert "startEditing(s, 'header')" in page, "the header is no longer a rename entry point"


def test_only_one_rename_field_can_mount():
    page = _page()
    assert page.count("editingId === s.id && editingWhere === 'sidebar'") == 2, \
        "a sidebar row can mount its field while the header has one open"
    assert page.count("editingId === currentSessionId && editingWhere === 'header'") == 1, \
        "the header's field is no longer scoped to a header rename"
    assert page.count("<RenameInput") == 3, \
        "the rename field was hand-copied again instead of being the shared one"


def test_the_name_has_one_source_for_both_bars():
    """Two bars exist for a structural reason (the desktop one lives inside the
    chat column so it travels inward with the dock). Two derivations would not."""
    page = _page()
    assert "const currentChatTitle = useMemo" in page, "the shared title derivation is gone"
    assert page.count("{currentChatTitle}") >= 2, \
        "one of the two bars derives the chat name for itself again"
    assert page.count("const chatLabel =") == 1 and ".replace('.json', '')" in page, \
        "the legacy-name strip was hand-copied again"


def test_the_top_fade_uses_the_shared_surface_tone():
    page = _page()
    assert "linear-gradient(to bottom, rgb(var(--chat-fog))" in page, \
        "the header fade no longer ends in the surface tone the column paints"
    fade = page.split("linear-gradient(to bottom, rgb(var(--chat-fog))", 1)
    before = fade[0][-700:]
    assert "pointer-events-none" in before, "the fade eats clicks meant for the transcript"
    assert "max-md:hidden" in before, "the fade paints on a layout that has no header"
    assert "rgba(255" not in fade[1][:400] and "#202020" not in fade[1][:400], \
        "a hardcoded colour is back in the fade, which cannot follow the theme"


def test_the_small_layout_still_has_exactly_one_bar():
    page = _page()
    assert 'className="md:hidden shrink-0 h-14 flex items-center gap-2 px-3 border-b' in page, \
        "the small layout's own bar changed shape"
    header = page.split('className="shrink-0 h-16 flex items-center gap-3 pl-6', 1)[1].split(">", 1)[0]
    assert "max-md:hidden" in header, \
        "the desktop header paints on a phone too, giving it two stacked bars"


def test_the_fog_is_the_surface_it_fades_over():
    """Two surfaces fade to --chat-fog now, and nothing in the tree noticed when
    it drifted from the surface it covers. One shade off reads as a grey haze on
    light and a black bar on dark."""
    css = _CSS.read_bytes().decode("utf-8").replace("\r\n", "\n")
    values = {}
    for name in ("--chat-fog", "--sfc-white"):
        values[name] = re.findall(rf"{name}:\s*([0-9 ]+);", css)
    assert len(values["--chat-fog"]) == 2 and len(values["--sfc-white"]) == 2, \
        f"the light/dark pair changed shape: {values}"
    for fog, surface in zip(values["--chat-fog"], values["--sfc-white"]):
        assert fog.strip() == surface.strip(), \
            f"the fog ({fog.strip()}) drifted from the surface it fades over ({surface.strip()})"
