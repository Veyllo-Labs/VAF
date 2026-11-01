# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""vLLM's GPU memory share: the desktop reserve of a GPU that drives a display, the floor to
0.01, the share set by hand, the refusal that names the reserve, the display readers and the
slider's share table (vaf/core/vllm_server.py, vaf/core/gpu_detection.py).

Nothing here starts nvidia-smi or vLLM: the readers get a faked ``subprocess.run`` or a sysfs
tree under tmp_path, and every other test pins the display state (this machine drives a
display, CI runners do not).

The numbers are the measured case: an RTX 3080 (10240 MiB) with 8500 MiB free and the cached
Qwen3-4B-AWQ config (weights 2542 MiB, 36 layers, 8 KV heads, head dim 128, 40960 positions),
int8 KV cache.

MUTATIONS, each turns the named test red:
- drop the reserve cap line in ``auto_share`` -> test_reserve_caps_the_automatic_share;
- ``round`` instead of the floor, or drop the epsilon -> test_the_share_is_floored;
- cap a share set by hand with the reserve -> test_a_share_by_hand_is_never_capped;
- return ``too_big`` for every refusal -> test_a_model_that_fits_only_without_the_reserve;
- drop the f prefix of a segment of the reserve refusal ->
  test_the_reserve_refusal_reads_its_numbers;
- treat ``Disabled`` as True, or None as no reserve -> test_nvidia_display_reader /
  test_desktop_reserve_follows_the_display;
- read a connector of another card, or count ``disconnected`` -> test_amd_display_reader;
- drop the last step of the table, or read the free memory while a server runs ->
  test_share_table.
