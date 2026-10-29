# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The API model catalog (vaf/core/model_catalog.py) and everything derived from it.

One table decides which models each provider offers and what the spend estimate
bills for them. Before it, the dropdown list and the price table were two hand
copies that disagreed: four offered models priced at the unknown-model rate,
defaults on models their provider had shut down, and a TypeScript third copy.
These tests pin the table's own consistency, the views derived from it, the
estimate's per-model cache rate and long tier, the vision lookup, the
date-aware config migration, and a guard that refuses a model list or a price
anywhere else in the tree.
"""
import ast
import datetime as dt
import json
import re
from pathlib import Path

import pytest

from vaf.core import cost, model_catalog as mc
from vaf.core.config import Config
from vaf.core.cost import cache_usage, estimate_cost

REPO_ROOT = Path(__file__).resolve().parent.parent
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── AC1: the catalog itself ───────────────────────────────────────────────────

def test_catalog_is_dated_sourced_and_imports_nothing_from_vaf():
    """MUTATION: import Config at the top of model_catalog.py (config.py builds
    its DEFAULTS from the catalog at load, so that is a cycle); or set the date
    to tomorrow."""
    assert _ISO.match(mc.CATALOG_AS_OF)
    assert dt.date.fromisoformat(mc.CATALOG_AS_OF) <= dt.date.today()
    for prov in mc.CATALOG.values():
        assert prov.source_url.startswith("https://"), prov.name
        assert _ISO.match(prov.checked_on), prov.name
        assert dt.date.fromisoformat(prov.checked_on) <= dt.date.today(), prov.name
    tree = ast.parse((REPO_ROOT / "vaf" / "core" / "model_catalog.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any(n == "vaf" or n.startswith("vaf.") for n in names), names


def test_every_id_is_unique_across_the_catalog():
    ids = mc.all_model_ids()
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"ids named twice: {dupes}"


def test_entries_are_internally_consistent():
    for prov in mc.CATALOG.values():
        for e in prov.models:
            if e.priced:
                assert 0 < e.input and 0 < e.output, e.id
            if e.cached_input is not None:
                assert e.priced and 0 < e.cached_input <= e.input, e.id
            if e.long_context_from is not None:
                assert e.long_input and e.long_output, e.id
                assert e.long_input >= e.input and e.long_output >= e.output, e.id
            if e.shutdown_on:
                assert _ISO.match(e.shutdown_on), e.id
        r, w = prov.cache_read_multiplier, prov.cache_write_multiplier
        assert (r is None) == (w is None), prov.name
        for alias, targets in prov.aliases.items():
            assert set(targets) == {"fast", "pro"}, alias


# ── AC2: the old names are views of the catalog ───────────────────────────────

def test_the_old_tables_are_derived_and_no_literal_copy_is_left():
    """MUTATION: paste a literal provider entry back into config.py (a
    `"fallback": [` table) or a `PRICES.update` into cost.py."""
    assert Config.PROVIDER_MODELS == mc.provider_models()
    assert cost.PRICES == mc.flat_prices()
    assert cost.PROVIDER_PRICING == mc.provider_pricing()
    assert cost.PRICES_AS_OF == mc.CATALOG_AS_OF
    for rel in ("vaf/core/config.py", "vaf/core/cost.py"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert '"fallback": [' not in src, rel
        assert "PRICES.update" not in src, rel


def test_the_panel_lists_current_models_in_panel_order():
    pricing = mc.provider_pricing()
    assert tuple(pricing) == mc.PANEL_ORDER
    for name, spec in pricing.items():
        listed = {m for m, _i, _o in spec["models"]}
        legacy = {e.id for e in mc.CATALOG[name].models if e.legacy}
        assert listed and not (listed & legacy), name


# ── AC3: defaults ─────────────────────────────────────────────────────────────

def test_defaults_are_pinned():
    """MUTATION: set the OpenAI default back to gpt-4o (shut down by OpenAI on
    2027-01-14)."""
    assert {p: info["default"] for p, info in Config.PROVIDER_MODELS.items()} == {
        "openai": "gpt-5.6-terra",
        "anthropic": "claude-sonnet-4-6",
        "deepseek": "deepseek-flash",
        "google": "gemini-2.5-flash",
        "openrouter": "anthropic/claude-sonnet-4.6",
        "veyllo": "veyllo-chat",
    }
    for p in Config.PROVIDER_MODELS:
        assert Config.DEFAULTS[f"api_model_{p}"] == Config.PROVIDER_MODELS[p]["default"]


# ── AC4: everything a user can pick has a price ───────────────────────────────

def _offered():
    for p, info in Config.PROVIDER_MODELS.items():
        for m in dict.fromkeys([info["default"], *info["fallback"]]):
            yield p, m


@pytest.mark.parametrize("provider,model", list(_offered()))
def test_every_offered_model_has_a_price(provider, model):
    """MUTATION: drop the price of gemini-2.5-flash-lite (it was unpriced before
    the catalog); or drop the dotted-to-dashed step of find_model (the OpenRouter
    default goes unknown and is billed at 15/75)."""
    aliases = mc.CATALOG[provider].aliases
    if model in aliases:
        for target in aliases[model].values():
            assert estimate_cost(provider, target, 1000, 1000).cost_known, (model, target)
        return
    assert estimate_cost(provider, model, 1000, 1000).cost_known, model


# ── AC5: nothing offered is going away; replacements are real ─────────────────

def test_no_offered_model_is_deprecated_and_every_replacement_is_current():
    """MUTATION: mark gpt-4o offered again; or point a replaced_by at a legacy id."""
    for prov in mc.CATALOG.values():
        by_id = {e.id: e for e in prov.models}
        default = by_id[prov.default]
        assert not default.legacy and not default.shutdown_on, prov.name
        for e in prov.models:
            if e.offered:
                assert not e.legacy and not e.shutdown_on, e.id
            if e.replaced_by:
                target = by_id.get(e.replaced_by)
                assert target is not None and not target.legacy, (e.id, e.replaced_by)
        for old, new in prov.migrations.items():
            assert old not in by_id, old
            assert new in by_id and not by_id[new].legacy, (old, new)
        for alias, targets in prov.aliases.items():
            for t in targets.values():
                assert t in by_id and not by_id[t].legacy, (alias, t)


# ── AC6: the spellings providers put around an id ────────────────────────────

def test_ids_resolve_through_prefix_snapshot_and_dotted_version():
    """MUTATION: drop the snapshot-suffix step (the dated OpenAI id goes unknown);
    drop the vendor/`models/` step (the OpenRouter and Google spellings do)."""
    got = estimate_cost("openrouter", "anthropic/claude-sonnet-4.6", 1_000_000, 0)
    assert got.cost_known and got.usd == 3.00
    dated = estimate_cost("openai", "gpt-5.6-terra-2026-09-01", 100_000, 0)
    assert dated.cost_known and dated.usd == estimate_cost("openai", "gpt-5.6-terra", 100_000, 0).usd
    assert estimate_cost("anthropic", "claude-haiku-4-5-20251001", 1000, 0).cost_known
    assert estimate_cost("google", "models/gemini-2.5-flash", 1000, 0).cost_known


def test_an_openrouter_rate_above_the_vendor_rate_is_billed_as_openrouter_publishes_it():
    """OpenRouter serves deepseek-v4-pro through third-party hosts at an input
    price above DeepSeek's own; the vendor entry would under-count it."""
    got = estimate_cost("openrouter", "deepseek/deepseek-v4-pro", 1_000_000, 0)
    assert got.cost_known and got.usd == 1.60


