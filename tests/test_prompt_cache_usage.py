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
import pathlib

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


# ─────────────────────────────────────────────────────────────────────────────
# CAPTURE: one test per provider code path, with realistic payloads
# ─────────────────────────────────────────────────────────────────────────────
# Realistic rather than convenient, because the shapes differ in exactly the ways
# that break a reader: DeepSeek sends its own spelling and no details object,
# Anthropic reports outside the prompt total, Google reports a read and no write.
# Every provider also gets a payload with NO cache fields, which is the case an
# old server or a stripping proxy produces and the one that turns a metric
# silently to zero.


class _Msg:
    content = "hi"
    tool_calls = None


class _Choice:
    message = _Msg()


class _Response:
    """A non-streaming OpenAI-compatible response."""

    def __init__(self, usage):
        self.choices = [_Choice()]
        self.usage = usage


class _Chunk:
    """The trailing usage-only chunk real providers send with include_usage."""

    def __init__(self, usage):
        self.choices = []
        self.usage = usage


def _openai_provider(client_returns):
    from vaf.core.api_backend import OpenAIProvider
    provider = OpenAIProvider("openai", "dummy-key")
    provider._create_with_retry = lambda kwargs: client_returns
    return provider


def _drive_openai(provider, stream):
    list(provider.chat_completion(_MSGS, 0.7, 128, stream, "gpt-4o", None))
    return provider.last_request_usage


@pytest.mark.parametrize("stream", [True, False])
def test_an_openai_shaped_provider_reports_its_cache_read(stream):
    """One class serves openai, deepseek, openrouter, veyllo and local, but the
    streaming and non-streaming branches are separate code, so both are driven."""
    usage = _SdkUsage(prompt_tokens=9000, completion_tokens=50,
                      prompt_tokens_details=_SdkUsage(cached_tokens=8400))
    provider = _openai_provider(iter([_Chunk(usage)]) if stream else _Response(usage))
    got = _drive_openai(provider, stream)
    assert got["cache_read_tokens"] == 8400
    assert got["cache_measured"] is True
    assert got["input_tokens"] == 9000


@pytest.mark.parametrize("stream", [True, False])
def test_deepseeks_own_spelling_survives_the_provider_path(stream):
    """DeepSeek rides the same class and sends no details object at all."""
    usage = _SdkUsage(prompt_tokens=900, completion_tokens=20,
                      prompt_cache_hit_tokens=500, prompt_cache_miss_tokens=400)
    provider = _openai_provider(iter([_Chunk(usage)]) if stream else _Response(usage))
    got = _drive_openai(provider, stream)
    assert got["cache_read_tokens"] == 500
    assert got["input_tokens"] == 900          # the total still counts hit + miss


@pytest.mark.parametrize("stream", [True, False])
def test_an_openai_shaped_provider_that_reports_no_cache_stays_unmeasured(stream):
    usage = _SdkUsage(prompt_tokens=100, completion_tokens=20)
    provider = _openai_provider(iter([_Chunk(usage)]) if stream else _Response(usage))
    got = _drive_openai(provider, stream)
    assert got["cache_measured"] is False
    assert got["input_tokens"] == 100


def test_anthropic_reports_a_read_and_a_write_outside_the_prompt_total():
    """MUTATION: pass in_input=True here and the ledger double-counts the cached
    span, because Anthropic's input_tokens already excludes it."""
    from vaf.core.api_backend import AnthropicProvider
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.usage = {"input_tokens": 0, "output_tokens": 0}
    provider.last_request_usage = blank_request_usage()
    final = _SdkUsage(usage=_SdkUsage(input_tokens=12, output_tokens=50,
                                      cache_read_input_tokens=8400,
                                      cache_creation_input_tokens=1200))
    list(provider._emit_final(final, False))
    got = provider.last_request_usage
    assert got["input_tokens"] == 12           # the provider's own number, untouched
    assert (got["cache_read_tokens"], got["cache_write_tokens"]) == (8400, 1200)
    assert got["cache_in_input"] is False


