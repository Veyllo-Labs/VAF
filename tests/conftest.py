# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Shared test isolation.

VAF_LOG_DIR is pointed at a per-session temp directory for the WHOLE suite:
several code paths (security events, timeline, domain logs) write to the real
log directory as a side effect, and tests exercising them must never pollute
the developer's actual logs - the security dashboard counts those files as
real audit data (live incident: suite runs left synthetic skill_blocked events
in the production security log, making the "threats blocked today" counter lie).
Tests that need their own log dir still monkeypatch VAF_LOG_DIR per-test.
"""
import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_log_dir(tmp_path_factory):
    import os
    log_dir = tmp_path_factory.mktemp("vaf-test-logs")
    old = os.environ.get("VAF_LOG_DIR")
    os.environ["VAF_LOG_DIR"] = str(log_dir)
    yield
    if old is None:
        os.environ.pop("VAF_LOG_DIR", None)
    else:
        os.environ["VAF_LOG_DIR"] = old
