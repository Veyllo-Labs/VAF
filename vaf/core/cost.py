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
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from vaf.core.platform import Platform

SPEND_FORMAT = "spend-1-9c14f7"

# Distinguishes "caller passed None" from "caller passed nothing" for the two
# context-carried arguments, where None is itself a meaningful value (the local
# admin's ledger).
_UNSET = object()

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
#
# When these prices were last checked against the providers' own pricing pages.
# Shown wherever a price is, because a list price without a date is a claim
# about today that was true on some other day: providers reprice, and a stale
# figure presented as current is worse than no comparison. Update this stamp in
# the SAME change as any price below - the guard test pins the shape, not the
# value, so nothing stops a price moving while the date stands still except
# this sentence and the reviewer reading it.
PRICES_AS_OF = "2026-08-17"
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
    # The unit the PROVIDER publishes, carried per call because it cannot be
    # recovered later: Veyllo prices in EUR and everyone else in USD, so a
    # ledger that stored only a number was showing euros with a dollar sign,
    # and two providers' amounts could be added into a figure that means
    # nothing. The field name `usd` stays for ledgers written before this.
    currency: str = "USD"

    def as_text(self) -> str:
        sym = "€" if self.currency == "EUR" else "$"
        return f"~{sym}{self.usd:.4f}" + ("" if self.cost_known else " (estimate: unknown model)")


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


def _currency_for(provider: str) -> str:
    """The unit a provider publishes its list prices in. Unknown -> USD."""
    spec = PROVIDER_PRICING.get(str(provider or "").strip().lower())
    return str((spec or {}).get("currency") or "USD")


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
                                max(0, int(input_tokens)), max(0, int(output_tokens)),
                                currency=_currency_for(prov))
        name = str(model or "").strip()
        price = PRICES.get(name)
        if price is None:
            # Try the bare name behind a vendor prefix ("anthropic/claude-...").
            price = PRICES.get(name.rsplit("/", 1)[-1])
        known = price is not None
        pin, pout = price or UNKNOWN_PRICE
        _in, _out = max(0, int(input_tokens)), max(0, int(output_tokens))
        usd = (_in * pin + _out * pout) / 1_000_000
        return CostEstimate(round(usd, 6), known, name, _in, _out,
                            currency=_currency_for(prov))
    except Exception:
        return CostEstimate(0.0, False, str(model or ""))


_LANE: "ContextVar[str]" = ContextVar("vaf_usage_lane", default="main")
_SCOPE: "ContextVar[Optional[str]]" = ContextVar("vaf_usage_scope", default=None)


@contextmanager
def usage_context(lane: Optional[str] = None, scope: Any = _UNSET):
    """Name the lane (and optionally the account) for the calls made inside.

    Accounting has to happen where the tokens ARRIVE - one place in the backend -
    but only the caller knows whether this is a chat turn, a coder run or a
    memory compaction. So the caller labels, the backend records. Nested lanes
    restore the outer one, and a lane that is never set reads as ``main``.
    """
    lane_token = _LANE.set(str(lane)) if lane else None
    scope_token = None if scope is _UNSET else _SCOPE.set(None if scope is None else str(scope))
    try:
        yield
    finally:
        if lane_token is not None:
            _LANE.reset(lane_token)
        if scope_token is not None:
            _SCOPE.reset(scope_token)


def usage_lane(name: str):
    """Label every model call made inside the decorated function.

    A decorator rather than a ``with`` block at each site, because the lanes
    that need labelling are whole functions whose bodies would otherwise have to
    be reindented - a diff nobody can review for the one thing it changes. The
    generator branch matters: a lane that streams does its work while the caller
    consumes it, so wrapping only the call that BUILDS the generator would leave
    the label off every token it later yields.
    """
    def decorate(fn):
        import functools
        import inspect

        if inspect.isasyncgenfunction(fn):
            @functools.wraps(fn)
            async def agen(*args, **kwargs):
                with usage_context(lane=name):
                    async for item in fn(*args, **kwargs):
                        yield item
            return agen
        if inspect.isgeneratorfunction(fn):
            @functools.wraps(fn)
            def gen(*args, **kwargs):
                with usage_context(lane=name):
                    yield from fn(*args, **kwargs)
            return gen
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def acall(*args, **kwargs):
                with usage_context(lane=name):
                    return await fn(*args, **kwargs)
            return acall

        @functools.wraps(fn)
        def call(*args, **kwargs):
            with usage_context(lane=name):
                return fn(*args, **kwargs)
        return call
    return decorate


