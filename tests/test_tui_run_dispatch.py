# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf run` lane dispatch: flag beats config, config beats default.

The classic gap this guards: a lane can be fully built and tested while the
command still routes past it (the wiring, not the stage). Each test pins one
row of the dispatch table in run.py - if the table drifts, the row goes red,
not the user's first `vaf run` after the update.
"""
from types import SimpleNamespace

import pytest

import vaf.cli.cmd.run as run_mod


@pytest.fixture()
def dispatch(monkeypatch):
    """Stub every lane, drive the real run() body, record where it lands."""
    calls = []
    monkeypatch.setattr(run_mod, "_run_modern",
                        lambda *a, **k: calls.append(("modern", a, k)))
    monkeypatch.setattr(run_mod, "_run_classic",
                        lambda *a, **k: calls.append(("classic", a, k)))
    import vaf.cli.tui_app.app as app_mod
    monkeypatch.setattr(app_mod, "run_tui",
                        lambda **k: calls.append(("app", (), k)))

    import vaf.cli.cmd.update as update_mod
    monkeypatch.setattr(update_mod, "maybe_notify_update", lambda: None)
    monkeypatch.setattr(run_mod, "_quiet_cli_http_logs", lambda: None)

    config = {}
    import vaf.core.config as config_mod
    monkeypatch.setattr(
        config_mod.Config, "get",
        classmethod(lambda cls, key, default=None: config.get(key, default)))

    def _invoke(classic=False, web=False, message=None, theme=None, session=None):
        run_mod.run(SimpleNamespace(invoked_subcommand=None), message=message,
                    verbose=False, classic=classic, theme=theme,
                    session=session, web=web)
        return calls

    return SimpleNamespace(invoke=_invoke, config=config, calls=calls)


def test_default_is_the_fullscreen_app(dispatch):
    calls = dispatch.invoke()
    assert [c[0] for c in calls] == ["app"]


def test_classic_flag_beats_everything(dispatch):
    dispatch.config["tui_mode"] = "app"
    calls = dispatch.invoke(classic=True)
    assert [c[0] for c in calls] == ["classic"]


def test_config_modern_keeps_the_previous_lane(dispatch):
    dispatch.config["tui_mode"] = "modern"
    calls = dispatch.invoke()
    assert [c[0] for c in calls] == ["modern"]


def test_config_classic_works_without_the_flag(dispatch):
    dispatch.config["tui_mode"] = "classic"
    calls = dispatch.invoke()
    assert [c[0] for c in calls] == ["classic"]


def test_web_flag_routes_to_the_lane_that_owns_the_server(dispatch, monkeypatch):
    """Named boundary: the app lane does not start the web dashboard; an
    explicit --web must land where the server wiring lives, not drop it."""
    monkeypatch.setattr(run_mod, "_require_server_extra", lambda: None)
    calls = dispatch.invoke(web=True)
    assert [c[0] for c in calls] == ["modern"]
    assert calls[0][2].get("web_enabled") is True


def test_web_flag_wins_over_config_classic(dispatch, monkeypatch):
    """`tui_mode: classic` in the config must not silently DROP an explicit
    --web: the flag routes to the modern lane, which owns the server."""
    monkeypatch.setattr(run_mod, "_require_server_extra", lambda: None)
    dispatch.config["tui_mode"] = "classic"
    calls = dispatch.invoke(web=True)
    assert [c[0] for c in calls] == ["modern"]


def test_classic_flag_still_ignores_web(dispatch, monkeypatch):
    """Long-standing flag behavior stays: --classic --web is the plain prompt
    (the classic lane never started the server, and the flag wins)."""
    monkeypatch.setattr(run_mod, "_require_server_extra", lambda: None)
    calls = dispatch.invoke(classic=True, web=True)
    assert [c[0] for c in calls] == ["classic"]


def test_garbage_config_falls_back_to_the_app(dispatch):
    dispatch.config["tui_mode"] = "banana"
    calls = dispatch.invoke()
    assert [c[0] for c in calls] == ["app"]


def test_app_lane_receives_message_theme_and_session(dispatch):
    dispatch.config["theme"] = "vaf"
    calls = dispatch.invoke(message="hi", session="abc123")
    kind, _args, kwargs = calls[0]
    assert kind == "app"
    assert kwargs["message"] == "hi"
    assert kwargs["session_id"] == "abc123"
    assert kwargs["theme"] == "vaf"
