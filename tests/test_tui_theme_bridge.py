# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The theme bridge: one source of truth, and one color that never themes.

`vaf.cli.themes.THEMES` is the catalog; the bridge holds NO color values of
its own. The drift guard here is the whole point: a theme added to the catalog
must appear in the app without an edit, and a color changed there must change
here - a copied palette would pass today and rot tomorrow.
"""
import pytest

from vaf.cli.themes import THEMES
from vaf.cli.tui_app.theme_bridge import (
    THEME_ORDER,
    WHITE,
    css_variables_for,
    initial_theme_key,
    make_textual_theme,
    textual_theme_name,
)


def test_the_agent_eye_is_white_and_never_themed():
    """Identity, not decoration. Every avatar frame renders through this pin."""
    assert WHITE == "#ffffff"


def test_every_catalog_theme_builds_without_an_edit():
    assert THEME_ORDER == list(THEMES)
    for key in THEMES:
        theme = make_textual_theme(key)
        assert theme.name == f"vaf-{key}"


@pytest.mark.parametrize("key", list(THEMES))
def test_colors_come_from_the_catalog_not_from_copies(key):
    c = THEMES[key]
    theme = make_textual_theme(key)
    assert theme.primary == c["primary"]
    assert theme.background == c["background"]
    assert theme.surface == c["background_panel"]
    assert theme.error == c["error"]
    assert theme.variables["vaf-border"] == c["border"]
    assert theme.variables["vaf-muted"] == c["text_muted"]


@pytest.mark.parametrize("key", list(THEMES))
def test_css_variables_cover_the_vaf_set(key):
    """These four must exist from the very first CSS parse - a missing one is
    an UnresolvedVariableError at app startup, not a wrong color."""
    got = css_variables_for(key)
    assert set(got) == {"vaf-border", "vaf-border-active", "vaf-muted", "vaf-info"}


def test_theme_names_carry_the_prefix():
    """Unprefixed names could shadow Textual's builtin themes (nord, dracula)."""
    for key in THEMES:
        assert textual_theme_name(key).startswith("vaf-")


def test_initial_theme_precedence(monkeypatch):
    """CLI flag beats config beats default - the same order `vaf run` uses."""
    import vaf.core.config as config_mod

    known = [k for k in THEMES][:2]
    if len(known) < 2:
        pytest.skip("needs at least two themes in the catalog")

    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, key, default=None: known[1]))
    assert initial_theme_key(known[0]) == known[0]      # CLI wins
    assert initial_theme_key(None) == known[1]          # config wins
    assert initial_theme_key("no-such-theme") == known[1]

    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, key, default=None: "garbage"))
    assert initial_theme_key(None) == "vaf"             # default wins


def test_persist_rejects_unknown_keys(monkeypatch):
    """An unknown key must not land in the config - the classic lane reads the
    stored value at startup and would fall over a phantom theme."""
    import vaf.core.config as config_mod

    written = []
    monkeypatch.setattr(config_mod.Config, "set",
                        classmethod(lambda cls, k, v: written.append((k, v))))
    from vaf.cli.tui_app.theme_bridge import persist_theme
    persist_theme("no-such-theme")
    assert written == []


# ── the monochrome default (round 9) ────────────────────────────────────────────────

def _luminance(hex_colour: str) -> float:
    parts = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    parts = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
             for c in parts]
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _saturation(hex_colour: str) -> int:
    channels = [int(hex_colour[i:i + 2], 16) for i in (1, 3, 5)]
    return max(channels) - min(channels)


def test_the_default_theme_chrome_is_greyscale():
    """The chrome must not compete with the content - and it is what makes the
    agent's white eye the brightest thing on screen."""
    theme = THEMES["vaf"]
    for key in ("primary", "secondary", "info", "text", "text_muted",
                "background", "background_panel", "border", "border_active"):
        assert _saturation(theme[key]) <= 12, (
            f"{key}={theme[key]} carries colour, not a grey step")


def test_the_semantic_colours_survive_the_greyscale():
    """Desaturated far enough to sit in the ramp, saturated enough that a gate
    warning, an error and a success are still told apart - which is the one job
    colour has left here."""
    theme = THEMES["vaf"]
    for key in ("success", "warning", "error"):
        sat = _saturation(theme[key])
        assert sat >= 25, f"{key} lost its meaning ({theme[key]})"
        assert sat <= 110, f"{key} is too loud for a monochrome theme"

    # ...and they must not collapse into one another.
    hues = {k: theme[k] for k in ("success", "warning", "error")}
    assert len(set(hues.values())) == 3


def test_the_default_theme_is_readable():
    theme = THEMES["vaf"]
    assert _contrast(theme["text"], theme["background"]) >= 7.0
    assert _contrast(theme["text_muted"], theme["background"]) >= 4.5, (
        "muted text below the AA floor is decoration, not information")
    assert _contrast(theme["primary"], theme["background"]) >= 7.0


def test_the_ramp_actually_steps():
    """Panel above background, border above panel, active border above border -
    otherwise the greyscale is flat and nothing separates visually."""
    theme = THEMES["vaf"]
    ordered = ["background", "background_panel", "border", "border_active",
               "text_muted", "text"]
    values = [_luminance(theme[k]) for k in ordered]
    assert values == sorted(values), dict(zip(ordered, values))


def test_the_monochrome_theme_is_the_declared_default():
    """It used to work only through a fallback inside the resolver, with the
    key absent from DEFAULTS - so nothing declared what a fresh install gets."""
    from vaf.core.config import Config

    assert Config.DEFAULTS.get("theme") == "vaf"
    assert initial_theme_key(None) in THEMES


def test_a_chosen_theme_survives_the_next_start(monkeypatch, tmp_path):
    """The choice is written to the config, and the resolver reads that key -
    otherwise every `vaf run` would reset the look."""
    monkeypatch.setenv("HOME", str(tmp_path))
    import importlib

    import vaf.core.config as config_mod
    importlib.reload(config_mod)

    from vaf.cli.tui_app.theme_bridge import persist_theme

    other = next(k for k in THEMES if k != "vaf")
    assert initial_theme_key(None) == "vaf", "a fresh install must start on vaf"

    persist_theme(other)
    assert config_mod.Config.get("theme") == other, "the choice never reached disk"
    assert initial_theme_key(None) == other, "the next start ignored the choice"


def test_no_theme_can_paint_the_terminal_light():
    """The real scar. A `light` theme sat at the END of the cycle order, so
    pressing `t` often enough landed on it - and it PERSISTS, so the next
    start came up white with the agent's white mark invisible on it. It looked
    for all the world like the theme was neither applied nor saved; both were
    working, the stored value was simply a light theme.

    A terminal agent is a dark surface. Every theme in the catalog has to be
    one, or the same trap reopens the moment somebody adds a "solarized light"."""
    for key, theme in THEMES.items():
        background = _luminance(theme["background"])
        assert background < 0.25, (
            f"{key} has a light background ({theme['background']}) - the white "
            f"agent mark and every white accent become invisible on it")


def test_a_stored_theme_that_no_longer_exists_falls_back(monkeypatch):
    """Removing a theme must not strand anyone who had it selected."""
    import vaf.core.config as config_mod

    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, key, default=None: "light"))
    assert initial_theme_key(None) == "vaf"
    assert "light" not in THEMES
