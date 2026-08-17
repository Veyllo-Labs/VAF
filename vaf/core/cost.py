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
#
# PROVIDER_PRICING is the source, PRICES is the flat index derived from it, so a
# price can never be right in the comparison panel and stale in the estimate.
# Standard list prices only: no cache hits, no batch/off-peak discount, no
# long-context surcharge - a comparison built from each provider's best case
# would flatter whichever one has the most discount programmes. DeepSeek is
# quoted at PEAK (off-peak is half), OpenAI at short-context.
#
# CURRENCY IS NOT CONVERTED. Veyllo publishes EUR, everyone else USD, and this
# module does not carry an exchange rate it would have to keep current - so the
# unit is reported per provider and the reader converts, rather than being shown
# a rate that was true on the day it was typed.
PROVIDER_PRICING = {
    # Veyllo first: it is this product's own API, and the panel keeps that order.
    "veyllo": {"label": "Veyllo", "currency": "EUR", "models": [
        ("veyllo-chat", 0.90, 1.90),
        ("veyllo-vision", 3.45, 20.70),
    ]},
    "anthropic": {"label": "Anthropic", "currency": "USD", "models": [
        ("claude-haiku-4-5", 1.00, 5.00),
        ("claude-sonnet-5", 2.00, 10.00),
        ("claude-sonnet-4-6", 3.00, 15.00),
        ("claude-opus-5", 5.00, 25.00),
        ("claude-opus-4-8", 5.00, 25.00),
        ("claude-fable-5", 10.00, 50.00),
        ("claude-mythos-5", 10.00, 50.00),
    ]},
    "openai": {"label": "OpenAI", "currency": "USD", "models": [
        ("gpt-5.6-luna", 0.20, 1.20),
        ("gpt-5.6-terra", 2.00, 12.00),
        ("gpt-5.6-sol", 5.00, 30.00),
    ]},
    "google": {"label": "Google", "currency": "USD", "models": [
        ("gemini-3.5-flash-lite", 0.10, 0.40),
        ("gemini-3.7-flash", 1.50, 7.50),
        ("gemini-3.5-flash", 1.50, 9.00),
        ("gemini-3.1-pro", 2.00, 12.00),
    ]},
    "deepseek": {"label": "DeepSeek", "currency": "USD", "models": [
        ("deepseek-v4-flash", 0.44, 1.32),
        ("deepseek-v4-pro", 1.32, 3.96),
    ]},
}
# USD per 1M tokens (input, output), flattened from the catalog above plus the
# older model names that still appear in existing ledgers and configs - dropping
# those would silently reprice history at the unknown-model rate.
PRICES = {model: (pin, pout)
          for spec in PROVIDER_PRICING.values()
          for model, pin, pout in spec["models"]}
