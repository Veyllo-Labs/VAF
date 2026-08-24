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
from vaf.core.cost import (CostEstimate, cache_usage, estimate_cost, price_catalog,
                           record_spend, usage_totals)


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


def test_the_export_leads_with_a_neutral_note_and_names_its_period(spend_dir):
    """A reader opens this in a spreadsheet and sees numbers before prose, so the
    caveat has to be the first thing in the file - and it has to read as a
    statement of method, not as a disclaimer arguing its own case."""
    import asyncio
    from xml.etree.ElementTree import fromstring

    from vaf.api.config_routes import export_usage

    record_spend(None, estimate_cost("openai", "gpt-4o", 1000, 100))
    root = fromstring(asyncio.run(
        export_usage(request=None, days=30, _admin={"role": "admin"})).body)

    assert list(root)[0].tag == "note", "the note must come first"
    note = root.find("note").text.lower()
    assert "estimate" in note and "not counted by this software" in note
    assert "not converted between currencies" in note
    assert "upper bound" in note
    # The period is named on the document and matches the day rows.
    days = root.findall("daily/day")
    assert root.get("from") == days[0].get("date")
    assert root.get("to") == days[-1].get("date")


def test_the_export_carries_the_breakdowns_and_never_a_currency_sum(spend_dir):
    import asyncio
    from xml.etree.ElementTree import fromstring

    from vaf.api.config_routes import export_usage
    from vaf.core import cost as c

    with c.usage_context(lane="coder", scope=None):
        c.record_call("veyllo", "veyllo-chat", 1_000_000, 0)
    with c.usage_context(lane="main", scope=None):
        c.record_call("openai", "gpt-4o", 1_000_000, 0)

    root = fromstring(asyncio.run(
        export_usage(request=None, days=7, _admin={"role": "admin"})).body)

    # Two currencies, reported side by side rather than added.
    amounts = {a.get("currency"): a.get("value") for a in root.findall("totals/estimated-amount")}
    assert set(amounts) == {"EUR", "USD"}
    assert round(float(amounts["EUR"]), 2) == 0.90
    assert round(float(amounts["USD"]), 2) == 2.50

    assert {p.get("name") for p in root.findall("providers/provider")} == {"veyllo", "openai"}
    today = [d for d in root.findall("daily/day") if d.get("tokens") != "0"][0]
    assert {l.get("name") for l in today.findall("lane")} == {"coder", "main"}
    assert {p.get("model") for p in today.findall("provider")} == {"veyllo-chat", "gpt-4o"}


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
    mgr.session_usage = {"input_tokens": 0, "output_tokens": 0}

    def _impl(*a, **kw):
        # What a provider does: report the call's usage and add it to the
        # running session total, which is what the recorder measures.
        mgr.last_request_usage = {"input_tokens": 900, "output_tokens": 100}
        mgr.session_usage["input_tokens"] += 900
        mgr.session_usage["output_tokens"] += 100
        yield "hello"

    mgr._chat_completion_impl = _impl

    for lane in ("coder", "vision", "memory"):
        with c.usage_context(lane=lane, scope=None):
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
    mgr.session_usage = {"input_tokens": 0, "output_tokens": 0}

    def _ok(*a, **kw):
        mgr.last_request_usage = {"input_tokens": 500, "output_tokens": 50}
        mgr.session_usage["input_tokens"] += 500
        mgr.session_usage["output_tokens"] += 50
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
    # Counted as an attempt, billed for nothing: the failure is visible without
    # inventing tokens for it, and without re-billing the previous call's.
    assert out["totals"]["tokens"] == 550, "a failed call must not inherit the last one's tokens"
    assert out["totals"]["calls"] == 2
    assert out["totals"]["no_usage_calls"] == 1


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


def test_totals_are_the_instance_not_the_heaviest_user(spend_dir):
    """The comparison panel prices `totals`, and totals must span every account.

    The screenshot that prompted this read as though one user's sent-token count
    was being priced; it was in fact the instance's, with the received tokens
    added at the other rate. This pins the property the panel depends on, so a
    later change that scoped totals to one user would fail here rather than in
    a bill.
    """
    record_spend(None, estimate_cost("openai", "gpt-4o", 1000, 100))
    record_spend("ab12cd34", estimate_cost("openai", "gpt-4o", 200, 20))

    out = usage_totals(days=30)

    assert out["totals"]["input_tokens"] == 1200, "every account's sent tokens"
    assert out["totals"]["output_tokens"] == 120, "every account's received tokens"
    assert out["totals"]["tokens"] == 1320
    heaviest = out["users"][0]
    assert heaviest["tokens"] < out["totals"]["tokens"], (
        "the heaviest user must not be mistaken for the instance total"
    )
    assert out["totals"]["tokens"] == sum(u["tokens"] for u in out["users"])


