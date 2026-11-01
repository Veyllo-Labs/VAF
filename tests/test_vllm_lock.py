# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The hash-locked tree of the managed vLLM environment (vaf/core/vllm_requirements.lock).

The install runs ``pip install --require-hashes --no-deps --only-binary=:all: -r <lock>``, so
the lock IS the environment: every package pinned, every file hashed, and the vllm pin the
one the code expects. Whether the lock is true for every supported target is checked against
PyPI by scripts/check_vllm_lock.py (network); this guard is the offline half.

The ROCm build has its own lock (vaf/core/vllm_requirements_rocm.lock, generated with uv
for cp312 x86_64 manylinux_2_39), installed with vLLM's ROCm index as an extra index on the
command line; every guard of the grammar runs on both.

MUTATIONS, each turns the named test red:
- change the vllm pin in the lock -> test_the_lock_pins_the_codes_vllm;
- change the ROCm pin, or the index path -> test_the_rocm_lock_pins_the_codes_vllm;
- add `--extra-index-url x` to a lock -> test_no_index_option_inside_a_lock;
- delete one --hash line of a pinned entry, or add an unpinned line ->
  test_every_entry_is_pinned_and_hashed;
- check the ROCm lock at the CUDA targets -> test_the_checker_reads_the_rocm_target;
- drop the numpy cap from the input -> test_the_input_carries_the_numpy_cap;
- let a wheel above the target's glibc floor, a musllinux or a bare linux wheel count, or
  check every target at one floor -> test_the_checker_counts_only_installable_wheels.
