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
- change RenameScreen's default title -> test_rename_screen_default_unchanged;
- show the GPU memory row under llama.cpp -> test_gpu_memory_row_only_under_vllm;
- omit `gpu_share` in the bridge's call of apply -> test_a_gpu_share_pick_is_a_switch.
"""
import asyncio
from types import SimpleNamespace

import pytest

from vaf.cli.tui_app.screens import ConfirmScreen, RenameScreen, SettingsScreen
from vaf.core import local_engine as le
from vaf.core import vllm_server as vs


@pytest.fixture(autouse=True)
def no_display(monkeypatch):
    """The display state is pinned: this machine drives a display and a CI runner does not,
    and the desktop reserve follows it (vllm_server.desktop_reserve_mb)."""
    from vaf.core import vllm_server
    monkeypatch.setattr(vllm_server, "_display_active", lambda: False)


def _state(engine="llama", level="supported", profile="local", needs_install=False,
           setting="auto"):
    return {
        "engine": engine, "profile": profile,
        "support": {"level": level, "code": "supported" if level == "supported" else "amd",
                    "params": {}, "reason": "vLLM runs here (NVIDIA RTX 3080, 10 GB)."
                    if level == "supported" else "vLLM supports some AMD GPUs; not yet."},
        "vllm": {"pin": "0.32.0", "needs_install": needs_install, "download_gb_at_least": vs.DOWNLOAD_GB_AT_LEAST,
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
        apply_engine=lambda engine, model=None, install=False, gpu_share=None, **kw: calls.append(
            (engine, model, install) if gpu_share is None else (engine, model, install, gpu_share)),
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
    flat = [" ".join(text.split()) for _, text in picks]
    assert ("Runs GGUF models on almost any GPU or on the CPU alone, takes only the memory it "
            "needs and starts in seconds") in flat[0]
    assert "For everyday tasks on personal PCs, laptops and edge devices." in flat[0]
    assert ("For servers and high-end PCs that answer many requests at once. Runs Hugging Face "
            "models on a supported NVIDIA or AMD GPU, reserves its share of GPU memory up front "
            "and starts in one to two minutes") in flat[1]
    assert "▍" in picks[0][1] and "▍" not in picks[1][1], "the active engine is marked"
    assert "in use" in picks[0][1] and "in use" not in picks[1][1], "the active engine is named"
    assert [kind for kind, _, _ in rows][-2:] == ["sep", "back"], "a gap before Back"
    for text in picks:
        lines = text[1].split("\n")[1:]
        assert lines and all(line.startswith("[$vaf-muted]   ") or line.startswith("   ")
                             for line in lines), "a wrapped line starts under the marker"
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
    assert "0.32.0" in screen._question and f"{vs.DOWNLOAD_GB_AT_LEAST:.1f} GB" in screen._question
    assert "Qwen/Qwen3-4B-AWQ (2.67 GB)" in screen._question
    assert "earlier VAF" not in screen._question
    callback(False)
    assert calls == []
    callback(True)
    assert calls == [("vllm", None, True)]
    # Installed: no question, the switch runs straight away.
    s._engine_state = _state()
    s._stack, s._rows = ["main", "engine"], s._menu_rows("engine")
    s._activate([r[1] for r in s._rows].index("llama"))
    assert calls[-1] == ("llama", None, False)


def test_install_question_says_an_old_environment_is_replaced(monkeypatch):
    """An environment an earlier VAF set up is replaced, and the question says so first.
    MUTATION: drop the replace sentence from the question - red."""
    state = _state(needs_install=True)
    state["vllm"]["installed_version"] = "0.32.0"
    s, calls, pushed, _ = _screen(monkeypatch, state=state)
    s._stack = ["main", "engine"]
    s._load_engine_state()
    s._rows = s._menu_rows("engine")
    s._activate([r[1] for r in s._rows].index("vllm"))
    question = pushed[-1][0]._question
    assert question.startswith("vLLM 0.32.0 was set up by an earlier VAF and cannot start; it "
                               "is replaced. Install vLLM 0.32.0"), question
    assert calls == []


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

    def apply(engine, model=None, install=False, gpu_share=None, progress=None, output=None):
        seen.append((engine, model, install, output))
        progress({"phase": "install_vllm", "step": 1, "steps": 6, "error": ""})
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
    assert any("2/6" in str(n) for n in notes)


@pytest.mark.parametrize("code", ["install_failed", "cuda_probe_failed", "vllm_libs_failed"])
def test_install_failures_name_the_install_log(monkeypatch, tmp_path, code):
    """Every install failure, the two probe verdicts included, points at pip's log; the probe
    step is named for what it checks. MUTATION: drop vllm_libs_failed from the bridge's tuple
    - red."""
    from vaf.cli.tui_app.agent_bridge import AgentBridge
    from vaf.core import log_helper
    monkeypatch.setattr(log_helper, "get_app_log_dir", lambda: tmp_path)
    notes, lane = [], []

    def apply(engine, model=None, install=False, gpu_share=None, progress=None, output=None):
        progress({"phase": "install_probe", "step": 2, "steps": 6, "error": ""})
        return le.EngineSwitch("failed", code, "vLLM was installed, but it cannot start.", "vllm",
                               detail="The local engine is unchanged.")
    monkeypatch.setattr(le, "apply", apply)
    bridge = AgentBridge.__new__(AgentBridge)
    bridge.agent = SimpleNamespace(provider="local", reload_local_model=lambda: None)
    bridge.events = SimpleNamespace(event_note=lambda *a: notes.append(a),
                                    system_note=lambda *a: notes.append(a))
    bridge._emit = lambda *a: None
    bridge._refresh_context = lambda: None
    bridge._busy = False
    bridge._submit = lane.append
    bridge.apply_engine("vllm", install=True)
    lane[0]()
    assert any("checking that vLLM loads on the GPU" in str(n) for n in notes), notes
    assert f"Details: {tmp_path / 'vllm_install.log'}" in notes[-1][1], notes[-1]


def test_no_switch_notice_while_the_bridge_is_busy(monkeypatch):
    """A running turn makes the bridge refuse the switch; the screen must not then claim it is
    switching (after a confirmed multi-GB install question). An engine pick, which also moves
    the chat, is refused before the question."""
    s, calls, pushed, notified = _screen(monkeypatch, engine="vllm",
                                         state=_state(engine="vllm", needs_install=True))
    s.app._bridge.busy = True
    s._stack = ["main", "local_model"]
    s._load_engine_state()
    s._rows = s._menu_rows("local_model")
    s._activate([r[1] for r in s._rows].index("Qwen/Qwen3-8B-AWQ"))
    pushed[-1][1](True)
    assert calls == [] and notified == ["not while a turn is running"]
    assert s._stack == ["main", "local_model"], "the menu was reset for a switch that never ran"
    s._stack = ["main", "engine"]
    s._rows = s._menu_rows("engine")
    s._activate([r[1] for r in s._rows].index("llama"))
    assert calls == [] and len(pushed) == 1, "an engine pick asked or ran during a turn"
    assert notified[-1] == "not while a turn is running"


def test_refresh_rebuilds_when_the_menu_changes_shape(monkeypatch):
    """The engine switch finishes on the lane while the main menu is open: the GGUF search row
    goes away. Relabelling in place would shift the rows; the list is rebuilt instead."""
    s, *_ = _screen(monkeypatch, engine="llama")
    rebuilt = []
    s._rebuild = lambda: rebuilt.append(1)
    s._stack = ["main"]
    s._rows = s._menu_rows("main")
    s._row_statics = [SimpleNamespace(update=lambda text: None) for _ in s._rows]
    SettingsScreen._refresh_labels(s)       # the helper stubs the instance's one
    assert rebuilt == [], "same shape: labels only"
    monkeypatch.setattr(le, "is_vllm", lambda config=None: True)
    SettingsScreen._refresh_labels(s)
    assert rebuilt == [1]


def test_engine_switch_end_not_called_migration():
    """The full-screen TUI announced every maintenance job's end as the database migration;
    an engine switch is named as what it is, with its failure code. MUTATION: restore the
    migration text for every kind - red."""
    from vaf.cli.tui_app.app import maintenance_notice
    snap = {"kind": "vllm_engine", "active": False, "done": 5, "total": 7, "error": "no_fit"}
    text = maintenance_notice(snap, started=False)
    assert "migration" not in text and "vLLM" in text and "no_fit" in text and "vaf engine" in text
    assert maintenance_notice({**snap, "error": ""}, started=False) == "vLLM setup finished"
    assert "migration" not in maintenance_notice({**snap, "active": True}, started=True)
    assert "migration" in maintenance_notice({"kind": "memory_reembed", "error": ""}, started=False)


def test_engine_lines_match_catalogue():
    """The TUI's engine lines are the web cards' English lines: the recommendation, then the
    description. MUTATION: change one word of a TUI line - red."""
    import json
    from pathlib import Path

    from vaf.cli.tui_app.screens import SettingsScreen
    en = json.loads((Path(__file__).resolve().parent.parent / "web" / "messages" / "en.json")
                    .read_bytes())["localEngine"]
    lines = SettingsScreen.ENGINE_LINES
    assert lines["llama"] == (en["llamaName"], f"{en['llamaFor']}. {en['llamaLine']}")
    assert lines["vllm"] == (en["vllmName"], f"{en['vllmFor']}. {en['vllmLine']}")


def _memory(configured=0.0):
    steps = [{"share": p / 100, "budget_mb": int(p / 100 * 10240), "ok": p >= 67,
              "max_model_len": 40960 if p >= 78 else 32768, "need_mb": 6813}
             for p in range(30, 96)]
    return {"total_mb": 10240, "free_mb": 8500, "display": True, "reserve_mb": 3328,
            "configured": configured, "auto_share": 0.67, "auto_reserved": True,
            "kv_dtype": "int8_per_token_head", "range": [0.30, 0.95], "min_share": 0.67,
            "steps": steps}


def _with_memory(engine, configured=0.0):
    state = _state(engine=engine)
    state["vllm"]["memory"] = _memory(configured)
    return state


def test_gpu_memory_row_only_under_vllm(monkeypatch):
    """vLLM's share has a row in the engine menu while vLLM is the engine; llama.cpp reserves
    no share, so there is none under it."""
    s, *_ = _screen(monkeypatch, engine="vllm", state=_with_memory("vllm"))
    s._load_engine_state()
    rows = s._menu_rows("engine")
    assert ("submenu", "gpu_share", "GPU memory: Automatic") in rows
    s, *_ = _screen(monkeypatch, engine="vllm", state=_with_memory("vllm", 0.68))
    s._load_engine_state()
    assert ("submenu", "gpu_share", "GPU memory: 68%") in s._menu_rows("engine")
    s, *_ = _screen(monkeypatch, state=_with_memory("llama"))
    s._load_engine_state()
    assert not [r for r in s._menu_rows("engine") if r[1] == "gpu_share"]


def test_a_gpu_share_pick_is_a_switch(monkeypatch):
    """The list: the restart note, Automatic, 30 to 95 in fives and a stored value off that
    grid, each with its size and the fit from the table; a pick switches with ``gpu_share``.
    MUTATION: omit gpu_share in the bridge's call of apply - red (test below)."""
    s, calls, *_ = _screen(monkeypatch, engine="vllm", state=_with_memory("vllm", 0.68))
    s._load_engine_state()
    s._stack = ["main", "engine"]
    s._rows = s._menu_rows("engine")
    s._activate([r[1] for r in s._rows].index("gpu_share"))
    assert s._stack == ["main", "engine", "gpu_share"]
    rows = s._rows
    assert rows[0][0] == "note" and "restarts" in rows[0][2]
    values = [arg for kind, arg, _ in rows if kind == "gpu_share"]
    assert values[0] == 0.0 and "Automatic (67% now, 6.7 GB of 10.0 GB)" in rows[1][2]
    assert "3.3 GB of the card kept outside it for the screen" in rows[1][2]
    assert values[1:] == sorted([p / 100 for p in range(30, 96, 5)] + [0.68])
    text = {arg: label for kind, arg, label in rows if kind == "gpu_share"}
    assert "too small" in text[0.65] and "fits, context 32768" in text[0.7]
    assert "fits, context 40960" in text[0.8] and "▍" in text[0.68] and "▍" not in text[0.0]
    s._activate([r[1] for r in rows].index(0.7))
    assert calls == [("vllm", None, False, 0.7)]
    assert s._stack == ["main"]