def test_an_anthropic_call_without_cache_fields_stays_unmeasured():
    from vaf.core.api_backend import AnthropicProvider
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.usage = {"input_tokens": 0, "output_tokens": 0}
    provider.last_request_usage = blank_request_usage()
    list(provider._emit_final(_SdkUsage(usage=_SdkUsage(input_tokens=12, output_tokens=50)), False))
    assert provider.last_request_usage["cache_measured"] is False


def test_google_reports_its_cached_content_inside_the_prompt_total():
    from vaf.core.api_backend import GoogleProvider
    provider = GoogleProvider.__new__(GoogleProvider)
    provider.usage = {"input_tokens": 0, "output_tokens": 0}
    provider.last_request_usage = blank_request_usage()
    provider._record_usage(_SdkUsage(usage_metadata=_SdkUsage(
        prompt_token_count=5, candidates_token_count=7, thoughts_token_count=3,
        cached_content_token_count=300)))
    got = provider.last_request_usage
    assert (got["input_tokens"], got["output_tokens"]) == (5, 10)
    assert got["cache_read_tokens"] == 300
    assert got["cache_write_tokens"] == 0      # Gemini charges no write premium
    assert got["cache_in_input"] is True


def test_a_google_call_without_a_cache_count_stays_unmeasured():
    from vaf.core.api_backend import GoogleProvider
    provider = GoogleProvider.__new__(GoogleProvider)
    provider.usage = {"input_tokens": 0, "output_tokens": 0}
    provider.last_request_usage = blank_request_usage()
    provider._record_usage(_SdkUsage(usage_metadata=_SdkUsage(
        prompt_token_count=5, candidates_token_count=7)))
    assert provider.last_request_usage["cache_measured"] is False


# ─────────────────────────────────────────────────────────────────────────────
# PRICING: what the cached span actually costs
# ─────────────────────────────────────────────────────────────────────────────
# The estimate was wrong in BOTH directions before this, and which direction
# depended on the provider. For an OpenAI-shaped provider the whole prompt was
# charged at the full input rate, so the figure was several times too high. For
# Anthropic the cached span was never counted at all, because its input_tokens
# excludes it, so the figure was roughly ten times too LOW. The second one is the
# dangerous direction: `spend_budget_usd_per_day` reads this number, so a cap was
# letting through far more money than it believed.

from vaf.core.cost import PROVIDER_PRICING, estimate_cost


def test_an_estimate_without_cache_data_is_the_number_it_always_was():
    """The regression guard on every existing caller and every stored ledger."""
    assert estimate_cost("anthropic", "claude-sonnet-4-6", 12000, 500).usd == \
        round((12000 * 3.00 + 500 * 15.00) / 1_000_000, 6)


def test_an_unmeasured_call_is_priced_exactly_like_no_cache_data_at_all():
    got = estimate_cost("anthropic", "claude-sonnet-4-6", 12000, 500, cache=cache_usage())
    assert got.usd == estimate_cost("anthropic", "claude-sonnet-4-6", 12000, 500).usd
    assert got.cache_measured is False


def test_anthropic_cache_tokens_are_added_to_the_prompt_not_subtracted_from_it():
    """THE pricing test. Anthropic's input_tokens excludes the cached span, so
    the span has to be ADDED. Treating it the OpenAI way subtracts it from a
    total that never contained it and the call comes out nearly free.

    MUTATION: pass in_input=True at the Anthropic capture site, or drop the
    branch in _cache_split, and this goes red."""
    got = estimate_cost("anthropic", "claude-sonnet-4-6", 12, 50,
                        cache=cache_usage(8400, 1200, in_input=False))
    assert got.usd == round((12 * 3.00 + 8400 * 3.00 * 0.10 + 1200 * 3.00 * 1.25
                             + 50 * 15.00) / 1_000_000, 6)
    assert got.cache_prompt_tokens == 12 + 8400 + 1200


