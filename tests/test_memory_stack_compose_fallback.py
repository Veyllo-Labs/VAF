# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: a docker CLI WITHOUT the compose plugin (Homebrew docker + Colima on macOS
when ~/.docker/config.json lacks cliPluginsExtraDirs) fails 'docker compose up' with exit
125 and "unknown shorthand flag: 'f' in -f". ensure_service_stack must fall through to
the standalone docker-compose binary (which install.sh brews) instead of giving up - the
pre-fix code logged the error and returned, leaving the whole memory stack (incl. the
auth/setup DB) down on an otherwise healthy machine.

The stack lifecycle lives in vaf/core/service_stack.py now (moved out of the tray so
`vaf run` can start the same stack); the tray keeps only delegating names, and that
delegation is pinned here too.

Hermetic: subprocess.run is monkeypatched; no Docker, no containers.
"""
from pathlib import Path

import vaf.core.service_stack as stack
from vaf.core.service_stack import _compose_plugin_missing


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


def _hermetic(monkeypatch, fake_run):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])  # repo root: compose file exists
    monkeypatch.setattr(stack, "_ensure_macos_brew_path", lambda: None)
    monkeypatch.setattr(stack, "is_docker_daemon_running", lambda: True)
    monkeypatch.setattr(stack, "resolve_docker_exe", lambda: "docker")
    monkeypatch.setattr(stack.subprocess, "run", fake_run)


def test_compose_plugin_missing_falls_back_to_legacy_binary(monkeypatch):
    """THE Mac regression: 'docker compose up' exits 125 (no plugin) -> the legacy
    docker-compose invocation must still be attempted and succeed."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] != "docker-compose" and "compose" in cmd:
            return _Result(125, "unknown shorthand flag: 'f' in -f")
        return _Result(0)

    _hermetic(monkeypatch, fake_run)
    assert stack.ensure_service_stack() is True
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

    _hermetic(monkeypatch, fake_run)
    assert stack.ensure_service_stack() is False
    assert not any(c[0] == "docker-compose" for c in calls), (
        f"fallback ran on a real compose failure; calls: {calls}"
    )


def test_core_services_come_up_before_the_optional_builds(monkeypatch):
    """The two-phase order is the whole point: a failed OPTIONAL local build
    (tts/vaf-browser) must never abort the 'up' that carries the database."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "tts" in cmd:
            return _Result(1, "build failed")
        return _Result(0)

    _hermetic(monkeypatch, fake_run)
    assert stack.ensure_service_stack() is True, (
        "a failed optional build took the core result down with it")
    ups = [c for c in calls if "up" in c]
    assert "postgres" in ups[0] and "tts" not in ups[0]
    assert "tts" in ups[1]


def test_no_compose_file_is_an_honest_no_op(monkeypatch, tmp_path):
    """A pip install ships no compose file - nothing to start, nothing run."""
    calls = []
    monkeypatch.setattr(stack, "_ensure_macos_brew_path", lambda: None)
    monkeypatch.setattr(stack, "is_docker_daemon_running", lambda: True)
    monkeypatch.setattr(stack, "find_stack_root", lambda: None)
    monkeypatch.setattr(stack.subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)) or _Result(0))
    assert stack.ensure_service_stack() is False
    assert calls == []


def test_the_tray_delegates_to_the_engine(monkeypatch):
    import vaf.tray as tray

    started, stopped = [], []
    monkeypatch.setattr(tray, "ensure_service_stack",
                        lambda log=None: started.append(True))
    monkeypatch.setattr(tray, "stop_service_stack",
                        lambda log=None: stopped.append(True))
    tray.ensure_memory_stack_up()
    tray.stop_memory_stack()
    assert started == [True] and stopped == [True], (
        "the tray grew its own stack lifecycle back instead of delegating")


def test_optional_services_are_rebuilt_while_core_services_are_not(monkeypatch):
    """The browser and TTS images are BUILT from this repo rather than pulled, and a
    plain `up -d` reuses whatever image already exists. A checkout that moves ahead
    of its images therefore keeps running the old ones with nothing to show for it:
    an image predating the browser's KasmVNC stream answered CDP perfectly and
    every stream request with a 502, sixteen days after the code had moved on.

    The other half matters just as much: the core services are pulled, not built,
    so they must not pay for a build on every single start.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result(0)

    _hermetic(monkeypatch, fake_run)
    assert stack.ensure_service_stack() is True

    # Only the compose `up` invocations decide this pin: the browser age gate
    # also names vaf-browser in bare `docker inspect` probes (and, when the
    # image is stale, in its own `build --pull --no-cache`), and neither is an
    # optional-service START.
    ups = [c for c in calls if "up" in c]
    optional = [c for c in ups if any(s in c for s in stack.OPTIONAL_SERVICES)]
    assert optional, "the optional services were never started"
    for cmd in optional:
        assert "--build" in cmd, f"optional services started without --build: {cmd}"

    core_only = [c for c in ups
                 if any(s in c for s in stack.CORE_SERVICES)
                 and not any(s in c for s in stack.OPTIONAL_SERVICES)]
    assert core_only, "the core services were never started"
    for cmd in core_only:
        assert "--build" not in cmd, f"core services must not rebuild on every start: {cmd}"