"""
import subprocess
from types import SimpleNamespace

import pytest

from vaf.core import gpu_detection as gd
from vaf.core import vllm_server as vs

MIB = 1024 ** 2
SHAPE_4B = vs.ModelShape(weights_bytes=2542 * MIB, layers=36, kv_heads=8, head_dim=128,
                         max_position=40960)
SHAPE_8B = vs.ModelShape(weights_bytes=int(6.10e9), layers=36, kv_heads=8, head_dim=128,
                         max_position=40960)
SMALL = "Qwen/Qwen3-4B-AWQ"
LARGE = "Qwen/Qwen3-8B-AWQ"


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    """The NVIDIA build, the int8 plan of compute capability 8.6 and no display, unless a
    test says otherwise."""
    monkeypatch.setattr(vs, "backend", lambda: vs.CUDA)
    monkeypatch.setattr(vs, "compute_capability", lambda: (8, 6))
    monkeypatch.setattr(vs, "flashinfer_buildable", lambda env=None: False)
    monkeypatch.setattr(vs, "_display_active", lambda: False)


def test_reserve_caps_the_automatic_share():
    """With a display the automatic share on the measured card is 0.67 with context 32768
    (6813 of 6860 MiB), inside the band that kept the desktop alive; without one it stays
    0.78 with 40960, the share of the frozen desktop."""
    r = vs.fit(SHAPE_4B, 40960, 8500, 10240, kv_dtype=vs.INT8_KV, reserve_mb=vs.DESKTOP_RESERVE_MB)
    assert (r.ok, r.utilization, r.max_model_len, r.budget_mb) == (True, 0.67, 32768, 6860), r
    assert r.reserve_mb == vs.DESKTOP_RESERVE_MB and r.leaves_mb == 3380
    r = vs.fit(SHAPE_4B, 40960, 8500, 10240, kv_dtype=vs.INT8_KV)
    assert (r.ok, r.utilization, r.max_model_len, r.budget_mb) == (True, 0.78, 40960, 7987), r
    assert r.reserve_mb == 0
    assert vs.DESKTOP_RESERVE_MB == 3328


def test_the_share_is_floored():
    """The launch argument is the share the fit checked: a cap of exactly 0.675 is 0.67, not
    the 0.68 the two-decimal format would print, and 0.57 stays 0.57."""
    r = vs.fit(SHAPE_4B, 40960, 8500, 10240, kv_dtype=vs.INT8_KV, reserve_mb=3328)
    assert (10240 - 3328) / 10240 == 0.675
    assert r.utilization == 0.67
    argv = vs.build_command("/m", SMALL, 8080, r, "hermes", "qwen3")
    assert argv[argv.index("--gpu-memory-utilization") + 1] == "0.67"
    assert vs.auto_share(6212, 10000) == 0.57
    assert vs.floor_share(0.57) == 0.57 and vs.floor_share(0.579) == 0.57


def test_a_share_by_hand_is_never_capped():
    """A share set by hand is the override: the reserve never lowers it, but it says what is
    left and that it is below the reserve."""
    r = vs.fit(SHAPE_4B, 40960, 8500, 10240, kv_dtype=vs.INT8_KV, utilization=0.78,
               reserve_mb=3328)
    assert (r.utilization, r.below_reserve, r.leaves_mb, r.reserve_mb) == (0.78, True, 2253, 0)
    r = vs.fit(SHAPE_4B, 40960, 8500, 10240, kv_dtype=vs.INT8_KV, utilization=0.6, reserve_mb=3328)
    assert (r.utilization, r.below_reserve) == (0.6, False)
    r = vs.fit(SHAPE_4B, 40960, 8500, 10240, kv_dtype=vs.INT8_KV, utilization=0.78)
    assert r.below_reserve is False, "no display, nothing to keep for a screen"


def test_a_model_that_fits_only_without_the_reserve():
    """Refused with its own code, the share that fits and the way to set it; the 8B on a
    12 GB desktop names the smaller model too. A model that does not fit even without the
    reserve keeps ``too_big``."""
    r = vs.fit(SHAPE_4B, 40960, 7500, 8192, kv_dtype=vs.INT8_KV, reserve_mb=3328, model=SMALL)
    assert (r.ok, r.code, r.min_share, r.smaller_model) == (False, "too_big_reserve", 0.84, False)
    assert int(r.min_share * 8192) >= r.need_mb > int(0.83 * 8192)
    assert "--gpu-share 0.84" in r.reason and "Settings > AI & Model" in r.reason
    assert "freeze" in r.reason and "stays as it was" in r.reason
    assert "smaller model" not in r.reason
    r = vs.fit(SHAPE_8B, 40960, 11500, 12288, kv_dtype=vs.INT8_KV, reserve_mb=3328, model=LARGE)
    assert (r.code, r.smaller_model, r.budget_mb) == ("too_big_reserve", True, 8847), r
    assert "smaller model" in r.reason
    # Desktop programs hold 4018 MiB: no cap from the reserve, the plain refusal.
    r = vs.fit(SHAPE_4B, 40960, 6222, 10240, kv_dtype=vs.INT8_KV, reserve_mb=3328, model=SMALL)
    assert (r.code, r.reserve_mb) == ("too_big", 0)
    # Too big even with the whole automatic share.
    r = vs.fit(SHAPE_8B, 40960, 7000, 8192, kv_dtype=vs.INT8_KV, reserve_mb=3328, model=LARGE)
    assert r.code == "too_big"
    assert "too_big_reserve" in vs.FIT_CODES


def test_the_reserve_refusal_reads_its_numbers():
    """The English refusal is built from f-string segments: every placeholder is filled (the
    smallest share both in the sentence and in the command), and it says what the reserve is,
    room on the card outside vLLM, not memory that stays free. MUTATION: drop the f prefix of
    the "at least" segment - red."""
    r = vs.fit(SHAPE_4B, 40960, 7500, 8192, kv_dtype=vs.INT8_KV, reserve_mb=3328, model=SMALL)
    assert r.code == "too_big_reserve"
    least = f"{r.min_share:.2f}"
    assert f"at least {least} (`vaf engine vllm --gpu-share {least}`" in r.reason
    assert "{" not in r.reason and "}" not in r.reason, r.reason
    assert "3328 MiB of the card outside vLLM for the screen" in r.reason
    assert "stay free" not in r.reason


def _run(stdout, returncode=0):
    def run(argv, **kw):
        assert argv[:2] == ["nvidia-smi", "--query-gpu=display_active"], argv
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return run


@pytest.mark.parametrize("stdout, returncode, expected", [
    ("Enabled\n", 0, True), ("Disabled\n", 0, False), ("Disabled\nEnabled\n", 0, False),
    ("[N/A]\n", 0, None), ("", 0, None), ("Enabled\n", 9, None)])
def test_nvidia_display_reader(monkeypatch, stdout, returncode, expected):
    """The first GPU's ``display_active``: Enabled, Disabled, anything else unknown."""
    monkeypatch.setattr(subprocess, "run", _run(stdout, returncode))
    assert gd.nvidia_display_active() is expected


