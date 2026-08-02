# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""vaf themes -> Textual themes, plus the one color that is never themed.

The single source of truth is `vaf.cli.themes.THEMES` - this module holds NO
color values of its own, so a theme added there appears here without an edit
(pinned by tests/test_tui_theme_bridge.py). Persistence is the existing
`theme` config key, the same one the classic lane reads at startup.
"""
from typing import Optional

from textual.theme import Theme

from vaf.cli.themes import THEMES, ThemeManager

# The agent's eye. Identity, not decoration: it stays white in every theme.
WHITE = "#ffffff"

THEME_ORDER = list(THEMES)

# Textual ships builtin themes under names like "dracula"/"nord"; the prefix
# keeps ours from colliding with (or silently shadowing) those.
_PREFIX = "vaf-"


def textual_theme_name(key: str) -> str:
    return f"{_PREFIX}{key}"


def make_textual_theme(key: str) -> Theme:
    """One vaf theme as a Textual Theme; vaf-only colors ride as variables."""
    c = THEMES[key]
    return Theme(
        name=textual_theme_name(key),
        primary=c["primary"],
        secondary=c["secondary"],
        accent=c["accent"],
        foreground=c["text"],
        background=c["background"],
        surface=c["background_panel"],
        panel=c["background_panel"],
        success=c["success"],
        warning=c["warning"],
        error=c["error"],
        dark=True,
        variables={
            "vaf-border": c["border"],
            "vaf-border-active": c["border_active"],
            "vaf-muted": c["text_muted"],
            "vaf-info": c["info"],
        },
    )


def css_variables_for(key: str) -> dict:
    """The vaf-* variables for App.get_css_variables - they must exist from the
    very first CSS parse, before any theme is registered."""
    c = THEMES.get(key) or THEMES["vaf"]
    return {
        "vaf-border": c["border"],
        "vaf-border-active": c["border_active"],
        "vaf-muted": c["text_muted"],
        "vaf-info": c["info"],
    }


def initial_theme_key(cli_value: Optional[str] = None) -> str:
    """CLI flag beats config beats default - the same precedence `vaf run` uses."""
    if cli_value and cli_value in THEMES:
        return cli_value
    from vaf.core.config import Config
    stored = str(Config.get("theme", "vaf") or "vaf")
    return stored if stored in THEMES else "vaf"


def persist_theme(key: str) -> None:
    """Make a theme choice survive the relaunch: config key + in-process manager."""
    if key not in THEMES:
        return
    from vaf.core.config import Config
    Config.set("theme", key)
    ThemeManager.set_theme(key)
