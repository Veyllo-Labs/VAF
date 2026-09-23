# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A window written in raw hex has only one theme, and it is not the default one.

The palette is a folding per-utility swap: `bg-white`, `bg-gray-50..400`, the borders
and `text-gray-400..900` resolve through CSS variables that `.dark` re-points, so a
window built from those utilities turns dark by itself (docs/web-ui/DARKMODE.md,
docs/web-ui/LIGHTMODE.md). A raw hex resolves to nothing: it renders the same colour
in both themes.

It shipped that way. Every window built after dark mode arrived was written dark-first
in raw hex - the mail client, the account panel, the shared messenger shell and through
it the WhatsApp, Telegram and Discord windows, and the inbox. 547 hex sites across
eight files, none of them behind a `dark:` variant, so a person with light mode on (the
default) opened the inbox and got a near-black window inside a white app. Unit tests
could not see it: nothing renders colour, and the type checker is happy either way.

The two halves of the defect, which are the two checks below:

- a DARK hex on a base surface class (`bg-[#181818]`, `border-[#2e2e2e]`) is a dark
  surface in light mode;
- a LIGHT hex on a base ink class (`text-[#e8e8e8]`) is light text on a light surface,
  which is invisible rather than merely wrong.

Both are about the BASE class only. `dark:bg-[#181818]` is the correct way to state a
dark value, and a dark ink or a light surface on a base class is ordinary light-mode
styling, so neither is flagged.

NAMED BOUNDARY: this reads utility classes, not inline styles or JS colour tables.
`style={{background: '#262626'}}` is the same defect and this guard cannot see it; the
files that theme themselves that way read `useThemeStore` and are listed below with
that as their reason.
"""
import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# Utilities that paint a surface, and utilities that paint ink.
_SURFACE = ("bg", "border", "ring", "divide", "outline", "from", "via", "to", "shadow", "accent")
_INK = ("text", "placeholder", "decoration", "caret")

# A whole class token, e.g. `md:hover:bg-[#262626]/80` or `focus:border-[#444]`.
_TOKEN = re.compile(r"^(?P<mods>(?:[^\s\"'`]*:)?)(?P<util>[a-z][a-z-]*?)-\[#(?P<hex>[0-9a-fA-F]{3,8})\](?:/\d+)?$")

# "Dark" and "light" as the fold itself draws the line: the surface ramp tops out at
# #484848 (bg-gray-400) and the ink ramp bottoms out at #6b6b6b (text-gray-400).
_DARK_MAX = 0x60
_LIGHT_MIN = 0xC0

# Files whose raw hex is deliberate. A reason, not a name, earns a place here: a
# surface that is meant to look the same in both themes, or one that themes itself in
# JavaScript where this guard cannot follow.
_ALLOWED = {
    "web/components/CodeViewer.tsx":
        "the code viewer wears Monaco's own vs-dark chrome, a protected surface that stays dark",
    "web/components/HtmlViewer.tsx":
        "same editor chrome, around an iframe that carries its own document",
    "web/components/SettingsModal.tsx":
        "the embedded Monaco editor's tones (#1e1e1e, #252526, #d4d4d4) belong to vs-dark",
    "web/components/settings/CustomToolEditor.tsx":
        "the embedded Monaco editor's tones belong to vs-dark",
    "web/components/SubAgentWindow.tsx":
        "the WIN_* constants pair a light value with a dark one, and the editor surfaces "
        "are chosen by its own editorDark flag",
    "web/components/NotificationsModal.tsx":
        "the timeline draws with the tlColors(dark) token set, resolved through useThemeStore",
    "web/components/BrowserLiveTile.tsx":
        "the letterbox behind a live browser frame, dark like any media backdrop",
    "web/app/login/page.tsx":
        "the theme picker's own swatches: the dark option has to look dark while light mode is on",
}


def _tracked_web_files():
    out = subprocess.run(["git", "ls-files", "-z", "web/"], cwd=_REPO,
                         capture_output=True, check=True).stdout.decode("utf-8", "ignore")
    for rel in out.split("\0"):
        if rel.endswith((".tsx", ".ts")) and (_REPO / rel).is_file():
            yield rel, _REPO / rel


def _channels(raw: str):
    h = raw.lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) not in (6, 8):
        return None
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _offences():
    """Every base-class hex that can only be right in one theme, by kind."""
    dark_surfaces, light_inks = [], []
    for rel, path in _tracked_web_files():
        if rel in _ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for word in re.split(r"[\s\"'`{}(),]+", line):
                m = _TOKEN.match(word)
                if not m or "dark:" in m.group("mods"):
                    continue
                rgb = _channels(m.group("hex"))
                if rgb is None:
                    continue
                family = m.group("util").split("-")[-1]
                where = f"{rel}:{lineno}  {word}"
                if family in _SURFACE and max(rgb) <= _DARK_MAX:
                    dark_surfaces.append(where)
                elif family in _INK and min(rgb) >= _LIGHT_MIN:
                    light_inks.append(where)
    return dark_surfaces, light_inks


def test_the_fold_is_what_makes_a_plain_utility_theme_itself():
    """Shown rather than claimed: the same utility resolves to a light value at :root
    and a dark one under .dark, which is why a raw hex has only one theme."""
    css = (_REPO / "web" / "app" / "globals.css").read_text(encoding="utf-8")
    # The light values live in the `:root` declaration that also carries the doc-paper
    # escape (`:root, .dark .vaf-doc-paper`); the dark ones in the `.dark` block.
    light, dark = css.split(".dark {", 1)
    dark = dark.split("\n}", 1)[0]
    assert "--sfc-gray-50: 249 250 251" in light and "--sfc-gray-50: 24 24 24" in dark
    assert "--txt-gray-900: 17 24 39" in light and "--txt-gray-900: 236 236 236" in dark


def test_no_dark_surface_on_a_base_class():
    dark_surfaces, _ = _offences()
    assert not dark_surfaces, (
        "a dark colour on a base class paints a dark surface while light mode is on.\n"
        "Write the light value as the base class and the dark one as a dark: variant, or\n"
        "use the folding neutrals (bg-white, bg-gray-50..400, border-gray-100..400) which\n"
        "already carry both. docs/web-ui/LIGHTMODE.md has the table.\n  "
        + "\n  ".join(dark_surfaces)
    )


def test_no_light_ink_on_a_base_class():
    _, light_inks = _offences()
    assert not light_inks, (
        "light text on a base class is invisible on a light surface. Use text-gray-700..900\n"
        "(they fold), or state the light value as a dark: variant. Never dark:text-gray-900\n"
        "for dark ink: the text ramp folds, so it renders LIGHT.\n  "
        + "\n  ".join(light_inks)
    )


def test_every_exemption_carries_a_reason_and_still_exists():
    """An allowlist rots into a blanket. Each entry names a file that is really there and
    gives a reason a stranger can weigh."""
    tracked = {rel for rel, _ in _tracked_web_files()}
    for rel, reason in _ALLOWED.items():
        assert rel in tracked, f"{rel} is exempted but no longer tracked; drop the entry"
        assert len(reason.split()) >= 6, f"{rel}: the exemption needs a reason, not a label"
