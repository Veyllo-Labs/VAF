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
from vaf.core.cost import CostEstimate, estimate_cost, record_spend, usage_totals


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


def test_price_catalog_never_shows_a_price_it_cannot_justify():
    from vaf.core.cost import PRICES, price_catalog

    catalog = price_catalog()
    assert catalog, "the comparison panel needs at least one priced provider"
    for row in catalog:
        assert row["model"], "a comparison without its model is not a comparison"
        price = PRICES.get(row["model"]) or PRICES.get(row["model"].rsplit("/", 1)[-1])
        assert price == (row["input_per_1m"], row["output_per_1m"]), (
            "the panel must quote the SAME table the ledger estimates with"
        )


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
