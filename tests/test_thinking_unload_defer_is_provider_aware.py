# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: do not pin the local model for a run that will not use it.

The desktop tray unloads the local model once the main provider is a cloud one - the GGUF is
dead weight then. That unload asks ``should_defer_model_unload()`` first, so a background
thinking run is never cut off mid-flight ("think first, then unload").

The question it asked was provider-blind. With ``thinking_provider = inherit`` and a cloud
main provider, a thinking run builds its own agent on that cloud provider and never touches
the GGUF - yet the deferral still reported "keep it". Worse, it does not only report that
while a run is EXECUTING: it also does so while one is merely ELIGIBLE, and eligibility holds
for as long as somebody is idle. On a live desktop that turned a temporary deferral into a
permanent one: measured on a running instance, a scope had been eligible for 146 minutes past
its cooldown without a run happening, while ~3.4 GB of a 10 GB card stayed pinned by a model
nothing was using.

The fix is one question asked earlier: which provider would the run actually use? Only a run
that lands on the local model may hold it back. ``resolve_thinking_provider`` answers it the
same way the run itself does, so the watchdog and the run cannot disagree.
"""
import pytest

import vaf.core.thinking_mode as tm

IDLE_SCOPE = "12345678-1234-1234-1234-123456789abc"   # synthetic; never a real scope UUID


@pytest.fixture
def thinking(monkeypatch):
    """A machine where a thinking run is ELIGIBLE but not running - the state that used to
    pin the model forever. Config values are supplied per test."""
    cfg = {"thinking_enabled": True, "provider": "local", "thinking_provider": "inherit",
           "thinking_idle_minutes": 10, "thinking_cooldown_minutes": 110}

    class _Cfg:
        @staticmethod
        def get(key, default=None):
            return cfg.get(key, default)

    monkeypatch.setattr("vaf.core.config.Config", _Cfg)
    monkeypatch.setattr(tm, "is_locked", lambda scope=None: False)
    monkeypatch.setattr(tm, "get_idle_user_scope_ids", lambda _m: [IDLE_SCOPE])
    monkeypatch.setattr(tm, "_minutes_since_last_run", lambda _s: 999.0)  # cooldown long elapsed
    return cfg


# ── Which provider would a run use ───────────────────────────────────────────

def test_inherit_follows_the_main_provider(thinking):
    thinking["provider"] = "veyllo"
    assert tm.resolve_thinking_provider() == "veyllo"
    thinking["provider"] = "local"
    assert tm.resolve_thinking_provider() == "local"


def test_an_explicit_thinking_provider_wins(thinking):
    thinking["provider"] = "local"
    thinking["thinking_provider"] = "anthropic"
    assert tm.resolve_thinking_provider() == "anthropic"


@pytest.mark.parametrize("configured", ["", None, "  ", "INHERIT", " inherit "])
def test_blank_and_odd_inherit_values_fall_back_to_the_main_provider(thinking, configured):
    """The value travels through Settings and hand-edited config files."""
    thinking["provider"] = "veyllo"
    thinking["thinking_provider"] = configured
    assert tm.resolve_thinking_provider() == "veyllo"


# ── The deferral ─────────────────────────────────────────────────────────────

def test_a_cloud_run_does_not_hold_the_local_model(thinking):
    """THE regression. Everything else is identical to the deferring case - only the provider
    differs, and with it whether the run would ever touch the GGUF."""
    thinking["provider"] = "veyllo"
    assert tm.should_defer_model_unload() is False


def test_a_local_run_still_holds_it(thinking):
    """The original intent must survive: on a local provider an eligible run keeps the model,
    so the watchdog cannot unload it out from under the run that is about to start."""
    thinking["provider"] = "local"
    assert tm.should_defer_model_unload() is True


def test_thinking_pinned_to_local_holds_it_even_on_a_cloud_main_provider(thinking):
    """The case the fix must NOT break: someone runs their main chat in the cloud but keeps
    thinking on the local model to save cost. That run needs the GGUF."""
    thinking["provider"] = "veyllo"
    thinking["thinking_provider"] = "local"
    assert tm.should_defer_model_unload() is True


def test_an_executing_local_run_is_never_interrupted(thinking, monkeypatch):
    monkeypatch.setattr(tm, "is_locked", lambda scope=None: True)
    thinking["provider"] = "local"
    assert tm.should_defer_model_unload() is True


def test_an_executing_run_on_a_cloud_provider_holds_nothing(thinking, monkeypatch):
    """Even a RUNNING cloud thinking run has no claim on the local model."""
    monkeypatch.setattr(tm, "is_locked", lambda scope=None: True)
    thinking["provider"] = "veyllo"
    assert tm.should_defer_model_unload() is False


def test_disabled_thinking_defers_nothing(thinking):
    thinking["thinking_enabled"] = False
    assert tm.should_defer_model_unload() is False


def test_a_local_run_within_its_cooldown_does_not_hold_the_model(thinking, monkeypatch):
    """Not eligible yet -> no reason to keep it warm. Pins that the fix did not widen the
    deferral in the local case."""
    monkeypatch.setattr(tm, "_minutes_since_last_run", lambda _s: 5.0)
    thinking["provider"] = "local"
    assert tm.should_defer_model_unload() is False


# ── The branch this unblocks must not cut off work in flight ─────────────────

def test_the_cloud_unload_checks_work_in_flight():
    """The tray's cloud-unload sat behind a deferral that blocked it almost permanently, so it
    never needed its own work guard. Now that it actually fires, it needs the one its sibling
    branch already carries - a task, a sub-agent or a live call may still be using the model,
    and "the main provider is cloud" says nothing about that. Source-pinned because the branch
    lives inside the tray's activity loop."""
    from pathlib import Path

    import vaf.tray as tray_mod

    src = Path(tray_mod.__file__).read_text(encoding="utf-8")
    idx = src.find("unloading local model to free memory")
    assert idx != -1, "the cloud-unload branch is gone"
    condition = src[max(0, idx - 400):idx]
    for needle in ("not thinking_defer", "not voice_local_lane", "not work_busy"):
        assert needle in condition, f"the cloud-unload branch lost its guard: {needle}"
