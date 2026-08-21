# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Embedder persona override: `Agent(system_prompt=...)` replaces the on-disk
admin Soul in the system prompt for that instance only, while the engine's
technical instructions (thinking/action/verification) are always kept, and the
default (no override) still loads the Soul path."""
from types import SimpleNamespace

from vaf.core.system_prompt import SystemPromptManager

_OVERRIDE = "You are Captain Redbeard, a pirate. Always answer in pirate speak, arr."


def _build(override):
    agent = SimpleNamespace(_system_prompt_override=override) if override is not None else None
    return SystemPromptManager(tools=[], model_name="TestModel", agent_instance=agent).build_prompt()


def test_override_replaces_soul_but_keeps_mechanics():
    p = _build(_OVERRIDE)
    assert "Captain Redbeard" in p                       # the embedder persona is in
    assert "## Your Personality & Rules (Soul)" not in p  # the Soul persona block is out
    assert "## Technical Instructions" in p and "Thinking Format" in p  # engine mechanics kept
    assert "<identity>" in p and "</identity>" in p


def test_no_override_uses_soul_path():
    p = _build(None)
    assert "<identity>" in p
    assert "Captain Redbeard" not in p
    # Soul path (or the neutral fallback) still yields the technical block
    assert "## Technical Instructions" in p


def test_blank_override_falls_back_to_soul():
    # whitespace-only override must not blank the persona
    p = _build("   \n  ")
    assert "Captain Redbeard" not in p
    assert "## Technical Instructions" in p


def test_soul_continuity_addendum_is_always_in_the_persona():
    """The memory lane must hold no matter what a user wrote (or deleted) in
    their editable soul.md: the code-owned addendum rides the persona block on
    the soul path AND the fallback path - a deliberate, consistent echo of the
    memory_instructions block inside the section the model reads as WHO it is.
    The embedder override deliberately does not carry it: an override replaces
    the persona wholesale, and the embedder may not even register these tools."""
    from vaf.core.system_prompt import SOUL_CONTINUITY_ADDENDUM
    for tool in ("memory_search", "memory_save", "memory_update"):
        assert tool in SOUL_CONTINUITY_ADDENDUM, f"the echo must name {tool}"

    p = _build(None)          # soul path (or the fallback when no soul.md exists)
    ident = p.split("<identity>", 1)[1].split("</identity>", 1)[0]
    assert "Continuity (always in effect)" in ident, (
        "the addendum is not in the persona block - a soul without memory lines "
        "would lose the lane entirely"
    )

    p_override = _build(_OVERRIDE)
    ident_override = p_override.split("<identity>", 1)[1].split("</identity>", 1)[0]
    assert "Continuity (always in effect)" not in ident_override


def test_core_agent_stores_override():
    from vaf.core.agent import Agent as CoreAgent
    a = CoreAgent(system_prompt=_OVERRIDE, register_signals=False)
    assert a._system_prompt_override == _OVERRIDE
    b = CoreAgent(register_signals=False)
    assert b._system_prompt_override is None


def test_facade_stores_and_forwards_override(monkeypatch):
    from vaf import framework
    from vaf.framework import Agent

    assert Agent(system_prompt=_OVERRIDE)._system_prompt == _OVERRIDE
    assert Agent()._system_prompt is None

    # the facade must forward system_prompt into the CoreAgent it builds;
    # capture the constructor kwargs, then stop before the post-build wiring
    # (init_chat etc.) that a fake engine cannot satisfy.
    import pytest

    captured = {}

    class _Stop(Exception):
        pass

    class _FakeCore:
        def __init__(self, **kw):
            captured.update(kw)
            raise _Stop

    monkeypatch.setattr(framework, "CoreAgent", _FakeCore)
    with pytest.raises(_Stop):
        Agent(system_prompt=_OVERRIDE).core
    assert captured.get("system_prompt") == _OVERRIDE
