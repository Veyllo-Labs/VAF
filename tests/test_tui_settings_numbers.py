# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Free numeric values in the settings overlay, and the timeout-zero rule.

Two gaps from the parity audit close here: the context limit was an inert
"use `vaf settings`" row although writing `n_ctx` is exactly what that menu
does, and the numeric choices offered only fixed presets while the classic
menu took any value in range.

THE RULE THIS FILE EXISTS TO PIN, because it is a live footgun:
`subagent_timeout_minutes = 0` with `subagent_timeout_enabled = True` makes
the cleanup pass compute `cutoff = now - 0` - EVERY running sub-agent is
timed out on its next sweep (vaf/core/subagent_ipc.py). The classic menu
never stored the 0: it switched the ENABLED key off and left the minutes
alone (cli/cmd/settings.py, the duration branch). The overlay's own preset
"no limit" row used to store the 0 and carried exactly that defect; presets
and the Custom field now share one writer so the rule cannot fork again.
"""
from types import SimpleNamespace

import pytest

from vaf.cli.tui_app.screens import NumberScreen, SettingsScreen


# ── the shared writer ───────────────────────────────────────────────────────────────

class _Rig:
    """A detached SettingsScreen whose config and app are records."""

    def __init__(self, monkeypatch, values=None):
        import vaf.core.config as config_mod

        self.values = dict(values or {})
        self.written = {}
        self.notified = []
        monkeypatch.setattr(
            config_mod.Config, "set",
            classmethod(lambda cls, k, v: self.written.__setitem__(k, v)))

        fake_app = SimpleNamespace(
            notify=lambda msg, **kw: self.notified.append(msg),
            post_message=lambda msg: None,
            push_screen=lambda screen, cb=None: self.pushed.append((screen, cb)),
        )
        self.pushed = []

        class _Detached(SettingsScreen):
            app = property(lambda s: fake_app)

        s = _Detached.__new__(_Detached)
        s._cfg = lambda key, default=None: self.values.get(key, default)
        s._refresh_labels = lambda: None
        s._rebuild = lambda: None
        s._sync_live_agent = lambda k, v: None
        s._stack = ["main", "choice:x"]
        s._mic_devices = None
        self.screen = s


def test_zero_switches_the_timeout_off_instead_of_arming_it(monkeypatch):
    """The headline. Writing the 0 would time out every running sub-agent."""
    rig = _Rig(monkeypatch)
    rig.screen._write_choice("subagent_timeout_minutes", 0)
    assert rig.written == {"subagent_timeout_enabled": False}, (
        f"the zero reached the config: {rig.written}")
    assert any("off" in m for m in rig.notified), rig.notified


def test_a_real_duration_writes_minutes_and_switches_on(monkeypatch):
    """The classic contract's other half: choosing a duration means you want
    a timeout, so the enabled key follows."""
    rig = _Rig(monkeypatch)
    rig.screen._write_choice("subagent_timeout_minutes", 60)
    assert rig.written == {"subagent_timeout_minutes": 60,
                           "subagent_timeout_enabled": True}


def test_a_custom_duration_is_clamped_to_the_classic_range(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.screen._write_choice("subagent_timeout_minutes", 9999)
    assert rig.written["subagent_timeout_minutes"] == 480


def test_an_ordinary_key_writes_plainly(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.screen._write_choice("ux_auto_open_max_tabs", 7)
    assert rig.written == {"ux_auto_open_max_tabs": 7}


def test_n_ctx_says_when_it_applies(monkeypatch):
    """n_ctx is a llama-server launch argument and a build-time snapshot at
    its dominant consumers - a notify that claims immediate effect would be
    the provider-row lie all over again."""
    rig = _Rig(monkeypatch)
    rig.screen._write_choice("n_ctx", 65536)
    assert rig.written == {"n_ctx": 65536}
    assert any("next start" in m for m in rig.notified), rig.notified


# ── the rows ────────────────────────────────────────────────────────────────────────

def _rows_for(key, values=None):
    s = SettingsScreen.__new__(SettingsScreen)
    s._cfg = lambda k, default=None: (values or {}).get(k, default)
    return s._choice_rows(key)


@pytest.mark.parametrize("key", ["n_ctx", "subagent_timeout_minutes",
                                 "ux_auto_open_max_tabs"])
def test_numeric_keys_offer_a_custom_row(key):
    rows = _rows_for(key)
    assert ("number", key, "Custom...") in rows


def test_non_numeric_keys_offer_no_custom_row():
    rows = _rows_for("speech_language")
    assert not [r for r in rows if r[0] == "number"]


def test_the_no_limit_marker_follows_the_switch_not_the_minutes():
    """With the timeout DISABLED and minutes still 60, the truth is "no
    limit" - marking "60 minutes" would name a limit that is not in force."""
    rows = _rows_for("subagent_timeout_minutes",
                     {"subagent_timeout_minutes": 60,
                      "subagent_timeout_enabled": False})
    marked = [r[2] for r in rows if "▍" in str(r[2])]
    assert len(marked) == 1 and "no limit" in marked[0], marked

    rows = _rows_for("subagent_timeout_minutes",
                     {"subagent_timeout_minutes": 60,
                      "subagent_timeout_enabled": True})
    marked = [r[2] for r in rows if "▍" in str(r[2])]
    assert len(marked) == 1 and "60 minutes" in marked[0], marked