# ── AC7: the model's own cache rate ───────────────────────────────────────────

@pytest.mark.parametrize("provider,model,per_1m", [
    ("openai", "gpt-4o", 1.25),          # 0.5x: the gpt-4o generation
    ("openai", "gpt-5.6-terra", 0.20),   # 0.1x
    ("deepseek", "deepseek-flash", 0.006),
    ("anthropic", "claude-sonnet-5-5", 0.10),  # 0.05x
])
def test_a_cache_hit_is_billed_at_the_models_own_cached_rate(provider, model, per_1m):
    """A provider-wide fraction was wrong in both directions: gpt-4o's cached
    tokens cost half the input price, not a tenth. MUTATION: ignore
    cached_input and use the provider multiplier (gpt-4o comes out at 0.25)."""
    shaped = provider != "anthropic"
    got = estimate_cost(provider, model, 100_000 if shaped else 0, 0,
                        cache=cache_usage(100_000, 0, in_input=shaped))
    assert got.usd == pytest.approx(per_1m / 10, abs=1e-9)


# ── AC8: the long-context tier ────────────────────────────────────────────────

def test_a_prompt_above_the_threshold_bills_the_whole_call_at_the_long_tier():
    """MUTATION: remove the tier (300K comes out at 2.00/12.00)."""
    long = estimate_cost("openai", "gpt-5.6-terra", 300_000, 1_000_000)
    assert long.usd == pytest.approx(0.3 * 4.00 + 18.00)
    short = estimate_cost("openai", "gpt-5.6-terra", 200_000, 1_000_000)
    assert short.usd == pytest.approx(0.2 * 2.00 + 12.00)