def test_an_openai_shaped_prompt_total_already_contains_its_cached_span():
    """The other convention: subtract, or the cached tokens are billed twice."""
    got = estimate_cost("openai", "gpt-5.6-terra", 9000, 50,
                        cache=cache_usage(8400, 0, in_input=True))
    assert got.usd == round((600 * 2.00 + 8400 * 2.00 * 0.10 + 50 * 12.00) / 1_000_000, 6)
    assert got.cache_prompt_tokens == 9000


def test_the_same_call_is_cheaper_when_it_was_served_from_a_cache():
    fresh = estimate_cost("openai", "gpt-5.6-terra", 9000, 50)
    cached = estimate_cost("openai", "gpt-5.6-terra", 9000, 50,
                           cache=cache_usage(8400, 0, in_input=True))
    assert cached.usd < fresh.usd
    assert cached.cache_saved == round(8400 * 2.00 * 0.90 / 1_000_000, 6)


def test_a_model_this_table_cannot_price_gets_no_discount():
    """Same reasoning as UNKNOWN_PRICE: an unrecognised model must not be able
    to run cheap under a cap, and a discount is a way of running cheap."""
    got = estimate_cost("anthropic", "a-model-nobody-priced", 1000, 0,
                        cache=cache_usage(1000, 0, in_input=False))
    assert got.cost_known is False
    assert got.usd == round(2000 * 15.00 / 1_000_000, 6)
    assert got.cache_saved == 0.0


def test_a_provider_with_no_published_cached_rate_pays_the_full_input_rate():
    """The degradation case, and it errs high, which is the safe direction for
    a figure a spend cap reads."""
    got = estimate_cost("veyllo", "veyllo-chat", 1000, 0, cache=cache_usage(1000, 0))
    assert got.usd == round(1000 * 0.90 / 1_000_000, 6)
    assert got.cache_saved == 0.0


def test_a_free_provider_still_carries_the_cache_counts():
    got = estimate_cost("local", "some.gguf", 1000, 10, cache=cache_usage(400, 0))
    assert got.usd == 0.0
    assert got.cache_read_tokens == 400
    assert got.cache_measured is True


@pytest.mark.parametrize("provider", sorted(PROVIDER_PRICING))
def test_the_multiplier_table_is_all_or_nothing_and_in_range(provider):
    """Half a pair would price reads at a discount and writes at nothing, or the
    reverse, and neither shows up as an error anywhere."""
    spec = PROVIDER_PRICING[provider]
    read, write = spec.get("cache_read_multiplier"), spec.get("cache_write_multiplier")
    assert (read is None) == (write is None), f"{provider} carries half a pair"
    if read is not None:
        assert 0 < read <= 1.0, f"{provider} read multiplier is not a discount"
        assert 1.0 <= write <= 2.0, f"{provider} write multiplier is out of range"


def test_the_coder_lane_books_its_cache_read(monkeypatch):
    """The coder posts its own HTTP from a subprocess and never passes the
    manager, so it needs the reader pointed at it directly. It is described in
    its own file as typically the largest lane: leaving it out would not make
    the instance-wide rate incomplete, it would make it wrong."""
    from vaf.core import cost as cost_mod
    from vaf.tools import coder as coder_mod

    booked = {}
    monkeypatch.setattr(cost_mod, "record_call",
                        lambda *a, **k: booked.update(args=a, kwargs=k))
    coder_mod._record_coder_usage({
        "model": "deepseek-v4-pro",
        "usage": {"prompt_tokens": 900, "completion_tokens": 20,
                  "prompt_cache_hit_tokens": 500, "prompt_cache_miss_tokens": 400},
    })
    assert booked, "the coder lane recorded nothing at all"
    assert booked["kwargs"]["lane"] == "coder"
    assert booked["kwargs"]["cache"]["cache_read_tokens"] == 500
    assert booked["kwargs"]["cache"]["cache_measured"] is True


