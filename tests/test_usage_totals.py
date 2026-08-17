# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The Usage view: provider-reported tokens, per user, and never another's line.

Tokenizer independence is the property under test here, and it is structural
rather than clever: the ledger stores what the PROVIDER reported for a call it
billed, so two providers that disagree about what a token is still add up to
the invoice. Nothing in this lane tokenizes anything.
"""
import json
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("llama_cpp", MagicMock())

from vaf.core import cost as cost_mod
from vaf.core.cost import (CostEstimate, estimate_cost, price_catalog, record_spend,
                           usage_totals)


@pytest.fixture
def spend_dir(tmp_path, monkeypatch):
    """Point the ledger at a scratch directory; never the real user store."""
    monkeypatch.setattr(cost_mod.Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cost_mod, "_display_name", lambda scope: {"default": "Alice"}.get(scope, "Bob"))
    return tmp_path / "spend"


def test_estimate_carries_the_providers_own_counts():
    """The tokens travel with the estimate - they are not recomputed anywhere."""
    est = estimate_cost("openai", "gpt-4o", 1200, 300)
    assert est.input_tokens == 1200 and est.output_tokens == 300
    assert est.cost_known is True and est.usd > 0

    # Negative/garbage counts are floored, never propagated.
    assert estimate_cost("openai", "gpt-4o", -5, 0).input_tokens == 0


def test_record_and_aggregate_tokens_per_user(spend_dir):
    record_spend(None, estimate_cost("openai", "gpt-4o", 1000, 100))          # -> default
    record_spend(None, estimate_cost("openai", "gpt-4o", 500, 50))
    record_spend("ab12cd34", estimate_cost("deepseek", "deepseek-v4-pro", 200, 20))

    out = usage_totals(days=30)

    assert out["totals"]["input_tokens"] == 1700
    assert out["totals"]["output_tokens"] == 170
    assert out["totals"]["tokens"] == 1870
    assert out["totals"]["calls"] == 3
    # Heaviest first: that IS the "who consumed the most" answer.
    assert [u["username"] for u in out["users"]] == ["Alice", "Bob"]
    assert out["users"][0]["tokens"] == 1650
    assert out["users"][1]["scope"] == "ab12cd34"


def test_local_provider_is_free_and_still_counted_as_tokens(spend_dir):
    """Local tokens cost no money, so the money stays 0 - but they are real tokens."""
    record_spend(None, estimate_cost("local", "some-gguf", 900, 90))
    out = usage_totals(days=30)
    assert out["totals"]["usd"] == 0.0
    assert out["totals"]["tokens"] == 990


def test_unknown_model_is_flagged_as_an_upper_bound(spend_dir):
    record_spend(None, estimate_cost("openai", "brand-new-model", 100, 10))
    out = usage_totals(days=30)
    assert out["totals"]["estimated_usd_incomplete"] is True


def test_a_legacy_ledger_without_tokens_still_reports(spend_dir):
    """Ledgers written before tokens existed keep their money and say so."""
    spend_dir.mkdir(parents=True, exist_ok=True)
    (spend_dir / "default.json").write_text(json.dumps({
        "format": cost_mod.SPEND_FORMAT,
        "days": {cost_mod._today(): {"usd": 1.25, "calls": 4, "unknown_model_calls": 0}},
    }), encoding="utf-8")

    out = usage_totals(days=30)
    assert out["totals"]["calls"] == 4 and out["totals"]["usd"] == 1.25
    assert out["totals"]["tokens"] == 0
    assert out["users"][0]["tokens_recorded"] is False


def test_a_corrupt_ledger_does_not_hide_the_others(spend_dir):
    record_spend(None, estimate_cost("openai", "gpt-4o", 1000, 100))
    (spend_dir / "broken.json").write_text("{not json", encoding="utf-8")

    out = usage_totals(days=30)
    assert out["totals"]["tokens"] == 1100
    assert [u["scope"] for u in out["users"]] == ["default"]


def test_missing_spend_dir_is_an_empty_report(tmp_path, monkeypatch):
    monkeypatch.setattr(cost_mod.Platform, "data_dir", staticmethod(lambda: tmp_path))
    out = usage_totals(days=30)
    assert out["users"] == [] and out["totals"]["tokens"] == 0


def test_day_window_excludes_older_entries(spend_dir):
    spend_dir.mkdir(parents=True, exist_ok=True)
    (spend_dir / "default.json").write_text(json.dumps({
        "format": cost_mod.SPEND_FORMAT,
        "days": {
            "2001-01-01": {"usd": 9.0, "calls": 1, "input_tokens": 999, "output_tokens": 1},
            cost_mod._today(): {"usd": 0.5, "calls": 1, "input_tokens": 10, "output_tokens": 2},
        },
    }), encoding="utf-8")

    out = usage_totals(days=7)
    assert out["totals"]["tokens"] == 12, "an entry outside the window must not be summed"


def test_usage_route_is_admin_only():
    """The all-users view lists other tenants by name - one must not see another."""
    import inspect

    from vaf.api import config_routes

    assert "require_admin" in inspect.getsource(config_routes.get_usage)
    # The self-view exists so a non-admin still has an answer about themselves.
    assert "usage/me" in inspect.getsource(config_routes)


def test_daily_series_covers_every_day_including_quiet_ones(spend_dir):
    """A chart that drops quiet days makes a burst look like steady traffic."""
    record_spend(None, estimate_cost("openai", "gpt-4o", 1000, 100))

    out = usage_totals(days=7)

    assert len(out["daily"]) == 7
    assert [d["day"] for d in out["daily"]] == sorted(d["day"] for d in out["daily"])
    assert out["daily"][-1]["tokens"] == 1100, "today carries the traffic"
    assert sum(d["tokens"] for d in out["daily"][:-1]) == 0
    assert sum(d["tokens"] for d in out["daily"]) == out["totals"]["tokens"]


def test_shares_answer_who_used_the_most(spend_dir):
    record_spend(None, estimate_cost("openai", "gpt-4o", 750, 0))
    record_spend("ab12cd34", estimate_cost("openai", "gpt-4o", 250, 0))

    out = usage_totals(days=30)

    assert out["users"][0]["token_share"] == 75.0
    assert out["users"][1]["token_share"] == 25.0
    assert round(sum(u["token_share"] for u in out["users"])) == 100


def test_price_catalog_quotes_the_same_table_the_ledger_estimates_with():
    from vaf.core.cost import PRICES, price_catalog

    catalog = price_catalog()
    assert catalog, "the comparison panel needs at least one priced provider"
    for row in catalog:
        assert row["label"] and row["currency"] in {"EUR", "USD"}
        assert row["models"], "a provider without models cannot be compared"
        for m in row["models"]:
            assert PRICES.get(m["model"]) == (m["input_per_1m"], m["output_per_1m"]), (
                "the panel must quote the SAME prices the ledger estimates with"
            )


def test_veyllo_leads_the_comparison():
    """Deliberate order, not alphabetical: this product's own API comes first."""
    assert price_catalog()[0]["provider"] == "veyllo"


