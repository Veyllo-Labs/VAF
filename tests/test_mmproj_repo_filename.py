# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The projector filename is asked of the repo, never assumed.

The regression this pins: the auto path hardcoded "mmproj-F16.gguf" with the note
that Qwen and Gemma repos both ship it. Only Qwen does. Gemma's GGUF repos name the
projector after the model (mmproj-google_gemma-4-E4B-it-f16.gguf), so the download
raised EntryNotFoundError, the broad except swallowed it, and llama-server started
without --mmproj. The result was a server that runs perfectly and cannot see, while
Settings shows a configured vision model and `vision_available()` answers True. On
one machine that combination cost a day of looking in the wrong place.

Hermetic: the hub listing is stubbed, no network.
"""
import re
from pathlib import Path

import vaf.core.backend as backend

_BACKEND_SRC = Path(__file__).resolve().parent.parent / "vaf" / "core" / "backend.py"


class _FakeApi:
    def __init__(self, files):
        self._files = files

    def list_repo_files(self, repo_id):
        return list(self._files)


def _with_files(monkeypatch, files):
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda *a, **k: _FakeApi(files))


def test_gemma_style_repo_resolves_its_own_projector(monkeypatch):
    """The exact case that broke: the name carries the model, not the word F16."""
    _with_files(monkeypatch, [
        "google_gemma-4-E4B-it-Q4_K_M.gguf",
        "mmproj-google_gemma-4-E4B-it-bf16.gguf",
        "mmproj-google_gemma-4-E4B-it-f16.gguf",
        "README.md",
    ])
    assert backend._mmproj_filename_in_repo("bartowski/google_gemma-4-E4B-it-GGUF") == \
        "mmproj-google_gemma-4-E4B-it-f16.gguf"


def test_qwen_style_repo_still_resolves(monkeypatch):
    """The case that always worked must keep working."""
    _with_files(monkeypatch, ["Qwen3.5-9B-UD-Q8_K_XL.gguf", "mmproj-F16.gguf"])
    assert backend._mmproj_filename_in_repo("unsloth/Qwen3.5-9B-GGUF") == "mmproj-F16.gguf"


def test_bf16_is_not_mistaken_for_f16(monkeypatch):
    """`-f16` is a suffix of `-bf16`, so a naive match picks the wrong file. Only
    bf16 exists here, so bf16 is the honest answer; it must not be skipped either."""
    _with_files(monkeypatch, ["mmproj-model-bf16.gguf"])
    assert backend._mmproj_filename_in_repo("some/repo") == "mmproj-model-bf16.gguf"


def test_a_repo_without_a_projector_answers_empty(monkeypatch):
    """Fail-open: no projector must never look like a broken one."""
    _with_files(monkeypatch, ["model-Q4_K_M.gguf", "README.md"])
    assert backend._mmproj_filename_in_repo("text/only") == ""


def test_the_filename_is_never_hardcoded_again():
    """A static guard, because the defect was a literal and would return as one."""
    src = _BACKEND_SRC.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r'filename\s*=\s*"mmproj-[^"]*"', code), \
        "the projector filename is assigned a literal again instead of being read from the repo"
    assert "_mmproj_filename_in_repo(repo_id)" in code, \
        "the auto path no longer asks the repo for the projector name"
