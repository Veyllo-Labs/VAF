# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A model id stored for one provider is never sent to another.

THE DEFECT THIS PINS. After the main provider was switched from Veyllo to
DeepSeek, the tool / workflow model (`subagent_model`) still said `veyllo-chat`.
DeepSeek's pro context sent it verbatim, and every workflow step, background
run and router call answered 400 "you passed veyllo-chat". Four readers of the
key hand-rolled a `!= "deepseek-auto"` test, three readers of `VAF_TOOL_MODEL`
took it as it was, and the one guard the backend had (repeated by hand in the
browser bridge) knew local model names only.

The ownership question is answered from the model catalog
(`model_catalog.model_owner`/`catalog_fit`) and asked in one place,
`provider_registry.model_fits_provider`, which the API backend applies to every
explicit model of every call.
"""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf.core.model_catalog as mc
import vaf.core.provider_registry as pr
from vaf.core.config import Config, subagent_model_for, tool_model_for
from vaf.core.provider_registry import is_local_model_id, model_fits_provider

REPO_ROOT = Path(__file__).resolve().parents[1]
API = pr.api_provider_names()


@pytest.fixture(autouse=True)
def _fresh_notes(monkeypatch):
    """Every test sees a process that has reported nothing yet, and records
    what would have been printed instead of printing it."""
    monkeypatch.setattr(pr, "_foreign_noted", set())
    events = []
    import vaf.cli.ui as ui_mod
    monkeypatch.setattr(ui_mod.UI, "event",
                        staticmethod(lambda *a, **kw: events.append((a, kw))))
    return events


# ── AC1: the predicate ────────────────────────────────────────────────────────

@pytest.mark.parametrize("provider, model, expected", [
    # The incident, and its mirror images.
    ("deepseek", "veyllo-chat", False),
    ("deepseek", "gpt-5.6-terra", False),
    ("veyllo", "deepseek-flash", False),
    ("anthropic", "gemini-2.5-flash", False),
    # A release newer than the catalog is still recognised by its family.
    ("openai", "gpt-7-nova", True),
    ("deepseek", "deepseek-v5", True),
    ("openai", "deepseek-v5", False),
    ("deepseek", "gpt-7-nova", False),
    ("openai", "o3-deep-research", True),
    # An id no provider claims: the choice of whoever set it.
    ("openai", "llama-3.3-70b", True),
    ("deepseek", "llama-3.3-70b", True),
    # Vendor-slashed ids are OpenRouter's, and OpenRouter serves no bare id.
    ("openai", "anthropic/claude-sonnet-4.6", False),
    ("openai", "meta-llama/llama-4-scout", False),
    ("openrouter", "openrouter/auto", True),
    ("openrouter", "meta-llama/llama-4-scout", True),
    ("openrouter", "gpt-5.6-terra", False),
    ("openrouter", "llama-3.3-70b", False),
    # Spellings around a listed id.
    ("google", "models/gemini-2.5-flash", True),
    ("anthropic", "claude-sonnet-4-6-20251001", True),
    ("openai", "GPT-5.6-Terra", True),
    ("deepseek", "deepseek-auto", True),
    ("deepseek", "deepseek-reasoner", True),
    # Local names never reach an API provider; API ids never run locally.
    ("openai", "Veyllo/VQ-1_Instruct-q4_k_m", False),
    ("openai", "auto", False),
    ("openai", "unsloth/Qwen3.5-4B-GGUF", False),
    ("local", "Qwen3.5-4B-UD-Q8_K_XL.gguf", True),
    ("local", "Qwen/Qwen3-4B-AWQ", True),
    ("local", "auto", True),
    ("local", "gpt-4o", False),
    ("local", "veyllo-chat", False),
    # Blank never fits; a provider without a registry entry decides itself.
    ("openai", "", False),
    ("openai", "   ", False),
    ("my-gateway", "anything-at-all", True),
])
def test_the_truth_table(provider, model, expected):
    """MUTATION: drop the vendor-slash rule in `catalog_fit` (the openrouter
    and slashed-on-openai rows flip); drop the family lookup in `model_owner`
    (deepseek-v5 on openai flips)."""
    assert model_fits_provider(provider, model) is expected


def test_an_unknown_id_belongs_to_the_api_model_key_that_holds_it():
    """A custom id the catalog cannot place (a gateway's own name) belongs to
    the provider whose `api_model_<p>` holds it, so it does not ride a switch
    to another provider. MUTATION: drop the holder rule in
    `model_fits_provider`."""
    cfg = {"api_model_veyllo": "my-gateway-model"}
    assert model_fits_provider("veyllo", "my-gateway-model", config=cfg) is True
    assert model_fits_provider("deepseek", "my-gateway-model", config=cfg) is False
    assert model_fits_provider("local", "my-gateway-model", config=cfg) is False
    # Held by none: still usable wherever it was set.
    assert model_fits_provider("deepseek", "my-gateway-model", config={}) is True


def test_every_catalog_id_fits_its_own_provider_and_no_other():
    """The cross matrix over the whole catalog: entries, aliases and retired
    names, plus every PROVIDER_MODELS default and fallback."""
    owned = []
    for prov in mc.CATALOG.values():
        for mid in [e.id for e in prov.models] + list(prov.aliases) + list(prov.migrations):
            owned.append((prov.name, mid))
    for prov, entry in Config.PROVIDER_MODELS.items():
        owned += [(prov, entry["default"])] + [(prov, m) for m in entry["fallback"]]
    misfits = []
    for owner, mid in owned:
        for p in API + ["local"]:
            if model_fits_provider(p, mid) is not (p == owner):
                misfits.append((owner, mid, p))
    assert not misfits, misfits


def test_every_api_provider_has_a_catalog_and_no_family_is_shared():
    """A family two providers share would be nobody's, and their new releases
    would then fit both."""
    assert set(mc.CATALOG) == set(API)
    families = {}
    for prov in mc.CATALOG.values():
        for mid in [e.id for e in prov.models] + list(prov.aliases) + list(prov.migrations):
            m = mc._FAMILY.match(mid.lower())
            if m and "/" not in mid:
                families.setdefault(m.group(1), set()).add(prov.name)
    shared = {f: p for f, p in families.items() if len(p) > 1}
    assert not shared, shared
    assert mc._VENDOR_SLASH == {"openrouter"}


def test_the_local_shape_is_one_function():
    for name in ("auto", "AUTO", "x.gguf", "unsloth/Qwen3.5-4B-GGUF", "Veyllo/VQ-1_Instruct-q4_k_m",
                 "vq-1-instruct-q4"):
        assert is_local_model_id(name), name
    for name in ("gpt-5.6-terra", "veyllo-chat", "openrouter/auto", ""):
        assert not is_local_model_id(name), name


# ── AC4: one warning per process per (source, provider, model) ────────────────

def test_a_foreign_value_is_reported_once(_fresh_notes):
    """MUTATION: drop the once-set in `note_foreign_model`."""
    assert pr.note_foreign_model("deepseek", "veyllo-chat", "subagent_model") is True
    assert pr.note_foreign_model("deepseek", "veyllo-chat", "subagent_model") is False
    assert pr.note_foreign_model("deepseek", "veyllo-chat", "subagent_model") is False
    assert len(_fresh_notes) == 1
    (_src, msg), kw = _fresh_notes[0]
    assert kw.get("style") == "warning"
    assert "subagent_model" in msg and "veyllo-chat" in msg and "DeepSeek" in msg
    assert "ignored" in msg and "Tool / Workflow model" in msg
    assert "\u2014" not in msg
    # Another model, or another source, is news again.
    assert pr.note_foreign_model("deepseek", "gpt-5.6-terra", "subagent_model") is True
    assert pr.note_foreign_model("deepseek", "veyllo-chat", "browser_agent_model") is True
    assert len(_fresh_notes) == 3


# ── AC5: the config half ──────────────────────────────────────────────────────

@pytest.fixture
def store(monkeypatch):
    """A dict store, as in test_subagent_provider_pair.py: the real config is
    never read or written."""
    data = {}

    def _get(cls, key, default=None):
        if key in data:
            return data[key]
        return default if default is not None else cls.DEFAULTS.get(key)

    monkeypatch.setattr(Config, "get", classmethod(_get))
    monkeypatch.delenv("VAF_TOOL_MODEL", raising=False)
    return data


def test_subagent_model_for_returns_only_a_fitting_value(store, _fresh_notes):
    """MUTATION: return the value without the fit check in `subagent_model_for`."""
    store.update({"provider": "deepseek"})
    assert subagent_model_for() is None                      # empty
    store["subagent_model"] = "deepseek-auto"
    assert subagent_model_for() is None                      # the alias means "same as main"
    store["subagent_model"] = "veyllo-chat"
    assert subagent_model_for() is None                      # foreign: ignored ...
    assert subagent_model_for("deepseek") is None
    assert len(_fresh_notes) == 1                            # ... and said once
    store["subagent_model"] = "deepseek-v4-pro"
    assert subagent_model_for() == "deepseek-v4-pro"
    assert subagent_model_for("deepseek") == "deepseek-v4-pro"


def test_a_separate_lane_model_is_not_reported_on_the_main_provider(store, _fresh_notes):
    """With a separate sub-agent provider the main provider not taking the
    lane's model is the expected case, not a misconfiguration."""
    store.update({"provider": "deepseek", "subagent_use_separate_provider": True,
                  "subagent_provider": "openai", "subagent_model": "gpt-5.6-sol"})
    assert subagent_model_for() == "gpt-5.6-sol"
    assert subagent_model_for("deepseek") is None
    assert _fresh_notes == []


def test_subagent_model_for_reads_a_given_mapping(store):
    """The backend passes its own config snapshot (an embedder's dict)."""
    store.update({"provider": "deepseek", "subagent_model": "veyllo-chat"})
    assert subagent_model_for("deepseek", {"subagent_model": "deepseek-flash"}) == "deepseek-flash"
    assert subagent_model_for("deepseek", {"subagent_model": "veyllo-chat"}) is None


def test_tool_model_for_takes_the_env_only_when_it_fits(store, monkeypatch):
    """MUTATION: return the raw env value in `tool_model_for`."""
    assert tool_model_for("deepseek") is None
    monkeypatch.setenv("VAF_TOOL_MODEL", "veyllo-chat")
    assert tool_model_for("deepseek") is None
    assert tool_model_for("veyllo") == "veyllo-chat"
    monkeypatch.setenv("VAF_TOOL_MODEL", "deepseek-v4-pro")
    assert tool_model_for("deepseek") == "deepseek-v4-pro"


# ── AC6: BaseTool.query_llm ───────────────────────────────────────────────────

def test_query_llm_ignores_a_tool_model_of_another_provider(monkeypatch):
    """MUTATION: revert base.py to the raw `VAF_TOOL_MODEL` read."""
    from vaf.tools.base import BaseTool

    class _Probe(BaseTool):
        name = "probe"
        description = "probe"

        def run(self, **kwargs):
            return ""

    seen = {}
    monkeypatch.setattr(Config, "load", classmethod(
        lambda cls: {"provider": "deepseek", "api_model_deepseek": "deepseek-flash", "model": ""}))
    monkeypatch.setattr("vaf.core.completion.complete",
                        lambda messages, **kw: seen.update(kw) or "ok")
    monkeypatch.setenv("VAF_TOOL_MODEL", "veyllo-chat")
    assert _Probe().query_llm([{"role": "user", "content": "hi"}]) == "ok"
    assert seen["provider"] == "deepseek" and seen["model"] == "deepseek-flash"
    monkeypatch.setenv("VAF_TOOL_MODEL", "deepseek-v4-pro")
    _Probe().query_llm([{"role": "user", "content": "hi"}])
    assert seen["model"] == "deepseek-v4-pro"


# ── AC9: one reader of the key, one copy of the local shape ───────────────────

def _vaf_sources():
    for path in sorted((REPO_ROOT / "vaf").rglob("*.py")):
        yield path.relative_to(REPO_ROOT).as_posix(), path.read_text(encoding="utf-8")


def test_no_hand_reader_of_the_tool_model_key_is_left():
    """Four readers each hand-rolled the same check and none asked the
    provider. MUTATION: put `Config.get("subagent_model", "")` back into
    coder.py."""
    pattern = re.compile(r"""\.get\(\s*["']subagent_model["']""")
    found = [rel for rel, text in _vaf_sources()
             if rel != "vaf/core/config.py" and pattern.search(text)]
    assert not found, found


def test_no_hand_copy_of_the_local_shape_is_left():
    """MUTATION: restore the `"instruct-q"` test in browser_agent.py."""
    pattern = re.compile(r"""["'](?:instruct-q|vq-1)["']""")
    found = [rel for rel, text in _vaf_sources()
             if rel != "vaf/core/provider_registry.py" and pattern.search(text)]
    assert not found, found


def test_no_raw_reader_of_the_tool_model_env_is_left():
    """`VAF_TOOL_MODEL` is set by the two workflow runners and read through
    `tool_model_for`; the backend's pro-context detection is the one other
    reader, and it compares against the alias only."""
    readers = []
    for rel, text in _vaf_sources():
        for line in text.splitlines():
            if 'environ.get("VAF_TOOL_MODEL"' in line and "_prev_tool_model" not in line:
                readers.append(f"{rel}: {line.strip()}")
    allowed = {"vaf/core/config.py", "vaf/core/api_backend.py"}
    assert all(r.split(":")[0] in allowed for r in readers), readers


# ── AC10: the settings route ──────────────────────────────────────────────────

def test_the_model_fit_route_answers_with_the_predicate(monkeypatch):
    """MUTATION: have the route return `fits: True` unconditionally."""
    from vaf.api import config_routes
    from vaf.auth.middleware import _is_auth_exempt

    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {}))
    req = SimpleNamespace(state=SimpleNamespace(user=None))
    got = asyncio.run(config_routes.get_model_fit(req, provider="deepseek", model="veyllo-chat"))
    assert got == {"provider": "deepseek", "model": "veyllo-chat", "fits": False}
    got = asyncio.run(config_routes.get_model_fit(req, provider="deepseek", model="deepseek-v4-pro"))
    assert got["fits"] is True
    # No model: nothing to mark.
    assert asyncio.run(config_routes.get_model_fit(req, provider="deepseek", model=""))["fits"] is True
    assert not _is_auth_exempt("/api/config/model-fit")


