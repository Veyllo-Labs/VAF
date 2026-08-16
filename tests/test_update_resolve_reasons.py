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
`resolve_failure_message`, one honest sentence per reason. Both live in
`vaf/core/update_check.py` since the web UI started asking the same question;
the CLI is one of its callers and the wiring guards below watch both files.
"""
from types import SimpleNamespace

import requests

import vaf.cli.cmd.update as up
import vaf.core.update_check as uc


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

    monkeypatch.setattr(uc.requests, "get", fake)


# ── the reasons ─────────────────────────────────────────────────────────────────────

def test_rate_limited_is_named_with_its_reset_time(monkeypatch):
    """The headline. A 403 with the remaining-header at zero is GitHub saying
    "come back later" - not "offline" and not "no release"."""
    _get(monkeypatch, _resp(403, headers={"X-RateLimit-Remaining": "0",
                                          "X-RateLimit-Reset": "1754424000"}))
    rel, why = uc.resolve_latest_release(True)
    assert rel is None
    assert why.startswith("rate_limited:")
    msg = uc.resolve_failure_message(why)
    assert "rate limit" in msg
    assert "try again after" in msg, "the reset time from the header was dropped"


def test_a_real_permission_403_is_not_dressed_up_as_a_rate_limit(monkeypatch):
    """The remaining-header check is what keeps the message honest: a 403
    without it is a different problem and must say so."""
    _get(monkeypatch, _resp(403, headers={}))
    rel, why = uc.resolve_latest_release(True)
    assert why == "http:403"
    assert "rate limit" not in uc.resolve_failure_message(why)


def test_a_network_error_says_offline(monkeypatch):
    _get(monkeypatch, exc=requests.ConnectionError("no route"))
    rel, why = uc.resolve_latest_release(True)
    assert (rel, why) == (None, "offline")
    assert "reach GitHub" in uc.resolve_failure_message(why)


def test_an_empty_release_list_says_none_published(monkeypatch):
    _get(monkeypatch, _resp(200, json_data=[]))
    rel, why = uc.resolve_latest_release(True)
    assert (rel, why) == (None, "none")
    assert "No published release" in uc.resolve_failure_message(why)


def test_success_carries_no_reason(monkeypatch):
    _get(monkeypatch, _resp(200, json_data=[
        {"tag_name": "v0.1.0a20", "prerelease": True, "html_url": "u", "body": ""},
    ]))
    rel, why = uc.resolve_latest_release(True)
    assert why is None
    assert rel["tag"] == "v0.1.0a20"


def test_an_unparseable_reset_still_yields_a_sentence():
    """The header is attacker-adjacent input (any proxy can rewrite it); a bad
    value must degrade the message, not crash the updater."""
    msg = uc.resolve_failure_message("rate_limited:not-a-number")
    assert "rate limit" in msg and "try again after" not in msg


# ── the wiring (the stage is worthless if the callers keep the old sentence) ────────

def _sources():
    from pathlib import Path

    return {name: Path(mod.__file__).read_text(encoding="utf-8")
            for name, mod in (("update_check", uc), ("update", up))}


def test_the_one_size_fits_all_sentence_is_gone():
    src = _sources()
    assert src["update_check"].count("offline, or none published yet") == 1, (
        "the collapsed sentence is back in code (one mention lives in the "
        "resolver docstring as the record of why the reasons exist)"
    )
    assert "offline, or none published yet" not in src["update"], (
        "the collapsed sentence came back in the CLI"
    )
    assert "resolve_failure_message(why)" in src["update"], (
        "no caller asks for the reason"
    )


def test_every_caller_unpacks_the_tuple():
    """A caller still treating the result as a bare dict would be truthy-broken
    in the quiet direction: a (None, reason) tuple is truthy. Both files are
    scanned: the resolver lives in the framework now and the CLI is one caller
    among several, so a guard reading only one file would go blind."""
    found_any = False
    for name, src in _sources().items():
        calls = [line for line in src.splitlines()
                 if "resolve_latest_release(" in line and "def " not in line]
        for line in calls:
            if line.strip().startswith(("#", '"', "'")) or "`" in line:
                continue        # a docstring or comment naming the function
            found_any = True
            assert "=" in line and "," in line.split("=")[0], (
                f"{name}: caller does not unpack the (release, why) tuple: {line.strip()}"
            )
    assert found_any, "no callers found - the grep is broken"