@pytest.mark.parametrize("markers,expected", [
    ({}, "main"),
    ({"_run_kind": "chat"}, "main"),
    ({"_run_kind": "automation"}, "automation"),
    ({"_run_kind": "thinking"}, "thinking"),
    ({"_room_turn": {"room_id": "room-1"}}, "room"),
    ({"_background_run": True}, "background"),
    # A run kind wins over the generic background flag: automations set both.
    ({"_run_kind": "automation", "_background_run": True}, "automation"),
])
def test_unattended_lanes_are_named_not_folded_into_the_chat(markers, expected, monkeypatch):
    """Automations, thinking runs, room turns and background passes all bill the
    owner's API key while nobody is watching. Counting them was never the gap -
    they go through the same backend as everything else - but a bill that calls
    them all "main" cannot tell an operator that a 2am automation is the reason."""
    from vaf.core.agent import Agent

    monkeypatch.delenv("VAF_IN_SUBAGENT_TERMINAL", raising=False)
    cost_mod.set_usage_context(lane="main", scope=None)
    a = Agent.__new__(Agent)
    for k, v in markers.items():
        setattr(a, k, v)
    a._set_usage_context()
    assert cost_mod.current_lane() == expected


def test_a_subagent_process_bills_as_a_subagent(monkeypatch):
    """A sub-agent runs its OWN agent, whose run kind is an ordinary chat - so
    the process marker has to win or every sub-agent would bill as the user."""
    from vaf.core.agent import Agent

    monkeypatch.setenv("VAF_IN_SUBAGENT_TERMINAL", "1")
    cost_mod.set_usage_context(lane="main", scope=None)
    a = Agent.__new__(Agent)
    a._run_kind = "chat"
    a._set_usage_context()
    assert cost_mod.current_lane() == "subagent"


def test_a_non_admin_never_receives_cost_or_anyone_elses_share(spend_dir):
    """Stripped on the SERVER, not hidden in the UI: what a browser never gets
    cannot be read out of a network tab. Their own tokens and calls stay - that
    is their consumption - but the instance's bill is the operator's business,
    and a share is a statement about everyone else."""
    import asyncio
    from types import SimpleNamespace

    from vaf.api import config_routes

    record_spend(None, estimate_cost("openai", "gpt-4o", 1000, 100))
    record_spend("ab12cd34", estimate_cost("openai", "gpt-4o", 400, 40))

    req = SimpleNamespace(state=SimpleNamespace(
        user={"username": "Bob", "role": "user", "user_scope_id": "ab12cd34"}))
    mine = asyncio.run(config_routes.get_usage_me(request=req, days=30))

    assert mine["costs_visible"] is False
    assert mine["totals"]["tokens"] == 440, "own consumption stays visible"
    for row in mine["users"] + [mine["totals"]]:
        assert "usd" not in row, row
        assert "token_share" not in row, row
        assert "call_share" not in row, row
    # And nothing about the other account travels at all.
    assert [r["scope"] for r in mine["users"]] == ["ab12cd34"]

    admin = asyncio.run(config_routes.get_usage(
        request=req, days=30, _admin={"role": "admin"}))
    assert admin["costs_visible"] is True
    assert "usd" in admin["totals"]


def test_a_day_carries_its_lane_breakdown(spend_dir):
    """The bar answers "how much"; the only useful follow-up is "on what"."""
    from vaf.core import cost as c

    with c.usage_context(lane="coder", scope=None):
        c.record_call("openai", "gpt-4o", 1000, 100)
    with c.usage_context(lane="main", scope=None):
        c.record_call("openai", "gpt-4o", 200, 20)

    today = [d for d in usage_totals(days=7)["daily"] if d["tokens"]][0]

    assert today["tokens"] == 1320
    assert today["lanes"]["coder"]["tokens"] == 1100
    assert today["lanes"]["main"]["tokens"] == 220
    assert sum(v["tokens"] for v in today["lanes"].values()) == today["tokens"]


