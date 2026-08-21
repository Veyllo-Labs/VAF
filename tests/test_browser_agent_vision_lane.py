# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The browser agent's vision tiers, its dedicated LLM lane, and the guidance.

The tier decision is the user-facing promise: a vision-capable model SEES
(use_vision auto), a text-only one with a vision_provider gets described
screenshots on demand, and a setup with no vision at all still RUNS - it is
told so instead of failing. These tests pin the decision, the config lane
that feeds it, the guidance per tier, and the wiring that carries all of it
into the browser-use Agent.
"""

from pathlib import Path

import vaf.tools.browser_agent as ba


# ── vision tier ───────────────────────────────────────────────────────────

def test_vision_capable_model_sees(monkeypatch):
    monkeypatch.setattr(ba, "_model_supports_vision", lambda p, m: True)
    assert ba._browser_vision_mode("openai", "gpt-4o") == ("auto", "native")


def test_text_only_model_with_vision_provider_gets_descriptions(monkeypatch):
    monkeypatch.setattr(ba, "_model_supports_vision", lambda p, m: False)
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d="": "google" if k == "vision_provider" else d))
    assert ba._browser_vision_mode("deepseek", "deepseek-v4-flash") == (False, "described")


def test_no_vision_anywhere_still_runs_dom_only(monkeypatch):
    """The user's requirement, verbatim: without any vision model or API the
    run must be smart enough to continue - blind is a tier, not an error."""
    monkeypatch.setattr(ba, "_model_supports_vision", lambda p, m: False)
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d="": d))
    use_vision, tier = ba._browser_vision_mode("deepseek", "x")
    assert use_vision is False and tier == "blind"


def test_a_broken_registry_answer_degrades_instead_of_raising(monkeypatch):
    monkeypatch.setattr(ba, "_model_supports_vision",
                        lambda p, m: (_ for _ in ()).throw(RuntimeError("boom")))
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d="": d))
    assert ba._browser_vision_mode("openai", "gpt-4o") == (False, "blind")


# ── guidance ──────────────────────────────────────────────────────────────

def test_guidance_names_the_tools_that_beat_blind_scrolling():
    for tier in ("native", "described", "blind"):
        g = ba._browser_guidance(tier)
        assert "find_text" in g and "pages=10" in g and "collect_page_text" in g


def test_guidance_is_honest_about_what_each_tier_can_see():
    assert "screenshot" in ba._browser_guidance("native")
    assert "describe_page_visually" in ba._browser_guidance("described")
    blind = ba._browser_guidance("blind")
    # The blind tier must forbid the one action that cannot answer there.
    assert "do NOT call" in blind and "describe_page_visually" in blind


# ── the dedicated browser LLM lane ────────────────────────────────────────

def test_lane_keys_default_to_the_main_provider(monkeypatch):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d="": d))
    assert ba._resolve_browser_lane() == ("", None)


def test_lane_overrides_provider_and_model(monkeypatch):
    from vaf.core.config import Config
    values = {"browser_agent_provider": "google", "browser_agent_model": "gemini-2.5-pro"}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d="": values.get(k, d)))
    assert ba._resolve_browser_lane() == ("google", "gemini-2.5-pro")


def test_bridge_uses_the_lane(monkeypatch):
    monkeypatch.setattr(ba, "_resolve_browser_lane", lambda: ("google", "gemini-2.5-pro"))
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {"provider": "local", "model": "llama3"}))
    bridge = ba._build_vaf_bridge()
    assert bridge.provider == "google" and bridge.model == "gemini-2.5-pro"


def test_bridge_without_lane_keeps_the_main_provider(monkeypatch):
    monkeypatch.setattr(ba, "_resolve_browser_lane", lambda: ("", None))
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {"provider": "local", "model": "llama3"}))
    bridge = ba._build_vaf_bridge()
    assert bridge.provider == "local" and bridge.model == "llama3"


def test_lane_keys_are_registered_everywhere():
    """Rule 2: a config key exists in DEFAULTS, in the schema doc, and the
    doc's key-count line matches - or the registry copies have drifted."""
    from vaf.core.config import Config
    assert "browser_agent_provider" in Config.DEFAULTS
    assert "browser_agent_model" in Config.DEFAULTS
    schema = (Path(__file__).resolve().parents[1] / "docs" / "setup"
              / "CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    assert "`browser_agent_provider`" in schema
    assert "`browser_agent_model`" in schema
    assert f"({len(Config.DEFAULTS)} keys)" in schema


# ── wiring into browser-use ───────────────────────────────────────────────

def test_the_agent_gets_the_tier_not_a_hardcoded_false():
    """use_vision=False was the measured root of blind scrolling; the Agent
    must receive the computed mode and the guidance, or every tier above
    'blind' exists only on paper."""
    src = (Path(__file__).resolve().parents[1] / "vaf" / "tools"
           / "browser_agent.py").read_text(encoding="utf-8")
    body = src.split("async def _run_browser", 1)[1]
    # ignore comment lines: the tier explanation names the literal on purpose
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "use_vision=False" not in code
    assert "use_vision=_use_vision" in body
    assert "extend_system_message=_browser_guidance(_vision_tier)" in body


def test_collect_page_text_is_a_registered_action():
    controller = ba._build_browser_controller()
    names = list(controller.registry.registry.actions.keys())
    assert "collect_page_text" in names
    assert "describe_page_visually" in names   # the vision lane it complements
