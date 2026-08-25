# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A reply cut short by the output limit has to leave a trace somewhere.

When a provider stops generating because the output budget ran out, it says so:
`length` on every OpenAI-shaped lane, `max_tokens` on Anthropic, `MAX_TOKENS` on
Google. The agent saw that value in exactly one place, printed a line to the
terminal and did nothing else, so a user on the web UI got a reply that simply
stopped mid-thought and no file anywhere recorded that it had.

That is worse than a missing feature. It makes the failure unmeasurable: there
is no way to ask how often it happens, and therefore no number against which to
size a fix. These tests pin the recording end to end - provider, per-call
record, ledger line - so the question can be answered before anything is built
on the answer.

One thing is deliberately NOT done, and it is pinned here too: the
non-streaming lane records the reason without yielding it. Callers of that lane
join the chunks into the visible reply, so a JSON blob there is shown to the
user as text. That exact defect has been fixed twice in this repo already.
"""
import pytest

from vaf.core.cost import blank_request_usage, was_truncated

_MSGS = [{"role": "user", "content": "hi"}]


# ─────────────────────────────────────────────────────────────────────────────
# The vocabulary
# ─────────────────────────────────────────────────────────────────────────────

class _Enum:
    """An SDK enum, which renders as "FinishReason.MAX_TOKENS" rather than bare."""

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return f"FinishReason.{self.name}"


@pytest.mark.parametrize("value", ["length", "LENGTH", "max_tokens", "MAX_TOKENS",
                                   _Enum("MAX_TOKENS"), "FinishReason.MAX_TOKENS"])
def test_the_vocabulary_reads_every_provider_spelling(value):
    """Three providers, three words, one enum wrapper. MUTATION: compare the raw
    string and the Google lane stops reporting truncation at all."""
    assert was_truncated(value) is True


@pytest.mark.parametrize("value", [None, "", "stop", "tool_calls", "end_turn",
                                   "STOP", "content_filter", 0, object()])
def test_a_normal_ending_is_not_a_truncation(value):
    """An unknown spelling degrades to "not truncated", which is the safe
    direction: this signal exists to be believed when it fires."""
    assert was_truncated(value) is False


def test_the_blank_shape_carries_a_finish_reason():
    """MUTATION: drop the key and a call the provider says nothing about
    inherits the previous call's answer, because these dicts are updated key by
    key and only the blank shape resets them."""
    blank = blank_request_usage()
    assert "finish_reason" in blank
    assert blank["finish_reason"] is None


# ─────────────────────────────────────────────────────────────────────────────
# The ledger line
# ─────────────────────────────────────────────────────────────────────────────

def _record(monkeypatch, **kwargs):
    from vaf.core import cost as cost_mod
    written = []
    monkeypatch.setattr(cost_mod, "record_spend", lambda *a, **k: 0.0)
    monkeypatch.setattr("vaf.core.log_helper.append_usage_log",
                        lambda m: written.append(m))
    cost_mod.record_call("openai", "gpt-4o-mini", 900, 50, lane="main", **kwargs)
    assert written, "nothing was logged at all"
    return written[0]


def test_a_truncated_call_says_so_in_the_log(monkeypatch):
    """The whole point of the round: the event becomes countable."""
    assert " cut=length" in _record(monkeypatch, finish_reason="length")


def test_a_normal_call_leaves_the_line_exactly_as_it_was(monkeypatch):
    """A signal that fires on every line is not a signal. The line must be
    byte-identical to the one a caller that never reports a reason produces."""
    quiet = _record(monkeypatch, finish_reason="stop")
    silent = _record(monkeypatch)
    assert quiet == silent
    assert "cut=" not in quiet


# ─────────────────────────────────────────────────────────────────────────────
# Capture, one lane at a time
# ─────────────────────────────────────────────────────────────────────────────

class _Usage:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _Delta:
    content = None
    tool_calls = None
    reasoning_content = None


class _StreamChoice:
    def __init__(self, finish_reason):
        self.delta = _Delta()
        self.finish_reason = finish_reason


class _StreamChunk:
    def __init__(self, finish_reason):
        self.choices = [_StreamChoice(finish_reason)]
        self.usage = None


class _Msg:
    content = "cut off half way th"
    tool_calls = None


class _Response:
    def __init__(self, finish_reason):
        choice = _Usage(message=_Msg(), finish_reason=finish_reason)
        self.choices = [choice]
        self.usage = None


def _openai_provider(client_returns):
    from vaf.core.api_backend import OpenAIProvider
    provider = OpenAIProvider("openai", "dummy-key")
    provider._create_with_retry = lambda kwargs: client_returns
    return provider


@pytest.mark.parametrize("stream", [True, False])
def test_an_openai_shaped_provider_records_why_it_stopped(stream):
    """Streaming and non-streaming are separate code, so both are driven. One
    class serves openai, deepseek, openrouter, veyllo and the local lane."""
    returns = iter([_StreamChunk("length")]) if stream else _Response("length")
    provider = _openai_provider(returns)
    list(provider.chat_completion(_MSGS, 0.7, 128, stream, "gpt-4o", None))
    assert provider.last_request_usage["finish_reason"] == "length"
    assert was_truncated(provider.last_request_usage["finish_reason"])


def test_the_non_stream_lane_does_not_yield_the_reason_as_text():
    """THE regression this lane has already suffered twice: a caller with
    stream=False joins the chunks into the reply, so a yielded JSON blob is
    shown to the user. The reason is recorded and not emitted."""
    provider = _openai_provider(_Response("length"))
    out = "".join(str(c) for c in
                  provider.chat_completion(_MSGS, 0.7, 128, False, "gpt-4o", None))
    assert "finish_reason" not in out, out
    assert out == _Msg.content
    assert provider.last_request_usage["finish_reason"] == "length"


def test_anthropic_records_why_it_stopped():
    from vaf.core.api_backend import AnthropicProvider

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.usage = {"input_tokens": 0, "output_tokens": 0}
    provider.last_request_usage = blank_request_usage()
    final = _Usage(stop_reason="max_tokens", content=[],
                   usage=_Usage(input_tokens=12, output_tokens=8192))
    list(provider._emit_final(final, thinking_active=False))
    assert provider.last_request_usage["finish_reason"] == "max_tokens"
    assert was_truncated(provider.last_request_usage["finish_reason"])


def test_google_records_why_it_stopped():
    from vaf.core.api_backend import GoogleProvider

    provider = GoogleProvider.__new__(GoogleProvider)
    provider.usage = {"input_tokens": 0, "output_tokens": 0}
    provider.last_request_usage = blank_request_usage()
    resp = _Usage(candidates=[_Usage(finish_reason=_Enum("MAX_TOKENS"))],
                  usage_metadata=_Usage(prompt_token_count=100,
                                        candidates_token_count=8192,
                                        thoughts_token_count=0))
    provider._record_usage(resp)
    assert was_truncated(provider.last_request_usage["finish_reason"])


def test_a_provider_that_says_nothing_leaves_the_reason_unset():
    """The old-server case. Absent must read as absent, never as "stopped
    normally", or the metric quietly says every reply was fine."""
    provider = _openai_provider(_Response(None))
    list(provider.chat_completion(_MSGS, 0.7, 128, False, "gpt-4o", None))
    assert provider.last_request_usage["finish_reason"] is None


