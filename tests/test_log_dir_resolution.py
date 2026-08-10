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


def test_a_source_checkout_only_keeps_its_repo_logs_on_request(tmp_path, monkeypatch):
    """Checkout logs are OPT-IN now, and the reason is where checkouts live.

    Domain logs carry message text - the queue previews, the timeline's tool
    arguments, the search queries. A checkout can sit on any disk: on the machine
    where this was found, the repo was on an UNENCRYPTED partition while home and
    /var were LUKS, so every one of those files lay outside every protection the
    user had. The default target is the data dir under home; VAF_DEV_LOGS brings
    the old behaviour back for development.
    """
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")

    monkeypatch.delenv("VAF_DEV_LOGS", raising=False)
    assert source_checkout_logs(tmp_path) is None

    monkeypatch.setenv("VAF_DEV_LOGS", "1")
    assert source_checkout_logs(tmp_path) == tmp_path / "logs"


def test_this_checkout_resolves_into_the_data_dir_by_default(monkeypatch):
    """End to end on the real layout: logs follow the user's home, not the code."""
    from vaf.core.platform import Platform

    monkeypatch.delenv("VAF_LOG_DIR", raising=False)
    monkeypatch.delenv("VAF_DEV_LOGS", raising=False)
    repo = Path(__file__).resolve().parent.parent

    resolved = get_app_log_dir()

    assert resolved != repo / "logs"
    assert resolved == Platform.data_dir() / "logs"
