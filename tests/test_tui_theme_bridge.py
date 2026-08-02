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