def test_a_non_admin_gets_no_daily_series_at_all(spend_dir):
    """The series is instance-wide - it would show a tenant when everyone else
    was busy, which is the same disclosure the shares were withheld for."""
    import asyncio
    from types import SimpleNamespace

    from vaf.api import config_routes

    record_spend("ab12cd34", estimate_cost("openai", "gpt-4o", 100, 10))
    req = SimpleNamespace(state=SimpleNamespace(
        user={"username": "Bob", "role": "user", "user_scope_id": "ab12cd34"}))

    assert asyncio.run(config_routes.get_usage_me(request=req, days=7))["daily"] == []


def test_a_tool_is_counted_under_its_own_name(spend_dir, monkeypatch):
    """`complete()` already knew who called it; it just never said so to the
    ledger, so a web search inside a chat turn billed as "main"."""
    from vaf.core import completion

    seen = {}
    monkeypatch.setattr(completion, "_complete_inner",
                        lambda *a, **kw: seen.update(lane=cost_mod.current_lane()))
    cost_mod.set_usage_context(lane="main", scope=None)
    completion.complete("hi", caller="tool:web_search")
    assert seen["lane"] == "tool:web_search"
    # And the surrounding turn's lane comes back afterwards.
    assert cost_mod.current_lane() == "main"


def test_a_local_model_call_is_counted_too(spend_dir):
    """Free is not the same as invisible: a local call is still a model call,
    and it is the one lane that never passes the backend manager."""
    from vaf.core import cost as c

    c.record_call("local", "qwen-gguf", 800, 80, lane="tool:librarian")
    out = usage_totals(days=1)
    assert out["totals"]["tokens"] == 880
    assert out["totals"]["usd"] == 0.0, "local tokens cost no API money"


def test_the_coder_books_its_own_calls(spend_dir, monkeypatch):
    """The coder posts from a subprocess over its own HTTP client, so it never
    passes the backend manager - it was the last lane that could spend without
    appearing anywhere, and it is usually the largest."""
    from vaf.core.config import Config
    from vaf.tools import coder

    monkeypatch.setattr(Config, "get",
                        classmethod(lambda cls, k, d=None: "openai" if k == "provider" else d))
    coder._record_coder_usage({"model": "gpt-4o",
                               "usage": {"prompt_tokens": 5000, "completion_tokens": 500}})

    out = usage_totals(days=1)
    assert out["totals"]["tokens"] == 5500
    lanes = json.loads((spend_dir / "default.json").read_text(encoding="utf-8"))
    assert lanes["days"][cost_mod._today()]["lanes"]["coder"]["tokens"] == 5500


def test_the_coder_asks_the_stream_for_its_usage():
    """An OpenAI-compatible server sends none unless asked, and the coder streams."""
    import pathlib

    src = pathlib.Path("vaf/tools/coder.py").read_text(encoding="utf-8")
    assert '"include_usage": True' in src, "the stream would report no usage at all"
    assert "_record_coder_usage(_peek)" in src, "the usage chunk is requested but never read"


def test_amounts_are_kept_per_currency_never_added_across(spend_dir):
    """Veyllo publishes EUR and everyone else USD. Adding them produces a number
    that means nothing, and showing euros with a dollar sign is a lie about the
    unit - so the unit travels with the amount from the call onwards."""
    record_spend(None, estimate_cost("veyllo", "veyllo-chat", 1_000_000, 0))   # 0.90 EUR
    record_spend(None, estimate_cost("openai", "gpt-4o", 1_000_000, 0))        # 2.50 USD

    out = usage_totals(days=1)
    cur = out["totals"]["currencies"]

    assert round(cur["EUR"], 2) == 0.90
    assert round(cur["USD"], 2) == 2.50
    assert "EUR" in cur and "USD" in cur, "the two must stay apart"
    # The legacy field still exists for ledgers and the cap that read it.
    assert round(out["totals"]["usd"], 2) == 3.40


def test_the_estimate_states_its_own_unit():
    assert estimate_cost("veyllo", "veyllo-chat", 100, 10).currency == "EUR"
    assert estimate_cost("openai", "gpt-4o", 100, 10).currency == "USD"
    assert "€" in estimate_cost("veyllo", "veyllo-chat", 1_000_000, 0).as_text()
    assert "$" in estimate_cost("openai", "gpt-4o", 1_000_000, 0).as_text()


