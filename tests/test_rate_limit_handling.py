# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Provider rate-limit (429) handling: the wait parser and the two retry budgets.

The live incident: an OpenAI TPM 429 whose body said "Please try again in 186ms"
surfaced to the user as a lost turn. Three gaps compounded: the old parser read
only an INTEGER Retry-After header (none was sent; "186ms" is not an integer),
the fallback backoff waited 1s+2s, and the attempt budget (2) ran out while the
per-org token window was still saturated by the coder streaming beside the chat.

OpenAI's limits, measured live and per docs: per-organization, per-model, in
requests and tokens per minute (this account: 500 RPM / 200k TPM on gpt-5.6);
state comes back in x-ratelimit-{limit,remaining,reset}-{requests,tokens}
headers whose reset values are DURATION strings ("120ms", "0s", "6m0s"), a 429
may carry a Retry-After header in seconds, and the docs say to treat the named
wait as a minimum plus jitter, bounding total time spent retrying.
"""
import pytest

from vaf.core.api_backend import (
    BaseAIProvider,
    OpenAIProvider,
    rate_limit_wait_seconds,
)

INCIDENT_BODY = ("Error code: 429 - {'error': {'message': 'Rate limit reached for "
                 "gpt-5.6-luna in organization org-000000000000000000000000 on tokens "
                 "per min (TPM): Limit 200000, Used 200000, Requested 621. Please try "
                 "again in 186ms. Visit https://platform.openai.com/account/rate-limits "
                 "to learn more.', 'type': 'tokens', 'code': 'rate_limit_exceeded'}}")


# ── The parser: every format the provider actually uses ───────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("30", 30.0),            # Retry-After: integer seconds
    ("1.5", 1.5),            # fractional seconds
    ("186ms", 0.186),        # the incident's own format
    ("6.13s", 6.13),
    ("0s", 0.0),             # measured on x-ratelimit-reset-tokens
    ("6m0s", 360.0),         # measured format from the docs
    ("1h2m", 3720.0),
])
def test_header_duration_formats(raw, expected):
    assert rate_limit_wait_seconds({"retry-after": raw}) == pytest.approx(expected)


def test_the_incident_message_yields_186ms():
    """No header, the wait only in the body - exactly the live failure."""
    assert rate_limit_wait_seconds(None, INCIDENT_BODY) == pytest.approx(0.186)


def test_header_wins_over_message():
    got = rate_limit_wait_seconds({"Retry-After": "2"}, INCIDENT_BODY)
    assert got == pytest.approx(2.0)


@pytest.mark.parametrize("headers,text", [
    (None, ""),
    ({}, "some unrelated 429 body"),
    ({"retry-after": "garbage"}, ""),
    ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, ""),  # HTTP-date form: unsupported, not a crash
])
def test_no_source_means_none_so_backoff_applies(headers, text):
    assert rate_limit_wait_seconds(headers, text) is None


# ── 429 detection ─────────────────────────────────────────────────────────────

class _Err(Exception):
    def __init__(self, msg="", status=None, headers=None):
        super().__init__(msg)
        self.status_code = status
        if headers is not None:
            self.response = type("R", (), {"headers": headers})()


def test_a_429_and_the_incident_text_are_rate_limits_a_500_is_not():
    assert BaseAIProvider._is_rate_limit(_Err(status=429))
    assert BaseAIProvider._is_rate_limit(_Err(INCIDENT_BODY))
    assert not BaseAIProvider._is_rate_limit(_Err("boom", status=500))


# ── The two budgets in _with_retry ────────────────────────────────────────────

def _provider(monkeypatch, sleeps):
    p = OpenAIProvider("openai", "k")
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    return p


def test_a_rate_limit_asking_for_milliseconds_is_survived(monkeypatch):
    """The incident, replayed: two 429s naming 186ms, then success. The old
    count-based budget survived this too - what it could not survive was a
    saturated window (below) - but the WAIT must now come from the message."""
    sleeps = []
    p = _provider(monkeypatch, sleeps)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _Err(INCIDENT_BODY, status=429)
        return "ok"

    assert p._with_retry(flaky) == "ok"
    assert calls["n"] == 3
    # each wait = 186ms plus a small jitter, never a 1s/2s backoff
    assert all(0.186 <= s < 0.5 for s in sleeps), sleeps


def test_429_is_budgeted_in_seconds_not_attempts(monkeypatch):
    """A saturated window outlives api_retry_attempts=2; the wall-clock budget
    keeps retrying far past that count and succeeds."""
    sleeps = []
    p = _provider(monkeypatch, sleeps)
    monkeypatch.setattr("vaf.core.api_backend.Config.get",
                        staticmethod(lambda k, d=None: {"api_retry_attempts": 2,
                                                        "api_rate_limit_wait_max": 60,
                                                        "api_retry_after_max": 30}.get(k, d)))
    calls = {"n": 0}

    def saturated():
        calls["n"] += 1
        if calls["n"] <= 6:  # far beyond the counted budget of 2
            raise _Err("rate limit", status=429, headers={"retry-after": "1"})
        return "ok"

    assert p._with_retry(saturated) == "ok"
    assert calls["n"] == 7


def test_the_429_budget_is_finite(monkeypatch):
    """A window that never drains must surface the error, not hang the lane."""
    sleeps = []
    p = _provider(monkeypatch, sleeps)
    monkeypatch.setattr("vaf.core.api_backend.Config.get",
                        staticmethod(lambda k, d=None: {"api_retry_attempts": 2,
                                                        "api_rate_limit_wait_max": 5,
                                                        "api_retry_after_max": 30}.get(k, d)))

    def always():
        raise _Err("rate limit", status=429, headers={"retry-after": "2"})

    with pytest.raises(_Err):
        p._with_retry(always)
    assert sum(sleeps) <= 5.0, "waited past api_rate_limit_wait_max"


def test_5xx_keeps_the_counted_budget(monkeypatch):
    """A broken server is not made whole by patience: attempts stay counted."""
    sleeps = []
    p = _provider(monkeypatch, sleeps)
    monkeypatch.setattr("vaf.core.api_backend.Config.get",
                        staticmethod(lambda k, d=None: {"api_retry_attempts": 2,
                                                        "api_rate_limit_wait_max": 60,
                                                        "api_retry_after_max": 30}.get(k, d)))
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise _Err("boom", status=503)

    with pytest.raises(_Err):
        p._with_retry(broken)
    assert calls["n"] == 3  # initial + api_retry_attempts


def test_budget_zero_disables_429_retries(monkeypatch):
    p = _provider(monkeypatch, [])
    monkeypatch.setattr("vaf.core.api_backend.Config.get",
                        staticmethod(lambda k, d=None: {"api_rate_limit_wait_max": 0,
                                                        "api_retry_attempts": 2,
                                                        "api_retry_after_max": 30}.get(k, d)))
    calls = {"n": 0}

    def limited():
        calls["n"] += 1
        raise _Err("rate limit", status=429)

    with pytest.raises(_Err):
        p._with_retry(limited)
    assert calls["n"] == 1


# ── Registered, admin-only, documented ────────────────────────────────────────

def test_the_budget_key_is_registered_admin_only_and_documented():
    import pathlib
    from vaf.core.config import Config
    assert "api_rate_limit_wait_max" in Config.DEFAULTS
    assert "api_rate_limit_wait_max" in Config.GLOBAL_CONFIG_KEYS, (
        "how long a call may stall the lane is an instance decision, like the "
        "other api_retry_* keys beside it"
    )
    schema = (pathlib.Path(__file__).resolve().parents[1]
              / "docs" / "setup" / "CONFIG_SCHEMA.md").read_bytes().decode("utf-8")
    assert "`api_rate_limit_wait_max`" in schema


# ── The coder's raw-HTTP lane wires to the same parser ────────────────────────
# It cannot ride _with_retry (it streams via requests.post), so it has its own
# branch; before it, a 429 fell through to the generic non-200 return and the
# whole run died on a wait of milliseconds.

def test_coder_handles_429_with_the_shared_parser():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "vaf" / "tools" / "coder.py").read_bytes().decode("utf-8")
    # The exact `if` spelling: an `.index("status_code == 429")` alone stayed green
    # when the branch was disabled with a guard in front of the comparison.
    branch_at = src.index("if stream_response.status_code == 429:")
    generic_at = src.index('return f"Error: Server {stream_response.status_code}')
    assert branch_at < generic_at, "the 429 branch must run before the generic non-200 return"
    block = src[branch_at:branch_at + 2200]
    assert "rate_limit_wait_seconds" in block, "the coder must reuse the shared parser"
    assert "api_rate_limit_wait_max" in block, "the coder must honor the same budget"
    assert "continue" in block, "a budgeted 429 must re-enter the loop, not kill the run"
