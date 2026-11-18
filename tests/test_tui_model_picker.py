# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The terminal app's one picker for API, llama.cpp and vLLM, and the routes into it.

THE DEFECT. Settings > AI Provider and Settings > API Model dismissed the settings screen
and pushed the picker from it in the same handler. Textual binds a pushed screen's result
callback to the pump that is active at the push, which was the settings screen being
closed, so the pick was delivered to a closed pump and dropped: nothing written, nothing
switched, no word. Only `c` worked. The test below presses the real keys.

MUTATIONS, each turns the named test red:
- restore `self.dismiss(None); self.app.action_model()` in the settings rows ->
  test_settings_rows_switch_like_c[s-provider], [s-api_model] and
  test_no_dismiss_then_push_in_one_handler;
- keep the old inline write block in the bridge -> test_bridge_delegates_to_core;
- drop the local load step -> test_api_to_local_loads_the_local_model;
- load although an API backend, a server or an in-process model serves ->
  test_already_serving_is_not_reloaded[api_backend, use_server, llm];
- remove the vLLM row / keep the stale "model chosen in" text ->
  test_picker_rows_local_engines_first;
- show the local rows under the api profile -> test_picker_under_api_profile_shows_the_sentence;
- call `local_engine.state()` in on_mount -> test_picker_open_runs_no_probe;
- swap the first two rows of the table, call apply_engine before the question, let an
  unsupported vLLM pass -> test_choose_local_engine_table[...].
"""
import ast
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_tui_app_smoke import _settle, smoke_app  # noqa: F401
from vaf.cli.tui_app import screens as screens_mod
from vaf.cli.tui_app.screens import ConfirmScreen, ModelScreen, SettingsScreen
from vaf.core import local_engine as le
from vaf.core.config import Config

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-test-key-1234567890"


def _patch_config(monkeypatch, values: dict):
    real_get = Config.get.__func__
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d=None: values[k] if k in values else real_get(cls, k, d)))


# ── AC1: the Settings rows switch like `c` ────────────────────────────────────────

def _drive(smoke, word, row_kind, monkeypatch):
    app, bridge = smoke.app, smoke.bridge
    _patch_config(monkeypatch, {"provider": "anthropic", "api_model_anthropic": "claude-x"})
    monkeypatch.setattr(Config, "get_api_key", classmethod(lambda cls, p: KEY))
    calls = []
    monkeypatch.setattr(type(bridge), "apply_provider_change",
                        lambda self, *a, **k: calls.append((a, k)))

    async def _run():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app._submitted(SimpleNamespace(text=word))
            assert await _settle(pilot, lambda: isinstance(app.screen,
                                                            (SettingsScreen, ModelScreen)))
            if row_kind is not None:
                assert isinstance(app.screen, SettingsScreen)
                idx = next(i for i, r in enumerate(app.screen._rows) if r[0] == row_kind)
                for _ in range(idx):
                    await pilot.press("down")
                await pilot.press("enter")
                assert await _settle(pilot, lambda: isinstance(app.screen, ModelScreen)), \
                    "the settings row did not open the picker"
            rows = app.screen._rows
            lv = app.screen.query_one("#model-list")
            first_model = next(i for i, r in enumerate(rows) if r[0] == "model")
            while lv.index < first_model:
                await pilot.press("down")
            await pilot.press("enter")
            await _settle(pilot, lambda: bool(calls), timeout=3.0)
    asyncio.run(_run())
    bridge.shutdown()
    return calls


@pytest.mark.parametrize("word, row_kind", [("s", "provider"), ("s", "api_model"),
                                            ("c", None)],
                         ids=["s-provider", "s-api_model", "c"])
def test_settings_rows_switch_like_c(smoke_app, monkeypatch, word, row_kind):
    first = ModelScreen._models_for("anthropic")[0]
    calls = _drive(smoke_app, word, row_kind, monkeypatch)
    assert calls == [(("anthropic", first), {})], calls


# ── AC2: the class, statically ────────────────────────────────────────────────────

def _is_self_call(node, *chain) -> bool:
    """`self.<chain...>(...)`, e.g. ("app", "push_screen")."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    for name in reversed(chain):
        if not (isinstance(func, ast.Attribute) and (name == "*" or func.attr == name)):
            return False
        func = func.value
    return isinstance(func, ast.Name) and func.id == "self"


