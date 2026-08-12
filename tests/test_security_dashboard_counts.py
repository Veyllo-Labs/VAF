# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Logs window must mark WHERE a notification came from.

Two counters used to answer "how many security events today" differently. The
sidebar dot asked the security log and got the truth; the Logs window added up
the firewall, channel and skill module counters, which excludes every event kind
that belongs to no module. A kind outside those modules therefore lit the
outside badge and marked nothing inside - a notification with no source, which
is what a user reported after `default_db_password` fired for the first time.

Both read the same number now, so the drift cannot come back for the next kind
either. This test pins that property against the event kinds that have no module
of their own.
"""
import pytest

from vaf.core.security_events import SECURITY_EVENT_KINDS

# Kinds the dashboard's three module counters do NOT cover. Anything here is a
# kind that used to be invisible inside the window.
MODULELESS_KINDS = ("default_db_password", "cli_password_gate_failed")


@pytest.mark.parametrize("kind", MODULELESS_KINDS)
def test_the_kind_is_a_declared_security_event(kind):
    """A typo here would make the rest of the file pass while proving nothing."""
    assert kind in SECURITY_EVENT_KINDS


def test_the_overview_reports_the_number_the_log_actually_holds(monkeypatch):
    """MUTATION: drop `security_events_today` from the payload and this goes red.

    The window then falls back to the module sum, which is zero for these kinds -
    exactly the reported symptom.
    """
    import asyncio

    from vaf.api import security_routes

    events = [
        {"ts": "2026-08-12T17:51:24.642367", "kind": "default_db_password",
         "detail": "The memory database is using the published default password"},
        {"ts": "2026-08-12T17:52:00.000000", "kind": "cli_password_gate_failed",
         "detail": "three wrong attempts"},
    ]
    monkeypatch.setattr(security_routes, "read_security_events", lambda *a, **k: events)
    for name in ("collect_sandbox_status", "collect_channels_status",
                 "collect_guardrails_status", "collect_skills_status"):
        monkeypatch.setattr(security_routes, name, lambda *a, **k: None, raising=False)

    payload = asyncio.run(security_routes.security_overview(_={"role": "admin"}))

    assert payload["security_events_today"] == 2, (
        "the window would derive 0 from the module counters and mark nothing")
    assert payload["security_latest_ts"] == events[-1]["ts"]
