# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What the user is told when the LOCAL STT engine cannot run.

faster-whisper ships with no installer, so this branch is reached by people
who picked it in Settings - and the message they got named the package as
"not installed" for every ImportError, including an installed package whose
native dependency fails to load. These tests pin the honest diagnosis:
the real reason travels, and unrelated imports cannot fake it.
"""
import re
import sys
from pathlib import Path

import pytest

import vaf.core.web_server as web_server

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_cached_model(monkeypatch):
    """The loader is a singleton; a model cached by another test would skip
    the import entirely and make every assertion below vacuous."""
    monkeypatch.setattr(web_server, "_whisper_model", None, raising=False)


def test_the_real_import_failure_is_reported_not_a_guess(monkeypatch):
    """An arch mismatch in a native dependency (the frequent macOS case) says
    something different from an absent package, and the user needs to see it."""
    import importlib

    real = importlib.import_module

    def _fail(name, *a, **kw):
        if name == "faster_whisper":
            raise ImportError("dlopen(libctranslate2.dylib): incompatible architecture")
        return real(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", _fail)
    with pytest.raises(ImportError) as excinfo:
        web_server.get_whisper_model()
    assert "incompatible architecture" in str(excinfo.value)
    assert 'pip install "vaf[speech]"' in str(excinfo.value), \
        "the message must name an install command that actually exists"


def test_a_missing_psutil_is_not_blamed_on_faster_whisper(monkeypatch):
    """The memory bookkeeping used to sit in the same try as the import, so a
    missing psutil was reported as a missing faster-whisper. Diagnostics must
    never decide whether transcription works."""
    import builtins

    real_import = builtins.__import__

    def _no_psutil(name, *a, **kw):
        if name == "psutil":
            raise ImportError("No module named 'psutil'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    assert web_server._rss_mb() == 0.0


def test_the_websocket_error_names_the_setting_and_the_reason():
    """Wiring pin: the loader can only be honest if the handler passes the
    reason on instead of printing its own verdict."""
    import inspect

    src = inspect.getsource(web_server.websocket_endpoint)
    assert "{import_error}" in src, "the real import failure is swallowed again"
    assert "Settings > Voice" in src, "the message no longer names the setting"
    assert "faster-whisper not installed." not in src, \
        "the blanket 'not installed' verdict is back"


def test_choosing_a_cloud_provider_leaves_no_local_engine_behind():
    """Registry copy: the engine key is written in the browser, so the guard
    lives here. A cloud pick on top of an earlier local pick used to keep
    speech_stt_engine='local', which decided the lane again the day the key
    stopped resolving."""
    src = (_REPO / "web" / "components" / "SettingsModal.tsx").read_text(encoding="utf-8")
    block = re.search(
        r"handleChange\('speech_stt_provider', v\);(.{0,2000}?)handleChange\('speech_stt_api_model'",
        src, re.S)
    assert block, "the cloud STT provider branch moved - re-point this guard"
    assert "handleChange('speech_stt_engine', 'docker')" in block.group(1), \
        "picking a cloud STT provider leaves a contradictory engine behind"


def test_the_local_option_says_it_needs_a_separate_install():
    """No installer ships faster-whisper. An option labelled just "Local" reads
    like something that is already there."""
    for locale in ("en", "de"):
        data = (_REPO / "web" / "messages" / f"{locale}.json").read_text(encoding="utf-8")
        label = re.search(r'"localStt":\s*"([^"]*)"', data)
        assert label, f"localStt label missing from {locale}.json"
        assert "faster-whisper" in label.group(1), \
            f"{locale}: the local STT option hides that it is a separate install"


def test_the_speech_extra_actually_ships_the_local_engine():
    """The Settings option pointed at a package no install command delivered.
    The message now names `pip install "vaf[speech]"`, so the extra has to
    contain it - and the license table has to know about it."""
    # pyproject.toml is the packaging SSOT; text check as the Python 3.10
    # fallback sans tomllib (same shape as tests/test_installer_python_gate.py).
    pyproject = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
    speech_block = re.search(r"^speech = \[(.*?)\]", pyproject, re.S | re.M)
    assert speech_block, "the speech extra moved - re-point this guard"
    assert "faster-whisper" in speech_block.group(1), \
        'pip install "vaf[speech]" no longer delivers the local STT engine'
    if sys.version_info >= (3, 11):
        import tomllib

        speech = tomllib.loads(pyproject)["project"]["optional-dependencies"]["speech"]
        assert any(d.startswith("faster-whisper") for d in speech)

    third_party = (_REPO / "docs" / "legal" / "THIRD_PARTY.md").read_text(encoding="utf-8")
    assert "faster-whisper" in third_party, "AGPL notice: undeclared bundled dependency"