"""
import re
from pathlib import Path

import pytest

from vaf.core import vllm_server as vs

_ROOT = Path(__file__).resolve().parents[1]
_IN = _ROOT / "vaf" / "core" / "vllm_requirements.in"
_IN_ROCM = _ROOT / "vaf" / "core" / "vllm_requirements_rocm.in"
_LOCKS = pytest.mark.parametrize("lock", [vs.LOCK_PATH, vs.LOCK_PATH_ROCM],
                                 ids=["cuda", "rocm"])
_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s\\]+) \\$")
_HASH = re.compile(r"^    --hash=sha256:[0-9a-f]{64}( \\)?$")


def _lock_lines(lock=None):
    return (lock or vs.LOCK_PATH).read_bytes().decode("utf-8").splitlines()


def _entries(lock=None):
    """[(name, version, [hash lines])] in file order."""
    out = []
    for line in _lock_lines(lock):
        m = _ENTRY.match(line)
        if m:
            out.append((m.group(1).lower(), m.group(3), []))
        elif line.startswith("    --hash=") and out:
            out[-1][2].append(line)
    return out


def test_the_lock_pins_the_codes_vllm():
    pins = {name: version for name, version, _ in _entries()}
    assert pins.get("vllm") == vs.PIN, "the lock's vllm is not vllm_server.PIN; regenerate it"
    assert pins.get("torch"), "the lock has no torch"
    infile = _IN.read_bytes().decode("utf-8").splitlines()
    assert f"vllm=={vs.PIN}" in infile, "vllm_requirements.in does not pin vllm_server.PIN"
    head = "\n".join(_lock_lines()[:8])
    assert "--generate-hashes" in head and "vaf/core/vllm_requirements.in" in head


@_LOCKS
def test_every_entry_is_pinned_and_hashed(lock):
    """Every line is a comment, a pinned entry, a hash of one or blank; every entry carries a
    hash (pip's --require-hashes refuses the whole install otherwise)."""
    for no, line in enumerate(_lock_lines(lock), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        assert _ENTRY.match(line) or _HASH.match(line), (no, line)
    entries = _entries(lock)
    assert len(entries) > 150, len(entries)
    unhashed = [f"{n}=={v}" for n, v, hashes in entries if not hashes]
    assert not unhashed, unhashed
    # A continuation backslash on an entry's last hash would swallow the next entry.
    for name, version, hashes in entries:
        assert hashes[-1].endswith(" \\") is False, f"{name}=={version}"


def test_the_input_carries_the_numpy_cap():
    """A resolution on Python 3.13 alone picked numpy 2.4, which mistral-common refuses on 3.11
    and 3.12 (measured with scripts/check_vllm_lock.py): the cap keeps one lock true."""
    infile = _IN.read_bytes().decode("utf-8").splitlines()
    assert "numpy<2.4" in infile
    numpy = {name: version for name, version, _ in _entries()}["numpy"]
    assert tuple(int(p) for p in numpy.split(".")[:2]) < (2, 4), numpy


def _checker():
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_vllm_lock",
                                                  _ROOT / "scripts" / "check_vllm_lock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_checker_counts_only_installable_wheels():
    """The release check counts a wheel for a target only when pip would install it there:
    the right processor, Python, and a manylinux tag at or below the target's glibc floor
    (local_engine.MIN_GLIBC, measured per arch)."""
    from vaf.core import local_engine as le
    check = _checker()
    x86, arm = le.MIN_GLIBC["x86_64"], le.MIN_GLIBC["aarch64"]
    assert (x86, arm) == ((2, 31), (2, 34))
    llg = "llguidance-1.7.6-cp39-abi3-manylinux_2_31_x86_64.whl"
    assert check._fits(llg, "x86_64", 3, 13, x86)
    assert not check._fits(llg, "x86_64", 3, 13, (2, 28)), "a wheel above the floor counted"
    assert not check._fits(llg, "aarch64", 3, 13, arm), "another processor's wheel counted"
    legacy = "z3_solver-4.15.4.0-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
    assert check._fits(legacy, "x86_64", 3, 11, x86)
    assert not check._fits("z3_solver-4.15.4.0-py3-none-manylinux_2_34_aarch64.whl",
                           "aarch64", 3, 13, (2, 33))
    assert not check._fits("x-1.0-cp313-cp313-musllinux_1_2_x86_64.whl", "x86_64", 3, 13, x86)
    assert not check._fits("x-1.0-cp313-cp313-linux_x86_64.whl", "x86_64", 3, 13, x86)
    assert not check._fits("x-1.0-cp312-cp312-manylinux_2_28_x86_64.whl", "x86_64", 3, 13, x86)
    assert check._fits("x-1.0-py3-none-any.whl", "aarch64", 3, 14, arm)
    targets = check._targets()
    assert {(a, g) for a, _, _, g in targets} == {("x86_64", x86), ("aarch64", arm)}
    assert len(targets) == 8


_INDEX_OPTION = re.compile(r"^\s*(-i|--index-url|--extra-index-url|-f|--find-links|--trusted-host"
                           r"|--no-index)\b")


@_LOCKS
def test_no_index_option_inside_a_lock(lock):
    """pip honours index options inside a requirements file: one in a lock would move the
    install to another index without a change of the code. The ROCm index is a code constant
    on pip's command line (vllm_server.ROCM_INDEX)."""
    for no, line in enumerate(_lock_lines(lock), start=1):
        if line.lstrip().startswith("#"):
            continue
        assert not _INDEX_OPTION.match(line), (lock.name, no, line)


def test_the_rocm_lock_pins_the_codes_vllm():
    pins = {name: version for name, version, _ in _entries(vs.LOCK_PATH_ROCM)}
    assert pins.get("vllm") == f"{vs.PIN}+{vs.ROCM_VARIANT}" == vs.expected_version(vs.ROCM), \
        "the ROCm lock's vllm is not PIN+ROCM_VARIANT; regenerate it"
    # vllm's METADATA pins its ROCm torch to the build of the same index.
    assert pins.get("torch", "").startswith("2.13.0+"), pins.get("torch")
    infile = _IN_ROCM.read_bytes().decode("utf-8")
    assert f"vllm=={vs.PIN}+{vs.ROCM_VARIANT}" in infile.splitlines()
    assert vs.ROCM_INDEX == f"https://wheels.vllm.ai/rocm/{vs.PIN}/{vs.ROCM_VARIANT}"
    assert vs.ROCM_INDEX in infile, "the generation command names another index"
    head = "\n".join(_lock_lines(vs.LOCK_PATH_ROCM)[:3])
    assert "uv pip compile vaf/core/vllm_requirements_rocm.in --generate-hashes" in head
    assert "--python-version 3.12 --python-platform x86_64-manylinux_2_39" in head
    # The header names where the ROCm packages come from (uv leaves the index out unless
    # --custom-compile-command names the whole command).
    assert f"--extra-index-url {vs.ROCM_INDEX} --no-build" in head


def test_the_checker_reads_the_rocm_target():
    """The ROCm lock is checked at its one target: x86_64, Python 3.12, glibc 2.39."""
    from vaf.core import local_engine as le
    check = _checker()
    assert check._targets(rocm=True) == [("x86_64", 3, 12, (2, 39))]
    assert le.MIN_GLIBC_ROCM == (2, 39) and le.ROCM_PYTHON == (3, 12)
    assert check.is_rocm([], vs.LOCK_PATH_ROCM) and not check.is_rocm([], vs.LOCK_PATH)
    assert check.is_rocm(["--rocm"], Path("x.lock"))
    _, _, _, glibc = check._targets(rocm=True)[0]
    wheel = "vllm-0.32.0+rocm723-cp312-cp312-manylinux_2_39_x86_64.whl"
    assert check._fits(wheel, "x86_64", 3, 12, glibc)
    assert not check._fits(wheel.replace("cp312", "cp313"), "x86_64", 3, 12, glibc)
    assert not check._fits(wheel, "x86_64", 3, 12, (2, 35)), "a 2.39 wheel at a 2.35 floor"
