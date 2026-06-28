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
