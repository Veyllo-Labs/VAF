# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression: one implementation of the provider swap, and it is the agent's.

``Agent.reload_api_backend`` re-applies provider and API key to a RUNNING agent. It is the
only place that knows the whole job: a swap lock, the sub-agent pin and embedded-mode
guards, the embedded-key path (``_build_api_backend``), reattaching the structured event
sink, tearing the local stack down (``stop_server``, so the GGUF leaves VRAM), resetting the
tokenizer and refreshing the displayed model name.

One command, ``__CMD__:RELOAD_CONFIG`` (web_server), reached three receivers, and each had
reimplemented the swap by hand - two of them under the comment "same logic as
headless_runner", a copy of a copy. None of the three did any of the above. The visible
symptoms were a GGUF that stayed resident after switching to a cloud provider, an API key
change that only took effect after a restart, and a stale model name in the UI. The
invisible one was worse and is pinned below: when the backend failed to build, the copies
had already switched the agent to the cloud provider and torn the local stack down, leaving
it with no backend at all.

Two kinds of test, and they are not interchangeable. The behavioral ones exercise the
shared implementation directly - each pins one thing the copies did not do, so the
capability cannot quietly regress. The source guard at the bottom is what pins that the
three receivers actually go through it; no behavioral test can see that, because a receiver
that reimplements the swap never calls the method under test.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf.cli.cmd.run as run_mod
import vaf.core.headless_runner as runner_mod
from vaf.core.agent import Agent

CLOUD = "veyllo"


class FakeBackend:
    def __init__(self, provider):
        self.provider = provider
        self.event_sink = None


class FakeServer:
    def __init__(self):
        self.stopped = False

    def stop_server(self):
        self.stopped = True


def _agent(*, provider="local", api_backend=None, server=None, overrides=None, sink=None):
    """A stand-in carrying exactly the attributes reload_api_backend touches. Bound-method
    dispatch (Agent.reload_api_backend(fake, ...)) is the house pattern for exercising one
    dispatcher method without building a whole agent - see tests/test_tool_contract.py."""
    return SimpleNamespace(
        provider=provider,
        api_backend=api_backend,
        server=server,
        use_server=server is not None,
        llm="loaded-gguf" if provider == "local" else None,
        verbose=False,
        config={},
        _config_overrides=overrides,
        _event_sink=sink,
        _tokenizer_instance="stale",
        model_display_name="old-model",
        _build_api_backend=lambda p: FakeBackend(p),
    )


@pytest.fixture
def live_config(monkeypatch):
    """Config.load() is the LIVE on-disk config the method re-reads; tests set what it says."""
    cfg = {"provider": "local"}

    class _Cfg:
        @staticmethod
        def load():
            return dict(cfg)

        @staticmethod
        def get_default_model(provider):
            return f"default-{provider}"

    monkeypatch.setattr("vaf.core.agent.Config", _Cfg)
    monkeypatch.delenv("VAF_PROVIDER", raising=False)
    monkeypatch.delenv("VAF_TOOL_MODEL", raising=False)
    return cfg


# ── What the copies got wrong ────────────────────────────────────────────────

def test_a_key_only_change_is_applied(live_config):
    """The copies branched on `if old_provider != new_provider`, so changing only the API
    key was a silent no-op until the next restart. force=True is what RELOAD_CONFIG passes,
    because the command does not carry WHICH key changed."""
    live_config["provider"] = CLOUD
    ag = _agent(provider=CLOUD, api_backend=FakeBackend(CLOUD))
    first = ag.api_backend

    assert Agent.reload_api_backend(ag, force=True) is True
    assert ag.api_backend is not first, "the backend was not rebuilt for a key-only change"


def test_a_failed_build_leaves_the_agent_with_a_working_backend(live_config):
    """The sharpest one. The copies set provider=cloud, then use_server=False and llm=None,
    even when constructing the backend had raised - leaving provider=cloud with
    api_backend=None and the local stack gone, i.e. no backend at all (Rule 4, local-vs-API).
    The shared implementation reports failure and changes nothing."""
    live_config["provider"] = CLOUD
    server = FakeServer()
    ag = _agent(server=server)

    def _boom(_provider):
        raise RuntimeError("no api key")

    ag._build_api_backend = _boom

    assert Agent.reload_api_backend(ag, force=True) is False
    assert ag.provider == "local"
    assert ag.llm == "loaded-gguf"
    assert ag.use_server is True
    assert server.stopped is False, "the local server was torn down for a swap that failed"


def test_switching_to_a_cloud_provider_releases_the_local_server(live_config):
    """The VRAM leak: without stop_server the GGUF stays resident after the user moves to
    an API provider."""
    live_config["provider"] = CLOUD
    server = FakeServer()
    ag = _agent(server=server)

    assert Agent.reload_api_backend(ag, force=True) is True
    assert server.stopped is True
    assert ag.server is None
    assert ag.use_server is False
    assert ag.llm is None


