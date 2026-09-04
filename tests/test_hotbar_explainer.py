# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The hotbar explainer: every class it uses exists, and its stages stay in step.

Two failure modes this pins, both silent in a browser:

- A `.vaf-hb-*` class used in the markup with no rule behind it renders an
  empty box. Nothing errors, nothing logs, the drawing just loses a part.
- The pictogram is ONE 12s timeline split across six animations. If a single
  duration is edited, the pointer keeps clicking where the panel no longer is
  and the sequence stops telling the truth - and that is a thing nobody sees
  until they watch a whole loop.
"""
import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_CSS = _REPO / "web" / "app" / "globals.css"
_PAGE = _REPO / "web" / "app" / "page.tsx"


def _css() -> str:
    return _CSS.read_text(encoding="utf-8")


def _section() -> str:
    """The hotbar block of the stylesheet. Everything here is scoped to it -
    globals.css is 1300 lines with three reduced-motion blocks of its own."""
    parts = _css().split("Sub-agent hotbar", 1)
    assert len(parts) == 2, "the hotbar section is gone from globals.css"
    return parts[1]


def test_every_class_the_markup_uses_has_a_rule():
    used = set(re.findall(r"vaf-hb-[a-z0-9-]+", _PAGE.read_text(encoding="utf-8")))
    assert used, "the explainer markup vanished from page.tsx"
    # Only the base rules count. The reduced-motion block names most of the
    # same classes, and counting it would let a class whose ONLY mention is
    # there pass as styled - which is how this test first passed a mutation
    # that renamed a base rule away. Scoped to the hotbar section first: the
    # stylesheet carries three reduced-motion blocks, and splitting on the
    # file's first one lands far above this code.
    base = _section().split("@media (prefers-reduced-motion", 1)[0]
    defined = set(re.findall(r"\.(vaf-hb-[a-z0-9-]+)\s*[,{]", base))
    assert used <= defined, f"classes with no rule in globals.css: {sorted(used - defined)}"


def test_the_stages_share_one_timeline():
    # Every animation on the pictogram runs the same length, or the stages drift.
    durations = set(re.findall(r"animation:\s*vafHb\w+\s+([\d.]+)s", _section()))
    assert durations == {"12"}, f"the explainer's animations disagree: {sorted(durations)}"


def test_the_drawing_animates_nothing_that_repaints():
    # Repeating forever in an always-mounted panel: anything but transform and
    # opacity would repaint every frame for as long as the app is open.
    blocks = re.findall(r"@keyframes\s+vafHb\w+\s*\{(.*?)\n\}", _section(), re.S)
    assert len(blocks) >= 5, "the explainer keyframes are gone"
    for block in blocks:
        declarations = " ".join(re.findall(r"\{([^}]*)\}", block))
        props = set(re.findall(r"([a-z-]+)\s*:", declarations))
        assert props, "a keyframe block with no declarations - the parse is wrong, not the CSS"
        assert props <= {"transform", "opacity"}, f"repainting property in the loop: {sorted(props)}"


def test_the_drawing_runs_the_axis_the_rail_runs():
    """The drawing exists to teach the rail, so it has to keep the rail's shape.
    It taught a vertical strip on the window's right edge until the rail moved
    into the chat header; a drawing left on the old axis is the exact drift this
    file's other rhythm test is written against, and nothing else would notice.

    Which element MOVES is part of the shape: the real group is right-aligned
    and grows leftward, so a pick pushes the globe over while the plus keeps its
    gutter. A drawing where the plus slides teaches the opposite."""
    base = _section().split("@media (prefers-reduced-motion", 1)[0]
    seats = re.search(r"\.vaf-hb-dot\s*\{([^}]*)\}", base)
    assert seats and "top:" in seats.group(1), \
        "the seats no longer share one top, so the rail is not a horizontal band"
    for seat in ("globe", "slot", "plus"):
        rule = re.search(rf"\.vaf-hb-{seat}\s*\{{(.*?)\}}", base, re.S)
        assert rule and "right:" in rule.group(1), \
            f"the {seat} is positioned on the old vertical axis again"
    plus = re.search(r"\.vaf-hb-plus\s*\{(.*?)\}", base, re.S).group(1)
    assert "animation:" not in plus, \
        "the plus moves again - it keeps its gutter while the group grows leftward"
    assert "translateX" in _section() and "translateY" not in _section(), \
        "the rail's step animates on the vertical axis again"


def test_reduced_motion_still_shows_the_end_state():
    # With animations off the drawing must not collapse to an empty frame:
    # the picked state is what the sentence next to it describes.
    block = _section().split("prefers-reduced-motion", 1)[1]
    assert ".vaf-hb-pick { opacity: 1; }" in block
    assert ".vaf-hb-slot { opacity: 1;" in block


# ── the catalogue behind the tiles ────────────────────────────────────────────

def test_the_hotbar_lists_exactly_the_windows_sub_agents():
    """Rule 2: the kinds are a registry with copies. The tiles derive their
    kinds from SUBAGENT_KIND_BY_TOOL rather than listing them again, and this
    pins that the derivation is still in place - a hand-written second list is
    how one of them silently loses an agent."""
    page = _PAGE.read_text(encoding="utf-8")
    assert "SUBAGENT_KINDS: SubAgentKind[] = SUBAGENT_KIND_BY_TOOL.map" in page, \
        "the hotbar stopped deriving its kinds from the window's registry"
    kinds = set(re.findall(r"\[/\w+/i, '(\w+)'\]", page))
    assert kinds == {"coder", "research", "document", "librarian", "browser"}, kinds
    for table in ("SUBAGENT_TOOL_BY_KIND", "SUBAGENT_TRADE_ICON"):
        body = page.split(f"const {table}", 1)[1].split("};", 1)[0]
        listed = set(re.findall(r"^\s{4}(\w+):", body, re.M))
        assert listed == kinds, f"{table} covers {sorted(listed)}, kinds are {sorted(kinds)}"


def test_every_tile_has_wording_in_every_locale():
    # The tiles build their message keys from the kind at runtime, so a missing
    # key is not a compile error - next-intl throws when the panel opens.
    page = _PAGE.read_text(encoding="utf-8")
    kinds = set(re.findall(r"\[/\w+/i, '(\w+)'\]", page))
    for locale in ("en", "de"):
        catalogue = json.loads(
            (_REPO / "web" / "messages" / f"{locale}.json").read_bytes())["main"]
        for kind in kinds:
            cap = kind[0].upper() + kind[1:]
            for key in (f"subAgent{cap}Name", f"subAgent{cap}Desc"):
                assert key in catalogue, f"{locale}.json is missing {key}"
        for key in ("subAgentPaletteSearch", "subAgentPaletteEmpty"):
            assert key in catalogue, f"{locale}.json is missing {key}"


def test_the_browser_is_not_offered_as_a_pick():
    # It already owns a permanent seat in the rail (the globe). Offering it
    # would let someone add a thing that is never absent.
    page = _PAGE.read_text(encoding="utf-8")
    assert "const HOTBAR_KINDS: SubAgentKind[] = SUBAGENT_KINDS.filter(k => k !== 'browser');" in page
    assert "HOTBAR_KINDS\n" in page and ".map(kind => {" in page, \
        "the tiles stopped deriving from the browser-excluded list"


def test_a_specialist_tile_wears_its_own_accent_without_a_second_colour_list():
    """The palette tile tints the agent's BODY with the trade's accent and leaves
    the eye white, so the creature stays the same and the trade is readable
    before the name is.

    The invariant worth guarding is not the colour, it is where the colour comes
    from: `currentColor` inherited from the accent class the tile already uses
    for its badge. A hex here would be a second list beside SUBAGENT_ACCENT,
    and the two would drift the first time the dark palette moves one of them.
    """
    page = _PAGE.read_text(encoding="utf-8")
    tile = page.split("visibleHotbarPicks.map", 1)[1] if "visibleHotbarPicks.map" in page else page
    tile = page.split("toggleHotbarPick(kind)", 1)[1].split("</button>", 1)[0]
    assert "body: 'currentColor', ...SPECIALIST_SKIN" in tile, \
        "the palette tile stopped tinting the specialist's body from the accent class"
    assert "kind === 'coder' ? undefined" in tile, "the coder lost its plain body"
    assert "dot:" not in tile, "the eye is being tinted too - it stays white"
    # The accent must reach the avatar as a CLASS, not as a value copied here.
    assert "SUBAGENT_ACCENT[kind].icon" in tile
    for hexish in ("#ea580c", "#7c3aed", "#0f766e", "orange-600 ", "violet-600 "):
        assert hexish not in tile, f"a colour value was copied into the tile: {hexish}"


def test_the_rail_positions_come_from_one_rhythm():
    """The rail's spacing exists three times now: the plus, the picked agents,
    and the drawing that explains them. Hard-coded pixels in any of those three
    is how they drift apart - the drawing would then teach a rhythm the real
    rail does not keep.

    The rail runs left to right in the chat header, so the step is a CELL WIDTH:
    every seat is one cell with its glyph centred, and centre to centre is the
    same for every pair whatever the glyph's own size is."""
    page = _PAGE.read_text(encoding="utf-8")
    assert "const RAIL_STEP = 33;" in page
    assert "RAIL_STEP_TOP" not in page, "the vertical rail's top offset came back"
    assert page.count("style={{ width: RAIL_STEP }}") == 3, \
        "the globe, a pick and the plus are one cell each - a hand-sized seat drifts from the drawing"
    # No hard-coded seat on either axis, and none of the offsets the shared right
    # edge used to need.
    for stale in ("top-[51px]", "left-[51px]", "w-[33px]", "right-[19px]", "right-[21px]"):
        assert stale not in page, f"a hard-coded rail offset came back: {stale}"


