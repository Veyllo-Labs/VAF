# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Tests for the shared, locked, self-healing model-ensure (vaf.core.backend.ensure_model_available).

These cover the resolution + download decisions only; hf_hub_download is mocked so no network or GPU is
needed. The real bug this guards against: an empty models/ dir launching llama-server against a missing
file, and a bare config filename resolving to a non-existent "Veyllo/..." repo.
"""

from pathlib import Path
from unittest import mock

from vaf.core import backend


def _fake_download(repo_id, filename, local_dir, **kwargs):
    """Stand-in for hf_hub_download: 'fetch' the file by writing a stub into local_dir/filename."""
    p = Path(local_dir) / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"GGUF\x00stub")
    return str(p)


def test_resolve_known_bare_name_maps_to_repo():
    assert backend._resolve_model_ref("Qwen3.5-4B-UD-Q8_K_XL.gguf") == (
        "unsloth/Qwen3.5-4B-GGUF",
        "Qwen3.5-4B-UD-Q8_K_XL.gguf",
    )
    assert backend._resolve_model_ref("Qwen3.5-9B-Q6_K.gguf")[0] == "unsloth/Qwen3.5-9B-GGUF"
    # Unknown bare name -> no repo (caller must self-heal).
    assert backend._resolve_model_ref("totally-unknown.gguf") == (None, "totally-unknown.gguf")
    # Full repo/file splits correctly.
    assert backend._resolve_model_ref("owner/repo/file.gguf") == ("owner/repo", "file.gguf")


def test_present_model_returns_without_download(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    (models / "Qwen3.5-4B-Q6_K.gguf").write_bytes(b"GGUF")
    with mock.patch("huggingface_hub.hf_hub_download") as m:
        path = backend.ensure_model_available("Qwen3.5-4B-Q6_K.gguf", models)
    assert Path(path) == models / "Qwen3.5-4B-Q6_K.gguf"
    m.assert_not_called()


def test_known_bare_name_downloads_from_correct_repo(tmp_path):
    models = tmp_path / "models"
    with mock.patch("huggingface_hub.hf_hub_download", side_effect=_fake_download) as m:
        path = backend.ensure_model_available("Qwen3.5-4B-UD-Q8_K_XL.gguf", models)
    assert Path(path).name == "Qwen3.5-4B-UD-Q8_K_XL.gguf"
    assert Path(path).is_file()
    # The bare name must resolve to the real unsloth repo, NOT a bogus "Veyllo/..." one.
    assert m.call_args.kwargs["repo_id"] == "unsloth/Qwen3.5-4B-GGUF"


def test_unknown_bare_name_self_heals_to_vram_default(tmp_path):
    models = tmp_path / "models"
    with mock.patch("huggingface_hub.hf_hub_download", side_effect=_fake_download) as m, mock.patch(
        "vaf.core.gpu_detection.recommended_default_model",
        return_value="unsloth/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q4_K_M.gguf",
    ):
        path = backend.ensure_model_available("totally-unknown-model.gguf", models)
    # An unresolvable configured model falls back to a model that fits the GPU's VRAM.
    assert Path(path).name == "Qwen3.5-4B-Q4_K_M.gguf"
    assert m.call_args.kwargs["repo_id"] == "unsloth/Qwen3.5-4B-GGUF"


def test_idempotent_second_call_no_redownload(tmp_path):
    models = tmp_path / "models"
    with mock.patch("huggingface_hub.hf_hub_download", side_effect=_fake_download) as m:
        backend.ensure_model_available("Qwen3.5-9B-Q6_K.gguf", models)
        backend.ensure_model_available("Qwen3.5-9B-Q6_K.gguf", models)
    assert m.call_count == 1  # second call finds the file already present
