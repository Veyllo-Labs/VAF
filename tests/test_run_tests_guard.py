# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""run_tests must not be usable as a host shell.

run_tests executes in an isolated sandbox: a network-less COPY of the project with no .git and
no git binary. A coder that shells `git log`/`git restore` (or `apt-get install git`) through it
doom-loops forever - the real incident behind these tests. `_reject_non_test_command` intercepts
exactly those never-valid-here commands and points at the right tool, while leaving every genuine
test command (pytest, npm/cargo/go/make test, cd-then-test, pipes) untouched.
"""
import pytest

from vaf.tools.sandbox_test_runner import _reject_non_test_command


@pytest.mark.parametrize("cmd", [
    "git log --oneline",
    'git -C "/home/u/proj" log -- file.html',
    'cd "/home/u/proj" && git log --oneline',
    "git restore --source=HEAD~1 file.html",
    "sudo git status",
])
def test_git_commands_are_redirected(cmd):
    msg = _reject_non_test_command(cmd)
    assert msg is not None
    assert "git_log" in msg and "project_rollback" in msg
    assert "Nothing was run" in msg


@pytest.mark.parametrize("cmd", [
    "apt-get update && apt-get install -y git",
    "apt install git",
    "sudo apt-get install -y build-essential",
    "brew install git",
    "apk add git",
])
def test_os_package_installs_are_redirected(cmd):
    msg = _reject_non_test_command(cmd)
    assert msg is not None
    assert "Nothing was run" in msg
    assert "install" in msg.lower()


@pytest.mark.parametrize("cmd", [
    None,
    "",
    "python3 -m pytest -q",
    "pytest",
    "pytest tests/ -k finance",
    "npm test",
    "cargo test",
    "go test ./...",
    "make test",
    'cd "subdir" && python3 -m pytest -q',
    "pytest | tee out.log",          # pipe to a reporter, still a test run
    'pytest -k "a|b"',               # a pipe inside a quoted arg must not trip the guard
])
def test_legitimate_test_commands_pass_through(cmd):
    assert _reject_non_test_command(cmd) is None
