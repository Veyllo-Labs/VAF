# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`white` is not white in dark mode, so `dark:...bg-white` is a trap.

The dark theme is a FOLDING PALETTE: tailwind.config.ts maps `white` to
`rgb(var(--sfc-white))`, and globals.css re-points that variable to #202020
under `.dark` (the elevated-surface tone). Writing `dark:bg-white` therefore
asks for the DARK surface colour, which is the opposite of what the words say.

It shipped: the update dialog's primary button is light with dark text in dark
mode, and its hover was `dark:hover:bg-white`. Hovering folded the button to
#202020 while the label stayed #181818 - a dark label on a dark button, which
is how a user found it. Every other button of that shape in the app already
used the literal `dark:hover:bg-[#f5f5f5]`; those five did not.

NAMED BOUNDARY: the `/opacity` forms (`dark:bg-white/5`,
`dark:hover:bg-white/10`) are NOT banned here. They are also folded - a faint
dark film where a faint light one was meant - but they degrade to "hover barely
shows" rather than "text disappears", they are used deliberately as an overlay
idiom in several places, and changing them is a design decision rather than a
defect fix. If that is taken on, this is the guard to widen.
"""
import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# A bare `white` in any dark: variant - `dark:bg-white`, `dark:hover:bg-white`,
# `dark:group-hover:bg-white` - but never the `white/<opacity>` spelling.
_BARE_DARK_WHITE = re.compile(r"dark:(?:[a-z-]+:)*bg-white(?![/\w-])")


def _tracked_tsx():
    out = subprocess.run(["git", "ls-files", "-z", "web/"], cwd=_REPO,
                         capture_output=True, check=True).stdout.decode("utf-8", "ignore")
    for rel in out.split("\0"):
        if rel.endswith((".tsx", ".ts")) and (_REPO / rel).is_file():
            yield rel, _REPO / rel


def test_the_fold_is_real():
    """Why this guard exists, shown rather than claimed: the config maps white to
    the surface variable, and the dark block points that at a near-black."""
    cfg = (_REPO / "web" / "tailwind.config.ts").read_text(encoding="utf-8")
    css = (_REPO / "web" / "app" / "globals.css").read_text(encoding="utf-8")
    assert 'white: v("sfc-white")' in cfg, "white is no longer folded; this guard may be obsolete"
    dark_block = css.split(".dark {", 1)[1].split("\n}", 1)[0]
    assert "--sfc-white: 32 32 32" in dark_block, \
        "the dark surface tone moved; check whether bg-white is still a trap"


def test_no_bare_white_surface_under_a_dark_variant():
    hits = []
    for rel, path in _tracked_tsx():
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if _BARE_DARK_WHITE.search(line):
                hits.append(f"{rel}:{i}")
    assert not hits, (
        "`white` folds to #202020 under .dark, so these ask for a DARK surface "
        "while reading as white. Use the literal the rest of the app uses "
        "(dark:hover:bg-[#f5f5f5]) or an explicit hex:\n  " + "\n  ".join(hits)
    )