def test_the_event_sink_survives_the_swap(live_config):
    """Structured events (the Web UI's live feed) are attached to the BACKEND, so a swap
    that builds a fresh one silently ends them unless it reattaches."""
    live_config["provider"] = CLOUD
    sink = object()
    ag = _agent(sink=sink)

    Agent.reload_api_backend(ag, force=True)
    assert ag.api_backend.event_sink is sink


def test_the_tokenizer_and_the_displayed_model_are_refreshed(live_config):
    """A stale tokenizer miscounts the context of a different model, and the UI kept showing
    the previous model's name."""
    live_config["provider"] = CLOUD
    ag = _agent()

    Agent.reload_api_backend(ag, force=True)
    assert ag._tokenizer_instance is None
    assert ag.model_display_name == f"default-{CLOUD}"


def test_an_explicit_api_model_wins_over_the_default(live_config):
    live_config["provider"] = CLOUD
    live_config[f"api_model_{CLOUD}"] = "chosen-model"
    ag = _agent()

    Agent.reload_api_backend(ag, force=True)
    assert ag.model_display_name == "chosen-model"


# ── Guards the copies did not have at all ────────────────────────────────────

def test_a_subagent_pinned_to_a_provider_is_never_overridden(live_config, monkeypatch):
    """A sub-agent process is pinned via VAF_PROVIDER. The copies would have swapped it to
    whatever the live config said mid-run."""
    monkeypatch.setenv("VAF_PROVIDER", "anthropic")
    live_config["provider"] = CLOUD
    ag = _agent()

    assert Agent.reload_api_backend(ag, force=True) is False
    assert ag.provider == "local"


def test_embedded_mode_is_left_to_its_caller(live_config):
    """Agent(config={...}) is caller-controlled; on-disk config must not reach in."""
    live_config["provider"] = CLOUD
    ag = _agent(overrides={"api_key_veyllo": "x"})

    assert Agent.reload_api_backend(ag, force=True) is False
    assert ag.provider == "local"


def test_cloud_to_local_drops_the_api_backend(live_config):
    live_config["provider"] = "local"
    ag = _agent(provider=CLOUD, api_backend=FakeBackend(CLOUD))

    assert Agent.reload_api_backend(ag, force=True) is True
    assert ag.provider == "local"
    assert ag.api_backend is None


def test_without_force_an_unchanged_provider_is_a_no_op(live_config):
    """The no-op guard must survive: the self-heal call in headless_runner relies on it."""
    live_config["provider"] = CLOUD
    ag = _agent(provider=CLOUD, api_backend=FakeBackend(CLOUD))
    first = ag.api_backend

    assert Agent.reload_api_backend(ag) is False
    assert ag.api_backend is first


# ── One command, one implementation ──────────────────────────────────────────

def _reload_config_branches(src: str):
    """Every `RELOAD_CONFIG` handler body in a module, by indentation rather than by the
    next keyword - one of the three is the last branch of its chain, so anchoring on a
    following elif/else silently finds nothing and the guard passes for the wrong reason."""
    lines = src.splitlines()
    bodies = []
    for i, line in enumerate(lines):
        if 'cmd_type == "RELOAD_CONFIG"' not in line:
            continue
        indent = len(line) - len(line.lstrip())
        body = []
        for nxt in lines[i + 1:]:
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                break
            body.append(nxt)
        bodies.append("\n".join(body))
    return bodies


@pytest.mark.parametrize("module,expected", [(run_mod, 2), (runner_mod, 1)])
def test_every_reload_config_receiver_delegates_to_the_agent(module, expected):
    """web_server sends ONE __CMD__:RELOAD_CONFIG; these are the receivers. Each must ask
    the agent to swap rather than reconstruct the swap - reconstructing it is exactly how
    all three drifted, and how two of them became copies of a copy."""
    src = Path(module.__file__).read_text(encoding="utf-8")
    branches = _reload_config_branches(src)
    assert len(branches) == expected, (
        f"{module.__name__}: expected {expected} RELOAD_CONFIG handler(s), found {len(branches)}"
    )
    for body in branches:
        assert "reload_api_backend(force=True)" in body, (
            "a RELOAD_CONFIG receiver does not delegate the swap to the agent"
        )
        assert "APIBackendManager" not in body, (
            "a RELOAD_CONFIG receiver builds a backend itself again - that path misses "
            "stop_server, the embedded key, the event sink and the swap lock"
        )
        assert "use_server" not in body and ".llm =" not in body, (
            "a RELOAD_CONFIG receiver tears down the local stack itself again"
        )
