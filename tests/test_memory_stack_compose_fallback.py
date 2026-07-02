# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: a docker CLI WITHOUT the compose plugin (Homebrew docker + Colima on macOS
when ~/.docker/config.json lacks cliPluginsExtraDirs) fails 'docker compose up' with exit
125 and "unknown shorthand flag: 'f' in -f". ensure_memory_stack_up must fall through to
the standalone docker-compose binary (which install.sh brews) instead of giving up - the
pre-fix code logged the error and returned, leaving the whole memory stack (incl. the
auth/setup DB) down on an otherwise healthy machine.

Hermetic: subprocess.run is monkeypatched; no Docker, no containers.
"""
from pathlib import Path

import vaf.tray as tray
from vaf.tray import _compose_plugin_missing


def test_plugin_missing_classification():
    assert _compose_plugin_missing("unknown shorthand flag: 'f' in -f")
    assert _compose_plugin_missing("docker: 'compose' is not a docker command.")
    assert _compose_plugin_missing("unknown flag: --quiet-pull")
    # Real compose failures must NOT look like a missing plugin (no pointless second run
    # that would mask the actual error message).
    assert not _compose_plugin_missing("Error response from daemon: driver failed programming external connectivity")
    assert not _compose_plugin_missing("no space left on device")
    assert not _compose_plugin_missing("")


class _Result:
    def __init__(self, returncode: int, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr


def test_compose_plugin_missing_falls_back_to_legacy_binary(monkeypatch):
    """THE Mac regression: 'docker compose up' exits 125 (no plugin) -> the legacy
    docker-compose invocation must still be attempted and succeed."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] != "docker-compose" and "compose" in cmd:
            return _Result(125, "unknown shorthand flag: 'f' in -f")
        return _Result(0)

    monkeypatch.chdir(Path(__file__).resolve().parents[1])  # repo root: compose file exists
    monkeypatch.setattr(tray, "_ensure_macos_brew_path", lambda: None)
    monkeypatch.setattr(tray, "_is_docker_daemon_running", lambda: True)
    monkeypatch.setattr(tray, "_resolve_docker_exe", lambda: "docker")
    monkeypatch.setattr(tray.subprocess, "run", fake_run)

    tray.ensure_memory_stack_up()

    assert any(c[0] == "docker-compose" for c in calls), (
        f"legacy docker-compose fallback was never tried; calls: {calls}"
    )


def test_real_compose_failure_does_not_fall_back(monkeypatch):
    """A genuine compose error (e.g. port conflict) must surface as-is - running the legacy
    binary too would just fail again and bury the real error message."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] != "docker-compose" and "compose" in cmd:
            return _Result(1, "Error response from daemon: driver failed programming external connectivity")
        return _Result(0)

    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    monkeypatch.setattr(tray, "_ensure_macos_brew_path", lambda: None)
    monkeypatch.setattr(tray, "_is_docker_daemon_running", lambda: True)
    monkeypatch.setattr(tray, "_resolve_docker_exe", lambda: "docker")
    monkeypatch.setattr(tray.subprocess, "run", fake_run)

    tray.ensure_memory_stack_up()

    assert not any(c[0] == "docker-compose" for c in calls), (
        f"fallback ran on a real compose failure; calls: {calls}"
    )
