# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The four shipped defects and the four silent risks of the app lane.

Each of these shipped WORKING-LOOKING: a theme that changed half its colors, a
model chip reading a config key that does not exist, a settings row disagreeing
with the runtime it configures, a toggle that never reached the object acting on
it - plus four things the classic lane did that nobody misses until it hurts
(the tray unloading the model mid-session, git failing deep inside a turn, a
crash leaving no artifact, a broken install costing the user their chat).
Every test here pins one of them by the mechanism, not by the symptom.
"""
from types import SimpleNamespace

import pytest


# ── D1: the theme key the CSS variables actually resolve from ───────────────────────

def test_settings_theme_row_syncs_the_key_get_css_variables_reads():
    """`app.theme` alone changes Textual's palette; the vaf-* variables (border,
    muted, info) come from `app._theme_key` via get_css_variables(). Setting one
    without the other leaves half the palette on the old theme - and makes the
    `t` cycle count from the wrong index."""
    from vaf.cli.themes import THEMES
    from vaf.cli.tui_app.screens import SettingsScreen
    from vaf.cli.tui_app.theme_bridge import THEME_ORDER

    target_idx = 1 if len(THEME_ORDER) > 1 else 0
    target = THEME_ORDER[target_idx]

    fake_app = SimpleNamespace(theme=None, _theme_key="vaf",
                               notify=lambda *a, **k: None)
    screen = SettingsScreen.__new__(SettingsScreen)
    screen._stack = ["theme"]
    screen._rows = [("theme", target_idx, "")]
    screen._row_statics = []
    object.__setattr__(screen, "_activate_app", fake_app)

    import vaf.cli.tui_app.screens as screens_mod
    written = []
    original = screens_mod.persist_theme
    screens_mod.persist_theme = lambda key: written.append(key)
    type(screen).app = property(lambda self: fake_app)
    try:
        screen._refresh_labels = lambda: None
        screen._activate(0)
    finally:
        screens_mod.persist_theme = original
        del type(screen).app

    assert written == [target], "the choice must still be persisted"
    assert fake_app.theme == f"vaf-{target}"
    assert fake_app._theme_key == target, (
        "the app key stayed on the old theme - the vaf-* variables would too")
    assert target in THEMES


# ── D2: the model chip reads a key that exists ──────────────────────────────────────

def test_model_chip_reads_the_real_local_model_key(monkeypatch):
    """`model_name` is not in Config.DEFAULTS; the local model lives under
    `model`. Reading the phantom key made the chip say "local" forever."""
    from vaf.core.config import Config

    assert "model_name" not in Config.DEFAULTS, (
        "if this key ever exists, revisit the chip - the test is the reason it "
        "reads `model` today")
    assert "model" in Config.DEFAULTS

    from vaf.cli.tui_app.app import VafApp

    values = {"provider": "local", "model": "qwen3.5-14b-instruct-q4",
              "model_name": "PHANTOM"}
    monkeypatch.setattr(Config, "get",
                        classmethod(lambda cls, k, default=None: values.get(k, default)))

    top = SimpleNamespace(session_name="", model_chip="", mic_on=None)
    app = VafApp.__new__(VafApp)
    app._bridge = SimpleNamespace(session=SimpleNamespace(id="s1", name="probe"))
    type(app).query_one = lambda self, sel, cls=None: top
    try:
        app._refresh_chrome()
    finally:
        del type(app).query_one

    assert "qwen3.5-14b-instruct-q4" in top.model_chip
    assert "PHANTOM" not in top.model_chip


def test_model_chip_uses_the_per_provider_key_for_api_providers(monkeypatch):
    from vaf.core.config import Config
    from vaf.cli.tui_app.app import VafApp

    values = {"provider": "veyllo", "api_model_veyllo": "veyllo-chat",
              "model": "some-local.gguf"}
    monkeypatch.setattr(Config, "get",
                        classmethod(lambda cls, k, default=None: values.get(k, default)))

    top = SimpleNamespace(session_name="", model_chip="", mic_on=None)
    app = VafApp.__new__(VafApp)
    app._bridge = SimpleNamespace(session=SimpleNamespace(id="s1", name="probe"))
    type(app).query_one = lambda self, sel, cls=None: top
    try:
        app._refresh_chrome()
    finally:
        del type(app).query_one

    assert "veyllo-chat" in top.model_chip
    assert "some-local.gguf" not in top.model_chip


# ── D3: the settings row asks the question the runtime asks ─────────────────────────

@pytest.mark.parametrize("primary,legacy,expected", [
    (True, False, True),
    (False, True, True),      # the legacy key alone: the runtime says ON
    (False, False, False),
    (True, True, True),
])
def test_stt_row_follows_the_legacy_key_the_runtime_ors_in(monkeypatch,
                                                           primary, legacy, expected):
    """vaf/core/speech.py resolves STT as `speech_stt_enabled or stt_enabled`.
    A row reading only the first can show "off" while the microphone is live."""
    from vaf.cli.tui_app.screens import SettingsScreen

    values = {"speech_stt_enabled": primary, "stt_enabled": legacy}
    screen = SettingsScreen.__new__(SettingsScreen)
    screen._cfg = lambda key, default=None: values.get(key, default)

    assert screen._toggle_state("speech_stt_enabled") is expected


def test_runtime_still_ors_the_legacy_key(monkeypatch):
    """The premise of the test above, pinned against the real runtime - if the
    OR ever disappears, the row should stop compensating for it."""
    import inspect

    from vaf.core.speech import SpeechManager
    source = inspect.getsource(SpeechManager.is_stt_enabled)
    assert "stt_enabled" in source and "speech_stt_enabled" in source


# ── D4: a snapshot key must reach the running agent ─────────────────────────────────

def _toggle_screen(agent, values):
    """A SettingsScreen wired far enough to drive the REAL _activate."""
    from vaf.cli.tui_app.screens import SettingsScreen

    fake_app = SimpleNamespace(_bridge=SimpleNamespace(agent=agent),
                               notify=lambda *a, **k: None,
                               post_message=lambda *a, **k: None)
    screen = SettingsScreen.__new__(SettingsScreen)
    screen._cfg = lambda key, default=None: values.get(key, default)
    screen._refresh_labels = lambda: None
    type(screen).app = property(lambda self: fake_app)
    return screen


def test_persist_server_toggle_reaches_the_running_agent(monkeypatch):
    """Driven through the REAL _activate, not the helper: the wiring is the
    part that broke. The agent reads persist_server off its OWN config
    snapshot during shutdown, so Config.set alone never reaches it."""
    from vaf.cli.tui_app.screens import SettingsScreen
    import vaf.core.config as config_mod

    written = {}
    monkeypatch.setattr(config_mod.Config, "set",
                        classmethod(lambda cls, k, v: written.__setitem__(k, v)))

    agent = SimpleNamespace(config={"persist_server": False})
    screen = _toggle_screen(agent, {"persist_server": False})
    screen._rows = [("toggle", "persist_server", "")]
    try:
        screen._activate(0)
    finally:
        del type(screen).app

    assert written == {"persist_server": True}, "the file write is still required"
    assert agent.config["persist_server"] is True, (
        "the running agent never saw the change - it acts on its snapshot")


def test_live_read_keys_are_not_poked_into_the_agent_snapshot(monkeypatch):
    """Only keys the agent snapshots get the second write; poking a live-read
    key into agent.config would create a shadow value that outranks the file."""
    from vaf.cli.tui_app.screens import SettingsScreen
    import vaf.core.config as config_mod

    monkeypatch.setattr(config_mod.Config, "set",
                        classmethod(lambda cls, k, v: None))

    agent = SimpleNamespace(config={})
    screen = _toggle_screen(agent, {"web_ui_enabled": False})
    screen._rows = [("toggle", "web_ui_enabled", "")]
    try:
        screen._activate(0)
    finally:
        del type(screen).app

    assert "web_ui_enabled" not in agent.config


def test_toggle_flips_from_the_state_the_runtime_sees(monkeypatch):
    """The flip must start from the OR'd state, or a legacy-enabled STT would
    be turned ON again by a click meant to turn it off."""
    from vaf.cli.tui_app.screens import SettingsScreen
    import vaf.core.config as config_mod

    written = {}
    monkeypatch.setattr(config_mod.Config, "set",
                        classmethod(lambda cls, k, v: written.__setitem__(k, v)))

    agent = SimpleNamespace(config={})
    # runtime says ON (via the legacy key), the primary key says off
    screen = _toggle_screen(agent, {"speech_stt_enabled": False, "stt_enabled": True})
    screen._rows = [("toggle", "speech_stt_enabled", "")]
    try:
        screen._activate(0)
    finally:
        del type(screen).app

    assert written == {"speech_stt_enabled": False}, (
        "the click read the primary key alone and switched STT back ON")


def test_agent_still_reads_persist_server_from_its_snapshot():
    """The premise for D4: if the agent ever reads it live, drop the sync."""
    import inspect

    from vaf.core.agent import Agent
    source = inspect.getsource(Agent)
    assert 'self.config.get("persist_server"' in source


# ── R1: the tray's liveness signal ──────────────────────────────────────────────────

def test_boot_starts_the_heartbeat_and_the_git_preflight():
    """Both were skipped as 'server wiring'. The heartbeat is the ONLY signal
    the tray has that a CLI session lives - without it the tray unloads the
    local model out from under the user."""
    import inspect

    from vaf.cli.tui_app.agent_bridge import boot_bridge
    source = inspect.getsource(boot_bridge)
    assert "_heartbeat_loop" in source
    assert "_check_and_install_git" in source
    # A daemon, or quitting would hang on it.
    assert "daemon=True" in source


def test_git_preflight_failure_stops_the_boot(monkeypatch):
    """The classic lane exits when git is missing; the app lane must not sail
    on and fail deep inside a turn instead."""
    import vaf.cli.cmd.run as run_mod
    import vaf.cli.tui as tui_mod

    monkeypatch.setattr(run_mod, "_quiet_cli_http_logs", lambda: None)
    monkeypatch.setattr(run_mod, "_heartbeat_loop", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "_check_and_install_git", lambda tui: False)
    monkeypatch.setattr(tui_mod, "TUI", lambda *a, **k: SimpleNamespace(
        warning=lambda *a, **k: None, error=lambda *a, **k: None,
        info=lambda *a, **k: None, spinner=lambda *a, **k: None))

    from vaf.cli.tui_app.agent_bridge import boot_bridge
    with pytest.raises(SystemExit):
        boot_bridge(SimpleNamespace(), "vaf", None, False)


# ── R2: a failing turn leaves an artifact ───────────────────────────────────────────

def test_a_failing_turn_writes_the_traceback_to_the_crash_log(tmp_path, monkeypatch):
    """In app mode a printed traceback lands under the alternate screen. The
    dated crash file IS the artifact; only its path goes to the transcript."""
    import vaf.core.log_helper as log_helper
    monkeypatch.setattr(log_helper, "get_dated_log_path",
                        lambda name, ext="log": tmp_path / f"{name}.{ext}")

    from vaf.cli.tui_app.agent_bridge import _write_crash_log
    try:
        raise RuntimeError("probe failure")
    except RuntimeError:
        path = _write_crash_log()

    assert path is not None
    text = path.read_text()
    assert "probe failure" in text
    assert "RuntimeError" in text


def test_crash_logging_never_raises(monkeypatch):
    """An unwritable log directory must not turn one broken turn into two."""
    import vaf.core.log_helper as log_helper

    def _boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(log_helper, "get_dated_log_path", _boom)
    from vaf.cli.tui_app.agent_bridge import _write_crash_log
    assert _write_crash_log() is None


# ── R3: a broken install must not cost the chat ─────────────────────────────────────

def test_app_lane_falls_back_when_textual_cannot_be_imported(monkeypatch):
    """The modern lane grants itself exactly this degradation. Without it a
    broken/partial install turns `vaf run` into a traceback."""
    import builtins
    from types import SimpleNamespace as NS

    import vaf.cli.cmd.run as run_mod

    calls = []
    monkeypatch.setattr(run_mod, "_run_modern",
                        lambda *a, **k: calls.append("modern"))
    monkeypatch.setattr(run_mod, "_run_classic",
                        lambda *a, **k: calls.append("classic"))
    monkeypatch.setattr(run_mod, "_quiet_cli_http_logs", lambda: None)
    import vaf.cli.cmd.update as update_mod
    monkeypatch.setattr(update_mod, "maybe_notify_update", lambda: None)
    import vaf.core.config as config_mod
    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, key, default=None: default))

    real_import = builtins.__import__

    def _fail_app_import(name, *args, **kwargs):
        if name == "vaf.cli.tui_app.app":
            raise ImportError("No module named 'textual'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_app_import)

    run_mod.run(NS(invoked_subcommand=None), message=None, verbose=False,
                classic=False, theme=None, session=None, web=False)

    assert calls == ["modern"], "the app lane died instead of degrading"


def test_successful_app_run_does_not_also_start_a_second_lane(monkeypatch):
    """The fallback must not leak into the happy path: after the app returns,
    no other lane may run behind it."""
    from types import SimpleNamespace as NS

    import vaf.cli.cmd.run as run_mod
    import vaf.cli.tui_app.app as app_mod

    calls = []
    monkeypatch.setattr(app_mod, "run_tui", lambda **k: calls.append("app"))
    monkeypatch.setattr(run_mod, "_run_modern",
                        lambda *a, **k: calls.append("modern"))
    monkeypatch.setattr(run_mod, "_run_classic",
                        lambda *a, **k: calls.append("classic"))
    monkeypatch.setattr(run_mod, "_quiet_cli_http_logs", lambda: None)
    import vaf.cli.cmd.update as update_mod
    monkeypatch.setattr(update_mod, "maybe_notify_update", lambda: None)
    import vaf.core.config as config_mod
    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, key, default=None: default))

    run_mod.run(NS(invoked_subcommand=None), message=None, verbose=False,
                classic=False, theme=None, session=None, web=False)

    assert calls == ["app"]