def test_the_threshold_counts_the_whole_prompt_including_anthropics_cached_span():
    """Anthropic's input_tokens leaves the cached span out, and Haiku 5.5's
    100K threshold counts it in. MUTATION: compare input_tokens instead of the
    whole prompt (90K alone is below the threshold)."""
    got = estimate_cost("anthropic", "claude-haiku-5-5", 90_000, 1000,
                        cache=cache_usage(20_000, 0, in_input=False))
    assert got.usd == pytest.approx((90_000 * 0.50 + 20_000 * 0.05 + 1000 * 2.50) / 1_000_000)


# ── AC9: vision from the catalog ──────────────────────────────────────────────

def _vision_entries():
    for prov in mc.CATALOG.values():
        for e in prov.models:
            if e.vision:
                yield prov.name, e.id


@pytest.mark.parametrize("provider,model", list(_vision_entries()))
def test_every_vision_entry_is_vision_capable(provider, model):
    """The substring rules knew gpt-4o and claude-3 only, so the new OpenAI
    default and the OpenRouter default read as blind. MUTATION: remove the
    catalog lookup from model_supports_vision."""
    from vaf.core.provider_registry import model_supports_vision

    assert model_supports_vision(provider, model, probe_local=False) is True


def test_the_catalog_entry_decides_before_the_family_rule(monkeypatch):
    """A text-only model whose name matches a family fragment ("gpt-5") must
    answer from its entry. The family rules exist only for ids the catalog
    does not know. MUTATION: remove the catalog lookup from
    model_supports_vision (the substring rule says True)."""
    from vaf.core.provider_registry import model_supports_vision

    entry = mc.ModelEntry("gpt-5-text-only", 1.0, 1.0, vision=False)
    monkeypatch.setitem(mc._INDEX, entry.id, ("openai", entry))
    assert model_supports_vision("openai", "gpt-5-text-only", probe_local=False) is False
    assert model_supports_vision("openai", "gpt-5-something-new", probe_local=False) is True


def test_openrouter_vision_default_and_deepseek():
    from vaf.core.provider_registry import default_vision_model, model_supports_vision

    assert default_vision_model("openrouter") == Config.get_default_model("openrouter")
    assert model_supports_vision("openrouter", Config.get_default_model("openrouter")) is True
    assert model_supports_vision("openai", "openai/gpt-5.6-terra") is True
    assert model_supports_vision("deepseek", "deepseek-flash") is False
    # OpenRouter's DeepSeek is text only, and the entry says so.
    assert model_supports_vision("openrouter", "deepseek/deepseek-v4-pro") is False


# ── AC10: saved configs move to the successor, on the shutdown day ────────────

