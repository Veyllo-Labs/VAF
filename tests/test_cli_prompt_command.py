# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf prompt` and `vaf run prompt`: one implementation, and both spellings reach it.

MEASURED BEFORE THE FIX: `vaf run prompt -p "..."` failed with "No such option: -p". The
`vaf run` group takes an optional MESSAGE, and click let it swallow the subcommand's name. The
two commands were also two copies that had drifted: only the unreachable one bound the
local-admin identity (the scope the Web UI uses), so `vaf prompt` read and wrote memory under a
separate "default" identity. And the README's `vaf prompt "Hello"` failed with "Missing option
'--prompt'".

MUTATION: build the run group without _RunGroup and the first test goes red; give main.py its
own copy back and the second goes red.
"""
import pytest
from typer.testing import CliRunner

import vaf.cli.cmd.run as run_mod

runner = CliRunner()


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(run_mod, "prompt_once",
                        lambda prompt, output_format="text", session=None, save_session=False:
                        seen.append((prompt, output_format, session, save_session)))
    return seen


def test_vaf_run_prompt_reaches_the_subcommand(calls):
    out = runner.invoke(run_mod.app, ["prompt", "-p", "Erklaer das Repo", "--output-format", "json"])
    assert out.exit_code == 0, out.output
    assert calls == [("Erklaer das Repo", "json", None, False)]


def test_vaf_prompt_is_the_same_implementation(calls):
    from vaf.main import app

    for argv in (["prompt", "-p", "Hallo"], ["prompt", "Hallo"], ["run", "prompt", "Hallo"]):
        out = runner.invoke(app, argv)
        assert out.exit_code == 0, (argv, out.output)
    assert [c[0] for c in calls] == ["Hallo", "Hallo", "Hallo"]


def test_a_prompt_is_needed_and_only_once(calls):
    from vaf.main import app

    assert runner.invoke(app, ["prompt"]).exit_code != 0
    assert runner.invoke(app, ["prompt", "a", "-p", "b"]).exit_code != 0
    assert calls == []


def test_a_plain_message_to_vaf_run_is_still_a_message(monkeypatch):
    """The routing rule only claims a first token that NAMES a subcommand."""
    seen = []
    monkeypatch.setattr(run_mod, "_run_classic", lambda message, verbose, session: seen.append(message))
    import vaf.cli.cmd.update as update_mod
    monkeypatch.setattr(update_mod, "maybe_notify_update", lambda *a, **k: None)
    out = runner.invoke(run_mod.app, ["--classic", "Hallo Welt"])
    assert out.exit_code == 0, out.output
    assert seen == ["Hallo Welt"]


def test_the_one_implementation_binds_the_local_admin():
    import inspect

    src = inspect.getsource(run_mod.prompt_once)
    assert "_make_cli_agent(verbose=False, host_audio=False)" in src