def set_usage_context(lane: Optional[str] = None, scope: Any = _UNSET) -> None:
    """Set the label without a scope to leave, for a turn that has no block.

    The turn loop cannot wrap itself in a context manager without reindenting
    the whole of chat_step, and it does not need to: each turn sets the label
    again before it calls anything, so a stale one is always overwritten rather
    than read. Lanes that DO nest - a coder run inside a chat turn - use
    ``usage_context`` so the outer label comes back.
    """
    if lane:
        _LANE.set(str(lane))
    if scope is not _UNSET:
        _SCOPE.set(None if scope is None else str(scope))


def current_lane() -> str:
    return _LANE.get()



# Word/symbol count as a stand-in for a tokenizer. Deliberately crude: a real
# tokenizer would have to be loaded per provider and run on every call, for a
# number that is only ever used when the provider told us nothing. Words plus
# punctuation lands within a small factor of the truth for prose, which is what
# a fallback needs - it is not trying to be right, it is trying to stop a hole.
_WORDISH = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def estimate_tokens_roughly(text: str) -> int:
    """Approximate token count for text, when nobody reported a real one."""
    if not text:
        return 0
    return len(_WORDISH.findall(str(text)))


def record_call(provider: str, model: str, input_tokens: int, output_tokens: int,
                *, lane: Optional[str] = None, user_scope_id: Any = _UNSET,
                session_id: Optional[str] = None, reported: bool = True,
                estimated: bool = False) -> CostEstimate:
    """Record ONE model call: the ledger entry, the lane total, and a log line.

    THE entry point for accounting. Everything that reaches a model goes through
    it, so a new lane costs nothing to account for and cannot be forgotten - the
    reason the per-turn hook that used to live in the agent is gone rather than
    joined by a tenth copy. Never raises: accounting must not break a call.

    THE LEDGER IS THE RECORD; THE LOG IS A COPY. The per-day, per-user, per-lane
    totals in the spend files are what every report, the budget cap and the
    Usage view read. `usage_log` is a per-call trace written beside them for a
    human reading over the machine's shoulder - which call, which lane, which
    model - and NOTHING may be built on it. Logs get rotated, swept by the age
    GC, and deleted by anyone tidying a disk; a total that had to be re-derived
    from them would lose history the moment that happened. If a future reader
    needs per-call detail as data, it belongs in the ledger, not in a parser
    pointed at these lines.
    """
    est = estimate_cost(provider, model, input_tokens, output_tokens)
    lane_name = str(lane or _LANE.get() or "main")
    scope = _SCOPE.get() if user_scope_id is _UNSET else user_scope_id
    try:
        record_spend(scope, est, lane=lane_name, provider=provider,
                     reported=reported, estimated=estimated)
    except Exception:
        pass
    try:
        from vaf.core.log_helper import append_usage_log

        append_usage_log((
            f"lane={lane_name} provider={provider or '?'} model={model or '?'} "
            f"in={est.input_tokens} out={est.output_tokens} usd={est.usd:.6f}"
            f"{'' if reported else (' usage=estimated' if estimated else ' usage=none')}"
            f"{'' if est.cost_known else ' price=unknown'}"
            f" scope={_scope_key(scope)}"
            + (f" session={session_id}" if session_id else "")
        ))
    except Exception:
        pass
    return est


