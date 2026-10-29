# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The API model catalog: which models each provider offers, what they cost,
and which of them are going away.

One table, so the model a user can pick and the price the spend estimate bills
for it cannot disagree. Everything else is a view of it:

- ``Config.PROVIDER_MODELS`` (``provider_models()``): the default and the static
  dropdown list per provider, served to the web UI by ``GET /api/provider-models``;
- ``cost.PROVIDER_PRICING`` (``provider_pricing()``): the priced current models
  per provider for the "what would this cost elsewhere" panel;
- ``cost.PRICES`` (``flat_prices()``): every priced id, retired ones included,
  because a ledger or a config still names them;
- ``cost.PRICES_AS_OF`` (``CATALOG_AS_OF``): the day the prices were checked.

Prices are USD per 1M tokens (Veyllo: EUR net of VAT), standard tier, list
price. Where a provider shows a dated promotion, the entry carries the REGULAR
(higher) rate: this table feeds a spend cap, and a cap that reads the
promotional rate trips late once the promotion ends. Batch, flex, priority and
fast-mode prices, DeepSeek's off-peak half price, data-residency multipliers
and Google's per-hour cache storage are not modelled; VAF sends none of those
request shapes, and the estimate is an upper bound on what it does send.

``cached_input`` is the provider's own cache-read rate for that model, because
the fraction differs by model generation (OpenAI 0.5x on gpt-4o, 0.1x on
gpt-5.6; Anthropic 0.1x, 0.05x on the 5.5 models, 0.025x on 5.1). ``None``
means the provider's ``cache_read_multiplier`` applies; a provider without one
(Veyllo publishes no cached rate) bills cached tokens at the full input rate.
``long_context_from`` is the prompt size ABOVE which the provider bills the
whole call at ``long_input``/``long_output``; the cached rate scales with it.

A model id leaves the dropdown once the provider deprecates it, but stays
priced (``legacy``) so the history of a ledger is not repriced at the unknown
rate. ``shutdown_on`` is the provider's announced shutdown day; on and after
it, ``migration_target`` moves a saved config value to ``replaced_by``.
Replacements are models VAF can drive today, which is why the provider's own
recommendation is not always the one carried here.

Refreshing: edit the entries, move ``CATALOG_AS_OF`` and the provider's
``checked_on``, and run ``tests/test_model_catalog.py``. Sources, fetched
without a key:

- OpenAI: https://platform.openai.com/docs/pricing.md and
  https://platform.openai.com/docs/deprecations.md
- Anthropic: https://platform.claude.com/docs/en/about-claude/pricing.md and
  https://platform.claude.com/docs/en/about-claude/model-deprecations.md
- Google: https://ai.google.dev/gemini-api/docs/pricing.md.txt and
  https://ai.google.dev/gemini-api/docs/deprecations.md.txt
- DeepSeek: https://api-docs.deepseek.com/quick_start/pricing/ (the trailing
  slash matters) and https://api-docs.deepseek.com/news/news260910
- Veyllo: https://veyllo.app/pricing
- OpenRouter: https://openrouter.ai/api/v1/models (public JSON)

Named boundary: nothing here is on the public facade. An embedder selects a
model by id (``api_model_<provider>``) and gets the estimate and the cap
through the ``Agent`` without calling this module; the only readers outside
``vaf/core`` are two settings routes and the TUI, all inside this repo.

Deliberately import-light (dataclasses, typing, datetime, re) and free of
``vaf`` imports: ``config.py`` builds its DEFAULTS from it at module load.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

CATALOG_AS_OF = "2026-10-29"


@dataclass(frozen=True)
class ModelEntry:
    id: str
    input: Optional[float] = None    # per 1M tokens; None = not priced here (alias, OpenRouter id)
    output: Optional[float] = None
    cached_input: Optional[float] = None  # absolute per 1M; None = provider multiplier
    long_context_from: Optional[int] = None  # prompt tokens above which the long tier bills
    long_input: Optional[float] = None
    long_output: Optional[float] = None
    vision: bool = False   # VAF sends images to it
    offered: bool = False  # in the static dropdown
    legacy: bool = False   # priced for old ledgers and configs, never offered, not in the panel
    shutdown_on: str = ""  # ISO date of the provider's shutdown; "" = none announced
    replaced_by: str = ""
    note: str = ""

    @property
    def priced(self) -> bool:
        return self.input is not None and self.output is not None


