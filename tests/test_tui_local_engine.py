# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The engine in the full-screen TUI: an Engine row above "Select Active Model", the engine
submenu with both lines and this machine's verdict, the vLLM model submenu, the install that
asks first, and a GGUF pick that is refused under vLLM.

The switch itself is `local_engine.apply` on the bridge's lane (tests/test_local_engine_apply.py);
here the screen rows and the bridge's refusal are checked, with the state faked.

MUTATIONS, each turns the named test red:
- make the vLLM row an `engine_pick` on a `later` verdict -> test_engine_submenu_unsupported_is_note;
- call `apply_engine` before the confirm -> test_install_asks_first;
- list GGUFs under vLLM -> test_vllm_model_rows;
- write `model` under vLLM in `apply_local_model` -> test_gguf_pick_refused_under_vllm;
- change RenameScreen's default title -> test_rename_screen_default_unchanged.
"""
import asyncio
from types import SimpleNamespace

import pytest

from vaf.cli.tui_app.screens import ConfirmScreen, RenameScreen, SettingsScreen
from vaf.core import local_engine as le


def _state(engine="llama", level="supported", profile="local", needs_install=False,
           setting="auto"):
    return {
        "engine": engine, "profile": profile,
        "support": {"level": level, "code": "supported" if level == "supported" else "amd",
                    "params": {}, "reason": "vLLM runs here (NVIDIA RTX 3080, 10 GB)."
                    if level == "supported" else "vLLM supports some AMD GPUs; not yet."},
        "vllm": {"pin": "0.32.0", "needs_install": needs_install, "download_gb_at_least": 1.8,
                 "model_setting": setting, "model": "Qwen/Qwen3-4B-AWQ",
                 "auto_model": "Qwen/Qwen3-4B-AWQ",
                 "curated": [{"repo": "Qwen/Qwen3-4B-AWQ", "weights_gb": 2.67},
                             {"repo": "Qwen/Qwen3-8B-AWQ", "weights_gb": 6.10}]},
        "llama": {"model": "q.gguf"}, "conflicts": [],
    }


def _screen(monkeypatch, *, engine="llama", state=None):
    calls, pushed, notified = [], [], []
    bridge = SimpleNamespace(
        engine_state=lambda model=None: state,
        apply_engine=lambda engine, model=None, install=False: calls.append((engine, model, install)),
        list_local_models=lambda: (["a.gguf", "b.gguf"], "a.gguf"),
    )
    fake_app = SimpleNamespace(
        _bridge=bridge,
        notify=lambda msg, **kw: notified.append(msg),
        post_message=lambda msg: None,
        push_screen=lambda screen, callback=None: pushed.append((screen, callback)),
    )

    class _Detached(SettingsScreen):
        app = property(lambda s: fake_app)

    s = _Detached.__new__(_Detached)
    s._cfg = lambda key, default=None: {"provider": "local"}.get(key, default)
    s._refresh_labels = lambda: None
    s._rebuild = lambda: setattr(s, "_rows", s._menu_rows(s._stack[-1]))
    s._stack = ["main"]
    s._rows = []
    s._engine_state = None
    s._mic_devices = None
    s._automations = None
    monkeypatch.setattr(le, "is_vllm", lambda config=None: engine == "vllm")
    return s, calls, pushed, notified


@pytest.mark.parametrize("engine, label", [("llama", "Engine: llama.cpp"), ("vllm", "Engine: vLLM")])
def test_main_menu_engine_row(monkeypatch, engine, label):
    s, *_ = _screen(monkeypatch, engine=engine)
    rows = s._menu_rows("main")
    keys = [(kind, arg) for kind, arg, _ in rows]
    at = keys.index(("submenu", "engine"))
    assert rows[at][2] == label
    assert keys[at + 1] == ("submenu", "local_model"), "the engine sits right above the model"
    assert (("later", "search_models") in keys) is (engine == "llama"), \
        "the GGUF search belongs to llama.cpp only"


def test_engine_submenu_lists_both_with_their_lines(monkeypatch):
    s, *_ = _screen(monkeypatch, state=_state())
    s._load_engine_state()
    rows = s._menu_rows("engine")
    picks = [(arg, text) for kind, arg, text in rows if kind == "engine_pick"]
    assert [a for a, _ in picks] == ["llama", "vllm"]
    assert "GGUF models, runs on almost any hardware, starts in seconds" in picks[0][1]
    assert "Hugging Face models, fast with many requests, needs a supported GPU, starts in minutes" \
        in picks[1][1]
    assert "▍" in picks[0][1] and "▍" not in picks[1][1], "the active engine is marked"
    assert any(kind == "note" and "vLLM runs here" in text for kind, _, text in rows)


def test_engine_submenu_unsupported_is_note(monkeypatch):
    s, calls, *_ = _screen(monkeypatch, state=_state(level="later"))
    s._load_engine_state()
    rows = s._menu_rows("engine")
    assert [arg for kind, arg, _ in rows if kind == "engine_pick"] == ["llama"]
    notes = [text for kind, _, text in rows if kind == "note"]
    assert any("vLLM" in n and "not yet" in n and "AMD" in n for n in notes), notes


def test_engine_submenu_lean(monkeypatch):
    s, *_ = _screen(monkeypatch, state=_state(profile="api"))
    s._load_engine_state()
    rows = s._menu_rows("engine")
    assert not [r for r in rows if r[0] == "engine_pick"], "neither engine runs under api"
    notes = " ".join(text for kind, _, text in rows if kind == "note")
    assert "vLLM" in notes and "llama.cpp" in notes and 'profile "api"' in notes


def test_install_asks_first(monkeypatch):
    s, calls, pushed, _ = _screen(monkeypatch, state=_state(needs_install=True))
    s._stack = ["main", "engine"]
    s._load_engine_state()
    s._rows = s._menu_rows("engine")
    s._activate([r[1] for r in s._rows].index("vllm"))
    assert calls == [], "the install ran before the question was answered"
    screen, callback = pushed[-1]
    assert isinstance(screen, ConfirmScreen)
    assert "0.32.0" in screen._question and "1.8 GB" in screen._question
    assert "Qwen/Qwen3-4B-AWQ (2.67 GB)" in screen._question
    callback(False)
    assert calls == []
    callback(True)
    assert calls == [("vllm", None, True)]
    # Installed: no question, the switch runs straight away.
    s._engine_state = _state()
    s._stack, s._rows = ["main", "engine"], s._menu_rows("engine")
    s._activate([r[1] for r in s._rows].index("llama"))
    assert calls[-1] == ("llama", None, False)


def test_vllm_model_rows(monkeypatch):
    s, calls, pushed, _ = _screen(monkeypatch, engine="vllm",
                                  state=_state(engine="vllm", setting="someone/Qwen3-14B-AWQ"))
    s._stack = ["main"]
    s._rows = s._menu_rows("main")
    s._activate([r[1] for r in s._rows].index("local_model"))
    rows = s._rows
    kinds = [kind for kind, _, _ in rows]
    assert "local_model" not in kinds, "GGUFs listed under vLLM"
    values = [arg for kind, arg, _ in rows if kind == "vllm_model"]
    assert values == ["auto", "Qwen/Qwen3-4B-AWQ", "Qwen/Qwen3-8B-AWQ", "someone/Qwen3-14B-AWQ"]
    assert "Automatic for this GPU: Qwen/Qwen3-4B-AWQ (2.67 GB)" in rows[0][2]
    assert "▍" in rows[3][2], "the stored repo is marked"
    s._activate(kinds.index("vllm_model_other"))
    screen, callback = pushed[-1]
    assert isinstance(screen, RenameScreen)
    assert (screen._title, screen._placeholder) == ("Hugging Face model", "owner/name")
    callback("not a repo")
    assert calls == []
    callback("Qwen/Qwen3-8B-AWQ")
    assert calls == [("vllm", "Qwen/Qwen3-8B-AWQ", False)]


def test_gguf_pick_refused_under_vllm(monkeypatch):
    from vaf.cli.tui_app.agent_bridge import AgentBridge
    import vaf.core.config as config_mod
    written, notes, submitted = {}, [], []
    monkeypatch.setattr(config_mod.Config, "set",
                        classmethod(lambda cls, k, v: written.__setitem__(k, v)))
    monkeypatch.setattr(le, "is_vllm", lambda config=None: True)
    bridge = AgentBridge.__new__(AgentBridge)
    bridge.events = SimpleNamespace(event_note=lambda *a: notes.append(a),
                                    system_note=lambda *a: notes.append(a))
    bridge._busy = False
    bridge._submit = submitted.append
    bridge.apply_local_model("q.gguf")
    assert written == {} and submitted == [], "a GGUF was written under vLLM"
    assert "vLLM" in notes[0][1]
    monkeypatch.setattr(le, "is_vllm", lambda config=None: False)
    bridge.apply_local_model("q.gguf")
    assert len(submitted) == 1


def _render(screen):
    from textual.app import App, ComposeResult
    from textual.widgets import Input, Static
    box = {}

    class _Host(App):
        def compose(self) -> ComposeResult:
            yield Static("host")

    async def _drive():
        app = _Host()
        async with app.run_test(size=(90, 24)) as pilot:
            app.push_screen(screen)
            await pilot.pause()
            box["title"] = str(app.screen.query_one(".modal-title", Static).content)
            box["placeholder"] = app.screen.query_one("#rename-input", Input).placeholder
    asyncio.run(_drive())
    return box


def test_rename_screen_default_unchanged():
    """The rename keeps its words; the repo id field brings its own."""
    assert _render(RenameScreen()) == {"title": "[bold $text]Rename session[/]",
                                       "placeholder": "session name"}
    assert _render(RenameScreen(title="Hugging Face model", placeholder="owner/name")) == {
        "title": "[bold $text]Hugging Face model[/]", "placeholder": "owner/name"}


def test_apply_engine_runs_on_the_lane_and_reloads(monkeypatch, tmp_path):
    """The switch runs on the bridge's lane with pip's output in logs/vllm_install.log, and a
    local agent follows the new engine afterwards; a running turn refuses it."""
    from vaf.cli.tui_app.agent_bridge import AgentBridge
    from vaf.core import log_helper
    monkeypatch.setattr(log_helper, "get_app_log_dir", lambda: tmp_path)
    seen, reloads, notes, lane = [], [], [], []

    def apply(engine, model=None, install=False, progress=None, output=None):
        seen.append((engine, model, install, output))
        progress({"phase": "install_vllm", "step": 1, "steps": 7, "error": ""})
        return le.EngineSwitch("done", "switched_vllm", "The local engine is vLLM.", "vllm")
    monkeypatch.setattr(le, "apply", apply)
    bridge = AgentBridge.__new__(AgentBridge)
    bridge.agent = SimpleNamespace(provider="local", reload_local_model=lambda: reloads.append(1))
    bridge.events = SimpleNamespace(event_note=lambda *a: notes.append(a),
                                    system_note=lambda *a: notes.append(a))
    bridge._emit = lambda *a: None
    bridge._refresh_context = lambda: None
    bridge._busy = True
    bridge._submit = lane.append
    bridge.apply_engine("vllm", install=True)
    assert lane == [] and "not while a turn" in notes[-1][1]
    bridge._busy = False
    bridge.apply_engine("vllm", install=True)
    assert seen == [], "the switch ran on the caller's thread, not the lane"
    lane[0]()
    assert seen == [("vllm", None, True, str(tmp_path / "vllm_install.log"))]
    assert reloads == [1]
    assert any("2/7" in str(n) for n in notes)
