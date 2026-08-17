# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Per-user API spend estimate and cap (vaf/core/cost.py).

VAF measured tokens per call and discarded them: nothing aggregated, nothing
persisted, nothing could stop a run - on an instance that serves several LAN
tenants plus automations and thinking runs from the owner's API keys.

The properties that make a cap trustworthy are pinned here: an unknown model
must be priced HIGH (a cap that under-counts looks like protection and is
not), the ledger must be per user, and the default must be off so the estimate
can be measured before anyone caps anything.
"""
import json

import pytest

from vaf.core import cost
from vaf.core.config import Config

SCOPE_A = "aaaaaaaa-1111-2222-3333-444444444444"
SCOPE_B = "bbbbbbbb-5555-6666-7777-888888888888"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(cost.Platform, "data_dir", staticmethod(lambda: tmp_path))
    return tmp_path


def test_a_known_model_is_priced_from_the_table():
    est = cost.estimate_cost("openai", "gpt-4o", 1_000_000, 1_000_000)
    assert est.cost_known is True
    assert est.usd == pytest.approx(12.50, rel=1e-6)


def test_an_unknown_model_is_priced_high_and_says_so():
    """A cap must trip early on something it cannot price, not late."""
    est = cost.estimate_cost("openai", "some-model-shipped-tomorrow", 1_000_000, 0)
    assert est.cost_known is False
    assert est.usd >= 15.0, "an unknown model was priced cheaply"
    assert "estimate" in est.as_text()


def test_local_models_cost_nothing_and_that_is_known():
    est = cost.estimate_cost("local", "qwen3.5-4b", 5_000_000, 5_000_000)
    assert est.usd == 0.0 and est.cost_known is True


def test_spend_is_recorded_per_user(ledger):
    cost.record_spend(SCOPE_A, cost.CostEstimate(1.50, True, "gpt-4o"))
    cost.record_spend(SCOPE_A, cost.CostEstimate(0.50, True, "gpt-4o"))
    cost.record_spend(SCOPE_B, cost.CostEstimate(9.99, True, "gpt-4o"))
    assert cost.spent_today(SCOPE_A) == pytest.approx(2.00)
    assert cost.spent_today(SCOPE_B) == pytest.approx(9.99)
    assert cost.spent_today(None) == 0.0, "one tenant's spend leaked into another ledger"


def test_the_ledger_counts_how_often_it_was_guessing(ledger):
    cost.record_spend(SCOPE_A, cost.CostEstimate(1.0, False, "mystery"))
    data = json.loads((ledger / "spend" / f"{SCOPE_A}.json").read_text(encoding="utf-8"))
    day = next(iter(data["days"].values()))
    assert day["unknown_model_calls"] == 1
    assert data["format"] == "spend-1-9c14f7", "the persisted format tag changed"


def test_no_cap_by_default(ledger, monkeypatch):
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: 0 if k ==
                                                   "spend_budget_usd_per_day" else d))
    cost.record_spend(SCOPE_A, cost.CostEstimate(9999.0, True, "gpt-4o"))
    exceeded, spent, budget = cost.budget_exceeded(SCOPE_A)
    assert exceeded is False and budget == 0.0
    assert spent == pytest.approx(9999.0), "the estimate must be recorded even without a cap"


def test_a_cap_trips_for_its_own_user_only(ledger, monkeypatch):
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: 5.0 if k ==
                                                   "spend_budget_usd_per_day" else d))
    cost.record_spend(SCOPE_A, cost.CostEstimate(6.0, True, "gpt-4o"))
    assert cost.budget_exceeded(SCOPE_A)[0] is True
    assert cost.budget_exceeded(SCOPE_B)[0] is False


def test_accounting_never_breaks_a_turn(monkeypatch):
    """Best-effort by design: an unwritable ledger must not raise into chat_step."""
    monkeypatch.setattr(cost.Platform, "data_dir",
                        staticmethod(lambda: (_ for _ in ()).throw(OSError("no disk"))))
    assert cost.record_spend(SCOPE_A, cost.CostEstimate(1.0, True, "x")) == 0.0
    assert cost.spent_today(SCOPE_A) == 0.0


def test_the_budget_key_is_admin_write_only():
    assert Config.DEFAULTS["spend_budget_usd_per_day"] == 0
    assert Config.is_global_config_key("spend_budget_usd_per_day")
    assert Config.filter_for_non_admin({"spend_budget_usd_per_day": 999}) == {}


def test_every_call_is_billed_at_the_backend_not_at_the_turn():
    """Billing per TURN could only ever see the chat lane.

    A turn-boundary hook counted the main chat and nothing else: the coder,
    sub-agents, vision, voice, memory compaction, the mail composer and the
    browser agent all reached a model by other routes and spent invisibly.
    Recording moved to the one method every lane passes through, and the turn
    now only says whose turn it is. This pins that the hook did not quietly
    come back alongside it - two counters would double-bill the chat lane."""
    from pathlib import Path
    agent_src = Path("vaf/core/agent.py").read_text(encoding="utf-8")
    backend_src = Path("vaf/core/api_backend.py").read_text(encoding="utf-8")

    assert "_record_turn_spend" not in agent_src, \
        "the per-turn billing hook is back; it would double-count the chat lane"
    assert agent_src.count("def _set_usage_context") == 1
    assert "self._set_usage_context()" in agent_src, \
        "nothing labels the turn, so every call would land on the default ledger"

    i = backend_src.index("def chat_completion(self, messages")
    j = backend_src.index("def _record_call_usage")
    assert "self._record_call_usage(_before, model)" in backend_src[i:j], \
        "the public entry point no longer records the call it just made"
    assert backend_src.count("def _record_call_usage") == 1


def test_the_turn_boundary_carries_the_stop():
    """The cap must be checked where the wall-clock stop is - never mid-tool,
    which would cut between a tool call and its result (Rule 4.1)."""
    from pathlib import Path
    src = Path("vaf/core/agent.py").read_text(encoding="utf-8")
    i = src.index("from vaf.core.cost import budget_exceeded")
    j = src.index("time.monotonic() > _turn_deadline")
    assert 0 < j - i < 2500, "the spend stop drifted away from the turn boundary"
    region = src[i:j]
    assert "spend_budget_usd_per_day" in region, "the stop no longer names the config key"
    # The verdict must be ACTED on, not merely computed: a check whose result
    # goes nowhere is the failure mode this pin exists for.
    assert "if _over:" in region, "the budget verdict is computed but not acted on"
    assert "return _sp_msg" in region, "reaching the cap no longer ends the turn"
    # Spend is no longer recorded here (the backend books every call as it
    # happens), so the boundary only READS the ledger - which is also why the
    # cap now sees the coder and the sub-agents, not just the chat.