@dataclass(frozen=True)
class ProviderCatalog:
    name: str
    label: str
    currency: str
    default: str
    source_url: str
    checked_on: str
    models: Tuple[ModelEntry, ...]
    cache_read_multiplier: Optional[float] = None
    cache_write_multiplier: Optional[float] = None
    # Routing names that are not models: resolved to one before the call.
    aliases: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # Retired ids that are not entries (no price is known for them any more).
    migrations: Dict[str, str] = field(default_factory=dict)
    # A reseller that bills each vendor's model at that vendor's cache rates. For
    # any other provider serving a model it does not make, the vendor's discount
    # is not evidence of its own, so cached tokens pay its own rate (none: full).
    passes_vendor_cache_rates: bool = False
    # Families this provider owns beyond those of its listed ids, as full-match
    # patterns over the family name (model_owner): a naming scheme of which the
    # table lists one member or none.
    family_patterns: Tuple[str, ...] = ()


_OPENAI = ProviderCatalog(
    name="openai", label="OpenAI", currency="USD", default="gpt-5.6-terra",
    source_url="https://platform.openai.com/docs/pricing.md",
    checked_on="2026-10-29",
    # Fallback only: every entry carries its own cached rate. The write premium is
    # the 30-minute rate, 1.25x on every model that lists one.
    cache_read_multiplier=0.10, cache_write_multiplier=1.25,
    # The o-series (o1, o3, o4-mini, ...) is numbered, and only o3 is listed.
    family_patterns=(r"o\d+",),
    models=(
        ModelEntry("gpt-5.6-terra", 2.00, 12.00, 0.20, 272_000, 4.00, 18.00,
                   vision=True, offered=True),
        ModelEntry("gpt-5.6-luna", 0.20, 1.20, 0.02, 272_000, 0.40, 1.80,
                   vision=True, offered=True),
        ModelEntry("gpt-5.6-sol", 4.00, 20.00, 0.40, 272_000, 8.00, 30.00,
                   vision=True, offered=True),
        # Priced for the panel, not offered: VAF's request shape for gpt-6 is
        # unproven (openai_is_reasoning_model matches gpt-5 only).
        ModelEntry("gpt-6-astra", 10.00, 50.00, 1.00, 272_000, 20.00, 75.00, vision=True),
        ModelEntry("gpt-6.1-sol", 2.00, 10.00, 0.10, 272_000, 4.00, 15.00, vision=True),
        ModelEntry("gpt-6.1-terra", 1.00, 5.00, 0.05, 272_000, 2.00, 7.50, vision=True),
        ModelEntry("gpt-6-sol", 2.00, 10.00, 0.20, 272_000, 4.00, 15.00, vision=True),
        ModelEntry("gpt-6-luna", 0.10, 0.50, 0.01, 272_000, 0.20, 0.75, vision=True),
        # OpenAI recommends gpt-6-luna for the four below; gpt-5.6 is what VAF drives.
        ModelEntry("gpt-4o", 2.50, 10.00, 1.25, vision=True, legacy=True,
                   shutdown_on="2027-01-14", replaced_by="gpt-5.6-terra",
                   note="deprecated 2026-10-14"),
        ModelEntry("gpt-4o-mini", 0.15, 0.60, 0.075, vision=True, legacy=True,
                   shutdown_on="2027-01-14", replaced_by="gpt-5.6-luna",
                   note="deprecated 2026-10-14"),
        ModelEntry("gpt-4.1", 2.00, 8.00, 0.50, vision=True, legacy=True,
                   shutdown_on="2027-01-14", replaced_by="gpt-5.6-terra",
                   note="deprecated 2026-10-14"),
        ModelEntry("gpt-4.1-mini", 0.40, 1.60, 0.10, vision=True, legacy=True,
                   shutdown_on="2027-01-14", replaced_by="gpt-5.6-luna",
                   note="deprecated 2026-10-14"),
        # No cached rate was ever published, so cached tokens pay full input.
        ModelEntry("gpt-4-turbo", 10.00, 30.00, 10.00, vision=True, legacy=True,
                   shutdown_on="2026-10-23", replaced_by="gpt-5.6-sol",
                   note="OpenAI's own substitute"),
        ModelEntry("o3", 2.00, 8.00, 0.50, vision=True, legacy=True,
                   shutdown_on="2026-12-11", replaced_by="gpt-5.6-sol",
                   note="deprecated 2026-06-11"),
    ),
)

