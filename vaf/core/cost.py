# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Per-user API spend: an honest estimate, and a cap that can stop a turn.

VAF measured tokens per call and threw the number away. Nothing aggregated
them, nothing persisted them, and nothing could stop a run - while an instance
serves several LAN tenants plus automations and thinking runs on the owner's
API keys. A loop that starts at 2am ends when the bill says so.

Two deliberate properties:

- The number is an ESTIMATE and says so. `cost_known` is False when the model
  is not in the table, and an unknown model is priced HIGH on purpose, so a cap
  trips early rather than late. A cap that silently under-counts is worse than
  none, because it looks like protection.
- The ledger is per user (`~/.vaf/spend/<scope>.json`, local admin ->
  `default.json`, the shape reminders.py and thinking_workspace already use)
  and per day. One tenant cannot spend another's budget, and the owner can see
  who spent what without a database.

The cap is checked at the TURN BOUNDARY next to the wall-clock stop, never
mid-tool: stopping between a tool call and its result would break tool-call
adjacency (Rule 4.1).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from vaf.core.platform import Platform

SPEND_FORMAT = "spend-1-9c14f7"

# USD per 1M tokens (input, output). Public list prices, rounded up rather than
# down - this table decides when a cap trips, so erring low would be the
# expensive direction.
PRICES = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "o3": (2.00, 8.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-1": (15.00, 75.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "deepseek-v4-flash": (0.28, 0.42),
    "deepseek-v4-pro": (0.55, 2.19),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "veyllo-chat": (1.00, 3.00),
}
# What an unrecognised model costs. Deliberately at the expensive end of the
# table: an unknown model must not be able to run cheap under a cap.
UNKNOWN_PRICE = (15.00, 75.00)
# Local models cost no API money. They still burn electricity, which this
# module deliberately does not pretend to price.
FREE_PROVIDERS = frozenset({"local", ""})


@dataclass
class CostEstimate:
    usd: float
    cost_known: bool
    model: str = ""

    def as_text(self) -> str:
        return f"~${self.usd:.4f}" + ("" if self.cost_known else " (estimate: unknown model)")


def _scope_key(user_scope_id: Optional[str]) -> str:
    """Canonical per-user key. Mirrors thinking_workspace._scope_key."""
    if user_scope_id is None or not str(user_scope_id).strip():
        return "default"
    try:
        from vaf.core.config import get_local_admin_scope_id
        if str(user_scope_id).strip() == str(get_local_admin_scope_id()).strip():
            return "default"
    except Exception:
        pass
    return str(user_scope_id).strip()


def _ledger_path(user_scope_id: Optional[str]) -> Path:
    return Platform.data_dir() / "spend" / f"{_scope_key(user_scope_id)}.json"


def _today() -> str:
    """The user's day, not UTC - a budget is a human period."""
    try:
        from vaf.core.user_time import user_now
        return user_now().strftime("%Y-%m-%d")
    except Exception:
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d")


def estimate_cost(provider: str, model: str, input_tokens: int,
                  output_tokens: int) -> CostEstimate:
    """What this call probably cost. Never raises."""
    try:
        prov = str(provider or "").strip().lower()
        if prov in FREE_PROVIDERS:
            return CostEstimate(0.0, True, str(model or ""))
        name = str(model or "").strip()
        price = PRICES.get(name)
        if price is None:
            # Try the bare name behind a vendor prefix ("anthropic/claude-...").
            price = PRICES.get(name.rsplit("/", 1)[-1])
        known = price is not None
        pin, pout = price or UNKNOWN_PRICE
        usd = (max(0, int(input_tokens)) * pin + max(0, int(output_tokens)) * pout) / 1_000_000
        return CostEstimate(round(usd, 6), known, name)
    except Exception:
        return CostEstimate(0.0, False, str(model or ""))


def record_spend(user_scope_id: Optional[str], estimate: CostEstimate) -> float:
    """Add an estimate to today's ledger and return the new day total.

    Best-effort: a ledger that cannot be written must never break a turn.
    """
    try:
        path = _ledger_path(user_scope_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        day = _today()
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        days = data.get("days") or {}
        entry = days.get(day) or {"usd": 0.0, "calls": 0, "unknown_model_calls": 0}
        entry["usd"] = round(float(entry.get("usd") or 0.0) + estimate.usd, 6)
        entry["calls"] = int(entry.get("calls") or 0) + 1
        if not estimate.cost_known:
            entry["unknown_model_calls"] = int(entry.get("unknown_model_calls") or 0) + 1
        days[day] = entry
        # Keep the last 60 days: enough to answer "who spent what", small enough
        # to stay a file.
        for stale in sorted(days)[:-60]:
            days.pop(stale, None)
        data = {"format": SPEND_FORMAT, "days": days}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
        return float(entry["usd"])
    except Exception:
        return 0.0


def spent_today(user_scope_id: Optional[str]) -> float:
    try:
        path = _ledger_path(user_scope_id)
        if not path.exists():
            return 0.0
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(((data.get("days") or {}).get(_today()) or {}).get("usd") or 0.0)
    except Exception:
        return 0.0


def budget_exceeded(user_scope_id: Optional[str]) -> Tuple[bool, float, float]:
    """(exceeded, spent_today, budget). Budget 0 means no cap - the default."""
    try:
        from vaf.core.config import Config
        budget = float(Config.get("spend_budget_usd_per_day", 0) or 0)
    except Exception:
        budget = 0.0
    if budget <= 0:
        return False, spent_today(user_scope_id), 0.0
    spent = spent_today(user_scope_id)
    return (spent >= budget), spent, budget
