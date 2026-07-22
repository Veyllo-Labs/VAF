# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Installer Python-version gate: keep it in lockstep with the CI matrix.

Incident: the installers accepted any Python >= 3.10 (no upper bound), so a Windows machine
whose newest Python was 3.14 built the venv with an unsupported interpreter and the dependency
install crashed compiling packages without cp314 wheels (pyaudio -> missing portaudio.h), with
a misleading "network hiccup" message. The fix caps the accepted range at MAX_PYTHON_VERSION
and falls through to uv (which provisions a supported interpreter) for anything outside it.

These guards pin three things:
  1. install.ps1 and install.sh declare the SAME MIN/MAX supported range.
  2. That range equals exactly the CI matrix (ci.yml + ci-nightly.yml) - the tested versions
     ARE the supported versions; whoever bumps the matrix must bump the installers (and vice
     versa) in the same change.
  3. pyaudio stays OUT of the core requirements.txt (it is the optional vaf[speech] extra);
     its no-wheel-for-new-Python source build is what broke the install in the first place.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _minor(v: str) -> tuple[int, int]:
    major, minor = v.split(".")[:2]
    return int(major), int(minor)


def _ps1_range() -> tuple[str, str]:
    src = (ROOT / "install.ps1").read_text(encoding="utf-8")
    lo = re.search(r'\$MIN_PYTHON_VERSION\s*=\s*\[version\]"(\d+\.\d+)"', src)
    hi = re.search(r'\$MAX_PYTHON_VERSION\s*=\s*\[version\]"(\d+\.\d+)"', src)
    assert lo and hi, "install.ps1 must define MIN_PYTHON_VERSION and MAX_PYTHON_VERSION"
    return lo.group(1), hi.group(1)


def _sh_range() -> tuple[str, str]:
    src = (ROOT / "install.sh").read_text(encoding="utf-8")
    lo = re.search(r'^MIN_PYTHON_VERSION="(\d+\.\d+)"', src, re.M)
    hi = re.search(r'^MAX_PYTHON_VERSION="(\d+\.\d+)"', src, re.M)
    assert lo and hi, "install.sh must define MIN_PYTHON_VERSION and MAX_PYTHON_VERSION"
    return lo.group(1), hi.group(1)


def _ci_matrix_versions() -> set[str]:
    versions: set[str] = set()
    for wf in ("ci.yml", "ci-nightly.yml"):
        src = (ROOT / ".github" / "workflows" / wf).read_text(encoding="utf-8")
        for m in re.finditer(r"matrix:\s*'(\{.*?\})'", src, re.S):
            matrix = json.loads(m.group(1))
            for combo in matrix.get("include", []):
                pv = combo.get("python-version")
                if pv:
                    versions.add(str(pv))
    assert versions, "no python-version matrix found in ci.yml / ci-nightly.yml"
    return versions


def test_installers_declare_identical_range():
    assert _ps1_range() == _sh_range(), (
        "install.ps1 and install.sh disagree on the supported Python range - update both together"
    )


def test_installer_range_matches_ci_matrix():
    lo, hi = _ps1_range()
    matrix = _ci_matrix_versions()
    assert min(matrix, key=_minor) == lo, (
        f"CI matrix minimum {min(matrix, key=_minor)} != installer MIN {lo} - "
        "the tested versions ARE the supported versions; change both in the same commit"
    )
    assert max(matrix, key=_minor) == hi, (
        f"CI matrix maximum {max(matrix, key=_minor)} != installer MAX {hi} - "
        "the tested versions ARE the supported versions; change both in the same commit"
    )


def test_installers_fall_through_to_uv_on_unsupported_python():
    # the gate is only safe because an out-of-range Python leads to uv provisioning a
    # supported one - both installers must keep that message/path
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "provisioning a supported Python via uv" in ps1
    assert "provisioning a supported Python via uv" in sh
    # and both recreate a stale venv built with an unsupported interpreter
    assert "recreating it" in ps1.lower()
    assert "recreating it" in sh.lower()


def test_pyaudio_stays_out_of_core_requirements():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    active = [ln for ln in req.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert not any("pyaudio" in ln.lower() for ln in active), (
        "pyaudio must stay OUT of core requirements.txt (no wheels for brand-new Pythons broke "
        "the whole install); it belongs to the optional vaf[speech] extra in pyproject.toml"
    )
    # ...but must remain available via the speech extra (pyproject.toml is the
    # packaging SSOT; text check as the Python 3.10 fallback sans tomllib)
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "pyaudio" in pyproject and "speech = [" in pyproject
    if sys.version_info >= (3, 11):
        import tomllib

        data = tomllib.loads(pyproject)
        assert not any(
            "pyaudio" in dep.lower() for dep in data["project"]["dependencies"]
        ), "pyaudio must not be a base dependency"
        assert any(
            "pyaudio" in dep.lower()
            for dep in data["project"]["optional-dependencies"]["speech"]
        ), "pyaudio must live in the speech extra"
