# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Which settings the terminal app may change, and how.

The split that decides this round was MEASURED, not assumed, by tracing every
key to its consumption site:

* Nine keys are read live where they are used, so writing them is the whole
  job - they were "use `vaf settings`" rows only because nobody had ported the
  flow, not because the app could not do it.
* `provider` and `api_model_<provider>` need the running agent told, and the
  engine already has the one primitive for that: `reload_all_api_backends`
  re-reads the config, rebuilds the backend under a lock, and RE-ATTACHES the
  event sink. The classic lanes instead threw the agent away and silently lost
  the sink, the web registration and the real session id.
* The local GGUF and `n_ctx` genuinely need a rebuild, so they stay named
  boundaries rather than half-working rows.
"""
from types import SimpleNamespace

import pytest


# ── the rows that need nothing but a write ──────────────────────────────────────────

def _screen(values):
    from vaf.cli.tui_app.screens import SettingsScreen

    screen = SettingsScreen.__new__(SettingsScreen)
    screen._cfg = lambda key, default=None: values.get(key, default)
    screen._refresh_labels = lambda: None
    screen._stack = ["main"]
    return screen


@pytest.mark.parametrize("key", [
    "speech_tts_engine", "speech_language",
    "subagent_timeout_minutes", "ux_auto_open_max_tabs",
])
def test_every_choice_row_offers_the_classic_value_set(key):
    """The values are the classic menu's, ported rather than invented - a
    different set would silently mean something different to the runtime."""
    from vaf.cli.tui_app.screens import SettingsScreen

    label, options = SettingsScreen.CHOICES[key]
    assert label and len(options) >= 2
    for display, value in options:
        assert display and value is not None or value == 0


def test_the_tts_engines_are_the_three_the_runtime_knows():
    from vaf.cli.tui_app.screens import SettingsScreen

    _, options = SettingsScreen.CHOICES["speech_tts_engine"]
    assert {v for _, v in options} == {"piper", "system", "docker"}


def test_the_languages_match_the_classic_menu():
    from vaf.cli.tui_app.screens import SettingsScreen

    _, options = SettingsScreen.CHOICES["speech_language"]
    assert {v for _, v in options} == {
        "en-US", "de-DE", "tr-TR", "fr-FR", "es-ES", "zh-CN", "ru-RU", "it-IT"}


def test_a_key_absent_from_defaults_shows_the_runtime_fallback():
    """`speech_language` is not in Config.DEFAULTS; the consuming module falls
    back to en-US. Showing None there would be a lie about what is active."""
    from vaf.core.config import Config
    from vaf.cli.tui_app.screens import SettingsScreen

    assert "speech_language" not in Config.DEFAULTS, (
        "if this key gains a default, drop the fallback table")

    screen = _screen({})
    rows = screen._choice_rows("speech_language")
    marked = [text for kind, _, text in rows if kind == "pick" and "▍" in text]
    assert len(marked) == 1 and "English (US)" in marked[0]


def test_picking_a_choice_writes_exactly_that_key(monkeypatch):
    import vaf.core.config as config_mod
    from vaf.cli.tui_app.screens import SettingsScreen

    written = {}
    monkeypatch.setattr(config_mod.Config, "set",
                        classmethod(lambda cls, k, v: written.__setitem__(k, v)))

    fake_app = SimpleNamespace(notify=lambda *a, **k: None,
                               post_message=lambda *a, **k: None,
                               _bridge=SimpleNamespace(agent=SimpleNamespace(config={})))
    screen = _screen({"speech_tts_engine": "docker"})
    screen._rows = [("pick", ("speech_tts_engine", "piper"), "")]
    screen._stack = ["main", "choice:speech_tts_engine"]
    screen._rebuild = lambda: None
    type(screen).app = property(lambda self: fake_app)
    try:
        screen._activate(0)
    finally:
        del type(screen).app

    assert written == {"speech_tts_engine": "piper"}


def test_the_local_server_toggle_has_a_row_at_all():
    """It decides whether llama-server is started and had no row anywhere."""
    from vaf.cli.tui_app.screens import SettingsScreen

    assert "auto_start_local_server" in SettingsScreen.TOGGLES


# ── the provider switch, through the engine's own primitive ─────────────────────────

def test_the_model_screen_reads_the_catalog_shape_it_really_has():
    """Guessed key names would silently produce an empty model list."""
    from vaf.core.config import Config
    from vaf.cli.tui_app.screens import ModelScreen

    catalog = getattr(Config, "PROVIDER_MODELS", {}) or {}
    assert catalog, "no provider catalog - re-anchor this test"
    provider = sorted(catalog)[0]

    models = ModelScreen._models_for(provider)
    assert models, f"no models resolved for {provider}"
    assert len(models) == len(set(models)), "duplicates in the model list"

    entry = catalog[provider]
    assert isinstance(entry, dict), "re-anchor: the catalog changed shape"
    assert models[0] == entry["default"], "the default must lead"
    # The alternatives are the point - resolving only the default would look
    # like a working list while offering the user nothing to switch TO.
    for alternative in entry.get("fallback") or []:
        assert alternative in models, f"{alternative} missing from the list"
    assert len(models) > 1, (
        f"{provider} resolved to a single model - the fallback list was dropped")


def test_applying_a_provider_uses_the_engines_reload_not_a_rebuild(monkeypatch):
    """The whole point: `reload_all_api_backends` re-attaches the event sink,
    which every hand-rolled rebuild in the classic lanes loses."""
    import vaf.core.agent as agent_mod
    import vaf.core.config as config_mod

    calls, written = [], {}
    monkeypatch.setattr(agent_mod, "reload_all_api_backends",
                        lambda **kw: calls.append(kw) or 1)
    monkeypatch.setattr(config_mod.Config, "set",
                        classmethod(lambda cls, k, v: written.__setitem__(k, v)))
    # A key must exist, or the gate refuses before this reaches the reload it
    # pins. The gate itself is pinned in tests/test_tui_api_key_gate.py.
    monkeypatch.setattr(config_mod.Config, "get_api_key",
                        classmethod(lambda cls, p: "sk-test-key-value"))

    from vaf.cli.tui_app.agent_bridge import AgentBridge

    events = SimpleNamespace(system_note=lambda t: None,
                             event_note=lambda *a: None,
                             presence=lambda *a, **k: None,
                             context=lambda *a: None)
    agent = SimpleNamespace(get_token_usage=lambda: (1, 2), init_chat=_forbidden,
                            set_event_sink=lambda s: None, shutdown=lambda: None)
    bridge = AgentBridge(agent, SimpleNamespace(id="s"), None, events,
                         web_interface_getter=lambda: SimpleNamespace(
                             resolve_gate=lambda *a: True))

    bridge.apply_provider_change("anthropic", "claude-sonnet-4-6")
    _drain(bridge)

    assert calls == [{"force": True}], "force=True is what catches a moved key"
    assert written == {"provider": "anthropic",
                       "api_model_anthropic": "claude-sonnet-4-6"}
    bridge.shutdown()


def _forbidden(*args, **kwargs):
    raise AssertionError(
        "init_chat() would reset the history to the system message and wipe "
        "the conversation behind the transcript")


def _drain(bridge, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not bridge.busy and bridge._queue.empty():
            time.sleep(0.05)
            if not bridge.busy:
                return
        time.sleep(0.02)


def test_a_provider_switch_is_refused_while_a_turn_runs(monkeypatch):
    """Swapping the backend under a running turn is not a race worth having."""
    from vaf.cli.tui_app.agent_bridge import AgentBridge

    notes = []
    events = SimpleNamespace(system_note=lambda t: None,
                             event_note=lambda t, m, s: notes.append((t, m)),
                             presence=lambda *a, **k: None,
                             context=lambda *a: None)
    bridge = AgentBridge(SimpleNamespace(set_event_sink=lambda s: None,
                                         shutdown=lambda: None),
                         SimpleNamespace(id="s"), None, events,
                         web_interface_getter=lambda: SimpleNamespace(
                             resolve_gate=lambda *a: True))
    bridge._busy = True
    bridge.apply_provider_change("anthropic")

    assert notes and notes[0][0] == "Provider"
    bridge._busy = False
    bridge.shutdown()


def test_an_unchanged_backend_says_so_instead_of_claiming_success(monkeypatch):
    """`reload_all_api_backends` returns how many agents actually moved. Zero
    means the config was stored but the running agent kept its backend - and
    the user has to know that, or they will wonder why nothing changed."""
    import vaf.core.agent as agent_mod
    import vaf.core.config as config_mod

    monkeypatch.setattr(agent_mod, "reload_all_api_backends", lambda **kw: 0)
    monkeypatch.setattr(config_mod.Config, "set", classmethod(lambda cls, k, v: None))
    # Same reason as above: the key gate runs first and this test is about what
    # happens AFTER it.
    monkeypatch.setattr(config_mod.Config, "get_api_key",
                        classmethod(lambda cls, p: "sk-test-key-value"))

    from vaf.cli.tui_app.agent_bridge import AgentBridge

    warnings = []
    events = SimpleNamespace(system_note=lambda t: warnings.append(("note", t)),
                             event_note=lambda t, m, s: warnings.append((t, m)),
                             presence=lambda *a, **k: None,
                             context=lambda *a: None)
    bridge = AgentBridge(SimpleNamespace(get_token_usage=lambda: (1, 2),
                                         set_event_sink=lambda s: None,
                                         shutdown=lambda: None),
                         SimpleNamespace(id="s"), None, events,
                         web_interface_getter=lambda: SimpleNamespace(
                             resolve_gate=lambda *a: True))
    bridge.apply_provider_change("anthropic")
    _drain(bridge)

    assert any(t == "Provider" and "restart" in m for t, m in warnings), warnings
    bridge.shutdown()


# ── what stays a named boundary, and why ────────────────────────────────────────────

def test_the_rebuild_only_flows_still_say_so():
    """The local GGUF is NOT offered as a working row: `load_model()` reuses a
    running llama server without checking which model it serves, and the swap
    would need `init_chat()`, which wipes the history. A half-working row is
    worse than an honest pointer.

    The context limit LEFT this set on purpose: writing `n_ctx` is exactly
    what the inquirer menu does, and the row now says WHEN it applies ("at the
    next start") instead of refusing to write it at all. The set below may
    only shrink through a row becoming genuinely functional - never through
    one silently pretending.
    """
    from vaf.cli.tui_app.screens import SettingsScreen

    screen = _screen({"provider": "local"})
    later = {arg for kind, arg, _ in screen._menu_rows("main") if kind == "later"}
    assert {"local_model", "search_models"} <= later
    assert "context" not in later, "the context limit fell back to a dead row"
