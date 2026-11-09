# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A chat the provider refused as too large compresses, so the next message goes through.

Live: a 1136-message chat on the Veyllo API answered every new message with HTTP 413
request_too_large, because the round failed without changing the history and the context
budget was set above the size the provider took."""
import inspect
from types import SimpleNamespace

from vaf.core import agent as agent_module
from vaf.core.agent import Agent, request_too_large

REFUSAL = ("[API Error from veyllo: Error code: 413 - {'error': {'message': 'The request is too "
           "large.', 'type': 'invalid_request_error', 'code': 'request_too_large', 'param': None}}]")


def test_only_a_too_large_request_counts():
    assert request_too_large(REFUSAL)
    assert request_too_large("This model's maximum context length is 128000 tokens")
    # Failures a smaller history does not fix are never answered by compressing.
    assert not request_too_large("[API Error from veyllo: Error code: 429 - rate limited]")
    assert not request_too_large("Error code: 401 - invalid api key")
    assert not request_too_large("Error code: 500 - internal error")
    assert not request_too_large("")


class _Cm:
    def __init__(self):
        self.compressed = 0

    def should_compress(self, history):
        return False            # below the configured budget, as in the live chat

    def compress(self, history, working_memory=None):
        self.compressed += 1
        return history[:1] + history[-2:]

    def get_usage_percent(self, history):
        return 0.5

    def estimate_tokens(self, history):
        return 100


def _fake_agent():
    cm = _Cm()
    return SimpleNamespace(
        context_manager=cm, history=[{"role": "system"}] + [{"role": "user"}] * 10,
        get_token_usage=lambda include_tools=False: (0, 128000),
        _sync_compression_limit=lambda window: None, main_persistence=None,
        _after_compaction=lambda old: None, current_session_id=None), cm


def test_force_compresses_below_the_budget():
    """MUTATION: drop `not force and` in _compress_history_if_needed - red."""
    fake, cm = _fake_agent()
    assert Agent._compress_history_if_needed(fake) is False
    assert cm.compressed == 0
    assert Agent._compress_history_if_needed(fake, force=True) is True
    assert cm.compressed == 1 and len(fake.history) == 3


def test_the_failed_round_compresses_on_a_too_large_refusal():
    """The failed-round branch asks the classifier and forces the compression.
    MUTATION: remove the request_too_large block after round_failed - red."""
    source = inspect.getsource(agent_module.Agent)
    branch = source[source.index('"round_failed: the backend'):]
    branch = branch[:branch.index("_end_turn_without_answer(_round_failure")]
    assert "request_too_large(_round_failure)" in branch
    assert "_compress_history_if_needed(force=True)" in branch


def test_a_refused_chat_keeps_a_lower_limit_across_reloads(monkeypatch):
    """Every turn reloads the chat from its file, so the compressed history alone did not
    last (measured: the next request was 75,000 tokens again). The chat keeps a limit below
    70,000 instead, lowered by each further refusal, never below the settings floor.
    MUTATION: drop the cap in _compression_limit - red."""
    from vaf.core.context import CONTEXT_EFFORT_MIN
    monkeypatch.setattr(agent_module, "_session_compress_caps", {})
    fake = SimpleNamespace(api_backend=object(), config={"context_compress_tokens": 1_000_000},
                           current_session_id="yellow012345")
    assert Agent._compression_limit(fake, 128_000) == 128_000
    assert agent_module.tighten_session_cap("yellow012345") == agent_module.REQUEST_TOO_LARGE_CAP
    assert agent_module.REQUEST_TOO_LARGE_CAP < 70_000
    assert Agent._compression_limit(fake, 128_000) == agent_module.REQUEST_TOO_LARGE_CAP
    # Another chat is not affected.
    other = SimpleNamespace(api_backend=object(), config={"context_compress_tokens": 1_000_000},
                            current_session_id="green123456")
    assert Agent._compression_limit(other, 128_000) == 128_000
    # Each further refusal lowers it, bounded by the floor.
    caps = [agent_module.tighten_session_cap("yellow012345") for _ in range(10)]
    assert caps[0] < agent_module.REQUEST_TOO_LARGE_CAP and caps[-1] == CONTEXT_EFFORT_MIN


def test_the_round_is_asked_again_once_without_showing_the_refusal():
    """The person must not send the message again: a refusal as too large, before any text,
    is not streamed, the history is compressed and the round runs again, once per turn.
    MUTATION: drop the `continue` or the single-use flag - red."""
    source = inspect.getsource(agent_module.Agent.chat_step)
    assert "_too_large_retried = False" in source
    stream_side = source[source.index("if is_failure_chunk(chunk):"):]
    stream_side = stream_side[:stream_side.index("full_response += chunk")]
    assert "request_too_large(chunk)" in stream_side and "not _too_large_retried" in stream_side
    assert "break" in stream_side
    retry = source[source.index("and request_too_large(_round_failure)):"):]
    retry = retry[:retry.index("if _round_failure:")]
    assert "_too_large_retried = True" in retry
    assert "_compress_history_if_needed(force=True)" in retry
    assert retry.rstrip().endswith("continue")