def record_spend(user_scope_id: Optional[str], estimate: CostEstimate,
                 *, lane: Optional[str] = None, provider: Optional[str] = None,
                 reported: bool = True, estimated: bool = False) -> float:
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
        # Kept BESIDE `usd`, not instead of it: existing ledgers have no
        # currencies map, and the daily cap still reads `usd`. Amounts are only
        # ever summed within one currency here.
        _cur = getattr(estimate, "currency", "USD") or "USD"
        _curs = entry.get("currencies") or {}
        _curs[_cur] = round(float(_curs.get(_cur) or 0.0) + estimate.usd, 6)
        entry["currencies"] = _curs
        entry["calls"] = int(entry.get("calls") or 0) + 1
        # A call the provider never reported usage for. Counted rather than
        # estimated: the gap against an invoice is then a number the reader can
        # see and locate, instead of a percentage somebody added to be safe.
        if not reported:
            entry["no_usage_calls"] = int(entry.get("no_usage_calls") or 0) + 1
            if estimated:
                # The tokens are in the total so the figure is not short, and
                # counted here as well so the total can say how much of itself
                # was estimated. A number that hides its own provenance is the
                # thing this whole lane exists not to produce.
                entry["estimated_tokens"] = int(entry.get("estimated_tokens") or 0) \
                    + max(0, int(estimate.input_tokens)) + max(0, int(estimate.output_tokens))
        # Tokens are added beside the money rather than replacing it: the money
        # is an estimate from a price table that ages, the tokens are what the
        # provider reported. A ledger written before this existed simply has no
        # token keys, and the reader treats a missing key as zero.
        entry["input_tokens"] = int(entry.get("input_tokens") or 0) + max(0, int(estimate.input_tokens))
        entry["output_tokens"] = int(entry.get("output_tokens") or 0) + max(0, int(estimate.output_tokens))
        if not estimate.cost_known:
            entry["unknown_model_calls"] = int(entry.get("unknown_model_calls") or 0) + 1
        # Per-lane totals, so the report can say what the coder cost versus the
        # chat rather than only what the account cost.
        if lane:
            lanes = entry.get("lanes") or {}
            slot = lanes.get(lane) or {"tokens": 0, "calls": 0, "usd": 0.0}
            slot["tokens"] = int(slot.get("tokens") or 0) + max(0, int(estimate.input_tokens)) + max(0, int(estimate.output_tokens))
            slot["calls"] = int(slot.get("calls") or 0) + 1
            slot["usd"] = round(float(slot.get("usd") or 0.0) + estimate.usd, 6)
            _lc = slot.get("currencies") or {}
            _lc[_cur] = round(float(_lc.get(_cur) or 0.0) + estimate.usd, 6)
            slot["currencies"] = _lc
            lanes[lane] = slot
            entry["lanes"] = lanes
        # Per provider AND model, because the product runs several at once: the
        # chat on one, vision on another, sub-agents and the thinker on a third,
        # any of them local. "What did this cost" is unanswerable without saying
        # WHERE it ran, and the price differs by an order of magnitude between
        # them. Keyed provider/model so a provider switch does not hide behind
        # one row.
        _pkey = f"{provider or 'unknown'}/{estimate.model or '?'}"
        _provs = entry.get("providers") or {}
        _pslot = _provs.get(_pkey) or {"tokens": 0, "calls": 0, "usd": 0.0, "currencies": {}}
        _pslot["tokens"] += max(0, int(estimate.input_tokens)) + max(0, int(estimate.output_tokens))
        _pslot["calls"] = int(_pslot.get("calls") or 0) + 1
        _pslot["usd"] = round(float(_pslot.get("usd") or 0.0) + estimate.usd, 6)
        _pc = _pslot.get("currencies") or {}
        _pc[_cur] = round(float(_pc.get(_cur) or 0.0) + estimate.usd, 6)
        _pslot["currencies"] = _pc
        _provs[_pkey] = _pslot
        entry["providers"] = _provs
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



