# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf audit` (vaf/cli/cmd/audit.py): the exit code is the contract a script or a CI job
reads, so each of its three values is pinned, together with the three output formats and
the dismiss/show round trip. The model is the fake reviewer of test_code_audit.py.
"""
import json
import shutil

import pytest
from typer.testing import CliRunner

from tests.test_code_audit import BUGGY, OFF_BY_ONE, _change, _Model, repo  # noqa: F401 - fixture
from vaf.cli.cmd import audit as audit_cmd
from vaf.core import code_audit as ca

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

runner = CliRunner()


@pytest.fixture
def model(monkeypatch):
    """Whatever the test puts in `holder["model"]` answers for the configured provider."""
    holder = {"model": _Model([OFF_BY_ONE])}
    monkeypatch.setattr(ca, "ask_via_complete", lambda **kw: holder["model"])
    return holder


def _run(repo, *args):
    return runner.invoke(audit_cmd.app, ["run", str(repo), *args])


def test_a_verified_finding_exits_1_and_is_printed(repo, model):
    """MUTATION: exit 0 whatever the report says."""
    _change(repo, "app.py", BUGGY)
    result = _run(repo)
    assert result.exit_code == 1, result.output
    assert "The loop skips the first item" in result.output
    assert "app.py:3" in result.output


def test_a_clean_change_exits_0(repo, model):
    model["model"] = _Model([])
    _change(repo, "app.py", "def total(items):\n    return sum(items) or 0\n")
    result = _run(repo)
    assert result.exit_code == 0, result.output
    assert "Status: COMPLETE" in result.output


def test_fail_on_raises_the_bar(repo, model):
    """A major finding with --fail-on critical: reported, but the exit is 0.
    MUTATION: ignore --fail-on."""
    _change(repo, "app.py", BUGGY)
    result = _run(repo, "--fail-on", "critical")
    assert result.exit_code == 0, result.output
    assert "The loop skips the first item" in result.output


def test_a_run_without_a_model_never_exits_0(repo, model):
    """--no-llm reviews nothing with a model, so even with --fail-on none it is not a pass.
    MUTATION: let `none` override an incomplete run."""
    _change(repo, "app.py", BUGGY)
    result = _run(repo, "--no-llm", "--fail-on", "none")
    assert result.exit_code == 2, result.output


def test_json_is_machine_readable_and_carries_the_fix_prompt(repo, model):
    _change(repo, "app.py", BUGGY)
    result = _run(repo, "--format", "json")
    data = json.loads(result.stdout)          # progress lines go to stderr, never in here
    assert "reviewed 1/1" in result.stderr
    assert data["status"] == "complete"
    [finding] = data["findings"]
    assert finding["start_line"] == 3 and finding["fix_prompt"]
    assert ca.UNTRUSTED_PREAMBLE in data["fix_prompt"]


def test_what_the_model_lane_prints_never_reaches_the_json(repo, model):
    """A provider error is printed by the backend while the audit runs; measured, 70 such
    lines once made `--format json` unparseable. MUTATION: let the audit write to stdout."""
    inner = model["model"]

    def noisy(messages, max_tokens):
        print("[WARN] complete(cli:audit): backend error: 402 insufficient credits")
        return inner(messages, max_tokens)

    model["model"] = noisy
    _change(repo, "app.py", BUGGY)
    result = _run(repo, "--format", "json")
    assert json.loads(result.stdout)["status"] == "complete"
    assert "insufficient credits" in result.stderr


def test_prompt_format_starts_with_the_status(repo, model):
    """An agent reading the prompt must see an incomplete run as incomplete before any
    finding. MUTATION: drop the status line."""
    _change(repo, "app.py", BUGGY)
    result = _run(repo, "--format", "prompt")
    assert result.stdout.startswith("Code audit complete. 1 verified finding(s).")
    assert "In @app.py:" in result.output


def test_bad_arguments_exit_2(repo, model):
    assert _run(repo, "--committed", "--uncommitted").exit_code == 2
    assert _run(repo, "--format", "xml").exit_code == 2
    assert _run(repo, "--fail-on", "everything").exit_code == 2


def test_dismiss_then_rerun_reports_nothing_and_show_prints_the_last_audit(repo, model):
    """MUTATION: dismiss writes nowhere the next run reads."""
    _change(repo, "app.py", BUGGY)
    first = json.loads(_run(repo, "--format", "json").stdout)
    finding_id = first["findings"][0]["id"]

    dismissed = runner.invoke(audit_cmd.app, ["dismiss", finding_id, "--reason",
                                              "skipping the header row", "--repo", str(repo)])
    assert dismissed.exit_code == 0, dismissed.output

    second = _run(repo)
    assert second.exit_code == 0, second.output
    assert "1 dismissed earlier" in second.output

    shown = runner.invoke(audit_cmd.app, ["show", str(repo)])
    assert shown.exit_code == 0 and "1 dismissed earlier" in shown.output


def test_show_without_an_audit_says_so(repo):
    result = runner.invoke(audit_cmd.app, ["show", str(repo)])
    assert result.exit_code == 1