def test_the_bridge_passes_the_share(monkeypatch, tmp_path):
    from vaf.cli.tui_app.agent_bridge import AgentBridge
    from vaf.core import log_helper
    monkeypatch.setattr(log_helper, "get_app_log_dir", lambda: tmp_path)
    seen, lane = [], []

    def apply(engine, model=None, install=False, gpu_share=None, progress=None, output=None):
        seen.append((engine, gpu_share))
        return le.EngineSwitch("done", "unchanged_vllm", "vLLM.", "vllm")
    monkeypatch.setattr(le, "apply", apply)
    bridge = AgentBridge.__new__(AgentBridge)
    bridge.agent = SimpleNamespace(provider="api", reload_local_model=lambda: None)
    bridge.events = SimpleNamespace(event_note=lambda *a: None, system_note=lambda *a: None)
    bridge._emit = lambda *a: None
    bridge._refresh_context = lambda: None
    bridge._busy = False
    bridge._submit = lane.append
    bridge.apply_engine("vllm", gpu_share=0.7)
    lane[0]()
    assert seen == [("vllm", 0.7)]


# ── An engine pick also moves the chat to this machine ───────────────────────────────

@pytest.mark.parametrize("status, code", [("done", "switched_vllm"), ("failed", "start_failed"),
                                          ("refused", "unsupported"),
                                          ("needs_install", "needs_install")])