# ─────────────────────────────────────────────────────────────────────────────
# The last hop
# ─────────────────────────────────────────────────────────────────────────────

def test_the_reason_survives_the_hop_into_the_ledger(monkeypatch):
    """Capture is worth nothing if the recorder drops it. MUTATION: remove
    finish_reason= from a record_call site and this is the only test that
    notices."""
    from vaf.core import api_backend as ab

    mgr = ab.APIBackendManager.__new__(ab.APIBackendManager)
    mgr.provider_name = "openai"
    mgr.config = {}
    mgr.session_usage = {"input_tokens": 900, "output_tokens": 50}
    mgr.last_request_usage = {**blank_request_usage(), "finish_reason": "length"}

    # Patched on the source module: the recorder imports it inside the function,
    # so the name is looked up at call time and there is nothing on api_backend
    # to replace.
    seen = {}
    monkeypatch.setattr("vaf.core.cost.record_call",
                        lambda *a, **k: seen.update(k) or None)
    mgr._record_call_usage({"input_tokens": 0, "output_tokens": 0}, "gpt-4o-mini")
    assert seen.get("finish_reason") == "length", seen


# ─────────────────────────────────────────────────────────────────────────────
# The two lanes that do not pass through the backend manager
# ─────────────────────────────────────────────────────────────────────────────
# Both post their own HTTP and book their own calls, so neither inherits the
# capture above. The coder lane is typically the largest in the product, and a
# free local answer that stops half way is still a broken answer.


def test_the_local_lane_records_why_it_stopped(monkeypatch, tmp_path):
    """`complete()` already read finish_reason for a domain log, but only when
    the content came back EMPTY. A reply cut off mid-sentence left no trace."""
    import requests

    from vaf.core import cost as cost_mod
    from vaf.core.completion import complete

    seen = {}
    monkeypatch.setattr(cost_mod, "record_spend", lambda *a, **k: 0.0)
    monkeypatch.setattr("vaf.core.log_helper.append_usage_log", lambda m: seen.setdefault("line", m))

    def fake_post(url, json=None, timeout=None):
        class _R:
            status_code = 200

            def json(self):
                return {"model": "qwen.gguf",
                        "usage": {"prompt_tokens": 500, "completion_tokens": 600},
                        "choices": [{"finish_reason": "length",
                                     "message": {"content": "half a sen"}}]}
        return _R()

    monkeypatch.setattr(requests, "post", fake_post)
    complete("q", provider="local")
    assert "cut=length" in seen.get("line", ""), seen


def test_the_coder_lane_records_why_it_stopped(monkeypatch):
    """MUTATION: drop finish_reason= from _record_coder_usage and a truncated
    coder run reads as a model that simply answered briefly."""
    from vaf.core import cost as cost_mod
    from vaf.tools.coder import _record_coder_usage

    seen = {}
    monkeypatch.setattr(cost_mod, "record_spend", lambda *a, **k: 0.0)
    monkeypatch.setattr("vaf.core.log_helper.append_usage_log", lambda m: seen.setdefault("line", m))
    _record_coder_usage({
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 40000, "completion_tokens": 8192},
        "choices": [{"finish_reason": "length"}],
    })
    assert "cut=length" in seen.get("line", ""), seen
    assert "lane=coder" in seen["line"]


def test_a_lane_that_finished_normally_still_writes_the_plain_line(monkeypatch):
    """The quiet case on the same two lanes, so the field cannot creep in."""
    from vaf.core import cost as cost_mod
    from vaf.tools.coder import _record_coder_usage

    seen = {}
    monkeypatch.setattr(cost_mod, "record_spend", lambda *a, **k: 0.0)
    monkeypatch.setattr("vaf.core.log_helper.append_usage_log", lambda m: seen.setdefault("line", m))
    _record_coder_usage({
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 40000, "completion_tokens": 900},
        "choices": [{"finish_reason": "stop"}],
    })
    assert "cut=" not in seen.get("line", ""), seen
