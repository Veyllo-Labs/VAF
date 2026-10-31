# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The hash-locked tree of the managed vLLM environment (vaf/core/vllm_requirements.lock).

The install runs ``pip install --require-hashes --no-deps --only-binary=:all: -r <lock>``, so
the lock IS the environment: every package pinned, every file hashed, and the vllm pin the
one the code expects. Whether the lock is true for every supported target is checked against
PyPI by scripts/check_vllm_lock.py (network); this guard is the offline half.

MUTATIONS, each turns the named test red:
- change the vllm pin in the lock -> test_the_lock_pins_the_codes_vllm;
- delete one --hash line of a pinned entry, or add an unpinned line ->
  test_every_entry_is_pinned_and_hashed;
- drop the numpy cap from the input -> test_the_input_carries_the_numpy_cap.
"""
import re
from pathlib import Path

from vaf.core import vllm_server as vs

_ROOT = Path(__file__).resolve().parents[1]
_IN = _ROOT / "vaf" / "core" / "vllm_requirements.in"
_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s\\]+) \\$")
_HASH = re.compile(r"^    --hash=sha256:[0-9a-f]{64}( \\)?$")


def _lock_lines():
    return vs.LOCK_PATH.read_bytes().decode("utf-8").splitlines()


def _entries():
    """[(name, version, [hash lines])] in file order."""
    out = []
    for line in _lock_lines():
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


def test_every_entry_is_pinned_and_hashed():
    """Every line is a comment, a pinned entry, a hash of one or blank; every entry carries a
    hash (pip's --require-hashes refuses the whole install otherwise)."""
    for no, line in enumerate(_lock_lines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        assert _ENTRY.match(line) or _HASH.match(line), (no, line)
    entries = _entries()
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
