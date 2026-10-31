# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""VAF's GPU hide for in-process PyTorch and its undo for the GPU engines it starts
(vaf/core/gpu_detection.py ``hide_gpu_from_inprocess_torch`` / ``gpu_child_env``).

The tray and the headless runner hide the GPU from torch in their own process; the vLLM
install and its probe, the vLLM server and the llama server must get it back, while a
``CUDA_VISIBLE_DEVICES`` the user set passes through untouched.

MUTATIONS, each turns the named test red:
- record every name regardless of prior presence (the user's "" is dropped) ->
  test_a_user_set_value_survives_the_hide_and_the_undo;
- ``gpu_child_env`` returns ``dict(os.environ)`` -> test_hide_records_and_child_env_undoes;
- restore the setdefault in tray.py -> test_entry_points_call_the_hide;
- revert backend.py's llama env to ``os.environ.copy()`` -> test_gpu_children_build_from_gpu_child_env.
"""
import os
import re
from pathlib import Path

import pytest

from vaf.core import gpu_detection as gd

_ROOT = Path(__file__).resolve().parents[1]
_NAMES = ("CUDA_VISIBLE_DEVICES", "PYTORCH_CUDA_ALLOC_CONF", gd.HIDE_RECORD)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_hide_records_and_child_env_undoes(clean_env):
    gd.hide_gpu_from_inprocess_torch()
    assert os.environ["CUDA_VISIBLE_DEVICES"] == ""
    assert os.environ["PYTORCH_CUDA_ALLOC_CONF"] == "max_split_size_mb:32"
    assert set(os.environ[gd.HIDE_RECORD].split(",")) == {"CUDA_VISIBLE_DEVICES",
                                                          "PYTORCH_CUDA_ALLOC_CONF"}
    gd.hide_gpu_from_inprocess_torch()      # idempotent (headless runner inside the tray)
    assert os.environ[gd.HIDE_RECORD].count("CUDA_VISIBLE_DEVICES") == 1
    clean_env.setenv("VAF_TEST_MARK", "kept")
    env = gd.gpu_child_env()
    assert not any(name in env for name in _NAMES), sorted(n for n in _NAMES if n in env)
    assert env["VAF_TEST_MARK"] == "kept" and env.get("PATH") == os.environ.get("PATH")
    # The process itself keeps the hide.
    assert os.environ["CUDA_VISIBLE_DEVICES"] == ""
    # An explicit base (a child of a hiding VAF process inherited the record) works the same.
    base = {"PATH": "/usr/bin", "CUDA_VISIBLE_DEVICES": "", gd.HIDE_RECORD: "CUDA_VISIBLE_DEVICES"}
    assert gd.gpu_child_env(base) == {"PATH": "/usr/bin"}


@pytest.mark.parametrize("value", ["0", ""])
def test_a_user_set_value_survives_the_hide_and_the_undo(clean_env, value):
    clean_env.setenv("CUDA_VISIBLE_DEVICES", value)
    gd.hide_gpu_from_inprocess_torch()
    assert os.environ["CUDA_VISIBLE_DEVICES"] == value, "the hide changed a user's value"
    assert os.environ[gd.HIDE_RECORD] == "PYTORCH_CUDA_ALLOC_CONF"
    env = gd.gpu_child_env()
    assert env["CUDA_VISIBLE_DEVICES"] == value, "the undo removed a user's value"
    assert "PYTORCH_CUDA_ALLOC_CONF" not in env and gd.HIDE_RECORD not in env


def test_no_record_without_a_hide(clean_env):
    clean_env.setenv("CUDA_VISIBLE_DEVICES", "1")
    clean_env.setenv("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    gd.hide_gpu_from_inprocess_torch()
    assert gd.HIDE_RECORD not in os.environ
    env = gd.gpu_child_env()
    assert env["CUDA_VISIBLE_DEVICES"] == "1"
    assert env["PYTORCH_CUDA_ALLOC_CONF"] == "expandable_segments:True"


def _src(rel: str) -> str:
    return (_ROOT / rel).read_bytes().decode("utf-8")


@pytest.mark.parametrize("rel", ["vaf/tray.py", "vaf/core/headless_runner.py"])
def test_entry_points_call_the_hide(rel):
    """The hide runs before any other vaf or torch import that could load torch."""
    src = _src(rel)
    assert 'setdefault("CUDA_VISIBLE_DEVICES"' not in src
    call = re.search(r"^(_hide_gpu|hide_gpu_from_inprocess_torch)\(\)$", src, re.M)
    assert call, "the hide is not called at module level"
    head = src[:call.start()]
    imports = [line for line in head.splitlines()
               if re.match(r"(from|import) ", line) and ("vaf" in line or "torch" in line)]
    assert imports == [line for line in imports if "gpu_detection" in line], imports


def test_gpu_children_build_from_gpu_child_env():
    backend = _src("vaf/core/backend.py")
    llama = backend[backend.index('run_env["LLAMA_ARG_N_PARALLEL"]') - 400:
                    backend.index('run_env["LLAMA_ARG_N_PARALLEL"]')]
    assert "run_env = gpu_child_env()" in llama
    assert "run_env = os.environ.copy()" not in backend
    vllm = _src("vaf/core/vllm_server.py")
    install = vllm[vllm.index("def install("):vllm.index("def _install_steps(")]
    assert "env = gpu_child_env()" in install and "dict(os.environ)" not in install
    child = vllm[vllm.index("def child_env("):vllm.index("def _looks_like_vllm_server(")]
    assert "gpu_child_env(base)" in child and "os.environ" not in child
