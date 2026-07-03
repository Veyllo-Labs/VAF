# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Apple Silicon `model: "auto"` selection (unified-memory GPU budget).

Regression background: detect_apple_silicon() hardcoded vram_mb=0 and was missing
from _detect_vram_gb()'s chain, so EVERY Apple Silicon Mac read as "no GPU" and
auto-selection always picked the smallest 4B/Q4 model regardless of 16/32/128 GB.

The budget is 65% of hw.memsize CAPPED AT RAM MINUS 6 GB: on unified memory the
"VRAM" and the RAM macOS/Colima/backend/tray live in are the same bytes. Without
the cap a 16 GB Mac (most common config) reported 10.4 GB, crossed the 4B->9B
threshold by 0.4 GB and over-committed itself into swap churn.

Hermetic: platform/sysctl faked; recommended_default_model is a pure function.
"""
from types import SimpleNamespace

import pytest

import vaf.core.gpu_detection as gpu
from vaf.core.gpu_detection import recommended_default_model


GiB = 1024 ** 3


def _apple_env(monkeypatch, memsize_bytes, sysctl_fails=False):
    monkeypatch.setattr(
        gpu, "platform",
        SimpleNamespace(system=lambda: "Darwin", machine=lambda: "arm64"),
    )

    class FakeSub:
        @staticmethod
        def check_output(cmd, timeout=None):
            if sysctl_fails:
                raise FileNotFoundError("sysctl")
            assert cmd == ["sysctl", "-n", "hw.memsize"]
            return str(memsize_bytes).encode()

    monkeypatch.setattr(gpu, "subprocess", FakeSub)


@pytest.mark.parametrize("ram_gb,expected_mb", [
    # 65% binds from ~17.1 GB up; below that the RAM-6GB host reserve binds.
    (16, 10240),   # 16 GB Mac: reserve binds -> exactly 10.0 GB -> stays in the 4B tier
    (32, 21299),   # 32 GB Mac: 65% binds -> 20.8 GB (the end-to-end validated figure)
    (8, 2048),     # 8 GB Mac: reserve binds -> 2 GB -> smallest 4B quant (as before the fix)
    (64, 42598),   # 64 GB Mac: 65% binds
])
def test_apple_budget_math(monkeypatch, ram_gb, expected_mb):
    _apple_env(monkeypatch, ram_gb * GiB)
    info = gpu.detect_apple_silicon()
    assert info is not None and info.vendor == "apple"
    assert info.vram_mb == expected_mb


def test_apple_budget_sysctl_failure_reports_zero(monkeypatch):
    """sysctl failure must fall back to 0 exactly like the pre-fix behavior."""
    _apple_env(monkeypatch, 32 * GiB, sysctl_fails=True)
    info = gpu.detect_apple_silicon()
    assert info is not None
    assert info.vram_mb == 0


def test_not_darwin_returns_none(monkeypatch):
    monkeypatch.setattr(
        gpu, "platform",
        SimpleNamespace(system=lambda: "Linux", machine=lambda: "x86_64"),
    )
    assert gpu.detect_apple_silicon() is None


def test_detect_vram_chain_includes_apple(monkeypatch):
    """The original bug: _detect_vram_gb never asked detect_apple_silicon."""
    monkeypatch.setattr(gpu, "detect_nvidia_gpu", lambda: None)
    monkeypatch.setattr(gpu, "detect_amd_gpu", lambda: None)
    monkeypatch.setattr(
        gpu, "detect_apple_silicon",
        lambda: gpu.GPUInfo(vendor="apple", model="Apple Silicon", vram_mb=10240),
    )
    assert gpu._detect_vram_gb() == pytest.approx(10.0)


@pytest.mark.parametrize("budget_gb,expect_model,expect_quant", [
    (10.0, "4B", "UD-Q8_K_XL"),   # 16 GB Mac post-fix: largest 4B quant, NOT the 9B
    (20.8, "9B", "UD-Q8_K_XL"),   # 32 GB Mac: the contributor-validated selection
    (2.0, "4B", "Q4_K_M"),        # 8 GB Mac: smallest quant
    (11.7, "9B", "Q5_K_M"),       # 18 GB Mac
    (15.6, "9B", "Q6_K"),         # 24 GB Mac
    (31.2, "9B", "BF16"),         # 48 GB Mac
    (0.0, "4B", "Q4_K_M"),        # no GPU detectable: unchanged smallest default
])
def test_model_tiers_for_real_mac_sizes(budget_gb, expect_model, expect_quant):
    chosen = recommended_default_model(vram_gb=budget_gb)
    assert expect_model in chosen, chosen
    assert expect_quant in chosen, chosen