def _dismiss_then_push(tree) -> list:
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = sorted((n for n in ast.walk(fn) if isinstance(n, ast.Call)),
                       key=lambda n: (n.lineno, n.col_offset))
        dismissed = False
        for call in calls:
            if _is_self_call(call, "dismiss"):
                dismissed = True
            elif dismissed and (
                    _is_self_call(call, "app", "push_screen")
                    or (_is_self_call(call, "app", "*")
                        and call.func.attr.startswith("action_"))):
                found.append(f"{fn.name}:{call.lineno}")
    return found


def test_no_dismiss_then_push_in_one_handler():
    src = (ROOT / "vaf" / "cli" / "tui_app" / "screens.py").read_text(encoding="utf-8")
    assert _dismiss_then_push(ast.parse(src)) == []
    # The guard sees the shape it is for.
    bad = ("def f(self):\n    self.dismiss(None)\n    self.app.action_model()\n"
           "def g(self):\n    self.dismiss(None)\n    self.app.push_screen(X())\n"
           "def h(self):\n    self.dismiss(None)\n    self.app.call_later(self.app.action_model)\n")
    assert _dismiss_then_push(ast.parse(bad)) == ["f:3", "g:6"]


# ── AC5 / AC6: the bridge ─────────────────────────────────────────────────────────