def _merge_currencies(target: dict, entry: dict) -> None:
    """Add one record's amounts into `target`, per currency, losing nothing.

    Records written before the currency was stored have only a bare number.
    They cannot be attributed honestly - the field was called `usd` while a
    Veyllo call in it was euros - so they go into `"?"` rather than being
    guessed into a real currency or, worse, dropped. Dropping was the live
    defect: a period holding both kinds displayed only the new amounts, which
    silently hid almost the entire total.
    """
    known = entry.get("currencies") or {}
    total = float(entry.get("usd") or 0.0)
    for cur, val in known.items():
        target[cur] = round(float(target.get(cur) or 0.0) + float(val or 0.0), 6)
    # A record can be PART old: the day the currency started being stored holds
    # calls from before it as well, so its map covers only some of its total.
    # The remainder is as unattributable as a fully old record, and it has to be
    # carried rather than dropped - dropping it is what hid nine tenths of a
    # month behind a three-cent figure.
    rest = round(total - sum(float(v or 0.0) for v in known.values()), 6)
    if rest > 0.000001:
        target["?"] = round(float(target.get("?") or 0.0) + rest, 6)


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
        "usd": 0.0, "currencies": {}, "calls": 0, "providers": {},
        "no_usage_calls": 0, "estimated_tokens": 0, "estimated_usd_incomplete": False}}
    try:
        base = Platform.data_dir() / "spend"
        if not base.is_dir():
            return out
        span = max(1, int(days or 1))
        cutoff = (datetime.now() - timedelta(days=span - 1)).strftime("%Y-%m-%d")
        # Every day in the window, including the ones nobody used: a chart that
        # silently drops quiet days makes a burst look like steady traffic.
        by_day = {(datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d"):
                  {"tokens": 0, "calls": 0, "usd": 0.0, "currencies": {}, "lanes": {}, "providers": {}}
                  for n in range(span)}
        rows = []
        for path in sorted(base.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue  # a corrupt ledger must not hide every other user
            scope = path.stem
            agg = {"input_tokens": 0, "output_tokens": 0, "usd": 0.0, "currencies": {},
                   "calls": 0, "unknown_model_calls": 0, "no_usage_calls": 0,
                   "estimated_tokens": 0, "last_active": ""}
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
                    _merge_currencies(agg["currencies"], entry)
                    agg["calls"] += _dcalls
                    agg["unknown_model_calls"] += int(entry.get("unknown_model_calls") or 0)
                    agg["no_usage_calls"] += int(entry.get("no_usage_calls") or 0)
                    agg["estimated_tokens"] += int(entry.get("estimated_tokens") or 0)
                except Exception:
                    continue
                if str(day) in by_day:
                    slot = by_day[str(day)]
                    slot["tokens"] += _din + _dout
                    slot["calls"] += _dcalls
                    slot["usd"] = round(slot["usd"] + _dusd, 6)
                    _merge_currencies(slot["currencies"], entry)
                    # Per-lane, summed across accounts: the day's bar answers
                    # "how much", and the only useful follow-up is "on what".
                    for _pk, _ps in (entry.get("providers") or {}).items():
                        _pa = slot["providers"].setdefault(
                            str(_pk), {"tokens": 0, "calls": 0, "usd": 0.0, "currencies": {}})
                        try:
                            _pa["tokens"] += int(_ps.get("tokens") or 0)
                            _pa["calls"] += int(_ps.get("calls") or 0)
                            _pa["usd"] = round(_pa["usd"] + float(_ps.get("usd") or 0.0), 6)
                            _merge_currencies(_pa["currencies"], _ps)
                        except Exception:
                            continue
                    for _lane, _ls in (entry.get("lanes") or {}).items():
                        _agg = slot["lanes"].setdefault(
                            str(_lane), {"tokens": 0, "calls": 0, "usd": 0.0, "currencies": {}})
                        try:
                            _agg["tokens"] += int(_ls.get("tokens") or 0)
                            _agg["calls"] += int(_ls.get("calls") or 0)
                            _agg["usd"] = round(_agg["usd"] + float(_ls.get("usd") or 0.0), 6)
                            _merge_currencies(_agg["currencies"], _ls)
                        except Exception:
                            continue
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
            _merge_currencies(out["totals"]["currencies"], r)
            out["totals"]["calls"] += r["calls"]
            out["totals"]["no_usage_calls"] += int(r.get("no_usage_calls") or 0)
            out["totals"]["estimated_tokens"] += int(r.get("estimated_tokens") or 0)
            if r["unknown_model_calls"]:
                out["totals"]["estimated_usd_incomplete"] = True
        out["totals"]["tokens"] = out["totals"]["input_tokens"] + out["totals"]["output_tokens"]
        out["totals"]["usd"] = round(out["totals"]["usd"], 4)
        out["daily"] = [{"day": d, **by_day[d]} for d in sorted(by_day)]
        for _d in out["daily"]:
            for _pk, _ps in (_d.get("providers") or {}).items():
                _t = out["totals"]["providers"].setdefault(
                    _pk, {"tokens": 0, "calls": 0, "usd": 0.0, "currencies": {}})
                _t["tokens"] += int(_ps.get("tokens") or 0)
                _t["calls"] += int(_ps.get("calls") or 0)
                _t["usd"] = round(_t["usd"] + float(_ps.get("usd") or 0.0), 6)
                _merge_currencies(_t["currencies"], _ps)
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


# Where the daily reference rate is cached. One file, one line of JSON: the ECB
# publishes once per business day, so fetching per page view would be pointless
# traffic on somebody else's free server.
_FX_CACHE = "fx_rates.json"
# Frankfurter serves the ECB's own reference rates under an MIT licence. The ECB
# is the SOURCE and is named wherever a converted figure appears - their terms
# ask for attribution, and a converted number without its origin and date is not
# checkable anyway.
_FX_URL = "https://api.frankfurter.dev/v1/latest"


def fx_rate(base: str = "EUR", quote: str = "USD", *, max_age_hours: int = 20) -> Optional[dict]:
    """The ECB reference rate, cached on disk. None when it cannot be had.

    None rather than a stale number on failure: a conversion is only honest if
    the reader can see WHICH rate produced it and when it was published, so a
    figure with no rate behind it must not be offered at all. The cached entry
    carries its own date for exactly that reason, and the caller shows it.
    """
    import time

    path = Platform.data_dir() / _FX_CACHE
    key = f"{base}{quote}".upper()
    cached = {}
    try:
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8")) or {}
            entry = cached.get(key)
            if entry and (time.time() - float(entry.get("fetched_at") or 0)) < max_age_hours * 3600:
                return entry
    except Exception:
        cached = {}
    try:
        import requests

        resp = requests.get(_FX_URL, params={"base": base, "symbols": quote}, timeout=6)
        if not resp.ok:
            return cached.get(key) or None
        data = resp.json() or {}
        rate = (data.get("rates") or {}).get(quote.upper())
        if not rate:
            return cached.get(key) or None
        entry = {"base": base.upper(), "quote": quote.upper(), "rate": float(rate),
                 "date": str(data.get("date") or ""), "source": "European Central Bank",
                 "fetched_at": time.time()}
        try:
            cached[key] = entry
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cached), encoding="utf-8")
        except Exception:
            pass
        return entry
    except Exception:
        # Offline, or the service is down. A previously cached rate is still a
        # real published rate and carries its own date, so the reader can judge
        # its age; inventing one would not be.
        return cached.get(key) or None