def test_migration_target_is_date_aware():
    """MUTATION: compare `<` instead of `<=` (the shutdown day itself stays)."""
    assert mc.migration_target("gpt-4o", today=dt.date(2027, 1, 13)) is None
    assert mc.migration_target("gpt-4o", today=dt.date(2027, 1, 14)) == "gpt-5.6-terra"
    assert mc.migration_target("deepseek-chat") == "deepseek-flash"
    assert mc.migration_target("gpt-5.6-terra") is None
    assert mc.migration_target("unsloth/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q4_K_M.gguf") is None
    assert mc.migration_target("") is None


def _load_with(monkeypatch, tmp_path, saved: dict, today: dt.date) -> tuple:
    from vaf.core import migrations

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"config_format_version": migrations.CONFIG_FORMAT_VERSION,
                                **saved}), encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(mc, "_today", lambda: today)
    monkeypatch.setattr("vaf.core.log_helper.append_domain_log", lambda *a, **k: None)
    loaded = Config.load()
    return loaded, json.loads(path.read_text(encoding="utf-8"))


def test_config_load_moves_retired_ids_and_persists_them(monkeypatch, tmp_path):
    """MUTATION: drop subagent_model from Config's model-id key list."""
    gguf = "unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q4_K_M.gguf"
    saved = {
        "api_model_deepseek": "deepseek-v4-flash",
        "api_model_openai": "gpt-4-turbo",
        "subagent_model": "deepseek-chat",
        "thinking_model": "deepseek-v4-flash",
        "vision_model": "gpt-4o",
        "voice_agent_model": gguf,
    }
    loaded, raw = _load_with(monkeypatch, tmp_path, saved, dt.date(2027, 1, 13))
    for got in (loaded, raw):
        assert got["api_model_deepseek"] == "deepseek-flash"
        assert got["api_model_openai"] == "gpt-5.6-sol"
        assert got["subagent_model"] == "deepseek-flash"
        assert got["thinking_model"] == "deepseek-flash"
        assert got["vision_model"] == "gpt-4o"      # still answers until its shutdown day
        assert got["voice_agent_model"] == gguf      # a local model is never touched


def test_config_load_moves_a_deprecated_id_on_its_shutdown_day(monkeypatch, tmp_path):
    loaded, raw = _load_with(monkeypatch, tmp_path, {"api_model_openai": "gpt-4o"},
                             dt.date(2027, 1, 14))
    assert loaded["api_model_openai"] == raw["api_model_openai"] == "gpt-5.6-terra"


def test_config_load_does_not_write_a_default_into_a_sparse_file(monkeypatch, tmp_path):
    loaded, raw = _load_with(monkeypatch, tmp_path, {"provider": "openai"}, dt.date(2027, 6, 1))
    assert loaded["api_model_openai"] == "gpt-5.6-terra"
    assert "api_model_openai" not in raw


# ── AC12: a tool without a configured model uses the provider default ─────────

def test_query_llm_falls_back_to_the_catalog_default_for_every_provider(monkeypatch):
    """The hand list covered three providers; Veyllo, DeepSeek and OpenRouter
    users had every tool query skipped. MUTATION: restore the three-provider
    if-chain in BaseTool.query_llm."""
    from vaf.tools.base import BaseTool

    class _Probe(BaseTool):
        name = "probe"
        description = "probe"

        def run(self, **kwargs):
            return ""

    seen = {}
    monkeypatch.setattr(Config, "load", classmethod(
        lambda cls: {"provider": "veyllo", "api_model_veyllo": "", "model": ""}))
    monkeypatch.delenv("VAF_TOOL_MODEL", raising=False)
    monkeypatch.setattr("vaf.core.completion.complete",
                        lambda messages, **kw: seen.update(kw) or "ok")
    assert _Probe().query_llm([{"role": "user", "content": "hi"}]) == "ok"
    assert seen["model"] == "veyllo-chat"


# ── AC14: the CLI says when the prices were checked ───────────────────────────