def test_the_ledger_says_which_provider_ran_where(spend_dir):
    """The product runs several at once - chat on one, vision on another,
    sub-agents and the thinker elsewhere, any of them local. "What did this
    cost" is unanswerable without saying where it ran."""
    from vaf.core import cost as c

    with c.usage_context(lane="main", scope=None):
        c.record_call("veyllo", "veyllo-chat", 1_000_000, 0)
    with c.usage_context(lane="vision", scope=None):
        c.record_call("openai", "gpt-4o", 100_000, 0)
    with c.usage_context(lane="subagent", scope=None):
        c.record_call("local", "qwen-gguf", 50_000, 0)

    out = usage_totals(days=1)
    provs = out["totals"]["providers"]

    assert set(provs) == {"veyllo/veyllo-chat", "openai/gpt-4o", "local/qwen-gguf"}
    assert provs["veyllo/veyllo-chat"]["tokens"] == 1_000_000
    assert round(provs["veyllo/veyllo-chat"]["currencies"]["EUR"], 2) == 0.90
    assert round(provs["openai/gpt-4o"]["currencies"]["USD"], 2) == 0.25
    assert provs["local/qwen-gguf"]["usd"] == 0.0, "local runs cost no API money"
    # And the same split is on the day, so a bar can be opened on it.
    day = [d for d in out["daily"] if d["tokens"]][0]
    assert set(day["providers"]) == set(provs)


def test_nothing_is_lost_when_old_and_new_records_meet(spend_dir):
    """The live defect: a period holding records from before the currency was
    stored displayed only the new amounts, so a month of spending hid behind a
    three-cent figure. Amounts that cannot be attributed to a currency are
    carried as unknown - never guessed into a real one, and never dropped."""
    spend_dir.mkdir(parents=True, exist_ok=True)
    (spend_dir / "default.json").write_text(json.dumps({
        "format": cost_mod.SPEND_FORMAT,
        "days": {
            # Fully old: a bare number, no currency anywhere.
            "2001-01-01": {"usd": 4.0, "calls": 10},
            # PART old: the day the change landed carries calls from both sides.
            cost_mod._today(): {"usd": 3.0, "calls": 5, "currencies": {"EUR": 1.0}},
        },
    }), encoding="utf-8")

    out = usage_totals(days=10_000)
    cur = out["totals"]["currencies"]

    assert cur["EUR"] == 1.0
    assert cur["?"] == 6.0, "4.00 fully old + 2.00 unattributed remainder"
    assert round(sum(cur.values()), 6) == round(out["totals"]["usd"], 6), (
        "the per-currency amounts must add up to the recorded total"
    )


def test_the_rate_is_cached_and_never_invented(tmp_path, monkeypatch):
    """A converted figure is only honest if the rate behind it can be seen, so a
    failed fetch returns nothing rather than a guess - and a cached rate carries
    its own publication date so the reader can judge its age."""
    import json as _json

    from vaf.core import cost as c

    monkeypatch.setattr(c.Platform, "data_dir", staticmethod(lambda: tmp_path))
    calls = {"n": 0}

    class _Resp:
        ok = True

        @staticmethod
        def json():
            calls["n"] += 1
            return {"rates": {"USD": 1.16}, "date": "2026-08-17"}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp())

    first = c.fx_rate()
    assert first["rate"] == 1.16 and first["date"] == "2026-08-17"
    assert first["source"] == "European Central Bank", "the source must be named"

    # Second call inside the window is served from disk: the ECB publishes once
    # a business day, so asking per page view is traffic on a free service.
    c.fx_rate()
    assert calls["n"] == 1
    assert _json.loads((tmp_path / c._FX_CACHE).read_text())["EURUSD"]["rate"] == 1.16


def test_no_rate_means_no_conversion_offered(tmp_path, monkeypatch):
    from vaf.core import cost as c

    monkeypatch.setattr(c.Platform, "data_dir", staticmethod(lambda: tmp_path))

    import requests
    def _boom(*a, **kw):
        raise OSError("offline")
    monkeypatch.setattr(requests, "get", _boom)

    assert c.fx_rate() is None, "a missing rate must not become a made-up one"