def test_the_local_lane_reports_no_cache_rather_than_a_zero_one(monkeypatch):
    """llama-server reports prefix reuse under `timings`, not in `usage`. Left
    unmeasured on purpose: a free lane claiming a 0% hit rate would drag the
    instance-wide figure down for work that costs nothing."""
    assert cache_usage_from_openai({"prompt_tokens": 500, "completion_tokens": 20}) \
        ["cache_measured"] is False


class _TrailingUsageProvider:
    """A provider shaped like the real ones: the usage arrives LAST and yields nothing.

    Every OpenAI-compatible server sends its usage in a final chunk that carries
    no choices, so the provider records the figures and the caller sees no chunk
    for them. A fake that records before it yields hides exactly the defect this
    test exists for.
    """

    def __init__(self):
        self.provider_name = "openai"
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_request_usage = blank_request_usage()
        self.call = 0

    def chat_completion(self, *args, **kwargs):
        self.call += 1
        yield f"answer {self.call}"          # a content chunk
        # ... and now the trailing usage-only chunk, which yields nothing.
        self.usage["input_tokens"] += 100 * self.call
        self.usage["output_tokens"] += self.call
        self.last_request_usage.update({
            "input_tokens": 100 * self.call, "output_tokens": self.call,
            **cache_usage(80 * self.call, 0, in_input=True)})


def test_a_call_books_its_own_tokens_and_not_the_previous_call_s(monkeypatch):
    """MUTATION: take the sync out of the `finally` in `_chat_single` and this
    goes red, because the per-chunk sync never runs again after the last chunk
    that had content.

    Measured against the live API before the fix: three calls in a row booked an
    estimate, then the first call's tokens, then the second's, while the provider
    had reported its own each time. Every call billed the previous one."""
    from vaf.core import cost as cost_mod
    from vaf.core.api_backend import APIBackendManager

    booked = []
    monkeypatch.setattr(cost_mod, "record_call", lambda *a, **k: booked.append((a, k)))

    mgr = APIBackendManager.__new__(APIBackendManager)
    mgr.provider_name = "openai"
    mgr.config = {}
    mgr.provider = _TrailingUsageProvider()
    mgr.session_usage = {"input_tokens": 0, "output_tokens": 0}
    mgr.last_request_usage = blank_request_usage()

    for expected in (1, 2, 3):
        before = dict(mgr.session_usage)
        list(mgr._chat_single(_MSGS, model="gpt-4o-mini"))
        mgr._record_call_usage(before, "gpt-4o-mini")
        args, kwargs = booked[-1]
        assert args[2] == 100 * expected, (
            f"call {expected} booked {args[2]} input tokens, not its own {100 * expected}")
        assert args[3] == expected
        assert kwargs["cache"]["cache_read_tokens"] == 80 * expected


def test_the_manager_lane_books_its_cache_read(monkeypatch):
    """THE test this round was missing, and the reason it was missing is worth
    stating: the propagation guard above drives `_chat_single` and stops where
    the record is complete. Every lane that reaches a model through the manager
    - chat, sub-agents, vision, voice, compaction, mail - books its spend one
    hop further on, in `_record_call_usage`, and a capture that dies at that hop
    leaves the whole round looking finished while the ledger and the spend cap
    see nothing.

    MUTATION: drop `cache=` from any of the three record_call sites in
    `_record_call_usage` and this goes red."""
    from vaf.core import cost as cost_mod
    from vaf.core.api_backend import APIBackendManager

    booked = {}
    monkeypatch.setattr(cost_mod, "record_call",
                        lambda *a, **k: booked.update(args=a, kwargs=k))

    mgr = APIBackendManager.__new__(APIBackendManager)
    mgr.provider_name = "anthropic"
    mgr.config = {}
    mgr.session_usage = {"input_tokens": 12, "output_tokens": 50}
    mgr.last_request_usage = {**blank_request_usage(), "input_tokens": 12,
                              "output_tokens": 50, "cache_read_tokens": 8400,
                              "cache_write_tokens": 1200, "cache_measured": True,
                              "cache_in_input": False}
    mgr._record_call_usage({"input_tokens": 0, "output_tokens": 0}, "claude-sonnet-4-6")

    assert booked, "the manager lane recorded nothing at all"
    cache = booked["kwargs"].get("cache")
    assert cache, "the manager lane books spend without the cache figures it just captured"
    assert cache["cache_read_tokens"] == 8400
    assert cache["cache_in_input"] is False


