# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf repair --json` has to survive a pipe.

The documented use is `vaf repair --json | jq`, and stdout is then not a
terminal. Printing the payload through the console renderer breaks that in two
quiet ways: it falls back to 80 columns and word-wraps, which puts a raw
newline inside a JSON string as soon as one `reason` sentence is long enough,
and its markup parser swallows any [bracketed] text in a value. Both produce
output that looks fine on screen and fails at `json.loads`.
"""
import json

import typer
from typer.testing import CliRunner

import vaf.cli.cmd.repair as repair


LONG_REASON = ("The container publishes host port 5432, but VAF is configured to reach "
               "it on 5433. Change the config key `memory_db_url` to the published port, "
               "or set the matching port variable in ~/.vaf/compose.env and start the "
               "stack again. VAF does not rewrite this for you.")


def _status():
    return {
        "docker": {"available": True, "reason": "ok", "detail": ""},
        "stack_root": "/repo",
        "services": [{
            "name": "vaf-memory-db", "service_key": "postgres", "required": True,
            "exists": True, "running": True, "health": "healthy", "host_ports": [],
            "configured_port": 5433, "port_mismatch": True, "probe": None,
            "probe_ok": None, "state": "error",
            # A [bracketed] fragment as well: the markup parser would eat it.
            "reason": LONG_REASON + " [see DOCKER_SERVICES.md]",
        }],
        "checked_at": "now",
    }


def _app():
    app = typer.Typer()
    app.command()(repair.cmd_repair)
    return app


def test_check_json_survives_a_pipe(monkeypatch):
    monkeypatch.setattr(repair, "collect_service_status", _status)
    result = CliRunner().invoke(_app(), ["--check", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)          # the whole point
    assert parsed["services"][0]["reason"].endswith("[see DOCKER_SERVICES.md]"), \
        "markup parsing ate part of the value"
    assert "\n" not in parsed["services"][0]["reason"], "the renderer wrapped inside a string"


def test_repair_json_has_no_prose_above_it(monkeypatch):
    """A human-readable progress line printed before the payload makes stdout
    start with something `json.loads` cannot read."""
    monkeypatch.setattr(repair, "repair_service_stack",
                        lambda progress=None, **kw: {"ok": True, "degraded": [],
                                                     "steps": [], "status_after": _status()})
    result = CliRunner().invoke(_app(), ["--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["ok"] is True


def test_a_healthy_run_says_so_but_never_over_a_broken_service(monkeypatch):
    """`ok` is required-only, so an optional container that will not start must
    not fail the command - and must not be papered over with "healthy" either."""
    monkeypatch.setattr(repair, "repair_service_stack",
                        lambda progress=None, **kw: {"ok": True, "degraded": ["vaf-tts"],
                                                     "steps": [], "status_after": _status()})
    result = CliRunner().invoke(_app(), [])
    assert result.exit_code == 0
    assert "vaf-tts" in result.stdout
    assert "stack is healthy" not in result.stdout