def test_stamping_legacy_amounts_fills_only_what_is_missing(spend_dir):
    """The operator states the currency; the software never infers it. And it
    touches only the unattributed part - a day that straddles the change keeps
    the amounts it already recorded."""
    spend_dir.mkdir(parents=True, exist_ok=True)
    (spend_dir / "default.json").write_text(json.dumps({
        "format": cost_mod.SPEND_FORMAT,
        "days": {
            "2001-01-01": {"usd": 4.0, "calls": 10},
            "2001-01-02": {"usd": 3.0, "calls": 5, "currencies": {"EUR": 1.0}},
            "2001-01-03": {"usd": 2.0, "calls": 2, "currencies": {"USD": 2.0}},
        },
    }), encoding="utf-8")

    res = cost_mod.stamp_legacy_currency("EUR")

    assert res == {"files": 1, "days": 2, "amount": 6.0, "currency": "EUR"}
    days = json.loads((spend_dir / "default.json").read_text(encoding="utf-8"))["days"]
    assert days["2001-01-01"]["currencies"] == {"EUR": 4.0}
    assert days["2001-01-02"]["currencies"] == {"EUR": 3.0}, "1.00 kept + 2.00 stamped"
    assert days["2001-01-03"]["currencies"] == {"USD": 2.0}, "already attributed: untouched"
    assert (spend_dir / "default.json.bak").exists(), "the record is backed up first"

    # Running it twice changes nothing more: there is no unattributed part left.
    assert cost_mod.stamp_legacy_currency("EUR")["days"] == 0
    out = usage_totals(days=10_000)
    assert "?" not in out["totals"]["currencies"]


def test_stamping_refuses_a_currency_it_cannot_display():
    assert "error" in cost_mod.stamp_legacy_currency("CHF")


def test_two_identical_calls_are_both_counted(spend_dir):
    """Measured against a provider's dashboard: VAF was reporting fewer tokens
    than were billed. The recorder compared the last request's numbers against a
    snapshot, so a call whose counts matched the previous one exactly looked
    like "nothing new" and was dropped - and the utility lanes send near-
    identical prompts back to back, so it was not rare."""
    from vaf.core.api_backend import APIBackendManager

    mgr = APIBackendManager.__new__(APIBackendManager)
    mgr.provider_name = "openai"
    mgr.config = {"api_model_openai": "gpt-4o"}
    mgr.event_sink = None
    mgr.last_request_usage = {"input_tokens": 0, "output_tokens": 0}
    mgr.session_usage = {"input_tokens": 0, "output_tokens": 0}

    def _same(*a, **kw):
        # The SAME counts every time, which is exactly the case that vanished.
        mgr.last_request_usage = {"input_tokens": 700, "output_tokens": 70}
        mgr.session_usage["input_tokens"] += 700
        mgr.session_usage["output_tokens"] += 70
        yield "x"

    mgr._chat_completion_impl = _same
    for _ in range(3):
        list(mgr.chat_completion([]))

    out = usage_totals(days=1)
    assert out["totals"]["calls"] == 3, "every call must be booked, identical or not"
    assert out["totals"]["tokens"] == 3 * 770


def test_a_call_the_provider_never_reported_is_counted_as_such(spend_dir):
    """We always know WHO called - lane, provider and model are decided before
    the request goes out. What an aborted stream withholds is the provider's own
    usage report. Counting the call with zero tokens turns the gap against an
    invoice into a number the reader can see and locate, which is the honest
    alternative to adding a safety margin nobody could verify."""
    from vaf.core.api_backend import APIBackendManager

    mgr = APIBackendManager.__new__(APIBackendManager)
    mgr.provider_name = "openai"
    mgr.config = {"api_model_openai": "gpt-4o"}
    mgr.event_sink = None
    mgr.last_request_usage = {"input_tokens": 0, "output_tokens": 0}
    mgr.session_usage = {"input_tokens": 0, "output_tokens": 0}

    def _ok(*a, **kw):
        mgr.session_usage["input_tokens"] += 400
        mgr.session_usage["output_tokens"] += 40
        yield "x"

    def _silent(*a, **kw):
        yield "x"          # a stream that ends without ever reporting usage

    mgr._chat_completion_impl = _ok
    list(mgr.chat_completion([]))
    mgr._chat_completion_impl = _silent
    list(mgr.chat_completion([]))

    out = usage_totals(days=1)
    assert out["totals"]["calls"] == 2, "both attempts are calls"
    assert out["totals"]["no_usage_calls"] == 1, "the silent one is named, not hidden"
    # The silent call is sized by the fallback rather than left at zero, and the
    # part that came from it is counted separately - so a reader can subtract it
    # and get back to the figures the provider actually reported.
    assert out["totals"]["estimated_tokens"] > 0
    assert out["totals"]["tokens"] - out["totals"]["estimated_tokens"] == 440


