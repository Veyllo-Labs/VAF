# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A provider that reports nothing about its cache must not read as a zero hit rate.

Providers serve the leading tokens of a request from a cache and bill them at a
fraction of the input price, but each one reports it under a different name and
several report nothing at all. Normalising those spellings is the easy half. The
half that decides whether the resulting number means anything is telling "the
provider said nothing" apart from "the provider said zero": averaged together,
an unmeasurable lane drags a displayed hit rate down and hides the very thing
the number exists to show.

So `cache_measured` is a positive statement rather than an absence, and an
explicitly reported 0 counts as measured, because a genuine cache miss is a
measurement.
"""
import pytest

from vaf.core.cost import blank_request_usage, cache_usage, cache_usage_from_openai

_CACHE_KEYS = ("cache_read_tokens", "cache_write_tokens", "cache_measured", "cache_in_input")


class _SdkUsage:
    """An SDK response model: attributes, not keys, and extras materialised."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


def test_the_blank_shape_carries_every_key():
    """MUTATION: drop a key and the readers below inherit the previous call's
    numbers, because these dicts are overwritten key by key and never reset."""
    blank = blank_request_usage()
    assert set(blank) == {"input_tokens", "output_tokens", *_CACHE_KEYS}
    assert blank["cache_measured"] is False
    assert blank["cache_read_tokens"] == 0


def test_a_provider_that_says_nothing_is_not_a_zero_hit_rate():
    """THE anti-defect test. `cache_measured` False is the only thing that keeps
    an unmeasurable lane out of the denominator."""
    got = cache_usage_from_openai({"prompt_tokens": 100, "completion_tokens": 20})
    assert got["cache_measured"] is False
    assert got["cache_read_tokens"] == 0


def test_a_reported_zero_is_a_measurement_not_a_gap():
    """A cache miss is a fact about this call. MUTATION: fold the two cases
    together (`if not read`) and a real 0% hit rate becomes invisible."""
    got = cache_usage_from_openai({"prompt_tokens_details": {"cached_tokens": 0}})
    assert got["cache_measured"] is True
    assert got["cache_read_tokens"] == 0


def test_the_documented_openai_field_is_read():
    got = cache_usage_from_openai({"prompt_tokens": 9000,
                                   "prompt_tokens_details": {"cached_tokens": 8400}})
    assert got["cache_read_tokens"] == 8400
    assert got["cache_in_input"] is True


def test_deepseeks_own_field_is_read_when_the_standard_one_is_absent():
    """DeepSeek documents only its own spelling, and `prompt_tokens` is the sum
    of hit and miss, so the standard total stays correct either way."""
    got = cache_usage_from_openai({"prompt_tokens": 900,
                                   "prompt_cache_hit_tokens": 500,
                                   "prompt_cache_miss_tokens": 400})
    assert got["cache_read_tokens"] == 500
    assert got["cache_measured"] is True


def test_the_standard_field_wins_when_a_provider_sends_both():
    """MUTATION: reverse the precedence and a gateway that fills both with
    different numbers reports the vendor-specific one."""
    got = cache_usage_from_openai({"prompt_tokens_details": {"cached_tokens": 700},
                                   "prompt_cache_hit_tokens": 500})
    assert got["cache_read_tokens"] == 700


def test_a_bare_cached_tokens_is_read():
    assert cache_usage_from_openai({"cached_tokens": 64})["cache_read_tokens"] == 64


def test_a_passthrough_gateway_reports_a_cache_write_too():
    got = cache_usage_from_openai({"prompt_tokens_details": {"cached_tokens": 10,
                                                             "cache_write_tokens": 3}})
    assert (got["cache_read_tokens"], got["cache_write_tokens"]) == (10, 3)


def test_an_sdk_object_reads_the_same_as_a_dict():
    """The OpenAI SDK allows extra fields, so a vendor's own spelling arrives as
    an attribute. Both shapes reach this reader and must agree."""
    obj = _SdkUsage(prompt_tokens=900, prompt_cache_hit_tokens=500)
    assert cache_usage_from_openai(obj) == cache_usage_from_openai(
        {"prompt_tokens": 900, "prompt_cache_hit_tokens": 500})


def test_anthropic_counts_sit_outside_the_prompt_total():
    """Anthropic's `input_tokens` excludes both cache figures while every
    OpenAI-shaped provider includes them. Getting this backwards produces a
    wrong invoice, not a wrong display, so it travels with the measurement."""
    got = cache_usage(8400, 1200, in_input=False)
    assert got["cache_in_input"] is False
    assert (got["cache_read_tokens"], got["cache_write_tokens"]) == (8400, 1200)


@pytest.mark.parametrize("payload", [None, "not a usage object", 42, {"prompt_tokens_details": None},
                                     {"prompt_tokens_details": {"cached_tokens": "many"}},
                                     _SdkUsage()])