def test_a_call_the_provider_never_reported_is_not_booked_as_a_cache_miss(monkeypatch):
    """An aborted or failed stream books zero tokens. It must stay OUT of the
    hit-rate denominator rather than entering it as a 0% hit, which is what an
    unconditional cache dict would do."""
    from vaf.core import cost as cost_mod
    from vaf.core.api_backend import APIBackendManager

    booked = {}
    monkeypatch.setattr(cost_mod, "record_call",
                        lambda *a, **k: booked.update(kwargs=k))

    mgr = APIBackendManager.__new__(APIBackendManager)
    mgr.provider_name = "anthropic"
    mgr.config = {}
    mgr.session_usage = {"input_tokens": 0, "output_tokens": 0}
    mgr.last_request_usage = blank_request_usage()
    mgr._record_call_usage({"input_tokens": 0, "output_tokens": 0}, "claude-sonnet-4-6")

    assert booked, "an unreported call was not booked at all"
    assert (booked["kwargs"].get("cache") or {}).get("cache_measured") is not True


# ─────────────────────────────────────────────────────────────────────────────
# WHAT THE PRODUCT SAYS ABOUT ITS OWN NUMBERS
# ─────────────────────────────────────────────────────────────────────────────


def test_a_non_admin_never_receives_a_money_field():
    """/api/usage/me shows an account its own consumption and withholds the cost
    of the instance's API keys. The filter used to be a DENYLIST, so every field
    the ledger learned went out by default until somebody noticed: `currencies`
    had been going out since it was added, and `cache_saved` would have been the
    second. It is an allowlist now, so this test pins the POLICY rather than one
    field name.

    MUTATION: put any money field back in _OWN_FIELDS and this goes red."""
    import re

    from vaf.api import config_routes

    src = pathlib.Path(config_routes.__file__).read_text(encoding="utf-8")
    allow = re.search(r"_OWN_FIELDS = \(([^)]*)\)", src, re.S)
    assert allow, "the non-admin row filter is no longer an allowlist"
    fields = set(re.findall(r'"([a-z_]+)"', allow.group(1)))
    for money in ("usd", "currencies", "cache_saved", "token_share", "call_share"):
        assert money not in fields, f"`{money}` is money or a comparison and reaches a non-admin"
    assert "cache_read_tokens" in fields, "an account cannot see its own cache consumption"


def test_the_product_no_longer_claims_it_ignores_cache_discounts():
    """Three places told the reader the amounts apply no cache discount. They do
    now, so all three had to change in the same round: a report whose stated
    method is not the method used is worse than one that says nothing.

    Rule 2 prefers a guard over a prose rule, so the claim is pinned here."""
    from vaf.api import config_routes
    from vaf.core import cost as cost_mod

    for module in (cost_mod, config_routes):
        text = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        assert "no cache hits" not in text, f"{module.__name__} still claims it ignores cache"
        assert "without cache" not in text, f"{module.__name__} still claims it ignores cache"


def test_the_pricing_change_is_in_the_changelog():
    """A displayed cost figure and an enforced spend cap both change, which is
    the definition of user-facing in this repo."""
    text = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text.split("## [Unreleased]", 1)[1].split("\n## ", 1)[0].lower()
    # Specific on purpose. A bare search for "cache" passed on an unrelated entry
    # that merely mentioned a caching SETTING, which is the vacuous shape this
    # round was just caught on twice.
    assert "spend limit" in unreleased or "daily limit" in unreleased, (
        "the [Unreleased] section does not mention that the spend limit's arithmetic changed")
    assert "cached rate" in unreleased, (
        "the [Unreleased] section does not say a cached prompt is priced differently now")