_ANTHROPIC = ProviderCatalog(
    name="anthropic", label="Anthropic", currency="USD", default="claude-sonnet-4-6",
    source_url="https://platform.claude.com/docs/en/about-claude/pricing.md",
    checked_on="2026-10-29",
    # The write premium is the FIVE-MINUTE rate, the only one VAF produces: the
    # adapter sends an ephemeral cache_control with no ttl (an hour is 2.0x).
    cache_read_multiplier=0.10, cache_write_multiplier=1.25,
    models=(
        ModelEntry("claude-sonnet-4-6", 3.00, 15.00, 0.30, vision=True, offered=True,
                   note="retirement not before 2027-02-17"),
        ModelEntry("claude-opus-4-8", 5.00, 25.00, 0.50, vision=True, offered=True),
        ModelEntry("claude-haiku-4-5", 1.00, 5.00, 0.10, vision=True, offered=True,
                   note="listed active; retirement 'not sooner than 2026-10-15'"),
        # Priced for the panel, not offered: Claude 5.x answers 400 to temperature,
        # which VAF still sends when adaptive thinking is off.
        ModelEntry("claude-sonnet-5-5", 2.00, 10.00, 0.10, vision=True),
        ModelEntry("claude-opus-5-5", 4.00, 20.00, 0.20, vision=True),
        ModelEntry("claude-haiku-5-5", 0.10, 0.50, 0.01, 100_000, 0.50, 2.50, vision=True,
                   note="prompt length counts cache reads and writes"),
        ModelEntry("claude-fable-5-1", 10.00, 50.00, 0.25, vision=True),
        ModelEntry("claude-mythos-5-1", 10.00, 50.00, 0.25, vision=True),
        ModelEntry("claude-sonnet-5", 2.00, 10.00, 0.20, vision=True),
        ModelEntry("claude-opus-5", 5.00, 25.00, 0.50, vision=True),
        ModelEntry("claude-fable-5", 10.00, 50.00, 1.00, vision=True),
        ModelEntry("claude-mythos-5", 10.00, 50.00, 1.00, vision=True),
        ModelEntry("claude-opus-4-1", 15.00, 75.00, 1.50, vision=True, legacy=True,
                   shutdown_on="2026-08-05", replaced_by="claude-opus-4-8"),
    ),
)

_DEEPSEEK = ProviderCatalog(
    name="deepseek", label="DeepSeek", currency="USD", default="deepseek-flash",
    source_url="https://api-docs.deepseek.com/quick_start/pricing/",
    checked_on="2026-10-29",
    # Quoted at PEAK (off-peak is half). The fallback multiplier is rounded up
    # from the published 0.02x and 0.033x; every entry carries its own rate.
    cache_read_multiplier=0.04, cache_write_multiplier=1.00,
    models=(
        # DeepSeek lists image input for deepseek-flash; VAF sends none until the
        # wire format is probed (ProviderSpec.vision_capable is False).
        ModelEntry("deepseek-flash", 0.30, 1.20, 0.006, offered=True,
                   note="served by DeepSeek-V4.1-Flash-1017"),
        ModelEntry("deepseek-v4-pro", 1.32, 3.96, 0.044, offered=True,
                   note="DeepSeek-V4-Pro-0813"),
        ModelEntry("deepseek-v4-flash", 0.30, 1.20, 0.006, legacy=True,
                   shutdown_on="2026-09-10", replaced_by="deepseek-flash",
                   note="retired; the name still routes to V4.1-Flash at the Flash price"),
    ),
    aliases={"deepseek-auto": {"fast": "deepseek-flash", "pro": "deepseek-v4-pro"}},
    migrations={
        "deepseek-chat": "deepseek-flash",
        "deepseek-coder": "deepseek-flash",
        # deepseek-reasoner also rejects tool_choice, so a saved one 400s every tool turn.
        "deepseek-reasoner": "deepseek-flash",
        "deepseek-r1": "deepseek-flash",
    },
)

