# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Guards for the slim `pip install vaf` promise (base dependencies only).

The library facade (`from vaf import Agent`) and the `vaf` console script must
work with ONLY the base dependencies from pyproject.toml installed; everything
heavy is an opt-in extra. CI installs the full dependency set, so these tests
hide the extras-only packages behind an import blocker in a subprocess: any
eager import of an extras package then fails exactly like it would on a real
slim install. Without this fence, one careless top-level `import numpy` (or
fastapi, sqlalchemy, ...) on the eager path would break every slim install
while the fully-provisioned CI stays green.

Blocker caveat: the blocker RAISES ModuleNotFoundError from find_spec (raising
is the only way to shadow an installed package), while on a real slim install
`importlib.util.find_spec` returns None. Code paths probed here must not call
find_spec on extras at import time; vaf.main's dependency bootstrap does, so
the CLI test disables it via VAF_SKIP_DEP_CHECK (the bootstrap is
source-checkout-gated anyway and irrelevant to eager-import regressions).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Top-level import names that only exist via extras (pyproject.toml
# [project.optional-dependencies]). Deliberately NOT listed because they are
# transitive deps of the base install (a real slim install has them): pydantic
# (via openai), websockets (via google-genai), PIL/pillow (via qrcode[pil] -
# also a desktop-extra member, but base-reachable).
FORBIDDEN_MODULES = (
    # server
    "fastapi", "uvicorn",
    # memory
    "sqlalchemy", "asyncpg", "pgvector", "redis", "numpy",
    "sentence_transformers", "onnxruntime", "tokenizers",
    # browser
    "playwright", "browser_use",
    # desktop (pywebview's import name is "webview")
    "PySide6", "qtpy", "pystray", "webview",
    # messaging channels
    "discord", "telegram",
    # speech
    "speech_recognition", "pyaudio", "sherpa_onnx",
    # pdf / office documents
    "PyPDF2", "pdfplumber", "pytesseract", "pdf2image",
    "docx", "openpyxl", "pptx",
)

_BLOCKER_TEMPLATE = """\
import sys

_FORBIDDEN = frozenset({forbidden!r})


class _ExtrasBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _FORBIDDEN:
            raise ModuleNotFoundError(
                "No module named %r (blocked: extras-only on a slim install)" % fullname,
                name=fullname,
            )
        return None


sys.meta_path.insert(0, _ExtrasBlocker())

# Fail loud if a forbidden module was preloaded at interpreter startup (e.g. by
# a site-packages .pth hook): sys.modules is consulted BEFORE meta_path, so a
# preloaded module would silently un-fence the guard. (No brace literals here:
# this template goes through str.format for the _FORBIDDEN set.)
_preloaded = sorted(set(m.split(".")[0] for m in sys.modules if m.split(".")[0] in _FORBIDDEN))
if _preloaded:
    sys.stderr.write("BLOCKER_INEFFECTIVE: preloaded extras modules: %s\\n" % ", ".join(_preloaded))
    sys.exit(97)
"""


def _run_with_blocker(code: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run `code` in a subprocess with the extras blocker installed first."""
    script = _BLOCKER_TEMPLATE.format(forbidden=tuple(FORBIDDEN_MODULES)) + "\n" + code
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
        env=env,
    )


def test_library_facade_imports_on_base_only():
    """`from vaf import Agent` must stay importable without any extras.

    This eagerly loads the full vaf.core.agent chain, so it fences the entire
    module-level import graph of the engine against extras creep.
    """
    result = _run_with_blocker(
        "import vaf\n"
        "from vaf import Agent\n"
        "print('FACADE_OK', vaf.__version__)\n"
    )
    assert result.returncode == 0, (
        "The slim library facade broke: an extras-only package is imported "
        "eagerly on the `from vaf import Agent` path.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "FACADE_OK" in result.stdout


def test_cli_version_works_on_base_only():
    """`vaf --version` must exit 0 without any extras installed.

    Fences the CLI import chain (vaf.main and every command module it wires):
    a heavy dependency may only be imported lazily inside a command body, never
    at module level.
    """
    result = _run_with_blocker(
        "import sys\n"
        "sys.argv = ['vaf', '--version']\n"
        "import vaf.main\n"
        "try:\n"
        "    vaf.main.main()\n"
        "except SystemExit as e:\n"
        "    sys.exit(e.code or 0)\n",
        extra_env={"VAF_SKIP_DEP_CHECK": "1"},
    )
    assert result.returncode == 0, (
        "`vaf --version` broke on a slim install: a command module imports an "
        "extras-only package at module level.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib requires Python 3.11+")
def test_all_extra_references_every_other_extra():
    """The self-referential `all` extra must reference every other extra.

    pyproject.toml cannot compute a union like the old setup.py did, so `all`
    is written as vaf[extra1,extra2,...]; this guard keeps it complete when a
    new extra is added.
    """
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    others = sorted(name for name in extras if name != "all")
    all_spec = extras["all"]
    assert len(all_spec) == 1 and all_spec[0].startswith("vaf[") and all_spec[0].endswith("]"), (
        f"`all` must be the single self-referential requirement vaf[...]: {all_spec}"
    )
    referenced = sorted(all_spec[0][len("vaf["):-1].split(","))
    assert referenced == others, (
        f"`all` must reference every other extra exactly once.\n"
        f"referenced: {referenced}\nexpected:   {others}"
    )


def test_wheel_invariants_for_bootstrap_gate():
    """Invariants the pip-install bootstrap gate in vaf/main.py relies on.

    The gate detects a source checkout by requirements.txt sitting one level
    above the package. That stays sound only while (a) requirements.txt never
    lives INSIDE the vaf package (a wheel would then ship it into
    site-packages) and (b) package discovery stays pinned to vaf* (nothing
    else leaks into the wheel).
    """
    assert not (ROOT / "vaf" / "requirements.txt").exists(), (
        "vaf/requirements.txt would ship in the wheel and falsely open the "
        "source-checkout gate in vaf.main.bootstrap()"
    )
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'include = ["vaf*"]' in pyproject, (
        "package discovery must stay pinned to vaf* (see "
        "[tool.setuptools.packages.find] in pyproject.toml)"
    )


def test_bootstrap_gate_skips_pip_install_layout(monkeypatch):
    """bootstrap() must return before probing anything on a pip-install layout.

    The gate detects a source checkout via requirements.txt one level above the
    package; a pip-installed VAF (site-packages) must never prompt for or
    auto-run pip. Simulated by pretending requirements.txt is absent.
    """
    import importlib.util

    import vaf.main as vaf_main

    monkeypatch.delenv("VAF_SKIP_DEP_CHECK", raising=False)

    probes = []
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda *a, **k: probes.append(a) or None
    )
    real_exists = os.path.exists

    def fake_exists(path):
        if os.path.basename(str(path)) == "requirements.txt":
            return False
        return real_exists(path)

    monkeypatch.setattr(os.path, "exists", fake_exists)

    vaf_main.bootstrap()
    assert probes == [], (
        "bootstrap() must not probe dependencies when requirements.txt is absent "
        "next to the package (pip-install layout)"
    )