PRICES.update({
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "o3": (2.00, 8.00),
    "claude-opus-4-1": (15.00, 75.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
})
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
    # The provider's OWN count for this call, carried so the ledger can total
    # tokens as well as money. Deliberately not re-derived here: a token count
    # VAF computes depends on which tokenizer it guessed, while this number is
    # what the provider billed. Money is the estimate; tokens are the fact.
    input_tokens: int = 0
    output_tokens: int = 0

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
            # No money, but the tokens are real and still belong in the ledger:
            # dropping them here made the estimate lie about the call it
            # describes, and a usage view would show 0 for work that happened.
            return CostEstimate(0.0, True, str(model or ""),
                                max(0, int(input_tokens)), max(0, int(output_tokens)))
        name = str(model or "").strip()
        price = PRICES.get(name)
        if price is None:
            # Try the bare name behind a vendor prefix ("anthropic/claude-...").
            price = PRICES.get(name.rsplit("/", 1)[-1])
        known = price is not None
        pin, pout = price or UNKNOWN_PRICE
        _in, _out = max(0, int(input_tokens)), max(0, int(output_tokens))
        usd = (_in * pin + _out * pout) / 1_000_000
        return CostEstimate(round(usd, 6), known, name, _in, _out)
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
        # Tokens are added beside the money rather than replacing it: the money
        # is an estimate from a price table that ages, the tokens are what the
        # provider reported. A ledger written before this existed simply has no
        # token keys, and the reader treats a missing key as zero.
        entry["input_tokens"] = int(entry.get("input_tokens") or 0) + max(0, int(estimate.input_tokens))
        entry["output_tokens"] = int(entry.get("output_tokens") or 0) + max(0, int(estimate.output_tokens))
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


def usage_totals(days: int = 30) -> dict:
    """What the instance spent, per user, over the last *days* days.

    Tokenizer-independent by construction, and that is the whole point: every
    number here was reported by the PROVIDER for a call it billed, never
    counted by a tokenizer of ours. Two providers can disagree about what a
    token is and this total still matches the invoices, because it is the sum
    of what each of them said. The money beside it stays an estimate from the
    price table above, which is why the two are reported separately rather
    than as one figure.

    Reads every ledger in the spend directory, so it is an ADMIN view: one
    tenant must never see another's line. Callers enforce that.
    """
    from datetime import datetime, timedelta

    out = {"days": max(1, int(days or 1)), "users": [], "daily": [], "totals": {
        "input_tokens": 0, "output_tokens": 0, "tokens": 0,
        "usd": 0.0, "calls": 0, "estimated_usd_incomplete": False}}
    try:
        base = Platform.data_dir() / "spend"
        if not base.is_dir():
            return out
        span = max(1, int(days or 1))
        cutoff = (datetime.now() - timedelta(days=span - 1)).strftime("%Y-%m-%d")
        # Every day in the window, including the ones nobody used: a chart that
        # silently drops quiet days makes a burst look like steady traffic.
        by_day = {(datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d"):
                  {"tokens": 0, "calls": 0, "usd": 0.0} for n in range(span)}
        rows = []
        for path in sorted(base.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue  # a corrupt ledger must not hide every other user
            scope = path.stem
            agg = {"input_tokens": 0, "output_tokens": 0, "usd": 0.0,
                   "calls": 0, "unknown_model_calls": 0, "last_active": ""}
            for day, entry in (data.get("days") or {}).items():
                if str(day) < cutoff:
                    continue
                try:
                    _din = int(entry.get("input_tokens") or 0)
                    _dout = int(entry.get("output_tokens") or 0)
                    _dusd = float(entry.get("usd") or 0.0)
                    _dcalls = int(entry.get("calls") or 0)
                    agg["input_tokens"] += _din
                    agg["output_tokens"] += _dout
                    agg["usd"] += _dusd
                    agg["calls"] += _dcalls
                    agg["unknown_model_calls"] += int(entry.get("unknown_model_calls") or 0)
                except Exception:
                    continue
                if str(day) in by_day:
                    slot = by_day[str(day)]
                    slot["tokens"] += _din + _dout
                    slot["calls"] += _dcalls
                    slot["usd"] = round(slot["usd"] + _dusd, 6)
                if str(day) > agg["last_active"]:
                    agg["last_active"] = str(day)
            if not agg["calls"]:
                continue
            agg["tokens"] = agg["input_tokens"] + agg["output_tokens"]
            agg["usd"] = round(agg["usd"], 4)
            agg["scope"] = scope
            agg["username"] = _display_name(scope)
            # A ledger written before tokens were recorded still has calls and
            # money. Saying so beats showing a confident 0 tokens.
            agg["tokens_recorded"] = bool(agg["tokens"])
            rows.append(agg)
        rows.sort(key=lambda r: (r["tokens"], r["usd"]), reverse=True)
        out["users"] = rows
        for r in rows:
            out["totals"]["input_tokens"] += r["input_tokens"]
            out["totals"]["output_tokens"] += r["output_tokens"]
            out["totals"]["usd"] += r["usd"]
            out["totals"]["calls"] += r["calls"]
            if r["unknown_model_calls"]:
                out["totals"]["estimated_usd_incomplete"] = True
        out["totals"]["tokens"] = out["totals"]["input_tokens"] + out["totals"]["output_tokens"]
        out["totals"]["usd"] = round(out["totals"]["usd"], 4)
        out["daily"] = [{"day": d, **by_day[d]} for d in sorted(by_day)]
        # Share of the instance's tokens per account, so "who used the most" is
        # a number rather than a comparison the reader has to do by eye. Falls
        # back to the call share while a legacy ledger has no token counts.
        _tok = out["totals"]["tokens"]
        _cal = out["totals"]["calls"]
        for r in rows:
            r["token_share"] = round(100.0 * r["tokens"] / _tok, 1) if _tok else 0.0
            r["call_share"] = round(100.0 * r["calls"] / _cal, 1) if _cal else 0.0
    except Exception:
        pass  # a reporting view must never raise into a request
    return out


def price_catalog() -> list:
    """Every priced model per provider, for "what would this have cost elsewhere".

    The whole catalogue rather than one representative model, because the panel
    quotes each provider at its CHEAPEST model for the usage being compared, and
    which model that is depends on the input/output ratio - a provider can be
    cheapest on a read-heavy month and not on a write-heavy one. The caller does
    that arithmetic with the real token counts; this returns the prices and the
    unit they are quoted in. Order is deliberate, not alphabetical: Veyllo is
    this product's own API and stays first.
    """
    catalog = []
    for provider, spec in PROVIDER_PRICING.items():
        catalog.append({
            "provider": provider,
            "label": spec["label"],
            "currency": spec["currency"],
            "models": [{"model": m, "input_per_1m": pin, "output_per_1m": pout}
                       for m, pin, pout in spec["models"]],
        })
    return catalog


def _display_name(scope_key: str) -> str:
    """Account name for a ledger file name. Falls back to the key itself."""
    if scope_key == "default":
        try:
            from vaf.core.config import get_local_admin_username
            return str(get_local_admin_username() or "admin")
        except Exception:
            return "admin"
    try:
        from vaf.core.config import resolve_caller_username
        return str(resolve_caller_username(None, scope_key, allow_lookup=True) or scope_key)
    except Exception:
        return scope_key


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
