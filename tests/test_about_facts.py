# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One source for what About says, and three renderers that only style it.

THE DRIFT THIS PINS: the legal lines lived inline in three places - the
`vaf about` command, the inquirer settings menu (`show_about`), and now the
terminal app's overlay - and the first two had already drifted apart
(different version resolution, different credit line). Words that name the
licence and the contact address must not depend on which door a user opened.

The old `vaf about` also resolved its version through `importlib.util`
WITHOUT importing it (a latent AttributeError) and raised
PackageNotFoundError in a source checkout; the extraction removes both.
"""
import inspect

from vaf.cli.cmd.info import about_facts


def test_the_facts_resolve_in_a_source_checkout():
    """No dist metadata needed: the version falls back to vaf.version."""
    facts = about_facts()
    assert facts["version"] and facts["version"] != "dev"
    assert facts["copyright"].endswith("Veyllo GmbH")
    assert any("AGPL" in line for line in facts["license"])
    assert facts["contact"] == "legal@veyllo.io"
    assert facts["terms_url"].endswith("LICENSING.md")
    assert dict(facts["links"])["GitHub"].startswith("https://github.com/")


def test_the_shared_atoms_appear_exactly_once_in_the_facts():
    """The URL and the contact live as atoms; the license sentences are BUILT
    from them, so changing the atom changes every renderer at once."""
    facts = about_facts()
    assert facts["terms_url"] in facts["license"][1]
    assert facts["contact"] in facts["license"][2]


def test_the_typer_command_renders_from_the_facts():
    from vaf.cli.cmd import info

    src = inspect.getsource(info.about)
    assert "about_facts()" in src
    assert "legal@veyllo.io" not in src, "the contact is inlined again"
    assert "LICENSING.md" not in src, "the terms URL is inlined again"


def test_the_inquirer_menu_renders_from_the_facts():
    """show_about keeps its mascot and its copyleft prose - but the version,
    the copyright, the contact and the URL are the shared atoms."""
    import vaf.cli.cmd.settings as settings_mod

    src = inspect.getsource(settings_mod.show_about)
    assert "about_facts" in src
    for literal in ("legal@veyllo.io", "LICENSING.md/", "from vaf import __version__"):
        assert literal not in src, f"drifted copy is back: {literal}"


def test_the_overlay_renders_from_the_facts_and_the_menu_offers_it():
    from vaf.cli.tui_app import screens as screens_mod

    src = inspect.getsource(screens_mod.AboutScreen)
    assert "about_facts()" in src
    assert "veyllo.io" not in src.replace("legal@veyllo.io", ""), (
        "the overlay carries its own copy of a link")

    s = screens_mod.SettingsScreen.__new__(screens_mod.SettingsScreen)
    s._cfg = lambda k, d=None: {"provider": "local"}.get(k, d)
    kinds = [kind for kind, _arg, _l in s._menu_rows("main")]
    assert "about" in kinds, "the settings menu lost the About row"


def test_activating_the_row_opens_the_overlay():
    from types import SimpleNamespace

    from vaf.cli.tui_app.screens import AboutScreen, SettingsScreen

    pushed = []
    fake_app = SimpleNamespace(push_screen=lambda scr, cb=None: pushed.append(scr),
                               notify=lambda *a, **k: None,
                               post_message=lambda m: None)

    class _Detached(SettingsScreen):
        app = property(lambda s: fake_app)

    s = _Detached.__new__(_Detached)
    s._cfg = lambda k, d=None: {"provider": "local"}.get(k, d)
    s._refresh_labels = lambda: None
    s._stack = ["main"]
    s._mic_devices = None
    s._rows = s._menu_rows("main")
    idx = next(i for i, r in enumerate(s._rows) if r[0] == "about")
    s._activate(idx)
    assert pushed and isinstance(pushed[0], AboutScreen)