_GOOGLE = ProviderCatalog(
    name="google", label="Google", currency="USD", default="gemini-2.5-flash",
    source_url="https://ai.google.dev/gemini-api/docs/pricing.md.txt",
    checked_on="2026-10-29",
    # Text and image input rates (VAF sends no audio). Implicit caching: no write premium.
    cache_read_multiplier=0.10, cache_write_multiplier=1.00,
    models=(
        ModelEntry("gemini-2.5-flash", 0.30, 2.50, 0.03, vision=True, offered=True,
                   note="2.5 access limited to projects that used it before"),
        ModelEntry("gemini-2.5-pro", 1.25, 10.00, 0.125, 200_000, 2.50, 15.00,
                   vision=True, offered=True),
        ModelEntry("gemini-2.5-flash-lite", 0.10, 0.40, 0.01, vision=True, offered=True),
        ModelEntry("gemini-3.8-flash", 1.50, 7.50, 0.15, vision=True, offered=True,
                   note="promotion 0.75/3.75 until 2026-12-31; regular rate carried"),
        ModelEntry("gemini-3.5-flash-lite", 0.30, 2.50, 0.03, vision=True, offered=True),
        ModelEntry("gemini-3.6-flash", 1.50, 7.50, 0.15, vision=True,
                   note="promotion 0.75/3.75 until 2026-12-31; regular rate carried"),
        ModelEntry("gemini-3.1-pro-preview", 2.00, 12.00, 0.20, 200_000, 4.00, 18.00,
                   vision=True, note="preview, no shutdown announced"),
        ModelEntry("gemini-4-argon", 4.00, 20.00, 0.20, vision=True,
                   note="promotion 2.00/10.00 until 2026-11-30; regular rate carried"),
        # The two below are routed by Google to their successor; the date is the
        # day the routing was read off the deprecations page.
        ModelEntry("gemini-3.5-flash", 1.50, 7.50, 0.15, vision=True, legacy=True,
                   shutdown_on="2026-10-29", replaced_by="gemini-3.6-flash",
                   note="routes to gemini-3.6-flash"),
        ModelEntry("gemini-3.7-flash", 1.50, 7.50, 0.15, vision=True, legacy=True,
                   shutdown_on="2026-10-29", replaced_by="gemini-3.8-flash",
                   note="routes to gemini-3.8-flash"),
        ModelEntry("gemini-3.1-pro", 2.00, 12.00, 0.50, vision=True, legacy=True,
                   note="UNVERIFIED id: on no Google page; previous price and cached "
                        "rate kept, no migration"),
    ),
)

_OPENROUTER = ProviderCatalog(
    name="openrouter", label="OpenRouter", currency="USD",
    default="anthropic/claude-sonnet-4.6",
    source_url="https://openrouter.ai/api/v1/models",
    checked_on="2026-10-29",
    # Its models list publishes input_cache_read per model, equal to the vendor's
    # (claude-sonnet-4.6 0.30, gpt-5.6-terra 0.20, gemini-2.5-flash 0.03).
    passes_vendor_cache_rates=True,
    # Dotted ids (claude-sonnet-4.6), unlike Anthropic's dashed ones. An entry
    # without a price resolves through the vendor's entry after normalization;
    # OpenRouter's per-token prices equal the vendor's for these, or fall below.
    models=(
        ModelEntry("anthropic/claude-sonnet-4.6", vision=True, offered=True),
        ModelEntry("google/gemini-2.5-flash", vision=True, offered=True),
        ModelEntry("anthropic/claude-haiku-4.5", vision=True, offered=True),
        # Served by third-party hosts at OpenRouter's own rate, whose INPUT price is
        # above DeepSeek's: the vendor entry would under-count it.
        ModelEntry("deepseek/deepseek-v4-pro", 1.60, 3.20, 0.135,
                   note="OpenRouter's own rate, text only"),
    ),
)

_VEYLLO = ProviderCatalog(
    name="veyllo", label="Veyllo", currency="EUR", default="veyllo-chat",
    source_url="https://veyllo.app/pricing",
    checked_on="2026-10-29",
    # No cache multipliers: Veyllo publishes no cached-input rate, so a cached token
    # is billed at the full input rate here, the upper bound.
    models=(
        # Multimodal: one model serves chat and image input.
        ModelEntry("veyllo-chat", 0.90, 1.90, vision=True, offered=True),
        ModelEntry("veyllo-vision", 3.45, 20.70, vision=True,
                   note="UNVERIFIED id (the models list needs a key); price verified"),
    ),
)

# provider_registry.PROVIDER_SPECS order (CI-guarded): dropdowns and default
# enumerations derive from it.
CATALOG: Dict[str, ProviderCatalog] = {
    p.name: p for p in (_OPENAI, _ANTHROPIC, _DEEPSEEK, _GOOGLE, _OPENROUTER, _VEYLLO)
}