def test_a_silent_call_falls_back_to_a_marked_estimate(spend_dir, tmp_path, monkeypatch):
    """When the provider reports nothing, the call is still sized - roughly, from
    words and symbols - so the total is not short. What makes that acceptable is
    that it says so: the log line, the ledger and the total all record how much
    of the figure was estimated rather than reported."""
    monkeypatch.setenv("VAF_LOG_DIR", str(tmp_path))
    from vaf.core.api_backend import APIBackendManager

    mgr = APIBackendManager.__new__(APIBackendManager)
    mgr.provider_name = "openai"
    mgr.config = {"api_model_openai": "gpt-4o"}
    mgr.event_sink = None
    mgr.last_request_usage = {"input_tokens": 0, "output_tokens": 0}
    mgr.session_usage = {"input_tokens": 0, "output_tokens": 0}
    mgr._chat_completion_impl = lambda *a, **kw: iter(["hello there ", "friend"])

    list(mgr.chat_completion([{"role": "user", "content": "count these words, please!"}]))

    out = usage_totals(days=1)
    assert out["totals"]["calls"] == 1
    assert out["totals"]["no_usage_calls"] == 1
    assert out["totals"]["tokens"] > 0, "a silent call must not leave a hole"
    assert out["totals"]["estimated_tokens"] == out["totals"]["tokens"], (
        "all of it came from the fallback, and the total must say so"
    )
    line = list(tmp_path.glob("usage_*.log"))[0].read_text(encoding="utf-8")
    assert "usage=estimated" in line, "the log must name the fallback where it fired"


def test_the_rough_count_counts_words_and_symbols():
    from vaf.core.cost import estimate_tokens_roughly

    assert estimate_tokens_roughly("hello world") == 2
    assert estimate_tokens_roughly("count these words, please!") == 6   # 4 words + , + !
    assert estimate_tokens_roughly("") == 0


def test_the_ledger_maintenance_is_reachable_from_the_cli():
    """Rule of layers: a maintenance action that rewrites the spend ledger must
    not require reaching into Python. It was built and RUN that way once, which
    is exactly the gap this pins - a headless install has no settings tab."""
    from vaf.cli.cmd import usage as usage_cmd

    names = {c.name for c in usage_cmd.app.registered_commands}
    assert {"show", "set-currency"} <= names, names

    import vaf.main as main_mod
    src = __import__("pathlib").Path(main_mod.__file__).read_text(encoding="utf-8")
    assert 'app.add_typer(usage.app, name="usage"' in src, "the group is never mounted"


def test_the_cli_prints_one_amount_or_a_dash():
    """Same rule as the tab: euros and dollars are not added, and an amount with
    no recorded currency is not folded into one."""
    from vaf.cli.cmd.usage import _fmt

    assert _fmt({"EUR": 1.5}, 1.5) == "~€1.50"
    assert _fmt({"USD": 2.0}, 2.0) == "~$2.00"
    assert _fmt({"EUR": 1.0, "USD": 2.0}, 3.0) == "-"
    assert _fmt({"?": 4.0}, 4.0) == "-"
    assert _fmt({"EUR": 0.0001}, 0.0) == "~$0.00", "a sub-cent residue is not a currency"


# ── The prompt cache in the ledger ────────────────────────────────────────────
# The ledger is the record and the log is a copy, so every figure a report shows
# has to survive the log being rotated, swept or deleted. The denominator is the
# part that is easy to get wrong: a hit rate over `input_tokens` would divide by
# a total that mixes two reporting conventions AND includes every call from a
# provider that reports nothing about its cache.


def test_the_ledger_records_what_was_served_from_a_cache(spend_dir):
    record_spend(None, estimate_cost("anthropic", "claude-sonnet-4-6", 12, 50,
                                     cache=cache_usage(8400, 1200, in_input=False)),
                 provider="anthropic")
    totals = usage_totals(days=30)["totals"]
    assert totals["cache_read_tokens"] == 8400
    assert totals["cache_write_tokens"] == 1200
    assert totals["cache_measured_calls"] == 1
    assert totals["cache_measured_input_tokens"] == 9612      # 12 + 8400 + 1200