def test_make_local_moves_after_done_only(monkeypatch, tmp_path, status, code):
    """`make_local`: a done switch moves an API provider's chat to local and loads the model;
    a switch that did not finish leaves the provider where it was. MUTATION: call the
    provider switch before `apply`, or regardless of the status - red."""
    from vaf.cli.tui_app.agent_bridge import AgentBridge
    from vaf.core import log_helper, provider_switch
    monkeypatch.setattr(log_helper, "get_app_log_dir", lambda: tmp_path)
    order, lane = [], []
    agent = SimpleNamespace(provider="anthropic", api_backend=object(), use_server=False,
                            llm=None, reload_local_model=lambda: order.append("load") or True)

    def apply(engine, **kw):
        order.append("apply")
        return le.EngineSwitch(status, code, "text", "vllm")

    def switch(provider, model="", **kw):
        order.append(("switch", provider))
        agent.provider, agent.api_backend = "local", None
        return provider_switch.ProviderSwitch("done", "switched", "ok", "local", "", 1)
    monkeypatch.setattr(le, "apply", apply)
    monkeypatch.setattr(provider_switch, "switch_provider", switch)
    bridge = AgentBridge.__new__(AgentBridge)
    bridge.agent = agent
    bridge.events = SimpleNamespace(event_note=lambda *a: None, system_note=lambda *a: None)
    bridge._emit = lambda *a: None
    bridge._refresh_context = lambda: None
    bridge._busy = False
    bridge._submit = lane.append
    bridge.apply_engine("vllm", make_local=True)
    lane[0]()
    if status == "done":
        assert order == ["apply", ("switch", "local"), "load"]
    else:
        assert order == ["apply"] and agent.provider == "anthropic"