def test_currency_is_reported_not_converted():
    """No exchange rate is carried, so the unit has to travel with the price."""
    by_provider = {c["provider"]: c for c in price_catalog()}
    assert by_provider["veyllo"]["currency"] == "EUR"
    assert by_provider["anthropic"]["currency"] == "USD"


def test_legacy_model_names_stay_priced():
    """Dropping them would reprice history at the unknown rate and flag it as
    an upper bound. Asserted through cost_known rather than by comparing values:
    one legacy model happens to cost exactly the unknown rate, so a value
    comparison would test a coincidence instead of the property."""
    for legacy in ("gpt-4o", "claude-opus-4-1", "gemini-2.5-pro"):
        assert estimate_cost("openai", legacy, 1000, 100).cost_known is True, legacy


def test_price_catalog_is_imported_by_the_route():
    from vaf.core.cost import price_catalog as _pc  # noqa: F401
    import inspect

    from vaf.api import config_routes

    assert "price_catalog" in inspect.getsource(config_routes.get_usage_prices)


def test_xml_export_carries_the_numbers_and_how_they_were_measured(spend_dir):
    """A transparency record that omits its own method reads as an invoice."""
    import asyncio
    from xml.etree.ElementTree import fromstring

    from vaf.api.config_routes import export_usage

    record_spend(None, estimate_cost("openai", "gpt-4o", 1000, 100))
    resp = asyncio.run(export_usage(request=None, days=30, _admin={"role": "admin"}))

    assert resp.media_type == "application/xml"
    assert "attachment" in resp.headers["content-disposition"]
    root = fromstring(resp.body)
    assert root.tag == "vaf-usage"
    assert root.find("totals").get("tokens") == "1100"
    assert root.find("totals").get("api-calls") == "1"
    assert root.find("users/user").get("name") == "Alice"
    assert root.find("users/user").get("input-tokens") == "1000"
    assert len(root.findall("daily/day")) == 30
    # The method statement is the point of the export, not decoration.
    method = root.find("method")
    assert "provider" in method.find("tokens").text.lower()
    assert "estimat" in method.find("cost").text.lower()