def test_nvidia_display_reader_without_nvidia_smi(monkeypatch):
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(subprocess, "run", missing)
    assert gd.nvidia_display_active() is None


def _amd_tree(root, connectors, minor=129, other_card=None):
    """A KFD node (an RX 7900) with render minor ``minor`` and its DRM connectors."""
    node = root / "class" / "kfd" / "kfd" / "topology" / "nodes" / "1"
    node.mkdir(parents=True)
    (node / "properties").write_bytes(
        f"simd_count 192\nvendor_id 4098\ndrm_render_minor {minor}\n"
        "gfx_target_version 110000\n".encode())
    device = root / "class" / "drm" / f"renderD{minor}" / "device"
    device.mkdir(parents=True)
    (device / "mem_info_vram_total").write_bytes(str(24560 * MIB).encode())
    (device / "mem_info_vram_used").write_bytes(str(1024 * MIB).encode())
    for name, status, enabled in connectors:
        c = device / "drm" / "card1" / f"card1-{name}"
        c.mkdir(parents=True)
        (c / "status").write_bytes(f"{status}\n".encode())
        (c / "enabled").write_bytes(f"{enabled}\n".encode())
    if other_card:
        # The connector of another GPU, under ITS render device: never read for this one.
        c = root / "class" / "drm" / "renderD128" / "device" / "drm" / "card0" / "card0-DP-1"
        c.mkdir(parents=True)
        (c / "status").write_bytes(b"connected\n")
        (c / "enabled").write_bytes(b"enabled\n")


def test_amd_display_reader(tmp_path):
    """The connectors of the KFD node's render device: one connected or enabled is a display,
    connectors that are all disconnected and disabled are none, no connectors is unknown."""
    a = tmp_path / "a"
    _amd_tree(a, [("DP-1", "disconnected", "disabled"), ("HDMI-A-1", "connected", "enabled")])
    assert gd.amd_display_active(str(a)) is True
    b = tmp_path / "b"
    _amd_tree(b, [("DP-1", "disconnected", "disabled"), ("DP-2", "disconnected", "disabled")],
              other_card=True)
    assert gd.amd_display_active(str(b)) is False, "another card's screen is not this GPU's"
    c = tmp_path / "c"
    _amd_tree(c, [])
    assert gd.amd_display_active(str(c)) is None
    assert gd.amd_display_active(str(tmp_path / "none")) is None


@pytest.mark.parametrize("display, reserve", [(True, 3328), (False, 0), (None, 3328)])
def test_desktop_reserve_follows_the_display(monkeypatch, display, reserve):
    """A reserve too many costs context, which the slider gives back; one too few freezes
    the desktop: unknown keeps the reserve."""
    monkeypatch.setattr(vs, "_display_active", lambda: display)
    assert vs.desktop_reserve_mb() == reserve
    assert vs.desktop_reserve_mb(display) == reserve


def test_the_display_reader_follows_the_build(monkeypatch):
    """ROCm reads the DRM connectors, CUDA asks nvidia-smi."""
    monkeypatch.undo()
    monkeypatch.setattr(gd, "nvidia_display_active", lambda: True)
    monkeypatch.setattr(gd, "amd_display_active", lambda sys_root="/sys": False)
    monkeypatch.setattr(vs, "backend", lambda: vs.CUDA)
    assert vs._display_active() is True
    monkeypatch.setattr(vs, "backend", lambda: vs.ROCM)
    assert vs._display_active() is False