def test_without_make_local_an_api_provider_stays(monkeypatch, tmp_path):
    from vaf.cli.tui_app.agent_bridge import AgentBridge
    from vaf.core import log_helper, provider_switch
    monkeypatch.setattr(log_helper, "get_app_log_dir", lambda: tmp_path)
    lane, switched = [], []
    monkeypatch.setattr(le, "apply", lambda engine, **kw: le.EngineSwitch(
        "done", "switched_vllm", "text", "vllm"))
    monkeypatch.setattr(provider_switch, "switch_provider",
                        lambda *a, **k: switched.append(a))
    bridge = AgentBridge.__new__(AgentBridge)
    bridge.agent = SimpleNamespace(provider="anthropic", reload_local_model=lambda: None)
    bridge.events = SimpleNamespace(event_note=lambda *a: None, system_note=lambda *a: None)
    bridge._emit = lambda *a: None
    bridge._refresh_context = lambda: None
    bridge._busy = False
    bridge._submit = lane.append
    bridge.apply_engine("vllm")
    lane[0]()
    assert switched == []


def test_engine_menu_notes_the_api_provider(monkeypatch):
    """With an API provider the Engine submenu says where the chat runs and what a pick does.
    MUTATION: drop the note - red."""
    s, *_ = _screen(monkeypatch, state=_state())
    s._cfg = lambda key, default=None: {"provider": "anthropic"}.get(key, default)
    s._load_engine_state()
    rows = s._menu_rows("engine")
    assert rows[0] == ("note", None, "The main chat runs on Anthropic. Picking an engine "
                                     "moves it to this machine.")
    s, *_ = _screen(monkeypatch, state=_state())
    s._load_engine_state()
    assert not any("main chat runs on" in str(r[2]) for r in s._menu_rows("engine"))


