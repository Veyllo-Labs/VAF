# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Updater self-heal contract (vaf/cli/cmd/update.py).

Live incident 1: the updater's own npm step rewrote web/package-lock.json;
the dirty-tree pre-check then aborted EVERY future update (a Mac sat on a7
with four newer releases available).

Live incident 2 (the a12 verification on that Mac): the first fix parsed
porcelain lines with a fixed `line[3:]` offset, but `_git` STRIPS its
output - the leading space of ' M path' was gone in production, the path
read 'eb/package-lock.json', and the self-heal silently never fired. The
unit tests had fed the unstripped raw form, so they passed while the real
pipeline failed. Consequence here: the classification tests pin BOTH forms,
and an integration test drives the REAL `_git` against a real temp git
repo - fixtures must never diverge from the production input shape again.
"""
import subprocess
from pathlib import Path

from vaf.cli.cmd.update import _git, _restore_self_churn, _split_self_churn

STRIPPED = "M web/package-lock.json"        # what _git delivers (production)
RAW = " M web/package-lock.json"            # what raw porcelain delivers


def test_stripped_production_form_is_churn():
    # THE a12 bug: this exact form was misparsed and classified as real.
    real, churn = _split_self_churn(STRIPPED)
    assert real == []
    assert churn == ["web/package-lock.json"]


def test_unstripped_raw_form_is_churn_too():
    real, churn = _split_self_churn(RAW)
    assert real == []
    assert churn == ["web/package-lock.json"]


def test_real_changes_abort_in_both_forms():
    for line in ("M vaf/core/agent.py", " M vaf/core/agent.py", "MM web/package.json"):
        real, churn = _split_self_churn(line)
        assert real == [line] and churn == []


def test_mixed_output_splits_correctly():
    real, churn = _split_self_churn(STRIPPED + "\n M vaf/core/agent.py\n\n")
    assert real == [" M vaf/core/agent.py"]
    assert churn == ["web/package-lock.json"]


def test_empty_and_whitespace_output_is_clean():
    assert _split_self_churn("") == ([], [])
    assert _split_self_churn("\n  \n") == ([], [])


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "web").mkdir(parents=True)
    lock = root / "web" / "package-lock.json"
    lock.write_text('{"name": "release"}\n', encoding="utf-8")
    def g(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    g("init", "-q")
    g("config", "user.email", "test@test.local")
    g("config", "user.name", "test")
    g("add", "-A")
    g("commit", "-q", "-m", "release state")
    return root


def test_end_to_end_through_the_real_git_pipeline(tmp_path):
    """The full production path: a REAL dirty lockfile in a REAL git repo,
    status read through the updater's own _git (which strips), classified,
    and restored - the tree must end clean. This is the test shape that
    would have caught the a12 off-by-one."""
    root = _make_repo(tmp_path)
    (root / "web" / "package-lock.json").write_text('{"name": "churned"}\n', encoding="utf-8")

    code, dirty, _ = _git(root, "status", "--porcelain", "--untracked-files=no")
    assert code == 0 and dirty.strip()

    real, churn_paths = _split_self_churn(dirty)
    assert real == [], f"lockfile churn misclassified as real change: {real}"
    assert churn_paths == ["web/package-lock.json"]

    _restore_self_churn(root, churn_paths)
    code, dirty_after, _ = _git(root, "status", "--porcelain", "--untracked-files=no")
    assert code == 0 and dirty_after.strip() == ""
    assert (root / "web" / "package-lock.json").read_text(encoding="utf-8") == '{"name": "release"}\n'


def test_end_to_end_real_user_edit_still_aborts(tmp_path):
    root = _make_repo(tmp_path)
    (root / "real_source.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "real_source.py"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "src"], cwd=root, check=True, capture_output=True)
    (root / "real_source.py").write_text("x = 2\n", encoding="utf-8")

    _, dirty, _ = _git(root, "status", "--porcelain", "--untracked-files=no")
    real, churn_paths = _split_self_churn(dirty)
    assert churn_paths == []
    assert len(real) == 1 and real[0].endswith("real_source.py")
