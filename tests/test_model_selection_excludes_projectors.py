# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A vision projector is not a model, and no picker may offer one.

VAF's own local-vision download writes the multimodal projector into the same
`models/` directory as the models (`resolve_mmproj_for`: auto name
`mmproj-<stem>.gguf`, upstream repos ship `mmproj-F16.gguf`). Every model
picker listed `*.gguf` unfiltered, so the projector appeared as a choice - and
picking it wrote it into `model`, after which llama-server refused to start:

    error loading model: unsupported model architecture: 'clip'

Live incident: a model switch from Gemma to Qwen offered the Qwen projector,
and the local backend was dead until the config was corrected by hand.
"""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core.backend import is_selectable_model


@pytest.mark.parametrize("name,ok", [
    ("Qwen3.5-4B-UD-Q8_K_XL.gguf", True),
    ("google_gemma-4-E4B-it-Q4_K_M.gguf", True),
    # The two projector spellings that actually occur: VAF's per-model name
    # and the upstream repo name.
    ("mmproj-Qwen3.5-4B-UD-Q8_K_XL.gguf", False),
    ("mmproj-F16.gguf", False),
    ("MMPROJ-F16.GGUF", False),
    ("qwen-mmproj-F16.gguf", False),
    ("readme.txt", False),
    ("", False),
])
def test_the_filter_knows_a_projector_from_a_model(name, ok):
    assert is_selectable_model(name) is ok


def test_an_explicitly_configured_projector_is_excluded_by_name(monkeypatch):
    """A hand-set `vision_local_mmproj` may use any filename - it must not
    reappear in the picker just because it lacks the usual prefix."""
    import vaf.core.config as config_mod

    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, k, d=None:
                                    "owner/repo/vision-head.gguf"
                                    if k == "vision_local_mmproj" else d))
    assert is_selectable_model("vision-head.gguf") is False
    assert is_selectable_model("Qwen3.5-4B.gguf") is True


# ── every picker consumes the filter ────────────────────────────────────────────────

def test_the_terminal_app_picker_filters(tmp_path, monkeypatch):
    from vaf.cli.tui_app.agent_bridge import AgentBridge
    import vaf.core.config as config_mod

    (tmp_path / "Qwen3.5-4B.gguf").write_bytes(b"x")
    (tmp_path / "mmproj-Qwen3.5-4B.gguf").write_bytes(b"x")
    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, k, d=None:
                                    "Qwen3.5-4B.gguf" if k == "model" else d))

    b = AgentBridge.__new__(AgentBridge)
    b.agent = SimpleNamespace(models_dir=str(tmp_path))
    files, current = b.list_local_models()
    assert files == ["Qwen3.5-4B.gguf"], files
    assert current == "Qwen3.5-4B.gguf"


def test_the_classic_menu_and_the_web_listing_filter_too():
    """Source-level: both build their list from a directory scan, and the
    filter is worthless if a picker keeps its own unfiltered glob."""
    repo = Path(__file__).resolve().parents[1]
    settings = (repo / "vaf/cli/cmd/settings.py").read_text(encoding="utf-8")
    assert "is_selectable_model(f)" in settings, "the classic model menu lost the filter"

    web = (repo / "vaf/core/web_server.py").read_text(encoding="utf-8")
    assert web.count("is_selectable_model(f.name)") >= 2, (
        "a web model listing went back to an unfiltered *.gguf glob")


def test_a_poisoned_config_self_heals_instead_of_killing_the_server(monkeypatch, tmp_path):
    """A config written BEFORE the filter existed still points at a projector.
    Starting is more important than obeying it: the existing VRAM self-heal
    takes over, and says so."""
    import vaf.core.backend as backend

    import vaf.core.gpu_detection as gpu

    said = []
    monkeypatch.setattr(backend.UI, "event",
                        staticmethod(lambda *a, **k: said.append(a)))
    # Imported INSIDE the function, so the source module is what must be patched.
    monkeypatch.setattr(gpu, "recommended_default_model",
                        lambda: "healed-model.gguf")
    (tmp_path / "healed-model.gguf").write_bytes(b"x")
    monkeypatch.setattr(backend, "_resolve_model_ref",
                        lambda ref: (None, os.path.basename(str(ref))))

    out = backend.ensure_model_available("mmproj-Qwen3.5-4B.gguf", str(tmp_path))
    assert out.endswith("healed-model.gguf"), out
    assert any("vision projector" in str(a) for a in said), said


# ── the swap carries the user's sizing ──────────────────────────────────────────────

def test_a_model_swap_keeps_the_configured_context_and_gpu_layers(monkeypatch, tmp_path):
    """`Agent.load_model` passes n_ctx and gpu_layers; the SWAP path did not,
    so changing the model silently reset a configured 131072 window to the
    32768 default - and the setting still said it was 131072."""
    import vaf.core.backend as backend
    import vaf.core.config as config_mod

    model = tmp_path / "Qwen3.5-4B.gguf"
    model.write_bytes(b"x")
    values = {"n_ctx": 131072, "gpu_layers": 42}
    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, k, d=None: values.get(k, d)))
    monkeypatch.setattr(backend, "_loaded_model_matches", lambda p, port=8080: False)
    monkeypatch.setattr(backend, "get_loaded_model_id", lambda port=8080: None)

    seen = {}

    class _Mgr:
        def __init__(self, skip_cleanup=False):
            pass

        def start_server(self, path, n_gpu_layers=99, n_ctx=32768, **kw):
            seen.update(path=path, n_gpu_layers=n_gpu_layers, n_ctx=n_ctx)
            return True

    monkeypatch.setattr(backend, "ServerManager", _Mgr)
    assert backend.ensure_local_model(str(model)) is True
    assert seen["n_ctx"] == 131072, seen
    assert seen["n_gpu_layers"] == 42, seen


def test_the_swap_still_floors_the_context_window(monkeypatch, tmp_path):
    """The floor `Agent.load_model` applies (32768 minimum for local models)
    travels too - a config below it must not reach the server."""
    import vaf.core.backend as backend
    import vaf.core.config as config_mod

    model = tmp_path / "m.gguf"
    model.write_bytes(b"x")
    monkeypatch.setattr(config_mod.Config, "get",
                        classmethod(lambda cls, k, d=None:
                                    4096 if k == "n_ctx" else d))
    monkeypatch.setattr(backend, "_loaded_model_matches", lambda p, port=8080: False)
    monkeypatch.setattr(backend, "get_loaded_model_id", lambda port=8080: None)
    seen = {}

    class _Mgr:
        def __init__(self, skip_cleanup=False):
            pass

        def start_server(self, path, n_gpu_layers=99, n_ctx=32768, **kw):
            seen["n_ctx"] = n_ctx
            return True

    monkeypatch.setattr(backend, "ServerManager", _Mgr)
    backend.ensure_local_model(str(model))
    assert seen["n_ctx"] == 32768, seen


# ── local vision follows the main agent, and the server reuse knows it ──────────────

def test_an_empty_vision_setting_follows_the_main_agent(monkeypatch, tmp_path):
    """The default is EMPTY, and every other reader takes that to mean "use
    the main agent's provider if it can see". The server start read the raw
    key instead, so a local vision model launched WITHOUT its projector and
    answered "image input is not supported" while the settings looked fine
    (live incident on a local Gemma)."""
    import vaf.core.backend as backend
    import vaf.core.vision_infer as vi

    model = tmp_path / "google_gemma-4-E4B-it-Q4_K_M.gguf"
    model.write_bytes(b"x")
    (tmp_path / "mmproj-google_gemma-4-E4B-it-Q4_K_M.gguf").write_bytes(b"y")

    monkeypatch.setattr(vi, "select_vision_backend", lambda: ("local", "gemma"))
    assert backend.resolve_mmproj_for(str(model)).endswith(
        "mmproj-google_gemma-4-E4B-it-Q4_K_M.gguf")

    # A cloud vision provider still means: no projector on the local server.
    monkeypatch.setattr(vi, "select_vision_backend", lambda: ("veyllo", "veyllo-chat"))
    assert backend.resolve_mmproj_for(str(model)) == ""


def test_reuse_restarts_when_the_running_server_cannot_see(monkeypatch, tmp_path):
    """`--mmproj` is a launch argument: a server started before local vision
    was switched on stays blind forever if reuse only compares the model."""
    import vaf.core.backend as backend

    mgr = backend.ServerManager.__new__(backend.ServerManager)
    mgr.pid_file = str(tmp_path / "server.pid")

    mgr._save_vision_state("")                       # started WITHOUT vision
    assert mgr._running_vision_state() == ""
    mgr._save_vision_state("/models/mmproj-x.gguf")  # started WITH vision
    assert mgr._running_vision_state() == "/models/mmproj-x.gguf"

    # The comparison the reuse branch makes is a boolean one: has projector
    # vs wants projector. Both mismatches must force a restart.
    for running, wanted in (("", "/models/mmproj-x.gguf"), ("/models/mmproj-x.gguf", "")):
        mgr._save_vision_state(running)
        assert bool(wanted) != bool(mgr._running_vision_state())


def test_a_clean_shutdown_takes_the_vision_record_with_it(tmp_path):
    """The record describes ONE running server. Surviving its own server would
    make the next reuse check compare against a launch that is long gone."""
    import vaf.core.backend as backend

    mgr = backend.ServerManager.__new__(backend.ServerManager)
    mgr.pid_file = str(tmp_path / "server.pid")
    open(mgr.pid_file, "w").close()
    mgr._save_vision_state("/models/mmproj-x.gguf")

    mgr._remove_pid()
    assert not os.path.exists(mgr.pid_file)
    assert not os.path.exists(mgr._vision_state_file)
    assert mgr._running_vision_state() == ""


def test_the_reuse_branch_actually_consults_the_vision_state():
    """Wiring pin: the state file is worthless if the reuse path ignores it."""
    import inspect

    import vaf.core.backend as backend

    src = inspect.getsource(backend.ServerManager._start_server_locked)
    assert "_running_vision_state()" in src, "server reuse stopped checking for vision"