def test_prices_carry_the_date_they_were_checked():
    """A list price without a date is a claim about today, verified some other day."""
    import datetime as _dt
    import inspect

    from vaf.api import config_routes
    from vaf.core.cost import PRICES_AS_OF

    _dt.date.fromisoformat(PRICES_AS_OF)  # raises if the stamp is not a real date
    assert "PRICES_AS_OF" in inspect.getsource(config_routes.get_usage_prices), (
        "the date must travel to the client with the prices"
    )


def test_every_lane_is_counted_at_the_backend_choke_point(spend_dir):
    """The promise this round exists for: no lane can spend invisibly.

    Driven through APIBackendManager.chat_completion itself rather than a stub
    of it, because the point is that recording happens THERE - a test that
    called record_call directly would pass even if the hook were removed.
    """
    from vaf.core import cost as c
    from vaf.core.api_backend import APIBackendManager

    mgr = APIBackendManager.__new__(APIBackendManager)
    mgr.provider_name = "openai"
    mgr.config = {"api_model_openai": "gpt-4o"}
    mgr.event_sink = None
    mgr.last_request_usage = {"input_tokens": 0, "output_tokens": 0}

    def _impl(*a, **kw):
        # What a provider does: report the call's usage while streaming.
        mgr.last_request_usage = {"input_tokens": 900, "output_tokens": 100}
        yield "hello"

    mgr._chat_completion_impl = _impl

    for lane in ("coder", "vision", "memory"):
        with c.usage_context(lane=lane, scope=None):
            mgr.last_request_usage = {"input_tokens": 0, "output_tokens": 0}
            list(mgr.chat_completion([{"role": "user", "content": "hi"}]))

    out = usage_totals(days=1)
    assert out["totals"]["calls"] == 3, "one ledger entry per model call, whatever the lane"
    assert out["totals"]["tokens"] == 3000
    lanes = json.loads((spend_dir / "default.json").read_text(encoding="utf-8"))
    today = lanes["days"][cost_mod._today()]["lanes"]
    assert set(today) == {"coder", "vision", "memory"}
    assert today["coder"]["tokens"] == 1000