def _drain(bridge, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not bridge._busy and bridge._queue.empty():
            time.sleep(0.05)
            if not bridge._busy and bridge._queue.empty():
                return
        time.sleep(0.02)


def _bridge(agent):
    from vaf.cli.tui_app.agent_bridge import AgentBridge
    notes = []
    events = SimpleNamespace(system_note=lambda t: notes.append(("note", t)),
                             event_note=lambda t, m, s: notes.append((t, m)),
                             presence=lambda *a, **k: None, context=lambda *a: None)
    bridge = AgentBridge(agent, SimpleNamespace(id="s"), None, events,
                         web_interface_getter=lambda: SimpleNamespace(
                             resolve_gate=lambda *a: True))
    return bridge, notes


def _agent(**fields):
    base = dict(get_token_usage=lambda: (1, 2), set_event_sink=lambda s: None,
                shutdown=lambda: None)
    base.update(fields)
    return SimpleNamespace(**base)


def test_bridge_delegates_to_core(monkeypatch):
    from vaf.cli.tui_app.agent_bridge import AgentBridge
    from vaf.core import provider_switch
    seen = []
    monkeypatch.setattr(provider_switch, "switch_provider",
                        lambda *a, **k: seen.append((a, k)) or provider_switch.ProviderSwitch(
                            "done", "switched", "ok", "anthropic", "m", 1))
    bridge, _ = _bridge(_agent())
    bridge.apply_provider_change("anthropic", "m", new_key="sk-typed-key-value")
    _drain(bridge)
    bridge.shutdown()
    assert len(seen) == 1
    args, kwargs = seen[0]
    assert args == ("anthropic", "m") and kwargs["new_key"] == "sk-typed-key-value"
    assert kwargs["verify"] is AgentBridge._api_key_verifies


def _to_local(monkeypatch, agent):
    """The switch, stubbed as the real one leaves an agent moved off an API provider:
    provider local, no backend (vaf/core/agent.py reload_api_backend)."""
    from vaf.core import provider_switch

    def _switch(provider, model="", **kw):
        agent.provider, agent.api_backend = "local", None
        return provider_switch.ProviderSwitch("done", "switched", "ok", "local", "", 1)
    monkeypatch.setattr(provider_switch, "switch_provider", _switch)


def test_api_to_local_loads_the_local_model(monkeypatch):
    loads = []
    agent = _agent(provider="anthropic", api_backend=object(), use_server=False, llm=None,
                   reload_local_model=lambda: loads.append(1) or True)
    _to_local(monkeypatch, agent)
    monkeypatch.setattr(le, "is_vllm", lambda config=None: False)
    bridge, notes = _bridge(agent)
    bridge.apply_provider_change("local")
    _drain(bridge)
    bridge.shutdown()
    assert loads == [1], "the agent was left on local with nothing to serve"
    assert any("loading the local model" in str(m) for _, m in notes), notes
    assert ("note", "switched to local · llama.cpp") in notes


def test_a_load_that_fails_says_so(monkeypatch):
    agent = _agent(provider="anthropic", api_backend=object(), use_server=False, llm=None,
                   reload_local_model=lambda: False)
    _to_local(monkeypatch, agent)
    monkeypatch.setattr(le, "is_vllm", lambda config=None: True)
    bridge, notes = _bridge(agent)
    bridge.apply_provider_change("local")
    _drain(bridge)
    bridge.shutdown()
    assert any("did not come up" in str(m) and "vLLM" in str(m) for _, m in notes), notes


@pytest.mark.parametrize("serves", ["api_backend", "use_server", "llm"])
def test_already_serving_is_not_reloaded(monkeypatch, serves):
    """Any one of the three is something serving: a backend, the server, an in-process
    model. MUTATION: drop one of them from the bridge's check - its row turns red."""
    from vaf.core import provider_switch
    loads = []
    fields = {"api_backend": None, "use_server": False, "llm": None}
    fields[serves] = True if serves == "use_server" else object()
    agent = _agent(provider="local", reload_local_model=lambda: loads.append(1) or True,
                   **fields)
    monkeypatch.setattr(provider_switch, "switch_provider",
                        lambda p, m="", **kw: provider_switch.ProviderSwitch(
                            "done", "stored", "ok", "local", "", 0))
    monkeypatch.setattr(le, "is_vllm", lambda config=None: False)
    bridge, notes = _bridge(agent)
    bridge.apply_provider_change("local")
    _drain(bridge)
    bridge.shutdown()
    assert loads == []
    assert ("note", "local · llama.cpp is already in use") in notes


def test_a_refusal_is_a_note_and_nothing_else(monkeypatch):
    from vaf.core import provider_switch
    loads = []
    agent = _agent(provider="anthropic", api_backend=object(), use_server=False, llm=None,
                   reload_local_model=lambda: loads.append(1))
    monkeypatch.setattr(provider_switch, "switch_provider",
                        lambda p, m="", **kw: provider_switch.ProviderSwitch(
                            "refused", "profile_api", "profile api sentence", "local"))
    bridge, notes = _bridge(agent)
    bridge.apply_provider_change("local")
    _drain(bridge)
    bridge.shutdown()
    assert loads == [] and ("Provider", "profile api sentence") in notes


# ── AC7: the picker ───────────────────────────────────────────────────────────────

def _mount_picker(monkeypatch, values: dict):
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    _patch_config(monkeypatch, values)
    box = {}

    class _Host(App):
        def compose(self) -> ComposeResult:
            yield Static("host")

    async def _run():
        app = _Host()
        async with app.run_test(size=(110, 40)) as pilot:
            app.push_screen(ModelScreen(), lambda v: box.__setitem__("result", v))
            await pilot.pause()
            screen = app.screen
            box["rows"] = list(screen._rows)
            box["text"] = [str(item.query_one(Static).content)
                           for item in screen.query_one("#model-list").children]
            box["index"] = screen.query_one("#model-list").index
            await pilot.press("enter")
            await pilot.pause()
    asyncio.run(_run())
    return box


def test_picker_rows_local_engines_first(monkeypatch):
    box = _mount_picker(monkeypatch, {"provider": "local", "local_engine": "vllm",
                                      "model": "q.gguf", "vllm_model": "Qwen/Qwen3-4B-AWQ"})
    assert box["rows"][:3] == [("local", "llama"), ("local", "vllm"), ("sep", None)]
    assert all(kind in ("provider", "sep") for kind, _ in box["rows"][3:]), \
        "provider local lists no API models"
    assert "local · llama.cpp" in box["text"][0] and "q.gguf" in box["text"][0]
    assert "local · vLLM" in box["text"][1] and "Qwen/Qwen3-4B-AWQ" in box["text"][1]
    assert "▍" in box["text"][1] and "▍" not in box["text"][0], "the current engine is marked"
    assert box["index"] == 1
    assert box["result"] == ("local", "", False, "vllm")
    joined = " ".join(box["text"])
    assert "model chosen in" not in joined


def test_picker_marks_the_api_provider_and_lists_its_models(monkeypatch):
    box = _mount_picker(monkeypatch, {"provider": "anthropic", "local_engine": "llama",
                                      "vllm_model": ""})
    assert "automatic" in box["text"][1]
    assert not any("▍" in t for t in box["text"][:2])
    at = box["rows"].index(("provider", "anthropic"))
    assert box["index"] == at and "▍" in box["text"][at]
    assert ("model", ModelScreen._models_for("anthropic")[0]) in box["rows"]
    assert box["result"] == ("anthropic", "", False, None)


def test_picker_under_api_profile_shows_the_sentence(monkeypatch):
    box = _mount_picker(monkeypatch, {"provider": "anthropic", "inference_profile": "api"})
    assert ("local", "llama") not in box["rows"] and ("local", "vllm") not in box["rows"]
    assert box["rows"][0] == ("note", None)
    assert 'profile "api"' in box["text"][0]
    assert box["rows"][box["index"]] == ("provider", "anthropic")


def test_picker_open_runs_no_probe(monkeypatch):
    def _probe(*a, **k):
        raise AssertionError("the picker probed the hardware to draw its list")
    monkeypatch.setattr(le, "state", _probe)
    monkeypatch.setattr(le, "support", _probe)
    box = _mount_picker(monkeypatch, {"provider": "local"})
    assert box["rows"][0] == ("local", "llama")


# ── AC8: choose_local_engine ──────────────────────────────────────────────────────

def _state(engine="llama", level="supported", profile="local", needs_install=False):
    return {"engine": engine, "profile": profile,
            "support": {"level": level, "reason": "vLLM needs an NVIDIA GPU; llama.cpp runs "
                                                  "here." if level != "supported" else "ok"},
            "vllm": {"pin": "0.32.0", "needs_install": needs_install,
                     "download_gb_at_least": 4.0, "model": "Qwen/Qwen3-4B-AWQ",
                     "auto_model": "Qwen/Qwen3-4B-AWQ",
                     "curated": [{"repo": "Qwen/Qwen3-4B-AWQ", "weights_gb": 2.67}]}}


def _rig(monkeypatch, *, provider, engine, state=None, busy=False):
    calls, pushed, notified = [], [], []
    _patch_config(monkeypatch, {"provider": provider})
    monkeypatch.setattr(le, "is_vllm", lambda config=None: engine == "vllm")
    bridge = SimpleNamespace(
        busy=busy,
        engine_state=lambda model=None: calls.append(("engine_state",)) or state,
        apply_engine=lambda engine, **kw: calls.append(("apply_engine", engine, kw)),
        apply_provider_change=lambda *a, **k: calls.append(("provider", a)))
    app = SimpleNamespace(_bridge=bridge, notify=lambda m, **kw: notified.append(m),
                          push_screen=lambda s, cb=None: pushed.append((s, cb)))
    return app, calls, pushed, notified


def test_choose_local_engine_table(monkeypatch):
    choose = screens_mod.choose_local_engine
    # 1. a running turn: refused, nothing else
    app, calls, pushed, notified = _rig(monkeypatch, provider="anthropic", engine="llama",
                                        busy=True)
    choose(app, "vllm")
    choose(app, "llama")
    assert calls == [] and pushed == [] and notified == ["not while a turn is running"] * 2

    # 2. API provider, the engine already the current one: the provider only
    app, calls, pushed, _ = _rig(monkeypatch, provider="anthropic", engine="llama")
    choose(app, "llama")
    assert calls == [("provider", ("local",))] and pushed == []

    # 3. another engine: the state, the install question first, then the switch to local
    app, calls, pushed, _ = _rig(monkeypatch, provider="anthropic", engine="llama",
                                 state=_state(needs_install=True))
    choose(app, "vllm")
    assert calls == [("engine_state",)], "apply_engine ran before the question"
    screen, answer = pushed[-1]
    assert isinstance(screen, ConfirmScreen) and "Install vLLM 0.32.0" in screen._question
    answer(True)
    assert calls[-1] == ("apply_engine", "vllm", {"model": None, "install": True,
                                                  "gpu_share": None, "make_local": True})
    #    ... refused under the api profile, and for a vLLM this machine cannot run
    app, calls, pushed, notified = _rig(monkeypatch, provider="anthropic", engine="llama",
                                        state=_state(profile="api"))
    choose(app, "vllm")
    assert calls == [("engine_state",)] and 'profile "api"' in notified[-1]
    app, calls, pushed, notified = _rig(monkeypatch, provider="anthropic", engine="llama",
                                        state=_state(level="unsupported"))
    choose(app, "vllm")
    assert calls == [("engine_state",)] and pushed == []
    assert "vLLM needs an NVIDIA GPU" in notified[-1]

    # 4. provider local, same engine, from the picker: the provider switch (loads if idle)
    app, calls, pushed, _ = _rig(monkeypatch, provider="local", engine="vllm")
    choose(app, "vllm")
    assert calls == [("provider", ("local",))]

    # 5. provider local, same engine, from the Engine submenu: the switch itself (the retry)
    app, calls, pushed, _ = _rig(monkeypatch, provider="local", engine="vllm",
                                 state=_state(engine="vllm"))
    choose(app, "vllm", from_settings=True, state=_state(engine="vllm"))
    assert calls == [("apply_engine", "vllm", {"model": None, "install": False,
                                               "gpu_share": None, "make_local": False})]


def test_the_app_routes_a_local_pick_to_choose_local_engine(smoke_app, monkeypatch):
    app = smoke_app.app
    seen = []
    monkeypatch.setattr(type(app), "choose_local_engine",
                        lambda self, engine, **kw: seen.append(engine))
    monkeypatch.setattr(type(smoke_app.bridge), "apply_provider_change",
                        lambda self, *a, **k: seen.append(("provider", a)))
    app._model_chosen(("local", "", False, "vllm"))
    assert seen == ["vllm"]
    smoke_app.bridge.shutdown()


def test_settings_main_row_names_the_engine(monkeypatch):
    s = SettingsScreen.__new__(SettingsScreen)
    s._cfg = lambda key, default=None: {"provider": "local"}.get(key, default)
    monkeypatch.setattr(le, "is_vllm", lambda config=None: True)
    assert "AI Provider: [$text]LOCAL · vLLM[/]" in s._menu_rows("main")[0][2]
    monkeypatch.setattr(le, "is_vllm", lambda config=None: False)
    assert "AI Provider: [$text]LOCAL · llama.cpp[/]" in s._menu_rows("main")[0][2]


# ── Round of review fixes: the picker's height, the top bar, the texts ─────────────

def _largest_provider():
    catalog = getattr(Config, "PROVIDER_MODELS", {}) or {}
    return max(sorted(catalog), key=lambda p: len(ModelScreen._models_for(p)))


@pytest.mark.parametrize("from_api_model_row", [False, True])
def test_the_largest_provider_fits_at_80x24(smoke_app, monkeypatch, from_api_model_row):
    """Every row of the picker is visible in an 80x24 terminal for the provider with the
    most models, and from the API Model row the cursor starts on the model in use.
    MUTATION: the list cap back at 12 - red; ignore `focus_model` - red."""
    app, bridge = smoke_app.app, smoke_app.bridge
    provider = _largest_provider()
    models = ModelScreen._models_for(provider)
    _patch_config(monkeypatch, {"provider": provider, f"api_model_{provider}": models[-1]})
    box = {}

    async def _run():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_model(from_api_model_row)
            assert await _settle(pilot, lambda: isinstance(app.screen, ModelScreen))
            await pilot.pause()
            lv = app.screen.query_one("#model-list")
            box["rows"], box["height"] = len(app.screen._rows), lv.size.height
            box["bottom"] = app.screen.query_one("#model-box").region.bottom
            box["cursor"] = app.screen._rows[lv.index]
    asyncio.run(_run())
    bridge.shutdown()
    assert box["height"] >= box["rows"], f"{box['rows'] - box['height']} rows hidden"
    assert box["bottom"] <= 24
    if from_api_model_row:
        assert box["cursor"] == ("model", models[-1])
    else:
        assert box["cursor"] == ("provider", provider)


@pytest.mark.parametrize("values, chip", [
    ({"provider": "local", "local_engine": "vllm", "vllm_model": "Qwen/Qwen3-8B-AWQ",
      "model": "q.gguf"}, "local · vLLM · Qwen/Qwen3-8B-AWQ"),
    ({"provider": "local", "local_engine": "vllm", "vllm_model": "", "model": "q.gguf"},
     "local · vLLM · automatic"),
    ({"provider": "local", "local_engine": "llama", "model": "q.gguf"}, "local · q.gguf"),
    ({"provider": "anthropic", "api_model_anthropic": "claude-x"}, "anthropic · claude-x"),
])
def test_the_top_bar_names_what_runs(monkeypatch, values, chip):
    """Under vLLM the top bar named the GGUF, which only llama.cpp runs. MUTATION: drop the
    vLLM branch of `model_chip` - red."""
    from vaf.cli.tui_app.app import model_chip
    _patch_config(monkeypatch, values)
    monkeypatch.setattr(le, "is_vllm", lambda config=None: values.get("local_engine") == "vllm")
    assert model_chip() == chip


def test_long_local_names_end_in_an_ellipsis(monkeypatch):
    """A long GGUF or repo name stays on its row at 80 columns. MUTATION: drop the cut."""
    gguf = "Some-Very-Long-Model-Name-With-Many-Parts-Instruct-2026-Q4_K_M-imatrix.gguf"
    box = _mount_picker(monkeypatch, {"provider": "local", "local_engine": "llama",
                                      "model": gguf})
    assert gguf not in box["text"][0] and "…" in box["text"][0]
    assert gguf[:30] in box["text"][0]


def test_the_api_profile_line_points_at_the_profile_not_at_settings(monkeypatch):
    """Inside the picker "choose a provider under Settings" is circular, and backticks print
    as they are. MUTATION: back to `inference_profile.refusal` - red."""
    box = _mount_picker(monkeypatch, {"provider": "anthropic", "inference_profile": "api"})
    assert "Settings" not in box["text"][0] and "`" not in box["text"][0]
    assert "vaf profile local" in box["text"][0]


def test_a_failing_switch_shows_no_exception_text(monkeypatch):
    """MUTATION: put the exception's name or text back into the note - red."""
    from vaf.core import provider_switch

    def _boom(*a, **k):
        raise RuntimeError("internal-detail-xyz")
    monkeypatch.setattr(provider_switch, "switch_provider", _boom)
    bridge, notes = _bridge(_agent())
    bridge.apply_provider_change("anthropic")
    _drain(bridge)
    bridge.shutdown()
    joined = " ".join(str(m) for _, m in notes)
    assert "try again" in joined
    assert "RuntimeError" not in joined and "internal-detail-xyz" not in joined


def test_an_unreadable_engine_state_shows_no_exception_text(monkeypatch):
    app, calls, pushed, notified = _rig(monkeypatch, provider="anthropic", engine="llama")

    def _boom(model=None):
        raise RuntimeError("internal-detail-xyz")
    app._bridge.engine_state = _boom
    screens_mod.choose_local_engine(app, "vllm")
    assert notified and "internal-detail-xyz" not in notified[-1] and "try again" in notified[-1]


@pytest.mark.parametrize("model, downloads", [("", True), ("q.gguf", False)])
def test_the_load_note_says_when_a_default_model_is_downloaded(monkeypatch, model, downloads):
    """With llama.cpp and no GGUF set, the load downloads the default model first; a load
    that fails names the server's log. MUTATION: drop either sentence - red."""
    agent = _agent(provider="anthropic", api_backend=object(), use_server=False, llm=None,
                   reload_local_model=lambda: False)
    _to_local(monkeypatch, agent)
    monkeypatch.setattr(le, "is_vllm", lambda config=None: False)
    _patch_config(monkeypatch, {"model": model})
    bridge, notes = _bridge(agent)
    bridge.apply_provider_change("local")
    _drain(bridge)
    bridge.shutdown()
    joined = " ".join(str(m) for _, m in notes)
    assert ("downloads its default model" in joined) is downloads, joined
    from vaf.core.log_helper import get_app_log_dir
    assert f"the llama.cpp server log in {get_app_log_dir()}" in joined, joined


def test_one_engine_label():
    """The engine's display name has one home, `local_engine.LABELS`. MUTATION: a literal
    engine-name expression back in the terminal app - red."""
    import re
    assert le.label("llama") == "llama.cpp" and le.label("vllm") == "vLLM"
    assert screens_mod.SettingsScreen.ENGINE_LINES["vllm"][0] == le.LABELS["vllm"]
    q = "[\"']"
    pattern = re.compile(rf"{q}vLLM{q} if|{q}llama\.cpp{q} if|ENGINE_NAMES|"
                         rf"\({q}llama\.cpp{q},|\({q}vLLM{q},|{q}llama\.cpp  |{q}vLLM  ")
    for name in ("screens.py", "agent_bridge.py", "app.py"):
        src = (ROOT / "vaf" / "cli" / "tui_app" / name).read_text(encoding="utf-8")
        assert not pattern.search(src), name


def test_a_long_model_chip_ends_in_an_ellipsis(monkeypatch):
    """A long name pushed the session name out of the top bar at 80 columns, and the banner
    cut it without a mark. MUTATION: drop the cut in `model_chip` - red."""
    from vaf.cli.tui_app.app import MODEL_CHIP_CHARS, model_chip
    repo = "neuralmagic/Meta-Llama-3.1-70B-Instruct-quantized.w4a16-extra-long"
    _patch_config(monkeypatch, {"provider": "local", "local_engine": "vllm", "vllm_model": repo})
    monkeypatch.setattr(le, "is_vllm", lambda config=None: True)
    chip = model_chip()
    assert len(chip) == MODEL_CHIP_CHARS and chip.endswith("…")
    assert chip.startswith("local · vLLM · neuralmagic/")


def test_the_banner_model_row_fits_at_80_columns(smoke_app, monkeypatch):
    """The start banner's rows do not shrink: a long model row ran past the banner and was
    cut at its edge without a mark. MUTATION: the banner takes the top bar's limit - red."""
    from vaf.cli.tui_app.widgets import StartBanner
    app, bridge = smoke_app.app, smoke_app.bridge
    repo = "neuralmagic/Meta-Llama-3.1-70B-Instruct-quantized.w4a16-extra-long"
    _patch_config(monkeypatch, {"provider": "local", "local_engine": "vllm", "vllm_model": repo})
    monkeypatch.setattr(le, "is_vllm", lambda config=None: True)
    box = {}

    async def _run():
        async with app.run_test(size=(80, 24)) as pilot:
            for _ in range(3):
                await pilot.pause()
            banner = app.query_one(StartBanner)
            row = next(r for r in banner.query(".banner-row") if "model" in str(r.content))
            box["text"] = str(row.content)
            box["right"] = row.region.right
            box["edge"] = banner.query_one(".banner-row-wrap").region.right
    asyncio.run(_run())
    bridge.shutdown()
    assert "…" in box["text"], box["text"]
    assert box["right"] <= box["edge"], box