# The click's behaviour moved on: an icon used to write the tool mention into the
# message box, and now it OPENS that sub-agent's window while the turn carries
# which window is open. Pinned in tests/test_subagent_window_context.py, which is
# where the whole lane (chip, payload field, prompt block) is guarded together.


# ─────────────────────────────────────────────────────────────────────────────
# The explanation card: shows itself once, leaves on time, comes back on request
# ─────────────────────────────────────────────────────────────────────────────

def test_the_card_and_its_bar_keep_one_clock():
    """The timer that closes the card and the bar that shows the time running
    out must read the SAME constant: the stylesheet must not carry a duration
    of its own for the bar, and the markup must set it inline from the timer's
    value. MUTATION: write `animation: vafHbHelpBar 10s` into the CSS, or a
    literal into setTimeout, and this goes red."""
    page = _PAGE.read_text(encoding="utf-8")
    assert re.search(r"setTimeout\(\(\) => \{[^}]*?\}, HOTBAR_HELP_AUTO_MS\)", page, re.S), (
        "the auto-close timer no longer takes its delay from HOTBAR_HELP_AUTO_MS")
    assert "animationDuration: `${HOTBAR_HELP_AUTO_MS}ms`" in page, (
        "the countdown bar no longer takes its duration from HOTBAR_HELP_AUTO_MS")
    section = _section()
    assert not re.search(r"animation:\s*vafHbHelpBar", section), (
        "the stylesheet carries a duration for the bar; the timer and the bar can now disagree")
    assert "animation-name: vafHbHelpBar" in section