def test_a_failed_call_is_not_billed_twice(spend_dir):
    """last_request_usage survives a failed call; the snapshot is what stops a re-bill."""
    from vaf.core.api_backend import APIBackendManager

    mgr = APIBackendManager.__new__(APIBackendManager)
    mgr.provider_name = "openai"
    mgr.config = {"api_model_openai": "gpt-4o"}
    mgr.event_sink = None
    mgr.last_request_usage = {"input_tokens": 0, "output_tokens": 0}

    def _ok(*a, **kw):
        mgr.last_request_usage = {"input_tokens": 500, "output_tokens": 50}
        yield "x"

    def _boom(*a, **kw):
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover

    mgr._chat_completion_impl = _ok
    list(mgr.chat_completion([]))
    mgr._chat_completion_impl = _boom
    with pytest.raises(RuntimeError):
        list(mgr.chat_completion([]))

    out = usage_totals(days=1)
    assert out["totals"]["calls"] == 1, "the failed call reported nothing and must not be billed"
    assert out["totals"]["tokens"] == 550


def test_the_usage_log_survives_the_debug_switch(tmp_path, monkeypatch):
    """A spend record that a settings toggle can silence is not a record."""
    from vaf.core import log_helper

    monkeypatch.setenv("VAF_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(log_helper, "is_debug_logging_enabled", lambda: False)
    monkeypatch.setattr(cost_mod.Platform, "data_dir", staticmethod(lambda: tmp_path))

    with cost_mod.usage_context(lane="subagent", scope=None):
        cost_mod.record_call("openai", "gpt-4o", 700, 70, session_id="ab12cd34")

    written = list(tmp_path.glob("usage_*.log"))
    assert written, "the per-call usage log must not depend on debug logging"
    line = written[0].read_text(encoding="utf-8")
    assert "lane=subagent" in line and "in=700" in line and "out=70" in line
    assert "model=gpt-4o" in line and "session=ab12cd34" in line


def test_the_report_survives_the_logs_being_deleted(tmp_path, monkeypatch):
    """The ledger is the record; the log is a copy.

    Logs rotate, get swept by the age GC and get deleted by anyone tidying a
    disk. If a total could only be re-derived from them, that moment would take
    the history with it - so the report must be complete with no log present at
    all, and this deletes them to prove it rather than assuming it.
    """
    monkeypatch.setenv("VAF_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(cost_mod.Platform, "data_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cost_mod, "_display_name", lambda scope: "Alice")

    with cost_mod.usage_context(lane="coder", scope=None):
        cost_mod.record_call("openai", "gpt-4o", 1000, 100)

    for log in (tmp_path / "logs").glob("usage_*.log"):
        log.unlink()
    assert not list((tmp_path / "logs").glob("usage_*.log"))

    out = usage_totals(days=1)
    assert out["totals"]["tokens"] == 1100, "the totals come from the ledger, not the log"
    assert out["totals"]["calls"] == 1
    assert out["users"][0]["username"] == "Alice"


def test_every_llm_lane_carries_its_label():
    """A lane the log cannot name is a lane nobody can account for.

    Pinned by source, deliberately: the decorator is one line above a function
    and is exactly the kind of thing a later refactor drops without noticing.
    """
    import pathlib

    for path, lane in (
        ("vaf/memory/rag.py", "memory"),
        ("vaf/core/vision_infer.py", "vision"),
        ("vaf/core/voice_agent.py", "voice"),
        ("vaf/tools/librarian.py", "librarian"),
        ("vaf/api/mail_routes.py", "mail"),
        ("vaf/tools/browser_agent.py", "browser"),
    ):
        src = pathlib.Path(path).read_text(encoding="utf-8")
        assert f'@usage_lane("{lane}")' in src, f"{path} no longer labels its model calls"


def test_the_lane_label_survives_a_streaming_call(tmp_path, monkeypatch):
    """A streaming lane does its work while the CALLER consumes the generator,
    so a decorator that exited at build time would label none of it."""
    monkeypatch.setattr(cost_mod.Platform, "data_dir", staticmethod(lambda: tmp_path))

    @cost_mod.usage_lane("memory")
    def streaming_lane():
        yield cost_mod.current_lane()

    assert list(streaming_lane()) == ["memory"]
    # And the label is gone again afterwards: a lane must not leak into the turn.
    assert cost_mod.current_lane() == "main"
