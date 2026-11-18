# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The spend ledger books the model that SERVED a call, not the name it was asked for.

`APIBackendManager.chat_completion` is the accounting point for every lane, and it
used to book the caller's `model` argument (usually None, so the configured value).
With `api_model_deepseek = deepseek-auto` that is a routing alias no price list
names, so every call was priced at the unknown-model rate ($15 / $75 per million
tokens, no cache discount): more than ten times what DeepSeek bills. The same
mistake put a dropped foreign model id and a failover link's tokens on the wrong
row.

Every amount below is a literal worked out from the catalog's DeepSeek rates per
million tokens (flash 0.30 in / 1.20 out / 0.006 cached, pro 1.32 / 3.96 / 0.044),
never computed by `estimate_cost` here, so a change to the pricing rule cannot
move the expectation with it. The ledger is a scratch directory; the real store is
never read.
"""
import pytest

from vaf.core import cost as cost_mod
from vaf.core.api_backend import APIBackendManager
from vaf.core.cost import blank_request_usage, usage_totals

_MSGS = [{"role": "user", "content": "hi"}]
# 10,000 prompt tokens of which 9,000 were cache hits, 400 out.
_REPORT = {"input_tokens": 10_000, "output_tokens": 400, "cache_read_tokens": 9_000,
           "cache_in_input": True, "cache_measured": True}
# flash: 1,000 * 0.30 + 9,000 * 0.006 + 400 * 1.20 = 834 per million.
FLASH_USD = 0.000834
# pro: 1,000 * 1.32 + 9,000 * 0.044 + 400 * 3.96 = 3,300 per million.
PRO_USD = 0.0033

_PRO_TRIGGERS = ("VAF_IN_WORKFLOW_TERMINAL", "VAF_IN_AUTOMATION",
                 "VAF_COMPACTION_IN_PROGRESS", "VAF_TOOL_MODEL")


class _FakeProvider:
    """Reports the same usage on every call, the way a real provider mutates in
    place, and remembers the model it was sent."""

    def __init__(self, report=_REPORT, fail=None):
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_request_usage = blank_request_usage()
        self._report = dict(report)
        self._fail = fail
        self.sent = []

    def chat_completion(self, messages, temperature, max_tokens, stream, model,
                        tools, tool_choice, **kw):
        self.sent.append(model)
        if self._fail is not None:
            raise self._fail
        self.usage["input_tokens"] += self._report["input_tokens"]
        self.usage["output_tokens"] += self._report["output_tokens"]
        self.last_request_usage.update(self._report)
        yield "answer"


def _manager(provider_name="deepseek", config=None, provider=None):
    """A manager without SDK clients or a key, on a programmatic config."""
    mgr = APIBackendManager.__new__(APIBackendManager)
    mgr.provider_name = provider_name
    mgr._embedded = True
    mgr.config = {"failover_level": "off", **(config if config is not None
                                              else {"api_model_deepseek": "deepseek-auto"})}
    mgr.caller_config = None
    mgr.provider = provider or _FakeProvider()
    mgr.session_usage = {"input_tokens": 0, "output_tokens": 0}
    mgr.last_request_usage = blank_request_usage()
    mgr._failover_pinned_idx = 0
    mgr._failover_open_until = {}
    return mgr


@pytest.fixture
def spend_dir(tmp_path, monkeypatch):
    """The ledger in a scratch directory; never the real user store."""
    monkeypatch.setattr(cost_mod.Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cost_mod, "_display_name", lambda scope: "Alice")
    return tmp_path / "spend"


@pytest.fixture
def flash_context(monkeypatch):
    for var in _PRO_TRIGGERS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def booked(monkeypatch):
    """Every `record_call` as (provider, model), the ledger write left out."""
    calls = []
    monkeypatch.setattr(cost_mod, "record_call",
                        lambda provider, model, *a, **kw: calls.append((provider, model)))
    return calls


def _rows():
    return usage_totals(days=1)["totals"]


def test_deepseek_auto_chat_books_the_flash_model_at_flash_price(spend_dir, flash_context):
    """MUTATION: ignore the served model in `_record_call_usage` (the row becomes
    `deepseek/deepseek-auto`), or delete the write into `served` in `_chat_single`."""
    mgr = _manager()
    for _ in range(3):
        assert list(mgr.chat_completion(_MSGS)) == ["answer"]
    assert mgr.provider.sent == ["deepseek-flash"] * 3
    totals = _rows()
    assert set(totals["providers"]) == {"deepseek/deepseek-flash"}
    row = totals["providers"]["deepseek/deepseek-flash"]
    assert row["calls"] == 3
    assert round(row["usd"], 6) == round(FLASH_USD * 3, 6) == 0.002502
    assert totals["estimated_usd_incomplete"] is False


def test_a_pro_trigger_books_the_pro_model(spend_dir, flash_context, monkeypatch):
    """MUTATION: as above; with the stamp gone the row is `deepseek/deepseek-auto`."""
    monkeypatch.setenv("VAF_IN_AUTOMATION", "1")
    mgr = _manager()
    list(mgr.chat_completion(_MSGS))
    assert mgr.provider.sent == ["deepseek-v4-pro"]
    totals = _rows()
    assert set(totals["providers"]) == {"deepseek/deepseek-v4-pro"}
    assert round(totals["providers"]["deepseek/deepseek-v4-pro"]["usd"], 6) == PRO_USD
    assert totals["estimated_usd_incomplete"] is False


def test_a_dropped_foreign_model_books_the_model_that_served(flash_context, booked,
                                                            monkeypatch):
    """A lane model left over from another provider is dropped before the call and
    the provider's own model answers. MUTATION: stamp the caller's original
    `model` argument instead of the resolved one."""
    import vaf.core.api_backend as ab
    monkeypatch.setattr(ab, "note_foreign_model", lambda *a, **kw: None)
    mgr = _manager()
    list(mgr.chat_completion(_MSGS, model="gpt-5.6-terra"))
    assert mgr.provider.sent == ["deepseek-flash"]
    assert booked == [("deepseek", "deepseek-flash")]


def test_a_failover_link_books_under_its_own_provider(flash_context, booked, monkeypatch):
    """The primary fails before its first token, the backup serves. Its tokens
    were booked on the primary's row under the primary's model. MUTATION: book
    under `self.provider_name` in `_record_call_usage`."""
    primary = _manager(provider=_FakeProvider(fail=ConnectionError("connection refused")))
    link = _manager("anthropic", config={"api_model_anthropic": "claude-sonnet-4-6"})
    monkeypatch.setattr(primary, "_build_failover_chain",
                        lambda model: [(primary, model), (link, None)])
    assert list(primary.chat_completion(_MSGS)) == ["answer"]
    assert primary.provider.sent == ["deepseek-flash"]
    assert link.provider.sent == ["claude-sonnet-4-6"]
    assert booked == [("anthropic", "claude-sonnet-4-6")]


def test_a_call_that_reached_no_provider_does_not_inherit_the_last_stamp(flash_context,
                                                                         booked,
                                                                         monkeypatch):
    """The first call is served by a failover link, so a leftover would carry
    another provider AND another model. The calls after it reach no provider and
    book what they asked for: the configured alias, then an explicit model.
    MUTATION: keep the holder on the manager instead of creating it per call
    (the later calls book `anthropic/claude-sonnet-4-6`), or drop `model` from
    the fallback in `_record_call_usage` (the explicit `x` is lost)."""
    mgr = _manager(provider=_FakeProvider(fail=ConnectionError("connection refused")))
    link = _manager("anthropic", config={"api_model_anthropic": "claude-sonnet-4-6"})
    monkeypatch.setattr(mgr, "_build_failover_chain",
                        lambda model: [(mgr, model), (link, None)])
    list(mgr.chat_completion(_MSGS))
    events = []
    mgr.event_sink = events.append
    monkeypatch.setattr(mgr, "_chat_completion_impl", lambda *a, **kw: iter(()))
    list(mgr.chat_completion(_MSGS))
    list(mgr.chat_completion(_MSGS, model="x"))
    assert booked == [("anthropic", "claude-sonnet-4-6"), ("deepseek", "deepseek-auto"),
                      ("deepseek", "x")]
    ends = [e for e in events if e["type"] == "llm_end"]
    assert all("model" not in e["usage"] and "provider" not in e["usage"] for e in ends)


def test_a_call_that_reached_no_provider_is_not_priced_with_the_last_calls_cache(
        spend_dir, flash_context, monkeypatch):
    """A served call leaves its cache figures (9,000 read) on the manager's record,
    and only `_chat_single` resets it. A call that reaches no provider used to be
    priced with them: 0.000396 on a row of one estimated token, far above what the
    alias can cost today, so `vaf usage show` called a row written today a leftover
    of earlier versions. Its estimate is now priced without cache: the one token
    of "hi" at the pro rate, 1.32 per million, rounds to 0.000001. MUTATION:
    delete the blank cache for an empty holder in `_record_call_usage`."""
    mgr = _manager()
    list(mgr.chat_completion(_MSGS))
    monkeypatch.setattr(mgr, "_chat_completion_impl", lambda *a, **kw: iter(()))
    list(mgr.chat_completion(_MSGS))
    providers = _rows()["providers"]
    assert providers["deepseek/deepseek-auto"]["tokens"] == 1
    assert providers["deepseek/deepseek-auto"]["usd"] == 0.000001
    assert cost_mod.alias_rows_priced_as_unknown(providers) == []


def test_a_call_that_failed_at_the_provider_names_the_model_it_was_sent_to(flash_context,
                                                                          booked):
    """A call the provider refused still reached it, so the event and the
    ledger name the model it was sent to. MUTATION: delete the write into
    `served` in `_chat_single`."""
    mgr = _manager(provider=_FakeProvider(fail=ConnectionError("connection refused")))
    events = []
    mgr.event_sink = events.append
    with pytest.raises(ConnectionError):
        list(mgr.chat_completion(_MSGS))
    end = [e for e in events if e["type"] == "llm_end"][-1]
    assert end["ok"] is False
    assert (end["usage"]["provider"], end["usage"]["model"]) == ("deepseek", "deepseek-flash")
    assert booked == [("deepseek", "deepseek-flash")]


def test_overlapping_calls_each_book_the_model_that_served_them(flash_context, booked,
                                                                monkeypatch):
    """Two calls on one manager overlap: A is resolved to pro and still streaming
    when B runs on flash from start to finish (a `call_with_deadline` worker keeps
    its call going after the timeout while the turn makes the next one). With
    the stamp on the manager, A was booked at B's flash price, about 3.3x too
    low, and a spend cap reading it trips late. MUTATION: keep the holder on the
    manager instead of creating it per call."""
    mgr = _manager()
    events = []
    mgr.event_sink = events.append
    monkeypatch.setenv("VAF_IN_AUTOMATION", "1")
    call_a = mgr.chat_completion(_MSGS)
    assert next(call_a) == "answer"
    monkeypatch.delenv("VAF_IN_AUTOMATION")
    assert list(mgr.chat_completion(_MSGS)) == ["answer"]
    assert list(call_a) == []
    assert mgr.provider.sent == ["deepseek-v4-pro", "deepseek-flash"]
    assert booked == [("deepseek", "deepseek-flash"), ("deepseek", "deepseek-v4-pro")]
    ends = [e["usage"]["model"] for e in events if e["type"] == "llm_end"]
    assert ends == ["deepseek-flash", "deepseek-v4-pro"]


def test_llm_end_names_the_model_that_served(flash_context, booked):
    """The event's own `model` is what was asked for (None here); the usage
    snapshot says what answered. MUTATION: delete the write into `served` in
    `_chat_single`, or leave `_served` out of the event's usage."""
    mgr = _manager()
    events = []
    mgr.event_sink = events.append
    list(mgr.chat_completion(_MSGS))
    end = [e for e in events if e["type"] == "llm_end"][-1]
    assert end["model"] is None
    assert end["usage"]["model"] == "deepseek-flash"
    assert end["usage"]["provider"] == "deepseek"
    assert end["usage"]["input_tokens"] == 10_000