def test_engine_pick_routes_through_choose_local_engine(monkeypatch):
    """Engine rows go through `choose_local_engine` (which also moves the chat); the vLLM
    model and GPU memory rows keep the chat where it is. MUTATION: keep `engine_pick` calling
    the switch directly - red."""
    from vaf.cli.tui_app import screens as screens_mod
    chosen = []
    monkeypatch.setattr(screens_mod, "choose_local_engine",
                        lambda app, engine, **kw: chosen.append((engine, kw["from_settings"])))
    s, calls, *_ = _screen(monkeypatch, engine="vllm", state=_with_memory("vllm", 0.68))
    s._load_engine_state()
    s._stack = ["main", "engine"]
    s._rows = s._menu_rows("engine")
    s._activate([r[1] for r in s._rows].index("llama"))
    assert chosen == [("llama", True)] and calls == []
    seen = []
    s.app._bridge.apply_engine = lambda engine, **kw: seen.append(kw.get("make_local"))
    s._activate([r[1] for r in s._rows].index("gpu_share"))
    s._activate([r[1] for r in s._rows].index(0.7))
    assert seen == [False]


def test_the_stored_engine_reads_selected_under_an_api_provider(monkeypatch):
    """"in use" under "The main chat runs on Anthropic" contradicted it. MUTATION: keep
    "in use" whatever the provider - red."""
    s, *_ = _screen(monkeypatch, state=_state())
    s._cfg = lambda key, default=None: {"provider": "anthropic"}.get(key, default)
    s._load_engine_state()
    picks = [text for kind, _, text in s._menu_rows("engine") if kind == "engine_pick"]
    assert "selected" in picks[0] and "in use" not in picks[0]
    s, *_ = _screen(monkeypatch, state=_state())
    s._load_engine_state()
    picks = [text for kind, _, text in s._menu_rows("engine") if kind == "engine_pick"]
    assert "in use" in picks[0] and "selected" not in picks[0]


def test_settings_menus_open_on_the_first_row_that_can_be_picked(monkeypatch):
    """Real keys: `s`, Engine with an API provider; the list opens on llama.cpp, not on the
    note above it, so Enter picks. MUTATION: the cursor back at row 0 - red."""
    from tests.test_tui_app_smoke import _settle
    from tests.test_tui_model_picker import _patch_config
    from vaf.cli.tui_app import screens as screens_mod
    from vaf.cli.tui_app.app import TuiEvents, VafApp
    from vaf.cli.tui_app.agent_bridge import AgentBridge

    assert screens_mod.first_selectable([("note", None, ""), ("sep", None, ""),
                                         ("engine_pick", "llama", "")]) == 2
    _patch_config(monkeypatch, {"provider": "anthropic"})
    monkeypatch.setattr(le, "is_vllm", lambda config=None: False)
    monkeypatch.setattr(AgentBridge, "tasks_snapshot", lambda self: [])
    monkeypatch.setattr(AgentBridge, "engine_state", lambda self, model=None: _state())
    chosen = []
    monkeypatch.setattr(screens_mod, "choose_local_engine",
                        lambda app, engine, **kw: chosen.append(engine))
    events = TuiEvents()
    bridge = AgentBridge(SimpleNamespace(set_event_sink=lambda s: None, shutdown=lambda: None,
                                         get_token_usage=lambda: (1, 2)),
                         SimpleNamespace(id="s", name="s", messages=[]), None, events,
                         web_interface_getter=lambda: SimpleNamespace(
                             resolve_gate=lambda *a: True))
    app = VafApp(bridge, theme_key="vaf")
    events.bind(app)

    async def _run():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app.action_settings()
            assert await _settle(pilot, lambda: isinstance(app.screen, SettingsScreen))
            scr = app.screen
            scr._activate([r[1] for r in scr._rows].index("engine"))
            await pilot.pause()
            assert scr._rows[0][0] == "note"
            await pilot.press("enter")
            await pilot.pause()
    asyncio.run(_run())
    bridge.shutdown()
    assert chosen == ["llama"]


def test_the_install_question_says_the_chat_moves(monkeypatch):
    """When the pick moves an API provider's chat to this machine, the question says so.
    MUTATION: drop the sentence - red."""
    from tests.test_tui_model_picker import _patch_config
    from vaf.cli.tui_app import screens as screens_mod
    pushed = []
    app = SimpleNamespace(_bridge=SimpleNamespace(busy=False, apply_engine=lambda *a, **k: None),
                          notify=lambda *a, **k: None,
                          push_screen=lambda s, cb=None: pushed.append(s))
    for provider, moves in (("anthropic", True), ("local", False)):
        _patch_config(monkeypatch, {"provider": provider})
        screens_mod.switch_engine(app, "vllm", state=_state(needs_install=True),
                                  make_local=True)
        assert ("The main chat then runs on this machine." in pushed[-1]._question) is moves
    screens_mod.switch_engine(app, "vllm", state=_state(needs_install=True))
    assert "main chat" not in pushed[-1]._question


