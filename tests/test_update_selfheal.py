# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Updater self-heal contract (vaf/cli/cmd/update.py).

Live incident: the updater's own npm step rewrote web/package-lock.json;
the dirty-tree pre-check then aborted EVERY future update before doing
anything (a Mac sat on a7 with four newer releases available). Derived
artifacts must be classified as self-churn (restored), never as a reason
to abort - while real user edits keep aborting."""
from vaf.cli.cmd.update import _split_self_churn


def test_lockfile_churn_is_separated_from_real_changes():
    porcelain = " M web/package-lock.json\n M vaf/core/agent.py"
    real, churn = _split_self_churn(porcelain)
    assert churn == [" M web/package-lock.json"]
    assert real == [" M vaf/core/agent.py"]


def test_lockfile_only_dirt_means_clean_tree():
    real, churn = _split_self_churn(" M web/package-lock.json\n")
    assert real == [] and len(churn) == 1


def test_empty_and_whitespace_output_is_clean():
    assert _split_self_churn("") == ([], [])
    assert _split_self_churn("\n  \n") == ([], [])


def test_real_changes_still_abort():
    real, churn = _split_self_churn(" M web/package.json")
    assert real == [" M web/package.json"] and churn == []
