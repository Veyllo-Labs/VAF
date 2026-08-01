# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Checking a key must say WHICH kind of failure it was, and must never destroy the key.

Saving a wrong key was indistinguishable from saving a right one until the next chat turn
failed, which is the wrong place to learn it: the answer arrives as a chat error, detached
from the screen that caused it. The check closes that gap.

THE DISTINCTION EVERYTHING HERE RESTS ON. A provider REJECTING the key (401/403) is a fact
about the key. A timeout, a DNS failure or a 502 is a fact about the network or about the
provider's day. Both are "the check did not succeed", and only the first says anything about
what the user typed. Collapse them and an outage renders as "your key is wrong", sending
someone to re-issue a key that was never the problem - and if anything downstream ever acts
on that verdict, an outage would start deleting good keys.

The other half is what the check does NOT do: nothing. It reports. A check is not a
revocation, and handing a provider's bad afternoon the power to undo a correct key would be
a worse defect than the one it fixes.

The key never travels to the browser either: the endpoint resolves it server-side, so the
answer to "is my stored key any good" does not require the stored key to be handed out.
"""
import asyncio

import pytest

from vaf.core.api_keys import store_api_key


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _Client:
    """Stand-in for httpx.AsyncClient. Records what was sent so the auth shape is checked."""

    seen: dict = {}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        _Client.seen = {"url": url, "headers": dict(headers or {}), "params": dict(params or {})}
        if _Client.raises is not None:
            raise _Client.raises
        return _Resp(_Client.status, _Client.body)


_Client.status = 200
_Client.raises = None
_Client.body = ""


@pytest.fixture(autouse=True)
def _reset():
    _Client.status, _Client.raises, _Client.body, _Client.seen = 200, None, "", {}
    yield


def _check(provider: str, monkeypatch):
    import httpx

    from vaf.api.config_routes import check_api_key

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return asyncio.run(check_api_key(provider, _={"role": "admin"}))


def test_a_usable_key_reports_ok(monkeypatch):
    store_api_key("openai", "sk-good")
    assert (_check("openai", monkeypatch))["result"] == "ok"


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_is_reported_as_rejected_with_its_status(status, monkeypatch):
    """The status is carried through because the user is shown the number."""
    store_api_key("openai", "sk-wrong")
    _Client.status = status

    out = _check("openai", monkeypatch)

    assert out["result"] == "rejected"
    assert out["status"] == status


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_a_provider_having_a_bad_day_is_NOT_a_verdict_on_the_key(status, monkeypatch):
    """The assertion this file exists for.

    Anything that is not an authentication refusal says nothing about the key. If these ever
    collapse into "rejected", a five-minute outage tells every user their key is broken.
    """
    store_api_key("openai", "sk-fine")
    _Client.status = status

    out = _check("openai", monkeypatch)

    assert out["result"] == "unreachable", f"HTTP {status} was read as a verdict on the key"


def test_googles_400_with_the_invalid_key_marker_IS_a_verdict(monkeypatch):
    """Google refuses a bad key with 400, not 401 - measured 2026-08-01, and found live.

    The owner mistyped a Google key and was told "could not reach the provider": this
    endpoint committed the exact confusion it exists to prevent, from the other side. The
    response body carried `"reason": "API_KEY_INVALID"` - Google's machine-readable marker -
    and that, not the bare status, is what makes a 400 a fact about the key.
    """
    store_api_key("google", "AIzaSy-wrong")
    _Client.status = 400
    _Client.body = (
        '{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",'
        ' "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}'
    )

    out = _check("google", monkeypatch)

    assert out["result"] == "rejected", "Google's invalid-key answer was read as an outage"
    assert out["status"] == 400


def test_a_bare_400_without_the_marker_stays_an_outage(monkeypatch):
    """The half that keeps the Google fix honest.

    A 400 with no auth marker can be a malformed request or a proxy answering in the
    provider's place; reading every 400 as "your key is wrong" would re-create the collapse
    this file forbids, one status code at a time.
    """
    store_api_key("google", "AIzaSy-fine")
    _Client.status = 400
    _Client.body = '{"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad page size"}}'

    out = _check("google", monkeypatch)

    assert out["result"] == "unreachable", "a markerless 400 was read as a verdict on the key"


def test_a_network_failure_is_not_a_verdict_either(monkeypatch):
    store_api_key("openai", "sk-fine")
    _Client.raises = OSError("name resolution failed")

    out = _check("openai", monkeypatch)

    assert out["result"] == "unreachable"


@pytest.mark.parametrize("status", [200, 401, 500])
def test_the_check_never_removes_or_changes_the_key(status, monkeypatch):
    """A check is not a revocation - not even when the provider says the key is bad.

    Acting on the verdict would mean an outage, a rate limit or a provider incident could
    undo a key the user typed correctly. Telling them is the whole job.
    """
    from vaf.core.api_keys import resolve_api_key

    store_api_key("openai", "sk-untouched")
    _Client.status = status

    _check("openai", monkeypatch)

    assert resolve_api_key("openai") == "sk-untouched"


def test_no_key_configured_is_its_own_answer(monkeypatch):
    """Not "rejected": nothing was refused, there was nothing to refuse."""
    assert (_check("openai", monkeypatch))["result"] == "missing"


def test_a_provider_with_no_listing_says_so_instead_of_claiming_ok(monkeypatch):
    """"ok" would be a claim nothing measured - the honesty floor used across the dashboard."""
    store_api_key("local", "sk-x")
    assert (_check("local", monkeypatch))["result"] == "unsupported"


def test_the_stored_key_is_sent_to_the_provider_and_not_to_the_caller(monkeypatch):
    """It authenticates with the key, and the response carries no trace of it."""
    store_api_key("openai", "sk-secret-value")

    out = _check("openai", monkeypatch)

    assert "sk-secret-value" in str(_Client.seen), "the check did not authenticate at all"
    assert "sk-secret-value" not in repr(out), "the endpoint handed the key back to the caller"


def test_an_unreadable_store_is_reported_as_such(monkeypatch):
    """Same three-state honesty as the listing: broken is not the same as missing."""
    import vaf.api.config_routes as routes
    import vaf.core.api_keys as api_keys

    def _boom(*a, **kw):
        raise api_keys.ApiKeyUnavailable("payload damaged")

    monkeypatch.setattr(api_keys, "resolve_api_key", _boom)
    assert routes  # the route imports the function at call time, so patching the module works
    assert (_check("openai", monkeypatch))["result"] == "unreadable"
