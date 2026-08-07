# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Where VAF is allowed to put its log directory, and where it must not.

`get_app_log_dir` walks a candidate list and takes the first one it can create. The second
candidate was the directory one level above the `vaf` package - which in a git checkout is
the repository, and in a pip install is **site-packages**. An installed VAF was therefore
free to create a `logs/` folder inside the user's virtualenv, and something writing on every
tool call would do it reliably.

The fix is not to drop the candidate: only the tray pins `VAF_LOG_DIR`, so a `vaf run` from
a checkout depends on it, and removing it would split one machine's logs across two
directories with the Logs window reading only one of them. It is conditioned instead, on the
same marker `vaf/main.py` already uses to tell the two layouts apart - a `requirements.txt`
next to the package, which only the checkout and installer layouts ship.
"""
from pathlib import Path

from vaf.core.log_helper import get_app_log_dir, source_checkout_logs


def test_a_wheel_install_gets_no_logs_directory_next_to_the_package(tmp_path):
    """No requirements.txt above the package means site-packages, and the answer has to be
    "not here" - otherwise an embedded process creates logs/ inside the venv."""
    assert source_checkout_logs(tmp_path) is None


def test_a_source_checkout_keeps_its_repo_logs_directory(tmp_path):
    """The other half, and the reason the candidate is conditional rather than deleted."""
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    assert source_checkout_logs(tmp_path) == tmp_path / "logs"


def test_this_checkout_still_resolves_to_the_repo_logs_directory(monkeypatch):
    """End to end on the real layout, because the probe is only useful if it recognises the
    repository it is standing in."""
    monkeypatch.delenv("VAF_LOG_DIR", raising=False)
    repo = Path(__file__).resolve().parent.parent
    assert get_app_log_dir() == repo / "logs"
