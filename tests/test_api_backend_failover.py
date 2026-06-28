# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Failover safety for the provider chain.

A 4xx CLIENT error is a problem with the REQUEST, not a provider outage: failing over cannot help (the
same request fails everywhere) and, for a stateful gateway like Veyllo, it forwards provider-bound
tool_call ids to a provider that cannot honor them — turning one 400 into a cascade. Likewise, a
conversation that already carries tool_call ids (assistant tool_calls / tool results) must not fail over
mid-sequence. These tests pin the classification + decision logic.
"""
from vaf.core.api_backend import APIBackendManager as M


def test_classify_client_errors():
    for s in (
        "Error code: 400 - bad request",
        "error code: 401 unauthorized",
        "Error code: 403 forbidden",
        "error code: 404 not found",
        "Error code: 422 unprocessable",
        "... 'type': 'invalid_request_error' ...",
        "400 Bad Request for url",
    ):
        assert M._classify_failure(s) == "client_error", s


def test_classify_other_buckets_unchanged():
    assert M._classify_failure("Error code: 429 too many requests") == "rate_limit"
    assert M._classify_failure("Connection timed out") == "timeout"
    assert M._classify_failure("connection reset by peer") == "connection"
    assert M._classify_failure("Error code: 503 overloaded") == "server_error"
    assert M._classify_failure("Error code: 500 internal server error") == "server_error"
    assert M._classify_failure("something totally unexpected") == "unknown"


def test_429_stays_rate_limit_not_client_error():
    # 429 is handled before the 4xx client-error bucket so retry/backoff still applies.
    assert M._classify_failure("Error code: 429 - rate limit") == "rate_limit"


def test_messages_have_provider_bound_tool_calls():
    assert M._messages_have_provider_bound_tool_calls([{"role": "user", "content": "hi"}]) is False
    assert M._messages_have_provider_bound_tool_calls([{"role": "assistant", "content": "plain text"}]) is False
    assert M._messages_have_provider_bound_tool_calls([]) is False
    assert M._messages_have_provider_bound_tool_calls(None) is False
    # assistant message with tool_calls -> bound
    assert M._messages_have_provider_bound_tool_calls(
        [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]}]
    ) is True
    # a tool result references a prior tool_call id -> bound
    assert M._messages_have_provider_bound_tool_calls(
        [{"role": "tool", "tool_call_id": "call_1", "content": "x"}]
    ) is True


def test_should_not_failover_on_client_error():
    # 'local' provider needs no API key, so the manager constructs offline.
    m = M("local")
    assert m._should_failover_on("Error code: 400 - invalid_request_error") is False
    assert m._should_failover_on("Error code: 404 not found") is False
    # genuine provider outages still fail over (default failover_triggers is empty == any)
    assert m._should_failover_on("Error code: 503 overloaded") is True
    assert m._should_failover_on("connection refused") is True
    assert m._should_failover_on("request timed out") is True
