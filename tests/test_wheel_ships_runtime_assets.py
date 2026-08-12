# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An installed VAF must contain the files it reads at runtime.

Several subsystems load data from inside their own package
(`Path(__file__).parent / ...`): the vocabulary book, the coder's project
scaffolds, the seeded tool-knowledge cards, the WhatsApp bridge's node app.
A source checkout always has them, so nothing in the suite noticed that the
built wheel did not: measured on 0.1.0a21, `pip wheel .` produced 423 files
of which exactly 11 were non-Python - `media/` and `py.typed`. Every one of
the assets below was missing, i.e. `pip install vaf` yielded an agent whose
whole vocabulary silently fell back to the built-in patterns.

This guard BUILDS a wheel rather than reading pyproject.toml, because the
question is not what the config says but what setuptools produces
(`include-package-data`, the packages.find include list and the package-data
patterns interact, and only the artifact settles it).
"""
import pathlib
import subprocess
import sys
import zipfile

import pytest

# (import path in the package, glob in the repo, minimum count)
RUNTIME_ASSETS = (
    ("core/vocab/data", "vaf/core/vocab/data/*.json", 25),
    ("tools/coder_templates", "vaf/tools/coder_templates/**/*", 10),
    ("whare_wananga/knowledge", "vaf/whare_wananga/knowledge/*.json", 20),
    ("media", "vaf/media/**/*", 5),
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _pristine_copy(dest: pathlib.Path) -> pathlib.Path:
    """The tracked tree AS IT IS ON DISK, copied to a clean directory.

    Building in the repo would reuse its `build/lib*` directory, and setuptools
    keeps previously copied data files there - measured: with the vocab pattern
    deleted from pyproject.toml the wheel still contained the vocabulary, so the
    mutation ran green and the guard proved nothing. It also keeps the test from
    writing build artifacts into someone's working tree.
    """
    import shutil
    files = subprocess.run(["git", "ls-files"], cwd=str(_ROOT),
                           capture_output=True, text=True, timeout=120).stdout.split()
    dest.mkdir(parents=True, exist_ok=True)
    for rel in files:
        src = _ROOT / rel
        if not src.is_file():
            continue
        tgt = dest / rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, tgt)
    return dest


def _build_wheel(dest: pathlib.Path) -> pathlib.Path:
    tree = _pristine_copy(dest.parent / "src")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
         "--no-build-isolation", "--no-cache-dir", "-w", str(dest)],
        cwd=str(tree), capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        pytest.skip(f"wheel build unavailable in this environment: "
                    f"{(r.stderr or r.stdout)[-300:]}")
    wheels = list(dest.glob("*.whl"))
    if not wheels:
        pytest.skip("wheel build produced no artifact")
    return wheels[0]


def test_the_built_wheel_carries_every_runtime_asset(tmp_path):
    """MUTATION: drop one pattern from [tool.setuptools.package-data] and that
    subsystem's case goes red - which is exactly the state 0.1.0a21 shipped in."""
    wheel = _build_wheel(tmp_path / "dist")
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()

    for pkg_dir, repo_glob, minimum in RUNTIME_ASSETS:
        in_repo = [p for p in _ROOT.glob(repo_glob) if p.is_file()]
        in_wheel = [n for n in names if f"vaf/{pkg_dir}/" in n]
        assert len(in_repo) >= minimum, (
            f"{repo_glob} has shrunk to {len(in_repo)} files; re-check this guard "
            f"rather than lowering it silently")
        assert len(in_wheel) >= minimum, (
            f"an installed VAF would be missing {pkg_dir}: {len(in_wheel)} of "
            f"{len(in_repo)} files made it into the wheel")

    # The two lexicons this guard was written for, by name.
    assert any("vocab/data/confirm_yes.json" in n for n in names)
    assert any("vocab/data/capability_denial.json" in n for n in names)