# The comparison panel's order: Veyllo is this product's own API and stays first.
PANEL_ORDER: Tuple[str, ...] = ("veyllo", "anthropic", "openai", "google", "deepseek")

_INDEX: Dict[str, Tuple[str, ModelEntry]] = {
    entry.id: (prov.name, entry) for prov in CATALOG.values() for entry in prov.models
}

_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")


def provider_models() -> Dict[str, dict]:
    """``{provider: {"default": id, "fallback": [offered ids..., aliases...]}}``."""
    return {
        prov.name: {
            "default": prov.default,
            "fallback": [e.id for e in prov.models if e.offered] + list(prov.aliases),
        }
        for prov in CATALOG.values()
    }


def provider_pricing() -> Dict[str, dict]:
    """The current priced models per provider, in panel order, as
    ``{provider: {"label", "currency", ["cache_read_multiplier",
    "cache_write_multiplier"], "models": [(id, input, output), ...]}}``."""
    out: Dict[str, dict] = {}
    for name in PANEL_ORDER:
        prov = CATALOG[name]
        spec: dict = {"label": prov.label, "currency": prov.currency}
        if prov.cache_read_multiplier is not None:
            spec["cache_read_multiplier"] = prov.cache_read_multiplier
            spec["cache_write_multiplier"] = prov.cache_write_multiplier
        spec["models"] = [(e.id, e.input, e.output)
                          for e in prov.models if e.priced and not e.legacy]
        out[name] = spec
    return out


def flat_prices() -> Dict[str, Tuple[float, float]]:
    """Every priced id of every provider, retired ones included: ``{id: (in, out)}``."""
    return {e.id: (e.input, e.output)
            for prov in CATALOG.values() for e in prov.models if e.priced}


def _candidates(model_id: str) -> Iterator[str]:
    """The id as given, then the spellings a provider or a config puts around it:
    a vendor prefix (``anthropic/``, Google's ``models/``), a snapshot date
    (``-2026-09-01``, ``-20251001``) and OpenRouter's dotted Claude versions."""
    name = str(model_id or "").strip().lower()
    if not name:
        return
    yield name
    tail = name.rsplit("/", 1)[-1]
    yield tail
    bare = _SNAPSHOT_SUFFIX.sub("", tail)
    yield bare
    if bare.startswith("claude-"):
        yield bare.replace(".", "-")


def find_model(model_id: str, *, priced: bool = False) -> Optional[Tuple[str, ModelEntry]]:
    """``(provider, entry)`` for a model id, or ``None``. With ``priced`` an entry
    that carries no price (an OpenRouter id) is skipped so the lookup reaches
    the vendor's entry."""
    for cand in _candidates(model_id):
        hit = _INDEX.get(cand)
        if hit is not None and (not priced or hit[1].priced):
            return hit
    return None


def provider_catalog(provider: str) -> Optional[ProviderCatalog]:
    return CATALOG.get(str(provider or "").strip().lower())


def alias_target(provider: str, alias: str, pro: bool) -> Optional[str]:
    """The model a routing alias resolves to: its ``pro`` or its ``fast`` target."""
    prov = provider_catalog(provider)
    targets = (prov.aliases if prov else {}).get(str(alias or "").strip().lower())
    if not targets:
        return None
    return targets["pro" if pro else "fast"]


def _today() -> _dt.date:
    """The day a shutdown is compared against (a seam for tests)."""
    return _dt.date.today()


def migration_target(model_id: str, today: Optional[_dt.date] = None,
                     provider: Optional[str] = None) -> Optional[str]:
    """The id a saved config value should move to, or ``None``.

    Exact match only, so a local GGUF name or an id this table does not know is
    never touched. With ``provider``, only that provider's names move: an id
    saved for one provider is not another provider's retired model, even when
    the strings match. A retired name in a provider's ``migrations`` moves at
    once; an entry moves on and after its ``shutdown_on`` day, not before, so a
    model that still answers stays the user's choice until the provider turns
    it off.
    """
    name = str(model_id or "").strip()
    if not name:
        return None
    wanted = str(provider).strip().lower() if provider is not None else None
    for prov in CATALOG.values():
        if wanted is not None and prov.name != wanted:
            continue
        if name in prov.migrations:
            return prov.migrations[name]
    hit = _INDEX.get(name)
    if hit is None or (wanted is not None and hit[0] != wanted):
        return None
    entry = hit[1]
    if not entry.shutdown_on or not entry.replaced_by:
        return None
    day = today or _today()
    if _dt.date.fromisoformat(entry.shutdown_on) <= day:
        return entry.replaced_by
    return None


