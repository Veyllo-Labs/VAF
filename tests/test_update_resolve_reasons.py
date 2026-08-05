# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Why the updater could not answer, said as the reason it actually was.

THE INCIDENT. Minutes after v0.1.0a20 went live, `vaf update` on the same
machine said "Could not determine the latest release (offline, or none
published yet)" - twice. Neither half was true: GitHub's ANONYMOUS API limit
is 60 requests/hour per IP, shared by every process on the network, and a CI
watcher on the same connection had just spent all 60. Three different
situations collapsed into one sentence whose two halves suggest OPPOSITE
reactions (check your network vs. nothing to wait for), and the real one -
wait until the limit resets - was neither.

The resolver now returns `(release, why)` and the messages come from
`_resolve_failure_message`, one honest sentence per reason.
"""
from types import SimpleNamespace

import pytest
import requests

import vaf.cli.cmd.update as up


def _resp(status=200, json_data=None, headers=None):
    return SimpleNamespace(
        status_code=status,
        headers=headers or {},
        json=lambda: json_data if json_data is not None else [],
    )


def _get(monkeypatch, resp=None, exc=None):
    def fake(url, **kw):
        if exc is not None:
            raise exc
        return resp

    monkeypatch.setattr(up.requests, "get", fake)


# ── the reasons ─────────────────────────────────────────────────────────────────────

def test_rate_limited_is_named_with_its_reset_time(monkeypatch):
    """The headline. A 403 with the remaining-header at zero is GitHub saying
    "come back later" - not "offline" and not "no release"."""
    _get(monkeypatch, _resp(403, headers={"X-RateLimit-Remaining": "0",
                                          "X-RateLimit-Reset": "1754424000"}))
    rel, why = up._resolve_latest_release(True)
    assert rel is None
    assert why.startswith("rate_limited:")
    msg = up._resolve_failure_message(why)
    assert "rate limit" in msg
    assert "try again after" in msg, "the reset time from the header was dropped"


def test_a_real_permission_403_is_not_dressed_up_as_a_rate_limit(monkeypatch):
    """The remaining-header check is what keeps the message honest: a 403
    without it is a different problem and must say so."""
    _get(monkeypatch, _resp(403, headers={}))
    rel, why = up._resolve_latest_release(True)
    assert why == "http:403"
    assert "rate limit" not in up._resolve_failure_message(why)


def test_a_network_error_says_offline(monkeypatch):
    _get(monkeypatch, exc=requests.ConnectionError("no route"))
    rel, why = up._resolve_latest_release(True)
    assert (rel, why) == (None, "offline")
    assert "reach GitHub" in up._resolve_failure_message(why)


def test_an_empty_release_list_says_none_published(monkeypatch):
    _get(monkeypatch, _resp(200, json_data=[]))
    rel, why = up._resolve_latest_release(True)
    assert (rel, why) == (None, "none")
    assert "No published release" in up._resolve_failure_message(why)


def test_success_carries_no_reason(monkeypatch):
    _get(monkeypatch, _resp(200, json_data=[
        {"tag_name": "v0.1.0a20", "prerelease": True, "html_url": "u", "body": ""},
    ]))
    rel, why = up._resolve_latest_release(True)
    assert why is None
    assert rel["tag"] == "v0.1.0a20"


def test_an_unparseable_reset_still_yields_a_sentence():
    """The header is attacker-adjacent input (any proxy can rewrite it); a bad
    value must degrade the message, not crash the updater."""
    msg = up._resolve_failure_message("rate_limited:not-a-number")
    assert "rate limit" in msg and "try again after" not in msg


# ── the wiring (the stage is worthless if the callers keep the old sentence) ────────

def test_the_one_size_fits_all_sentence_is_gone():
    from pathlib import Path

    src = (Path(up.__file__)).read_text(encoding="utf-8")
    body = src.split('"""', 2)[-1]          # skip module+function docstrings text
    assert src.count("offline, or none published yet") == 1, (
        "the collapsed sentence is back in code (one mention lives in the "
        "resolver docstring as the record of why the reasons exist)"
    )
    assert "_resolve_failure_message(why)" in body, "no caller asks for the reason"


def test_every_caller_unpacks_the_tuple():
    """A caller still treating the result as a bare dict would be truthy-broken
    in the quiet direction: a (None, reason) tuple is truthy."""
    from pathlib import Path

    src = Path(up.__file__).read_text(encoding="utf-8")
    calls = [l for l in src.splitlines()
             if "_resolve_latest_release(" in l and "def " not in l]
    assert calls, "no callers found - the grep is broken"
    for line in calls:
        assert "=" in line and "," in line.split("=")[0], (
            f"caller does not unpack the (release, why) tuple: {line.strip()}"
        )