def test_plan_fit_applies_the_reserve_of_the_display(monkeypatch, tmp_path):
    """The start's fit: the reserve with a display (and the display state on the result), the
    configured share by hand, and an override that beats the configured one."""
    monkeypatch.setattr(vs.ModelShape, "from_dir", classmethod(lambda cls, d: SHAPE_4B))
    monkeypatch.setattr(vs, "_gpu_memory", lambda: (8500, 10240))
    monkeypatch.setattr(vs, "_configured_utilization", lambda: 0.0)
    monkeypatch.setattr(vs, "_display_active", lambda: True)
    r = vs.plan_fit(str(tmp_path), 40960)
    assert (r.utilization, r.max_model_len, r.display, r.kv_dtype) == (0.67, 32768, True, vs.INT8_KV)
    monkeypatch.setattr(vs, "_display_active", lambda: False)
    assert vs.plan_fit(str(tmp_path), 40960).utilization == 0.78
    monkeypatch.setattr(vs, "_configured_utilization", lambda: 0.6)
    assert vs.plan_fit(str(tmp_path), 40960).utilization == 0.6
    assert vs.plan_fit(str(tmp_path), 40960, utilization=0.7).utilization == 0.7
    assert vs.plan_fit(str(tmp_path), 40960, utilization=0).utilization == 0.78, \
        "0 is automatic, not the configured share"


def test_share_table(monkeypatch, tmp_path):
    """66 fits from 0.30 to 0.95 for a downloaded model, the smallest share that fits, the
    automatic share now with the reserve; none without a model, and no free memory while a
    local server holds its own."""
    monkeypatch.setattr(vs.ModelShape, "from_dir",
                        classmethod(lambda cls, d: SHAPE_4B if d else None))
    monkeypatch.setattr(vs, "_gpu_memory", lambda: (8500, 10240))
    monkeypatch.setattr(vs, "_configured_utilization", lambda: 0.0)
    monkeypatch.setattr(vs, "_display_active", lambda: True)
    t = vs.share_table(str(tmp_path), 40960, model=SMALL)
    steps = t["steps"]
    assert len(steps) == 66 and steps[0]["share"] == 0.30 and steps[-1]["share"] == 0.95
    assert [round(s["share"] * 100) for s in steps] == list(range(30, 96))
    at = {s["share"]: s for s in steps}
    assert at[0.66]["ok"] is False and at[0.67]["ok"] is True and at[0.67]["max_model_len"] == 32768
    assert at[0.78]["max_model_len"] == 40960 and at[0.67]["budget_mb"] == 6860
    assert t["min_share"] == 0.67
    assert (t["total_mb"], t["free_mb"], t["display"], t["reserve_mb"]) == (10240, 8500, True, 3328)
    assert (t["auto_share"], t["auto_reserved"], t["configured"]) == (0.67, True, 0.0)
    assert t["range"] == [0.30, 0.95] and t["kv_dtype"] == vs.INT8_KV
    # Beyond the free memory still has a step: the slider says it, the start refuses it.
    assert at[0.95]["ok"] is True and at[0.95]["budget_mb"] > 8500
    running = vs.share_table(str(tmp_path), 40960, model=SMALL, local_server_running=True)
    assert running["free_mb"] is None and running["auto_share"] is None
    assert len(running["steps"]) == 66, "the fits do not need the free figure"
    empty = vs.share_table(None, 40960, model=SMALL)
    assert empty["steps"] == [] and empty["min_share"] is None and empty["total_mb"] == 10240
    monkeypatch.setattr(vs, "_display_active", lambda: False)
    t = vs.share_table(str(tmp_path), 40960, model=SMALL)
    assert (t["reserve_mb"], t["auto_share"], t["auto_reserved"]) == (0, 0.78, False)