def offered_vision_models(provider: str) -> List[str]:
    """The offered ids of a provider that VAF sends images to, in table order."""
    prov = provider_catalog(provider)
    if prov is None:
        return []
    return [e.id for e in prov.models if e.offered and e.vision]


def all_model_ids() -> List[str]:
    """Every id the catalog names: entries, aliases and retired migration keys."""
    ids = list(_INDEX)
    for prov in CATALOG.values():
        ids.extend(prov.aliases)
        ids.extend(prov.migrations)
    return ids


# ── Which provider a model id belongs to ─────────────────────────────────────
#
# Derived from the table above, so a model added there is owned at once. An id
# the catalog lists belongs to its provider. An id it does not list belongs to the
# provider whose listed ids share its FAMILY, the leading name before the first
# "-" or "." ("gpt" in gpt-5.6-terra, "o3", "claude", "deepseek", "gemini",
# "veyllo"), so a release newer than this table (gpt-7, deepseek-v5) is still
# recognised. A family two providers share is nobody's. "vendor/model" ids are
# never resolved by family: the vendor names the maker, not the provider serving
# the call (OpenRouter serves anthropic/..., a local vLLM serves Qwen/...).

_FAMILY = re.compile(r"^([a-z]+\d*)(?:[-.]|$)")


def _bare_id(model_id: str) -> str:
    """Lowercased, trimmed, without Google's ``models/`` resource prefix."""
    name = str(model_id or "").strip().lower()
    return name[len("models/"):] if name.startswith("models/") else name


def _owner_tables() -> Tuple[Dict[str, str], Dict[str, str]]:
    exact: Dict[str, str] = {}
    families: Dict[str, set] = {}
    for prov in CATALOG.values():
        for mid in [e.id for e in prov.models] + list(prov.aliases) + list(prov.migrations):
            name = mid.lower()
            exact[name] = prov.name
            m = _FAMILY.match(name)
            if m and "/" not in name:
                families.setdefault(m.group(1), set()).add(prov.name)
    return exact, {fam: next(iter(p)) for fam, p in families.items() if len(p) == 1}


_OWNER_EXACT, _OWNER_FAMILY = _owner_tables()

# Providers whose every id is "vendor/model" (OpenRouter): they serve no bare id,
# and nobody else serves a slashed one.
_VENDOR_SLASH = frozenset(
    p.name for p in CATALOG.values() if p.models and all("/" in e.id for e in p.models)
)


def model_owner(model_id: str, by_family: bool = True) -> Optional[str]:
    """The provider a model id belongs to by this catalog, or ``None`` when it
    cannot tell (an id of no listed family, a ``vendor/model`` id it does not
    list, a local model name). A snapshot date and ``models/`` are ignored.
    ``by_family=False`` answers for listed ids only: a local model is often
    named after the family it was distilled from (deepseek-r1-distill-qwen)."""
    name = _bare_id(model_id)
    if not name:
        return None
    for cand in (name, _SNAPSHOT_SUFFIX.sub("", name)):
        if cand in _OWNER_EXACT:
            return _OWNER_EXACT[cand]
    if "/" in name or not by_family:
        return None
    m = _FAMILY.match(name)
    if not m:
        return None
    fam = m.group(1)
    if fam in _OWNER_FAMILY:
        return _OWNER_FAMILY[fam]
    for prov in CATALOG.values():
        if any(re.fullmatch(pat, fam) for pat in prov.family_patterns):
            return prov.name
    return None


def catalog_fit(provider: str, model_id: str) -> Optional[bool]:
    """Whether the catalog says ``model_id`` is one of ``provider``'s models:
    ``True``/``False`` when it knows, ``None`` when it does not (a provider it
    has no table for, or an id no provider claims and whose shape fits)."""
    prov = provider_catalog(provider)
    name = _bare_id(model_id)
    if prov is None or not name:
        return None
    owner = model_owner(name)
    if owner is not None:
        return owner == prov.name
    if (prov.name in _VENDOR_SLASH) != ("/" in name):
        return False
    return None