# ── AC11, AC12: the settings screen ───────────────────────────────────────────

_TSX = REPO_ROOT / "web" / "components" / "SettingsModal.tsx"


def test_the_tool_model_picker_asks_the_backend_and_marks_an_ignored_value():
    """MUTATION: drop the `{ignored && ...}` hint or the model-fit fetch."""
    src = _TSX.read_text(encoding="utf-8")
    assert "/api/config/model-fit?provider=" in src
    assert "tAdvanced('toolModelIgnoredOption'" in src
    assert re.search(r"\{ignored && \(\s*<p[^>]*>\{tAdvanced\('toolModelIgnored',", src)


@pytest.mark.parametrize("provider_key, model_key", [
    ("subagent_provider", "subagent_model"),
    ("failover_backup_provider", "failover_backup_model"),
])
def test_a_provider_select_resets_its_model(provider_key, model_key):
    """The model list changes under the value when the lane's provider changes.
    Reset to '' and not undefined: the save merges, so an absent key would keep
    the old value. MUTATION: remove either reset line."""
    src = _TSX.read_text(encoding="utf-8")
    m = re.search(
        r"onChange=\{\(v: string\) => \{\s*handleChange\('" + provider_key + r"', v\);(.*?)\}\}",
        src, re.S)
    assert m, f"{provider_key} select has no block handler"
    assert f"handleChange('{model_key}', '')" in m.group(1)


def test_same_as_main_chat_clears_the_saved_tool_model():
    """'Same as main chat' must write '' (the DEFAULTS value), or the merge on
    save keeps the foreign model it was meant to replace."""
    src = _TSX.read_text(encoding="utf-8")
    assert "handleChange('subagent_model', v || undefined)" not in src
    assert "handleChange('subagent_model', v || '')" in src
    assert Config.DEFAULTS["subagent_model"] == ""