def test_the_context_row_is_a_choice_now():
    """The audit gap: an inert pointer for a write the classic menu performs."""
    s = SettingsScreen.__new__(SettingsScreen)
    s._cfg = lambda k, default=None: {"provider": "local"}.get(k, default)
    kinds = {arg: kind for kind, arg, _ in s._menu_rows("main")}
    assert kinds.get("n_ctx") == "choice"
    assert "context" not in kinds


def test_the_number_row_opens_the_screen_and_writes_through_the_shared_writer(monkeypatch):
    """Wiring: activation pushes NumberScreen ON TOP (never dismiss-then-push,
    which would throw the settings stack away), and the callback lands in
    _write_choice - the same writer the presets use."""
    rig = _Rig(monkeypatch, {"subagent_timeout_minutes": 30})
    rig.screen._rows = rig.screen._choice_rows("subagent_timeout_minutes")
    idx = next(i for i, r in enumerate(rig.screen._rows) if r[0] == "number")

    rig.screen._activate(idx)
    assert len(rig.pushed) == 1, "no screen was pushed"
    screen, callback = rig.pushed[0]
    assert isinstance(screen, NumberScreen)

    callback(None)
    assert rig.written == {}, "a cancelled field wrote something"

    callback(0)
    assert rig.written == {"subagent_timeout_enabled": False}, (
        "the Custom path does not share the timeout-zero rule")


# ── the field itself ────────────────────────────────────────────────────────────────

def _drive_field(keys, typed=None, minimum=None, maximum=None):
    """Push the real field in a pilot app; return (result, hint_text)."""
    import asyncio

    from textual.app import App, ComposeResult
    from textual.widgets import Input, Static

    box = {"result": "unset", "hint": ""}

    class _Host(App):
        def compose(self) -> ComposeResult:
            yield Static("host")

    async def _drive():
        app = _Host()
        async with app.run_test(size=(90, 24)) as pilot:
            app.push_screen(
                NumberScreen("Context Limit", 32768, minimum=minimum,
                             maximum=maximum, hint="tokens"),
                lambda v: box.__setitem__("result", v))
            await pilot.pause()
            if typed is not None:
                app.screen.query_one("#number-input", Input).value = typed
            for key in keys:
                await pilot.press(key)
            await pilot.pause()
            if isinstance(app.screen, NumberScreen):
                box["hint"] = str(
                    app.screen.query_one("#number-hint", Static).content)

    asyncio.run(_drive())
    return box["result"], box["hint"]


def test_the_field_returns_an_int_never_a_str():
    """The settings rows compare stored values with `==`; a str would
    silently unmark every row while looking stored."""
    result, _ = _drive_field(["enter"], typed="49152", minimum=32768)
    assert result == 49152 and isinstance(result, int)


def test_out_of_range_keeps_the_field_open_with_the_reason():
    result, hint = _drive_field(["enter"], typed="1000", minimum=32768)
    assert result == "unset", "an out-of-range value was accepted"
    assert "out of range" in hint, hint


def test_garbage_keeps_the_field_open():
    result, hint = _drive_field(["enter"], typed="lots", minimum=1)
    assert result == "unset"
    assert "not a whole number" in hint, hint


def test_escape_and_empty_both_mean_change_nothing():
    result, _ = _drive_field(["escape"])
    assert result is None
    result, _ = _drive_field(["enter"], typed="")
    assert result is None
