# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A config change must reach EVERY agent in the process, not the one someone holds.

THE DEFECT THIS PINS (2026-07-31, live, owner-found). Saving a replaced API key had no
effect until VAF was restarted: a wrong key was saved, the previous one kept answering,
and only a restart made the wrong one active. The write side was correct and the reload
was correct; the REACH was wrong. With `parallel_main_workers = 5` and a cloud provider
the headless runner builds five chat agents and registers exactly one
(`headless_runner.py`, `if worker_id == 1`), so the tray re-applied the new key to a
fifth of the pool and the other four answered with the old one.

WHY THE OBVIOUS TEST WOULD HAVE MISSED IT, and therefore what is measured here. "The new
key arrives" is green with one agent, which is the default configuration and every
single-agent test. The failure only exists in the plural, so the assertions below count
agents rather than checking a key: `test_broadcast_reaches_every_agent...` is red the
moment the broadcast is narrowed back to one instance, and `test_constructing_an_agent...`
is red the moment the constructor stops registering. Between them they cover the two
halves - the reach and the wiring that supplies it.

The refusal side matters just as much: widening the reach must not widen the POLICY. A
broadcast that also overwrote an embedded caller's in-memory key, or unpinned a sub-agent
process, would trade a stale-key bug for a silent-substitution bug.
"""
import os
import weakref

import pytest


@pytest.fixture(autouse=True)
def _own_registry(monkeypatch):
    """A registry per test.

    The real one is process-global by design, so without this a module-scoped agent from
    another test file would be swept into these broadcasts (and these agents into that
    file's expectations). Both `_register_live_agent` and `live_agents` read the module
    global at call time, so swapping it also covers what `Agent.__init__` registers into.
    """
    import vaf.core.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_LIVE_AGENTS", weakref.WeakSet())
    yield


def _cloud_config(monkeypatch, provider="deepseek"):
    """Point the live on-disk config at a cloud provider, with a key in the store."""
    from vaf.core.config import Config
    from vaf.core.api_keys import store_api_key

    store_api_key(provider, "sk-the-new-one")
    base = Config.load()
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {**base, "provider": provider}))


# ── the wiring: a constructed agent is reachable ────────────────────────────────────

def test_constructing_an_agent_registers_it():
    """The half that cannot be added afterwards.

    A broadcast is only as good as the set it iterates. This is deliberately a REAL
    Agent: registering is one line in `__init__`, and a fake that calls the registry
    itself would assert that the test knows how to register, not that the agent does.
    """
    from vaf.core.agent import Agent, live_agents

    a = Agent(register_signals=False)
    assert a in live_agents()


def test_the_registry_holds_its_members_weakly():
    """The registry must not be the thing that keeps an agent alive.

    MEASURED WHILE WRITING THIS (2026-07-31): an Agent is NOT collected when the last
    obvious reference goes, and the registry has nothing to do with it -
    `Agent.__init__` calls `atexit.register(self._atexit_cleanup)`, and atexit holds the
    bound method for the life of the process. So `live_agents()` in practice returns
    every agent this process ever built. That is bounded and harmless where it matters
    (a worker pool is built once at startup, sub-agents run in their own processes), and
    it predates this registry - but a test asserting "a dropped agent disappears" would
    have been asserting something the code does not do.

    What IS this registry's own promise, and all that is measured here: membership adds
    no reference. Measured on a plain object, so the assertion cannot be rescued or
    broken by the Agent lifetime above.
    """
    import gc

    import vaf.core.agent as agent_mod
    from vaf.core.agent import live_agents

    class _Member:
        pass

    m = _Member()
    agent_mod._LIVE_AGENTS.add(m)
    assert live_agents() == [m]
    del m
    gc.collect()
    assert live_agents() == []


# ── the reach: the defect itself ────────────────────────────────────────────────────

def test_broadcast_reaches_every_agent_not_just_the_first(monkeypatch):
    """FIVE workers, one registered pointer: this is the shape that shipped.

    Two agents are enough to express it - the bug is 'not all of them', not 'not five'.
    """
    from vaf.core.agent import Agent, reload_all_api_backends

    first = Agent(register_signals=False)
    second = Agent(register_signals=False)
    _cloud_config(monkeypatch)

    changed = reload_all_api_backends(force=True)

    assert changed == 2
    for agent in (first, second):
        assert agent.provider == "deepseek"
        assert getattr(agent.api_backend, "api_key", None) == "sk-the-new-one"


def test_one_failing_agent_does_not_stop_the_rest(monkeypatch):
    """A config change reaching four of five is bad; reaching one is worse."""
    import vaf.core.agent as agent_mod
    from vaf.core.agent import Agent, reload_all_api_backends

    class _Broken:
        def reload_api_backend(self, *, force=False):
            raise RuntimeError("this agent is having a bad day")

    broken = _Broken()
    agent_mod._LIVE_AGENTS.add(broken)
    healthy = Agent(register_signals=False)
    _cloud_config(monkeypatch)

    assert reload_all_api_backends(force=True) == 1
    assert healthy.provider == "deepseek"


# ── the refusal side: more reach must not mean more power ───────────────────────────

def test_broadcast_leaves_an_embedded_agent_alone(monkeypatch):
    """An embedder's in-memory key is not the file's to replace.

    `reload_api_backend` re-reads the on-disk PROVIDER, so without its embedded guard a
    broadcast would move a library caller onto a provider they never chose. The guard is
    per instance; this asserts the broadcast does not route around it.
    """
    from vaf.core.agent import Agent, reload_all_api_backends

    embedded = Agent(register_signals=False, config_overrides={"provider": "local"})
    before = embedded.provider
    _cloud_config(monkeypatch)

    assert reload_all_api_backends(force=True) == 0
    assert embedded.provider == before


def test_broadcast_leaves_a_pinned_subagent_alone(monkeypatch):
    """A sub-agent process is pinned to its provider via VAF_PROVIDER."""
    from vaf.core.agent import Agent, reload_all_api_backends

    pinned = Agent(register_signals=False)
    before = pinned.provider
    _cloud_config(monkeypatch)
    monkeypatch.setitem(os.environ, "VAF_PROVIDER", "anthropic")

    assert reload_all_api_backends(force=True) == 0
    assert pinned.provider == before


# ── the harness half: the tray actually calls it ────────────────────────────────────

def test_tray_key_change_broadcasts(monkeypatch):
    """The observer that the live defect ran through.

    It used to read `web_interface.agent_instance`, a pointer the headless runner assigns
    from worker 1 only. Asserting the branch reaches the broadcast is the difference
    between a fixed primitive and a fixed product.
    """
    import threading

    import vaf.core.agent as agent_mod
    import vaf.tray as tray

    seen = {}
    done = threading.Event()

    def _spy(*, force=False):
        seen["force"] = force
        done.set()
        return 3

    monkeypatch.setattr(agent_mod, "reload_all_api_backends", _spy)
    tray.on_config_changed("api_key_deepseek", "sk-x", None)

    assert done.wait(timeout=10), "the tray never re-applied a changed API key"
    assert seen["force"] is True, "a key-only change needs force, or the no-op guard eats it"


def test_provider_change_broadcasts_without_force(monkeypatch):
    """`force` exists for the key case; a provider change is already a difference."""
    import threading

    import vaf.core.agent as agent_mod
    import vaf.tray as tray

    seen = {}
    done = threading.Event()

    def _spy(*, force=False):
        seen["force"] = force
        done.set()
        return 1

    monkeypatch.setattr(agent_mod, "reload_all_api_backends", _spy)
    tray.on_config_changed("provider", "deepseek", "local")

    assert done.wait(timeout=10)
    assert seen["force"] is False


# ── and it stays internal ───────────────────────────────────────────────────────────

def test_the_broadcast_is_not_on_the_public_facade():
    """Owner decision 2026-07-31, pinned here because the argument is easy to forget.

    The obvious move after building this was to export it, and it is wrong: the
    broadcast refuses for any agent carrying config overrides, which is every agent an
    embedder builds the way `docs/EMBEDDING.md` teaches. Publishing it would have given
    the intended audience a name that does nothing for them - and the facade has no
    single-agent reload either, so the plural would have arrived without the singular.
    The harness proved this primitive with five call sites; embedders have not.
    """
    import vaf

    assert "reload_all_api_backends" not in vaf.__all__
    assert not hasattr(vaf, "reload_all_api_backends")
