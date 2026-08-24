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
