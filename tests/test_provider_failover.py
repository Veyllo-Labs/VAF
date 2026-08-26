# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Unit tests for the provider-failover engine in APIBackendManager.

The engine wraps the single-provider call: on a failure BEFORE the first token it
retries down a configured provider chain. Once a real token has streamed it must NOT
fail over (that would duplicate output). With failover off it is a transparent pass-through.
"""

import time

from vaf.core.api_backend import APIBackendManager


def _bare_manager(config=None, provider_name="primary"):
    """Build a manager WITHOUT constructing real provider SDK clients."""
    mgr = APIBackendManager.__new__(APIBackendManager)
    mgr.provider_name = provider_name
    mgr.config = dict(config or {})
    mgr.session_usage = {"input_tokens": 0, "output_tokens": 0}
    mgr.last_request_usage = {"input_tokens": 0, "output_tokens": 0}
    mgr._failover_pinned_idx = 0
    mgr._failover_open_until = {}
    return mgr


class _FakeLink:
    """Stand-in chain link whose _chat_single yields a fixed script of chunks."""

    def __init__(self, name, script):
        self.provider_name = name
        self._script = list(script)
        self.consumed = False
        self.last_request_usage = {"input_tokens": 3, "output_tokens": 5}

    def _chat_single(self, *args, **kwargs):
        self.consumed = True
        for chunk in self._script:
            yield chunk


SENTINEL = "[API Error from primary: connection refused]"
_MSGS = [{"role": "user", "content": "hi"}]


def test_failover_off_is_transparent_passthrough():
    mgr = _bare_manager({"failover_level": "off"})
    mgr._chat_single = lambda *a, **k: iter(["hello ", "world"])
    assert list(mgr.chat_completion(_MSGS)) == ["hello ", "world"]


def test_switches_on_pre_first_token_error():
    mgr = _bare_manager({"failover_level": "balanced", "failover_timeout_s": 0})
    primary = _FakeLink("primary", [SENTINEL])          # fails before any token
    backup = _FakeLink("backup", ["from ", "backup"])
    mgr._build_failover_chain = lambda model: [(primary, None), (backup, None)]
    out = list(mgr.chat_completion(_MSGS))
    assert out == ["from ", "backup"]
    assert backup.consumed is True
    assert SENTINEL not in out                           # the failed link's error is swallowed


def test_no_failover_after_first_token():
    # primary streams a good token THEN errors -> must NOT switch (would duplicate output)
    mgr = _bare_manager({"failover_level": "balanced", "failover_timeout_s": 0})
    primary = _FakeLink("primary", ["Hello", " world", SENTINEL])
    backup = _FakeLink("backup", ["SHOULD-NOT-RUN"])
    mgr._build_failover_chain = lambda model: [(primary, None), (backup, None)]
    out = list(mgr.chat_completion(_MSGS))
    assert out == ["Hello", " world", SENTINEL]
    assert backup.consumed is False


def test_last_link_error_is_surfaced():
    mgr = _bare_manager({"failover_level": "basic", "failover_timeout_s": 0})
    primary = _FakeLink("primary", [SENTINEL])
    local = _FakeLink("local", ["[API Error from local: no model loaded]"])
    mgr._build_failover_chain = lambda model: [(primary, None), (local, None)]
    out = list(mgr.chat_completion(_MSGS))
    assert any("API Error" in c for c in out)            # both failed -> user still sees an error


def test_usage_is_mirrored_from_fallback_link():
    mgr = _bare_manager({"failover_level": "balanced", "failover_timeout_s": 0})
    primary = _FakeLink("primary", [SENTINEL])
    backup = _FakeLink("backup", ["ok"])
    mgr._build_failover_chain = lambda model: [(primary, None), (backup, None)]
    list(mgr.chat_completion(_MSGS))
    assert mgr.last_request_usage["output_tokens"] == 5  # copied from the link that answered


def test_sticky_pin_when_return_to_primary_off():
    mgr = _bare_manager({"failover_level": "balanced", "failover_return_to_primary": False,
                         "failover_timeout_s": 0})
    primary = _FakeLink("primary", [SENTINEL])
    backup = _FakeLink("backup", ["ok"])
    mgr._build_failover_chain = lambda model: [(primary, None), (backup, None)]
    list(mgr.chat_completion(_MSGS))
    assert mgr._failover_pinned_idx == 1                 # stays on the working backup link


def test_return_to_primary_resets_pin():
    mgr = _bare_manager({"failover_level": "balanced", "failover_return_to_primary": True,
                         "failover_timeout_s": 0})
    primary = _FakeLink("primary", [SENTINEL])
    backup = _FakeLink("backup", ["ok"])
    mgr._build_failover_chain = lambda model: [(primary, None), (backup, None)]
    list(mgr.chat_completion(_MSGS))
    assert mgr._failover_pinned_idx == 0                 # always retry primary first next time


def test_failover_on_slow_first_token_via_timeout():
    mgr = _bare_manager({"failover_level": "balanced", "failover_timeout_s": 0.05})

    class _SlowLink(_FakeLink):
        def _chat_single(self, *a, **k):
            self.consumed = True
            time.sleep(0.4)                              # slower than the 0.05s deadline
            yield "too late"

    primary = _SlowLink("primary", [])
    backup = _FakeLink("backup", ["fast backup"])
    mgr._build_failover_chain = lambda model: [(primary, None), (backup, None)]
    assert list(mgr.chat_completion(_MSGS)) == ["fast backup"]


def test_classify_and_trigger_gating():
    mgr = _bare_manager()
    assert mgr._classify_failure("HTTP 429 too many requests") == "rate_limit"
    assert mgr._classify_failure("Read timed out") == "timeout"
    assert mgr._classify_failure("503 Service Unavailable") == "server_error"
    assert mgr._classify_failure("connection refused") == "connection"

    mgr.config = {"failover_triggers": []}               # empty -> any error fails over
    assert mgr._should_failover_on("HTTP 429") is True

    mgr.config = {"failover_triggers": ["timeout"]}      # explicit list gates rate_limit out
    assert mgr._should_failover_on("HTTP 429 rate limit") is False
    assert mgr._should_failover_on("Read timed out") is True
    assert mgr._should_failover_on("connection refused") is True   # connection always fails over


def test_build_chain_off_returns_primary_only():
    mgr = _bare_manager({"failover_level": "off"})
    chain = mgr._build_failover_chain("m")
    assert len(chain) == 1 and chain[0][0] is mgr


# ═══════════════════════════════════════════════════════════════════════════════
# The dead-link breaker: failing over says "the primary is down"; on its own it
# never says "is it back yet". These pin both halves of that answer.
# ═══════════════════════════════════════════════════════════════════════════════

DEAD = "[API Error from primary: connection refused]"
_TOOL_MSGS = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "tool_calls": [{"id": "call_00_x"}]},
    {"role": "tool", "tool_call_id": "call_00_x", "content": "r"},
]


class _CountingLink(_FakeLink):
    """A link that counts attempts and whose script can change mid-test (recovery)."""

    def __init__(self, name, script, sleep=0.0):
        super().__init__(name, script)
        self.sleep = sleep
        self.calls = 0

    def _chat_single(self, *args, **kwargs):
        self.calls += 1
        if self.sleep:
            time.sleep(self.sleep)
        for chunk in self._script:
            yield chunk


def _breaker_manager(extra=None, clock=None):
    cfg = {"failover_level": "balanced", "failover_timeout_s": 0,
           "failover_return_to_primary": True, "failover_recheck_after_s": 300}
    cfg.update(extra or {})
    mgr = _bare_manager(cfg)
    if clock is not None:
        mgr._clock = clock
    return mgr


def _chain(mgr, primary, backup):
    mgr._build_failover_chain = lambda model: [(primary, None), (backup, None)]


def test_a_dead_link_is_not_paid_for_on_every_request():
    """The default mode always restarted at the primary, so recovery was automatic
    but every request first paid the primary's failure again. Measured against a
    primary that hangs: three requests, three attempts, one full failover_timeout_s
    of pure waiting each (30s by default)."""
    now = [1000.0]
    mgr = _breaker_manager(clock=lambda: now[0])
    primary, backup = _CountingLink("primary", [DEAD]), _CountingLink("backup", ["ok"])
    _chain(mgr, primary, backup)
    for _ in range(4):
        assert list(mgr.chat_completion(_MSGS)) == ["ok"]
        now[0] += 10.0                       # well inside the 300s window
    assert primary.calls == 1, (
        f"the dead primary was attempted {primary.calls}x across four requests; "
        "the breaker is meant to make that once until the recheck window passes"
    )


def test_the_next_ordinary_request_is_the_probe_once_the_window_passes():
    """Half-open without a timer: nothing pings the provider, the next real request
    simply finds it a candidate again. Success clears the breaker."""
    now = [1000.0]
    mgr = _breaker_manager(clock=lambda: now[0])
    primary, backup = _CountingLink("primary", [DEAD]), _CountingLink("backup", ["ok"])
    _chain(mgr, primary, backup)
    assert list(mgr.chat_completion(_MSGS)) == ["ok"]
    assert mgr._failover_open_until.get("primary") == 1300.0

    primary._script = ["primary is back"]
    now[0] += 299.0                          # one second short of the window
    assert list(mgr.chat_completion(_MSGS)) == ["ok"], "probed before the window passed"
    now[0] += 2.0                            # window passed
    assert list(mgr.chat_completion(_MSGS)) == ["primary is back"]
    assert "primary" not in mgr._failover_open_until, "a link that answered stays marked dead"


def test_a_failed_probe_re_arms_the_window_instead_of_retrying_every_request():
    now = [1000.0]
    mgr = _breaker_manager(clock=lambda: now[0])
    primary, backup = _CountingLink("primary", [DEAD]), _CountingLink("backup", ["ok"])
    _chain(mgr, primary, backup)
    list(mgr.chat_completion(_MSGS))
    now[0] += 301.0
    list(mgr.chat_completion(_MSGS))          # probe, still dead
    assert primary.calls == 2
    assert mgr._failover_open_until.get("primary") == now[0] + 300.0
    now[0] += 10.0
    list(mgr.chat_completion(_MSGS))
    assert primary.calls == 2, "a failed probe did not re-arm the window"


def test_sticky_mode_is_no_longer_a_dead_end():
    """With failover_return_to_primary OFF the pin used to be permanent: measured as
    five requests after the primary had recovered and zero attempts on it. Expiring
    the breaker alone does not fix it either - the primary becomes a candidate again
    but sits BEHIND the healthy backup, so it is never reached. The pin has to walk
    back over links whose window has passed."""
    now = [1000.0]
    mgr = _breaker_manager({"failover_return_to_primary": False}, clock=lambda: now[0])
    primary, backup = _CountingLink("primary", [DEAD]), _CountingLink("backup", ["ok"])
    _chain(mgr, primary, backup)
    assert list(mgr.chat_completion(_MSGS)) == ["ok"]
    assert mgr._failover_pinned_idx == 1                  # still sticky, as documented

    primary._script = ["primary is back"]
    now[0] += 100.0
    assert list(mgr.chat_completion(_MSGS)) == ["ok"], "left the working link too early"
    now[0] += 250.0                                       # window passed
    assert list(mgr.chat_completion(_MSGS)) == ["primary is back"]


def test_a_client_error_does_not_arm_the_breaker():
    """A 4xx is a problem with the REQUEST, not an outage. Marking the provider dead
    for five minutes over a malformed call would take a healthy provider out of the
    chain."""
    now = [1000.0]
    mgr = _breaker_manager(clock=lambda: now[0])
    primary = _CountingLink("primary", ["[API Error from primary: Error code: 400 invalid_request_error]"])
    backup = _CountingLink("backup", ["ok"])
    _chain(mgr, primary, backup)
    list(mgr.chat_completion(_MSGS))
    assert mgr._failover_open_until == {}, "a 4xx marked the provider dead"
    list(mgr.chat_completion(_MSGS))
    assert primary.calls == 2, "a 4xx took the primary out of the chain"


def test_the_whole_chain_cooling_down_still_attempts_the_request():
    """Skipping must never skip everything: a request with no link left to try would
    silently produce nothing at all."""
    mgr = _breaker_manager(clock=lambda: 1000.0)
    mgr._failover_open_until = {"primary": 9e9, "backup": 9e9}
    primary, backup = _CountingLink("primary", [DEAD]), _CountingLink("backup", ["ok"])
    _chain(mgr, primary, backup)
    assert list(mgr.chat_completion(_MSGS)) == ["ok"]


def test_never_skips_while_the_history_carries_provider_bound_tool_calls():
    """Skipping is a silent hand-over to the next provider, and that is exactly the
    cascade the tool-call guard exists to prevent: a stateful gateway 400s on ids it
    never issued. Mid-tool-sequence the chain is walked in full."""
    now = [1000.0]
    mgr = _breaker_manager(clock=lambda: now[0])
    primary, backup = _CountingLink("primary", [DEAD]), _CountingLink("backup", ["ok"])
    _chain(mgr, primary, backup)
    list(mgr.chat_completion(_MSGS))                      # breaker now open on primary
    before = primary.calls
    list(mgr.chat_completion(_TOOL_MSGS))
    assert primary.calls == before + 1, (
        "a conversation carrying provider-bound tool_call ids was handed to the next "
        "provider without the primary even being tried"
    )


def test_recheck_disabled_restores_the_previous_behaviour_exactly():
    now = [1000.0]
    mgr = _breaker_manager({"failover_recheck_after_s": 0}, clock=lambda: now[0])
    primary, backup = _CountingLink("primary", [DEAD]), _CountingLink("backup", ["ok"])
    _chain(mgr, primary, backup)
    for _ in range(3):
        list(mgr.chat_completion(_MSGS))
    assert primary.calls == 3, "with the recheck off the primary must be tried every time"
    assert mgr._failover_open_until == {}


def test_sticky_pin_stays_permanent_when_the_recheck_is_disabled():
    """The setting's documented behaviour before the breaker existed, kept reachable."""
    now = [1000.0]
    mgr = _breaker_manager({"failover_return_to_primary": False,
                            "failover_recheck_after_s": 0}, clock=lambda: now[0])
    primary, backup = _CountingLink("primary", [DEAD]), _CountingLink("backup", ["ok"])
    _chain(mgr, primary, backup)
    list(mgr.chat_completion(_MSGS))
    primary._script = ["primary is back"]
    now[0] += 10_000.0
    assert list(mgr.chat_completion(_MSGS)) == ["ok"], "the pin is no longer permanent with the recheck off"


def test_the_recheck_key_is_registered_and_admin_only():
    """A failover_* key inherits admin-only from the prefix list; a key named outside
    that prefix would be writable by any LAN user (config.py ADMIN_ONLY prefixes)."""
    from vaf.core.config import Config
    assert "failover_recheck_after_s" in Config.DEFAULTS
    assert Config.DEFAULTS["failover_recheck_after_s"] == 300
