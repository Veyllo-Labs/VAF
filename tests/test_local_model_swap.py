# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Model-aware single-server lifecycle (vaf/core/backend.py).

The dedicated voice-model lane swaps the ONE llama server between the voice
GGUF and the main GGUF. These tests pin the safety contract:
- `_loaded_model_matches` compares basenames and treats an unreadable id as
  MATCH (legacy reuse behavior preserved),
- `ensure_local_model` is a no-op when the server already holds the model,
  never touches ServerManager then, and requests a restart (with the
  provider gate skipped) when the model differs.
"""
import types

import vaf.core.backend as backend


class _Resp:
    def __init__(self, status=200, model_id=""):
        self.status_code = status
        self._id = model_id

    def json(self):
        return {"data": [{"id": self._id}]} if self._id else {"data": [{}]}


def _fake_models_endpoint(monkeypatch, model_id, status=200):
    def _get(url, timeout=None):
        assert "/v1/models" in url
        return _Resp(status, model_id)
    monkeypatch.setattr(backend.requests, "get", _get)


def test_loaded_model_matches_by_basename_case_insensitive(monkeypatch):
    _fake_models_endpoint(monkeypatch, "/x/models/Gemma-Test.GGUF")
    assert backend._loaded_model_matches("/other/dir/gemma-test.gguf") is True
    assert backend._loaded_model_matches("/other/dir/qwen-main.gguf") is False


def test_unreadable_model_id_counts_as_match(monkeypatch):
    # Legacy behavior: a healthy server whose id we cannot read is reused.
    def _boom(url, timeout=None):
        raise ConnectionError("down")
    monkeypatch.setattr(backend.requests, "get", _boom)
    assert backend._loaded_model_matches("/x/whatever.gguf") is True

    _fake_models_endpoint(monkeypatch, "", status=200)  # no id in the payload
    assert backend._loaded_model_matches("/x/whatever.gguf") is True


def test_ensure_local_model_noop_when_already_loaded(monkeypatch, tmp_path):
    model = tmp_path / "voice.gguf"
    model.write_bytes(b"gguf")
    _fake_models_endpoint(monkeypatch, str(model))

    class _Explode:
        def __init__(self, *a, **kw):
            raise AssertionError("ServerManager must not be touched on a match")
    monkeypatch.setattr(backend, "ServerManager", _Explode)
    assert backend.ensure_local_model(str(model)) is True


def test_ensure_local_model_swaps_on_mismatch(monkeypatch, tmp_path):
    model = tmp_path / "voice.gguf"
    model.write_bytes(b"gguf")
    _fake_models_endpoint(monkeypatch, "/x/models/qwen-main.gguf")
    seen = {}

    class _Mgr:
        def __init__(self, skip_cleanup=False):
            seen["skip_cleanup"] = skip_cleanup

        def start_server(self, model_path, skip_provider_gate=False, **kw):
            seen["model_path"] = model_path
            seen["skip_provider_gate"] = skip_provider_gate
            return True

    monkeypatch.setattr(backend, "ServerManager", _Mgr)
    assert backend.ensure_local_model(str(model), reason="test",
                                      skip_provider_gate=True) is True
    assert seen == {"skip_cleanup": True, "model_path": str(model),
                    "skip_provider_gate": True}


def test_ensure_local_model_missing_file_fails_closed(tmp_path):
    assert backend.ensure_local_model(str(tmp_path / "missing.gguf")) is False
    assert backend.ensure_local_model("") is False


def test_voice_model_ref_default_is_gemma(monkeypatch):
    from vaf.core.config import Config
    from vaf.core import voice_model as vm
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: ""))
    assert vm.voice_model_ref() == vm.DEFAULT_VOICE_MODEL
    assert "gemma-4-E4B" in vm.DEFAULT_VOICE_MODEL
    # The ref must be resolvable by the shared download entry point.
    repo, filename = backend._resolve_model_ref(vm.DEFAULT_VOICE_MODEL)
    assert repo == "bartowski/google_gemma-4-E4B-it-GGUF"
    assert filename == "google_gemma-4-E4B-it-Q4_K_M.gguf"


# ---------------------------------------------------------------------------
# Local vision (vision_provider=local, mmproj launch)
# ---------------------------------------------------------------------------

def _cfg_map(monkeypatch, mapping):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get",
                        classmethod(lambda cls, k, d=None: mapping.get(k, d)))


def test_mmproj_off_when_vision_not_local(monkeypatch, tmp_path):
    _cfg_map(monkeypatch, {"vision_provider": ""})
    model = tmp_path / "qwen3.5-4b-test.gguf"
    model.write_bytes(b"g")
    assert backend.resolve_mmproj_for(str(model)) == ""


def test_mmproj_found_on_disk_no_download(monkeypatch, tmp_path):
    _cfg_map(monkeypatch, {"vision_provider": "local"})
    model = tmp_path / "qwen3.5-4b-test.gguf"
    model.write_bytes(b"g")
    mmproj = tmp_path / "mmproj-F16.gguf"
    mmproj.write_bytes(b"m")
    assert backend.resolve_mmproj_for(str(model)) == str(mmproj)


def test_mmproj_unknown_model_fails_open_empty(monkeypatch, tmp_path):
    # No known repo, nothing on disk: vision quietly off, text server unharmed.
    _cfg_map(monkeypatch, {"vision_provider": "local"})
    model = tmp_path / "totally-custom.gguf"
    model.write_bytes(b"g")
    assert backend.resolve_mmproj_for(str(model)) == ""


def test_mmproj_explicit_ref_wins(monkeypatch, tmp_path):
    _cfg_map(monkeypatch, {"vision_provider": "local",
                           "vision_local_mmproj": "owner/repo/mmproj-custom.gguf"})
    model = tmp_path / "whatever.gguf"
    model.write_bytes(b"g")
    mmproj = tmp_path / "mmproj-custom.gguf"
    mmproj.write_bytes(b"m")
    assert backend.resolve_mmproj_for(str(model)) == str(mmproj)


def test_vision_local_mmproj_is_admin_only():
    from vaf.core.config import Config
    assert Config.is_global_config_key("vision_local_mmproj")