def test_running_out_or_dismissing_marks_it_seen_and_closing_the_palette_does_not():
    """Seen means read: the timer's expiry and the hand dismissal both write
    the flag; the effect's palette-closed branch must not, or a card cut short
    would never show again."""
    page = _PAGE.read_text(encoding="utf-8")
    expiry = re.search(r"setTimeout\(\(\) => \{(.*?)\}, HOTBAR_HELP_AUTO_MS\)", page, re.S).group(1)
    assert "markSubAgentHelpSeen()" in expiry, "running out no longer marks the card seen"
    close_fn = re.search(r"const closeSubAgentHelp = \(\) => \{(.*?)\n    \};", page, re.S).group(1)
    assert "markSubAgentHelpSeen()" in close_fn, "dismissing no longer marks the card seen"
    closed_branch = re.search(r"if \(!subAgentPaletteOpen\) \{(.*?)return;", page, re.S).group(1)
    assert "markSubAgentHelpSeen" not in closed_branch and "setItem" not in closed_branch, (
        "closing the palette marks the card seen; a card nobody could read is lost")
    # The flag lives in browser storage, which may be unavailable: guarded reads and writes.
    assert "try { localStorage.setItem(HOTBAR_HELP_SEEN_KEY, '1'); } catch" in page
    assert "try { seen = localStorage.getItem(HOTBAR_HELP_SEEN_KEY) === '1'; } catch" in page


def test_the_card_answers_escape_above_the_palette():
    """Escape while the card is up closes the card, not the palette under it:
    the card registers one level above the palette's own layer."""
    page = _PAGE.read_text(encoding="utf-8")
    palette = re.search(r"useEscapeLayer\(\{ active: subAgentPaletteOpen, level: (\d+)", page)
    card = re.search(r"useEscapeLayer\(\{ active: subAgentPaletteOpen && subAgentHelpOpen, level: (\d+), onEscape: closeSubAgentHelp", page)
    assert palette and card, "one of the two escape layers is gone"
    assert int(card.group(1)) > int(palette.group(1))


def test_the_card_has_wording_in_every_locale():
    for path in sorted((_REPO / "web" / "messages").glob("*.json")):
        catalogue = json.loads(path.read_bytes())["main"]
        for key in ("subAgentPaletteHelp", "subAgentPaletteHelpClose"):
            assert key in catalogue, f"{path.name} is missing {key}"


def test_the_tiles_keep_one_width_and_wrap():
    """A tile has a fixed track, never a share of the row: a column count
    (`grid-cols-N`) hands a 5120px window four 1200px tiles."""
    page = _PAGE.read_text(encoding="utf-8")
    i = page.index("shown.map(({ kind, name, desc, Icon })")
    grid = page[page.rfind("<div className=\"grid", 0, i):i]
    assert "repeat(auto-fill," in grid, "the tile grid stretches again: no fixed track"
    assert not re.search(r"\bgrid-cols-\d", grid), "a column count is back on the tile grid"
