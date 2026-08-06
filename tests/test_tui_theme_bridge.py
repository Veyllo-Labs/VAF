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
    # USERPROFILE too: Path.home() reads HOME on POSIX but USERPROFILE on Windows,
    # so redirecting only HOME leaves the test reading the real ~/.vaf on Windows.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    import importlib

    import vaf.core.config as config_mod

    # The reload has to be undone, and monkeypatch cannot do it: the rebinding happens
    # INSIDE reload(). `importlib.reload` gives config_mod a NEW Config class while every
    # module that already did `from vaf.core.config import Config` keeps the old one. Code
    # that imports Config lazily inside a function then reads the new class, so a later
    # test patching the old one patches something nobody reads. Measured before this
    # restore existed: six unrelated tests went red, three of them the fail-closed
    # WebSocket auth checks, and only when run after this file.
    _original_config = config_mod.Config
    importlib.reload(config_mod)
    try:
        from vaf.cli.tui_app.theme_bridge import persist_theme

        other = next(k for k in THEMES if k != "vaf")
        assert initial_theme_key(None) == "vaf", "a fresh install must start on vaf"

        persist_theme(other)
        assert config_mod.Config.get("theme") == other, "the choice never reached disk"
        assert initial_theme_key(None) == other, "the next start ignored the choice"
    finally:
        config_mod.Config = _original_config


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


# ── one truth about which theme is current ──────────────────────────────────────────

"""The incident this section was written for, and it is the SECOND of its kind.

`test_no_theme_can_paint_the_terminal_light` above records the first: a theme at
the end of the cycle order persisted, and the next start "looked for all the
world like the theme was neither applied nor saved". It happened again, this
time with `matrix` (near-black on #00ff41 green, which reads as a plain terminal),
and the app made it worse rather than better - the settings overlay marked `vaf`
while matrix was on screen, because the marker asked a store nobody had seeded.

Measured at the time of the fix: THREE readers of ThemeManager.current()
(screens.py, settings.py twice) against TWO lanes that seeded it by hand
(run.py's modern lane, main.py) and two that did not (the full-screen app,
`vaf-settings`).
"""


@pytest.fixture(autouse=True)
def _fresh_theme_cache():
    """`_current` is a class attribute and now caches across tests."""
    from vaf.cli.themes import ThemeManager
    before = ThemeManager._current
    ThemeManager._current = None
    yield
    ThemeManager._current = before


def _stored(monkeypatch, value):
    """Make the config report `value` for every key read (theme is the only one
    these tests touch)."""
    import vaf.core.config as config_mod
    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, key, default=None: value))


def _theme_rows():
    """The settings overlay's theme menu, built by the real method. It reads no
    `self`, so it runs without an app - the marker under test is the one a user
    actually sees."""
    from vaf.cli.tui_app.screens import SettingsScreen
    screen = SettingsScreen.__new__(SettingsScreen)
    return screen._menu_rows("theme")


def _marked(rows):
    return [key for key in THEME_ORDER
            if any(f"{key:<12}" in str(r[2]) and "▍" in str(r[2]) for r in rows)]


def test_the_current_theme_is_read_from_the_config_not_hardcoded(monkeypatch):
    from vaf.cli.themes import ThemeManager

    _stored(monkeypatch, "matrix")
    assert ThemeManager.current() == "matrix"


def test_get_theme_resolves_the_store_on_its_own(monkeypatch):
    """It must not depend on someone having called current() first: TUI() asks
    for colors before anything asks for the name (cli/tui.py builds its palette
    in __init__), and an unresolved cache would hand it the default look."""
    from vaf.cli.themes import ThemeManager

    _stored(monkeypatch, "matrix")
    assert ThemeManager.get_theme()["background"] == THEMES["matrix"]["background"]


def test_the_settings_marker_sits_on_the_theme_that_is_on_screen(monkeypatch):
    """The user-visible half. With matrix stored, the app paints matrix - and
    the overlay used to put its marker on `vaf` and contradict the colors."""
    _stored(monkeypatch, "matrix")
    assert initial_theme_key(None) == "matrix", "the app would not be on matrix"
    assert _marked(_theme_rows()) == ["matrix"]


def test_an_explicit_theme_flag_reaches_the_marker(monkeypatch):
    """`--theme` beats the stored value for the app, so it must beat it for the
    overlay too, or the two disagree for the whole session."""
    from vaf.cli.themes import ThemeManager

    _stored(monkeypatch, "matrix")
    ThemeManager.set_theme(initial_theme_key("dracula"))      # what run_tui does
    assert _marked(_theme_rows()) == ["dracula"]