def test_a_provider_that_reports_nothing_stays_out_of_the_denominator(spend_dir):
    """MUTATION: count every call in cache_measured_calls and a lane that cannot
    report drags the displayed hit rate towards zero, which is exactly the
    direction that hides a broken cache."""
    record_spend(None, estimate_cost("openai", "gpt-5.6-terra", 9000, 50,
                                     cache=cache_usage(8400, 0, in_input=True)),
                 provider="openai")
    record_spend(None, estimate_cost("local", "some-gguf", 5000, 100), provider="local")
    totals = usage_totals(days=30)["totals"]
    assert totals["calls"] == 2
    assert totals["cache_measured_calls"] == 1
    assert totals["cache_measured_input_tokens"] == 9000
    assert totals["cache_read_tokens"] / totals["cache_measured_input_tokens"] > 0.9


def test_the_provider_rows_carry_their_own_cache_counts(spend_dir):
    """A cache is a property of the provider a call ran on, so the breakdown has
    to be able to say WHICH provider is hitting and which is not."""
    record_spend(None, estimate_cost("anthropic", "claude-sonnet-4-6", 12, 50,
                                     cache=cache_usage(8400, 0, in_input=False)),
                 provider="anthropic")
    record_spend(None, estimate_cost("veyllo", "veyllo-chat", 1000, 100), provider="veyllo")
    rows = usage_totals(days=30)["totals"]["providers"]
    assert rows["anthropic/claude-sonnet-4-6"]["cache_read_tokens"] == 8400
    assert rows["veyllo/veyllo-chat"]["cache_measured_calls"] == 0


def test_a_ledger_written_before_the_cache_counters_still_reports(spend_dir):
    """The change is purely additive and every read defaults to zero, which is
    why the format tag did not have to move."""
    spend_dir.mkdir(parents=True, exist_ok=True)
    (spend_dir / "default.json").write_text(json.dumps({
        "format": cost_mod.SPEND_FORMAT,
        "days": {"2026-01-01": {"usd": 1.0, "calls": 2, "input_tokens": 10,
                                "output_tokens": 5, "unknown_model_calls": 0}},
    }), encoding="utf-8")
    totals = usage_totals(days=100000)["totals"]
    assert totals["calls"] == 2
    assert totals["cache_read_tokens"] == 0
    assert totals["cache_measured_calls"] == 0


def test_what_the_discount_was_worth_is_carried_per_currency(spend_dir):
    record_spend(None, estimate_cost("anthropic", "claude-sonnet-4-6", 12, 50,
                                     cache=cache_usage(8400, 0, in_input=False)),
                 provider="anthropic")
    saved = usage_totals(days=30)["totals"]["cache_saved"]
    assert saved == {"USD": round(8400 * 3.00 * 0.90 / 1_000_000, 6)}


def test_the_cache_totals_survive_the_log_being_deleted(spend_dir):
    """THE test that enforces `record_call`'s own rule: the ledger is the record
    and the usage log is a copy for a human reading over the machine's shoulder.
    Logs are rotated, swept by the age GC and deleted by anyone tidying a disk,
    so a figure that had to be re-derived from them would lose history the
    moment that happened.

    The first version of this test globbed the spend directory's parent and
    counted what it removed without ever asserting on the count. The session
    conftest redirects VAF_LOG_DIR to a different tree entirely, so it deleted
    nothing and passed either way. The log directory is therefore ASKED FOR
    here, and the test refuses to draw a conclusion from a deletion that did not
    happen - the same reason `test_the_doc_actually_makes_admin_only_claims`
    exists next to the claims it checks.

    MUTATION: report the cache numbers from the log line only and this goes red.
    """
    from vaf.core.cost import record_call
    from vaf.core.log_helper import get_app_log_dir

    record_call("anthropic", "claude-sonnet-4-6", 12, 50, lane="main",
                cache=cache_usage(8400, 1200, in_input=False))
    before = usage_totals(days=30)["totals"]
    assert before["cache_read_tokens"] == 8400

    logs = [p for p in get_app_log_dir().glob("usage_*.log")]
    assert logs, "record_call wrote no usage log, so this test would prove nothing"
    written = "".join(p.read_text(encoding="utf-8", errors="replace") for p in logs)
    assert "cache_read=8400" in written, "the copy under test was never written"
    for p in logs:
        p.unlink()

    after = usage_totals(days=30)["totals"]
    assert after["cache_read_tokens"] == 8400
    assert after["cache_measured_calls"] == before["cache_measured_calls"]
    assert after["cache_measured_input_tokens"] == before["cache_measured_input_tokens"]