def test_usage_show_prints_the_price_date(monkeypatch):
    """MUTATION: delete the date line from `vaf usage show`."""
    from typer.testing import CliRunner

    from vaf.cli.cmd import usage as usage_cmd

    monkeypatch.setattr(cost, "usage_totals", lambda days=30: {
        "days": days, "totals": {"tokens": 10, "calls": 1, "usd": 0.01, "currencies": {"USD": 0.01}},
        "users": []})
    result = CliRunner().invoke(usage_cmd.app, ["show"])
    assert result.exit_code == 0, result.output
    assert f"as of {mc.CATALOG_AS_OF}" in result.output


# ── AC15: the docs name the catalog defaults ─────────────────────────────────

def test_config_schema_default_table_matches_the_catalog():
    """MUTATION: leave gpt-4o in the CONFIG_SCHEMA table."""
    text = (REPO_ROOT / "docs" / "setup" / "CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    start = text.index("Default API models")
    rows = re.findall(r"^\| (\w+) \| `([^`]+)` \|$", text[start:start + 1200], re.M)
    assert dict(rows) == {p: i["default"] for p, i in Config.PROVIDER_MODELS.items()}


# ── The guard: no model list and no price outside the catalog ────────────────

# Substring heuristics that rank model NAMES, not lists of models to offer or
# prices to bill. Each entry is a file and the reason it may name catalog ids.
_ALLOWED = {
    # Teacher-tier ranking by name fragment, with its own override key.
    "vaf/whare_wananga/teacher.py",
}


def _violations_in(tree: ast.AST, ids: set) -> list:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            elts = node.elts
            named = [e.value for e in elts if isinstance(e, ast.Constant) and e.value in ids]
            if len(named) >= 2:
                out.append((node.lineno, f"model list {named}"))
            nums = [e for e in elts if isinstance(e, ast.Constant)
                    and isinstance(e.value, float)]
            if named and len(nums) >= 2:
                out.append((node.lineno, f"price for {named}"))
        elif isinstance(node, ast.Dict):
            parts = [k for k in node.keys if k is not None] + list(node.values)
            named = [e.value for e in parts if isinstance(e, ast.Constant) and e.value in ids]
            if len(named) >= 2:
                out.append((node.lineno, f"model table {named}"))
    return out


def test_no_model_list_or_price_outside_the_catalog():
    """A second list is how the dropdown and the price table drifted apart.
    MUTATION: put a `{"deepseek-chat": "deepseek-v4-flash"}` migration dict back
    into config.py, or a `("gpt-4o", 2.50, 10.00)` price anywhere under vaf/."""
    ids = set(mc.all_model_ids())
    found = []
    for path in sorted((REPO_ROOT / "vaf").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == "vaf/core/model_catalog.py" or rel in _ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found += [f"{rel}:{line} {what}" for line, what in _violations_in(tree, ids)]
    for sub in ("components", "lib", "app"):
        for path in sorted((REPO_ROOT / "web" / sub).rglob("*.ts*")):
            text = path.read_text(encoding="utf-8")
            named = sorted(i for i in ids if re.search(rf"""['"`]{re.escape(i)}['"`]""", text))
            if len(named) >= 2:
                found.append(f"{path.relative_to(REPO_ROOT).as_posix()} quotes {named}")
    assert not found, "model lists or prices outside vaf/core/model_catalog.py:\n" + "\n".join(found)


def test_the_guard_sees_what_it_is_meant_to_see():
    """The guard's own detector, on the three shapes it exists for."""
    ids = set(mc.all_model_ids())
    assert _violations_in(ast.parse('X = ["gpt-4o", "gpt-4o-mini"]'), ids)
    assert _violations_in(ast.parse('X = {"deepseek-chat": "deepseek-flash"}'), ids)
    assert _violations_in(ast.parse('X = ("gpt-4o", 2.50, 10.00)'), ids)
    # A behavior gate on a family prefix is not a list of models.
    assert not _violations_in(ast.parse('X = ("gemini-2.5", "gemini-3")'), ids)
