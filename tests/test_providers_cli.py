# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf providers` (vaf/cli/cmd/providers.py): the overview in a terminal.

Typer's CliRunner on the command alone, the configuration in the test.

MUTATION: build the rows from a role list of the command's own (its label map) instead
of `resolve_roles()` -> test_one_row_per_role and test_json_is_the_api_shape.
"""
import json
import re

import pytest
import typer
from typer.testing import CliRunner

from vaf.cli.cmd import providers as providers_mod
from vaf.core import model_roles as mr
from vaf.core.config import Config


@pytest.fixture
def config(monkeypatch):
    values = {"provider": "deepseek", "api_model_deepseek": "deepseek-v4-pro"}
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {**cls.DEFAULTS, **values}))
    import vaf.core.api_keys as api_keys
    monkeypatch.setattr(api_keys, "resolve_api_key", lambda provider, caller_config=None: "sk-x")
    return values


def _invoke(*args):
    app = typer.Typer()
    app.command()(providers_mod.providers_cmd)
    return CliRunner().invoke(app, list(args))


def test_one_row_per_role(config, monkeypatch):
    # One role fewer than the real list: a command with its own role list would still
    # print ten rows.
    short = mr.resolve_roles()[:-1]
    monkeypatch.setattr(mr, "resolve_roles", lambda *a, **k: short)
    out = _invoke()
    assert out.exit_code == 0, out.output
    assert "Images" in out.output and "not available" in out.output
    # Letters only: the table folds a long reason across lines.
    letters = re.sub(r"[^a-z]", "", out.output.lower())
    assert re.sub(r"[^a-z]", "", mr.REASON_TEXT["main_cannot_see"].lower()) in letters
    assert "Failover" not in out.output


def test_json_is_the_api_shape(config):
    out = _invoke("--json")
    assert out.exit_code == 0, out.output
    assert json.loads(out.output) == [a.as_dict() for a in mr.resolve_roles()]
    assert len(json.loads(out.output)) == len(mr.ROLES)


def test_the_command_is_registered():
    from vaf.main import app
    names = {c.name for c in app.registered_commands}
    assert "providers" in names