def test_a_payload_it_cannot_read_is_unmeasured_rather_than_an_exception(payload):
    """An old server, a proxy that drops fields, a field that is suddenly a
    string: none of these may take a request down, and none may invent a number."""
    got = cache_usage_from_openai(payload)
    assert got["cache_measured"] is False
    assert got["cache_read_tokens"] == 0


def test_a_negative_count_cannot_reach_the_ledger():
    assert cache_usage(-5, -1)["cache_read_tokens"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# PROPAGATION: the pipe between the provider and the ledger
# ─────────────────────────────────────────────────────────────────────────────
# Capturing a number the pipe then eats is the failure mode that makes this whole
# round look finished while measuring nothing: correct at the provider, zero at
# the ledger, and no test red anywhere. The copies were hand-written key by key,
# which is why `diffs` and `activity` had to be added twice on the WebSocket side
# of this codebase before anyone noticed the pattern.


def _bare_manager(provider):
    """A manager without any real SDK client, in the shape `_chat_single` needs."""
    from vaf.core.api_backend import APIBackendManager
    mgr = APIBackendManager.__new__(APIBackendManager)
    mgr.provider_name = "openai"
    mgr.config = {}
    mgr.provider = provider
    mgr.session_usage = {"input_tokens": 0, "output_tokens": 0}
    mgr.last_request_usage = blank_request_usage()
    return mgr


class _FakeProvider:
    """Reports whatever it is told to, the way a real provider mutates in place."""

    def __init__(self, *reports):
        self.provider_name = "openai"
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_request_usage = blank_request_usage()
        self._reports = list(reports)

    def chat_completion(self, *args, **kwargs):
        report = self._reports.pop(0) if self._reports else None
        if report is not None:
            self.usage["input_tokens"] += report.get("input_tokens", 0)
            self.usage["output_tokens"] += report.get("output_tokens", 0)
            self.last_request_usage.update(report)
        yield "answer"


_MSGS = [{"role": "user", "content": "hi"}]


def test_the_pipe_carries_a_field_nobody_hand_listed():
    """THE guard for the bug class. The canary is a key no copy in api_backend
    names, so it can only arrive if the sync copies what it is given rather than
    two fields it was taught. MUTATION: restore the two named assignments in
    `_chat_single` and this goes red."""
    provider = _FakeProvider({"input_tokens": 12, "output_tokens": 50,
                              "cache_read_tokens": 8400, "cache_measured": True,
                              "a_future_field": "canary"})
    mgr = _bare_manager(provider)
    list(mgr._chat_single(_MSGS, model="gpt-4o"))
    assert mgr.last_request_usage["cache_read_tokens"] == 8400
    assert mgr.last_request_usage["cache_measured"] is True
    assert mgr.last_request_usage["a_future_field"] == "canary"


def test_a_second_call_does_not_inherit_the_first_ones_cache_numbers():
    """A per-call figure that is only ever written and never reset reports the
    previous call for anything the provider says nothing about. MUTATION: drop
    the reset in `_chat_single` and the second call reports 8400 again."""
    provider = _FakeProvider({"input_tokens": 12, "output_tokens": 50,
                              "cache_read_tokens": 8400, "cache_measured": True},
                             None)
    mgr = _bare_manager(provider)
    list(mgr._chat_single(_MSGS, model="gpt-4o"))
    assert mgr.last_request_usage["cache_read_tokens"] == 8400
    list(mgr._chat_single(_MSGS, model="gpt-4o"))
    assert mgr.last_request_usage["cache_read_tokens"] == 0
    assert mgr.last_request_usage["cache_measured"] is False


def test_the_failover_mirror_leaves_a_complete_record():
    """A link may report a narrower dict than this manager holds; merging over
    the blank shape is what stops the previous call's remains from showing
    through. MUTATION: assign the link's dict straight across and the missing
    keys either vanish or go stale."""
    mgr = _bare_manager(_FakeProvider())
    mgr.last_request_usage.update({"cache_read_tokens": 999, "cache_measured": True})

    class _Link:
        last_request_usage = {"input_tokens": 3, "output_tokens": 5}

    list(mgr._stream_link(_Link(), "first", iter(["rest"])))
    assert mgr.last_request_usage["input_tokens"] == 3
    assert mgr.last_request_usage["output_tokens"] == 5
    assert mgr.last_request_usage["cache_read_tokens"] == 0
    assert mgr.last_request_usage["cache_measured"] is False
    assert set(_CACHE_KEYS) <= set(mgr.last_request_usage)


def test_the_llm_end_event_carries_the_cache_numbers():
    """The public facade promises `llm_end` with a usage snapshot, so the
    framework half of this round arrives without a facade change. It copies the
    dict wholesale, which is why it needs a test rather than an edit."""
    import inspect

    from vaf.core import api_backend
    src = inspect.getsource(api_backend.APIBackendManager.chat_completion)
    assert '"usage": dict(self.last_request_usage or {})' in src, (
        "llm_end no longer copies the whole usage dict, so a new field would be "
        "dropped on the way to Agent.on_event")
