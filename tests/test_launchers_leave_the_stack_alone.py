# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Docker service stack is started by the lanes that need it, never by a launcher script.

`vaf` is an alias for run_vaf.sh, and run_vaf.sh ran `docker compose up -d` before EVERY
command. Measured on a Mac with VAF stopped: `vaf --version` left seven containers running,
and `vaf stop` started the stack and then reported that VAF was not running. The lanes that
need the stack start it themselves through vaf/core/service_stack.py: the tray (desktop,
`vaf start`, vaf.sh, the systemd unit), the full-screen terminal app and the prompt lanes of
`vaf run`."""
import re
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_COMPOSE_UP = re.compile(r"compose\b[^\n]*\bup\b")


@pytest.mark.parametrize("script", ["run_vaf.sh", "start_vaf.sh", "vaf.sh"])
def test_no_launcher_script_brings_the_stack_up(script):
    """MUTATION: put the compose start back into run_vaf.sh."""
    text = (ROOT / script).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not _COMPOSE_UP.search(code), f"{script} starts the Docker stack itself"


def test_the_background_start_runs_the_primitive_once_and_only_with_a_compose_file(monkeypatch):
    """MUTATION: start without asking for the compose file, or not at all."""
    import vaf.core.service_stack as stack
    started = threading.Event()
    calls = []
    monkeypatch.setattr(stack, "ensure_service_stack",
                        lambda log=None: (calls.append(log), started.set()))
    monkeypatch.setattr(stack, "find_stack_root", lambda: None)
    assert stack.start_service_stack_in_background() is False
    assert not started.wait(0.2) and calls == []
    monkeypatch.setattr(stack, "find_stack_root", lambda: ROOT)
    assert stack.start_service_stack_in_background() is True
    assert started.wait(5) and len(calls) == 1


@pytest.mark.parametrize("flag", ["--classic"])
def test_the_prompt_lanes_of_vaf_run_start_the_stack(monkeypatch, flag):
    """The full-screen app starts the stack in its boot; the classic and modern lanes did
    not, and relied on the launcher script. MUTATION: drop the start before the lanes."""
    from typer.testing import CliRunner

    import vaf.cli.cmd.run as run_mod
    import vaf.core.service_stack as stack
    order = []
    monkeypatch.setattr(stack, "start_service_stack_in_background",
                        lambda log=None: order.append("stack") or True)
    monkeypatch.setattr(run_mod, "_run_classic", lambda *a, **k: order.append("classic"))
    monkeypatch.setattr(run_mod, "_run_modern", lambda *a, **k: order.append("modern"))
    result = CliRunner().invoke(run_mod.app, [flag])
    assert result.exit_code == 0, result.output
    assert order == ["stack", "classic"]