def test_the_app_lane_seeds_the_cache_at_startup():
    """Wiring, not stage: without this line the flag case above cannot happen,
    and the behavioural test would pass over an unwired lane."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "vaf" / "cli" / "tui_app"
           / "app.py").read_text(encoding="utf-8")
    tail = src.split("theme_key = initial_theme_key(theme)", 1)[1][:500]
    assert "ThemeManager.set_theme(theme_key)" in tail


def test_set_theme_overrides_this_process_without_writing_the_config(monkeypatch):
    """The classic `theme <name>` command changes the look for one session only
    (run.py and main.py call set_theme with no Config.set beside it). Resolving
    from the config on every read would silently undo it."""
    import vaf.core.config as config_mod
    from vaf.cli.themes import ThemeManager

    written = []
    _stored(monkeypatch, "vaf")
    monkeypatch.setattr(config_mod.Config, "set",
                        classmethod(lambda cls, k, v: written.append((k, v))))

    assert ThemeManager.set_theme("gruvbox") is True
    assert ThemeManager.current() == "gruvbox"
    assert written == [], "an in-process override reached the config"


def test_a_stored_theme_that_no_longer_exists_does_not_strand_the_manager(monkeypatch):
    """Same guarantee the resolver already gives; the cache must not undercut it."""
    from vaf.cli.themes import ThemeManager

    _stored(monkeypatch, "light")
    assert "light" not in THEMES
    assert ThemeManager.current() == "vaf"
    assert _marked(_theme_rows()) == ["vaf"]


def test_an_unreadable_config_still_yields_a_usable_theme(monkeypatch):
    """current() is called while drawing a menu. It must never raise into a
    paint - a broken config costs the default look, not the overlay."""
    import vaf.core.config as config_mod
    from vaf.cli.themes import ThemeManager

    def _boom(cls, key, default=None):
        raise OSError("config unreadable")

    monkeypatch.setattr(config_mod.Config, "get", classmethod(_boom))
    assert ThemeManager.current() == "vaf"
    assert ThemeManager.get_theme()["background"] == THEMES["vaf"]["background"]


def test_the_config_is_read_once_not_once_per_menu_row(monkeypatch):
    """Config.get re-reads the file on every call (Config.load has no cache) and
    the theme menu asks current() once per catalog entry."""
    import vaf.core.config as config_mod
    from vaf.cli.themes import ThemeManager

    reads = []

    def _counting(cls, key, default=None):
        reads.append(key)
        return "nord"

    monkeypatch.setattr(config_mod.Config, "get", classmethod(_counting))
    rows = _theme_rows()
    assert _marked(rows) == ["nord"]
    assert len(reads) <= 1, f"{len(reads)} config reads for {len(THEME_ORDER)} rows"
    assert ThemeManager.current() == "nord"


# ── browsing is not choosing ────────────────────────────────────────────────────────

"""THE THIRD THEME INCIDENT, same person, same landing spot. `t` used to
persist on every press, so looking through the catalog rewrote the startup
default step by step - and whoever walked the list once ended on its LAST
entry, matrix, which reads as a plain green terminal. Twice the next
`vaf run` then looked like the VAF theme was gone entirely.

The classic lane never had this trap: its `theme <name>` set only the
per-process cache (zero Config.set in that branch, measured), and the config
was written by the deliberate `vaf settings` selection alone. The tests below
pin that restored contract: `t` and `theme <name>` are session-only, the
Settings > Theme row is the one place a theme becomes the default.
"""


def _detached_app():
    import vaf.cli.tui_app.app as app_mod

    record = {"themes": [], "notes": []}

    class _A(app_mod.VafApp):
        theme = property(lambda s: "", lambda s, v: record["themes"].append(v))

        def notify(self, msg, **kw):
            record["notes"].append(str(msg))

    a = _A.__new__(_A)
    a._theme_key = "vaf"
    return a, record


def test_the_t_cycle_writes_no_config(monkeypatch):
    """The headline. Every press used to be a permanent choice.

    Config.get is stubbed alongside Config.set: the cache assertion below
    otherwise resolves from the REAL config file, and this test was caught
    passing under a mutation because the machine's stored theme happened to
    equal the expected one - written seconds earlier by a still-open app
    running the old persisting code, mid live-incident."""
    import vaf.core.config as config_mod
    from vaf.cli.themes import ThemeManager

    written = []
    monkeypatch.setattr(config_mod.Config, "set",
                        classmethod(lambda cls, k, v: written.append((k, v))))
    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, k, d=None: "vaf"))
    app, record = _detached_app()
    app.action_next_theme()
    assert written == [], f"browsing persisted again: {written}"
    assert app._theme_key == THEME_ORDER[1]
    assert record["themes"] == [f"vaf-{THEME_ORDER[1]}"]
    # The session cache moves WITH it, or the settings marker and the classic
    # renderers would name a different theme than the one on screen.
    assert ThemeManager.current() == THEME_ORDER[1]


def test_theme_by_name_is_session_only_like_the_classic_lane(monkeypatch):
    import vaf.core.config as config_mod

    written = []
    monkeypatch.setattr(config_mod.Config, "set",
                        classmethod(lambda cls, k, v: written.append((k, v))))
    app, record = _detached_app()
    app._cmd_theme(["gruvbox"])
    assert written == []
    assert app._theme_key == "gruvbox"


def test_the_notify_says_it_did_not_save():
    """The trap was invisible; the note now names the boundary and the way to
    make a choice stick."""
    app, record = _detached_app()
    app.action_next_theme()
    assert record["notes"] and "session" in record["notes"][0]
    assert "Settings" in record["notes"][0]


def test_the_settings_row_is_still_the_one_that_saves():
    """The other half of the contract, pinned from the source: the deliberate
    selection persists (test_tui_round1_defects drives the behavior)."""
    from pathlib import Path

    import vaf.cli.tui_app.screens as screens_mod

    src = Path(screens_mod.__file__).read_text(encoding="utf-8")
    assert "persist_theme(key)" in src, "the Settings row stopped saving"

    import vaf.cli.tui_app.app as app_mod

    app_src = Path(app_mod.__file__).read_text(encoding="utf-8")
    assert "persist_theme" not in app_src, (
        "a persisting path is back in the app's theme handlers")