def stamp_legacy_currency(currency: str, *, user_scope_id: Optional[str] = None) -> dict:
    """Attribute amounts recorded before the currency was stored, once.

    The ledger cannot know what those entries were billed in - the field was
    called `usd` while a Veyllo call inside it was euros - so the software will
    not guess. The OPERATOR knows which provider they were running, and this is
    how they say so: an explicit, one-time statement, not an inference.

    Only fills what is missing. An entry that already records its currency is
    never touched, and neither is the part of a straddling day that does - the
    remainder between a day's total and its recorded amounts is exactly what is
    unattributed, and exactly what gets stamped. A backup is written beside the
    ledger first, because this rewrites the record every report reads.
    """
    import shutil

    cur = str(currency or "").strip().upper()
    if cur not in {"EUR", "USD"}:
        return {"error": f"unsupported currency: {currency}"}
    base = Platform.data_dir() / "spend"
    if not base.is_dir():
        return {"files": 0, "days": 0, "amount": 0.0, "currency": cur}
    targets = ([_ledger_path(user_scope_id)] if user_scope_id is not None
               else sorted(base.glob("*.json")))
    files = days = 0
    total = 0.0
    for path in targets:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        changed = False
        for _day, entry in (data.get("days") or {}).items():
            known = entry.get("currencies") or {}
            rest = round(float(entry.get("usd") or 0.0)
                         - sum(float(v or 0.0) for v in known.values()), 6)
            if rest <= 0.000001:
                continue
            known[cur] = round(float(known.get(cur) or 0.0) + rest, 6)
            entry["currencies"] = known
            total = round(total + rest, 6)
            days += 1
            changed = True
        if changed:
            try:
                shutil.copy2(path, path.with_suffix(".json.bak"))
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                files += 1
            except Exception:
                continue
    return {"files": files, "days": days, "amount": total, "currency": cur}