def test_make_local_stored_but_not_moved_says_so(monkeypatch, tmp_path):
    """The provider is stored as local, but this agent kept its API backend (an agent pinned
    to its provider): a warning, as a provider switch gives. MUTATION: drop it - red."""
    from vaf.cli.tui_app.agent_bridge import AgentBridge
    from vaf.core import log_helper, provider_switch
    monkeypatch.setattr(log_helper, "get_app_log_dir", lambda: tmp_path)
    notes, lane = [], []
    monkeypatch.setattr(le, "apply", lambda engine, **kw: le.EngineSwitch(
        "done", "switched_vllm", "text", "vllm"))
    monkeypatch.setattr(provider_switch, "switch_provider",
                        lambda *a, **k: provider_switch.ProviderSwitch(
                            "done", "stored", "ok", "local", "", 0))
    bridge = AgentBridge.__new__(AgentBridge)
    bridge.agent = SimpleNamespace(provider="anthropic", reload_local_model=lambda: None)
    bridge.events = SimpleNamespace(event_note=lambda *a: notes.append(a),
                                    system_note=lambda *a: notes.append(a))
    bridge._emit = lambda *a: None
    bridge._refresh_context = lambda: None
    bridge._busy = False
    bridge._submit = lane.append
    bridge.apply_engine("vllm", make_local=True)
    lane[0]()
    assert any("kept its backend" in str(n) for n in notes), notes


def test_an_unreadable_engine_state_row_shows_no_exception_text(monkeypatch):
    """MUTATION: the exception text back in the Engine submenu row - red."""
    s, *_ = _screen(monkeypatch, state=None)

    def _boom(model=None):
        raise RuntimeError("internal-detail-xyz")
    s.app._bridge.engine_state = _boom
    s._load_engine_state()
    text = " ".join(str(r[2]) for r in s._menu_rows("engine"))
    assert "internal-detail-xyz" not in text and "try again" in text


@pytest.mark.parametrize("where", ["local_model", "engine", "reload_after_switch",
                                   "make_local_reload"])
def test_engine_and_model_failures_show_no_exception_text(monkeypatch, tmp_path, where):
    """The three notes of the engine and model lanes say what to do; the exception goes to the
    log. MUTATION: put `{exc}` back into any of them - its case turns red."""
    from vaf.cli.tui_app.agent_bridge import AgentBridge
    from vaf.core import log_helper, provider_switch
    monkeypatch.setattr(log_helper, "get_app_log_dir", lambda: tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("internal-detail-xyz")
    notes, lane = [], []
    agent = SimpleNamespace(provider="local", reload_local_model=_boom)
    if where == "engine":
        monkeypatch.setattr(le, "apply", _boom)
    else:
        monkeypatch.setattr(le, "apply", lambda engine, **kw: le.EngineSwitch(
            "done", "switched_vllm", "text", "vllm"))
    if where == "make_local_reload":
        agent.provider = "anthropic"

        def _switch(*a, **k):
            agent.provider = "local"
            return provider_switch.ProviderSwitch("done", "switched", "ok", "local", "", 1)
        monkeypatch.setattr(provider_switch, "switch_provider", _switch)
        agent.api_backend = agent.use_server = agent.llm = None
    monkeypatch.setattr(le, "is_vllm", lambda config=None: False)
    bridge = AgentBridge.__new__(AgentBridge)
    bridge.agent = agent
    bridge.events = SimpleNamespace(event_note=lambda *a: notes.append(a),
                                    system_note=lambda *a: notes.append(a))
    bridge._emit = lambda *a: None
    bridge._refresh_context = lambda: None
    bridge._busy = False
    bridge._submit = lane.append
    if where == "local_model":
        import vaf.core.config as config_mod
        monkeypatch.setattr(config_mod.Config, "set", classmethod(lambda cls, k, v: None))
        bridge.apply_local_model("q.gguf")
    else:
        bridge.apply_engine("vllm", make_local=where == "make_local_reload")
    lane[0]()
    joined = " ".join(str(n) for n in notes)
    assert "internal-detail-xyz" not in joined and "RuntimeError" not in joined, joined
    assert "try again" in joined or "server log" in joined, joined
