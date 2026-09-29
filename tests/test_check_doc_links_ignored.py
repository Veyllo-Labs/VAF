# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""scripts/check_doc_links.py judges the docs a clone will have, and the ones about to be added.

It scanned every *.md on disk, git-ignored local notes included, so a link between two
local-only files failed the check on every machine but the one that has both (measured on
macOS: CLAUDE.md -> a gitignored doc that exists on one workstation). Ignored files are not
sources now; an untracked file that is NOT ignored still is - a new doc not yet added is
exactly what the check exists to catch before the commit.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_doc_links.py"


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("LOCAL.md\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# a\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("[a](docs/a.md)\n", encoding="utf-8")
    (tmp_path / "LOCAL.md").write_text("[only here](docs/private-notes.md)\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "README.md", "docs/a.md"], cwd=tmp_path, check=True)
    return tmp_path


def _check(cwd):
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=cwd, capture_output=True, text=True)


def test_an_ignored_local_note_is_not_a_source(repo):
    """MUTATION: scan ignored files again and LOCAL.md's link fails the check here, as it
    did on every machine without the private note."""
    r = _check(repo)
    assert r.returncode == 0, r.stdout


def test_a_new_doc_not_yet_added_is_still_checked(repo):
    (repo / "NEW.md").write_text("[gone](docs/missing.md)\n", encoding="utf-8")
    r = _check(repo)
    assert r.returncode == 1 and "NEW.md" in r.stdout, r.stdout
