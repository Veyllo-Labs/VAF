# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""CI guard for the public library surface documented in docs/EMBEDDING.md.

docs/setup/RELEASING.md forbids breaking ``from vaf import Agent``, the
BaseTool contract, or documented config keys without a MAJOR bump and a
deprecation note. Until this file existed nothing in CI imported the facade,
so a breaking change to the promised surface would have shipped green.
Pattern: an executable contract, like tests/test_coder_provider_map.py.
"""
import inspect

import vaf
from vaf import Agent, CoreAgent
from vaf.tools.base import BaseTool


def test_facade_exports_exactly_the_documented_surface():
    assert vaf.__version__
    assert sorted(vaf.__all__) == ["Agent", "CoreAgent", "__version__"]
    assert dir(vaf) == sorted(vaf.__all__)


def test_agent_constructor_signature_is_stable():
    params = list(inspect.signature(Agent.__init__).parameters.values())[1:]
    assert [p.name for p in params] == ["config", "verbose"]
    config, verbose = params
    assert config.default is None
    assert verbose.kind is inspect.Parameter.KEYWORD_ONLY
    assert verbose.default is False


def test_agent_run_signature_is_stable():
    params = list(inspect.signature(Agent.run).parameters.values())[1:]
    assert [p.name for p in params] == ["prompt", "on_token"]
    assert params[1].default is None
    assert isinstance(Agent.core, property)


def test_coreagent_is_the_engine_class():
    from vaf.core.agent import Agent as EngineAgent

    assert CoreAgent is EngineAgent
    # Engine entry points the facade and documented embedding recipes rely on.
    for method in ("init_chat", "chat_step", "execute_tool", "set_event_sink"):
        assert callable(getattr(CoreAgent, method)), method
    engine_init = inspect.signature(CoreAgent.__init__).parameters
    for kw in ("verbose", "register_signals", "config_overrides"):
        assert kw in engine_init, kw


def test_basetool_contract_defaults_are_stable():
    expected_defaults = {
        "name": "base_tool",
        "coder_only": False,
        "permission_level": "read",
        "side_effect_class": "none",
        "channel_restrictions": (),
        "admin_only": False,
        "input_examples": [],
    }
    for attr, default in expected_defaults.items():
        assert getattr(BaseTool, attr) == default, attr
    assert isinstance(BaseTool.parameters, dict)
    # run() must stay abstract: a tool without run() must fail at class level.
    assert inspect.isabstract(BaseTool)


def test_entry_point_tools_register_into_the_agent(monkeypatch):
    """The pip-package extension path from docs/EMBEDDING.md: a third-party
    package publishing a BaseTool subclass under the ``vaf.tools`` entry-point
    group gets registered; coder-only and non-BaseTool entries are skipped and
    never break loading."""

    class GoodTool(BaseTool):
        name = "ep_good_tool"
        description = "entry-point smoke tool"

        def run(self, **kwargs):
            return "ok"

    class CoderOnlyTool(BaseTool):
        name = "ep_coder_tool"
        description = "skipped: targets the coder"
        coder_only = True

        def run(self, **kwargs):
            return "ok"

    class NotATool:
        pass

    class _FakeEntryPoint:
        def __init__(self, name, obj):
            self.name = name
            self._obj = obj

        def load(self):
            return self._obj

    def fake_entry_points(group=None, **kwargs):
        assert group == "vaf.tools"
        return [
            _FakeEntryPoint("good", GoodTool),
            _FakeEntryPoint("coder", CoderOnlyTool),
            _FakeEntryPoint("bad", NotATool),
        ]

    # The loader does `from importlib.metadata import entry_points` at call
    # time, so patching the module attribute reaches it.
    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)

    class _Holder:
        pass

    holder = _Holder()
    holder.tools = {}
    CoreAgent._load_entry_point_tools(holder)

    assert list(holder.tools) == ["ep_good_tool"]
    assert holder.tools["ep_good_tool"].run() == "ok"


class _StubCore:
    """Duck-typed CoreAgent for facade lifecycle tests (no engine load)."""

    calls: list = []

    def __init__(self, verbose=False, register_signals=True, config_overrides=None):
        self.api_backend = None
        self.llm = None
        self.use_server = False

    def init_chat(self):
        type(self).calls.append("init_chat")

    def load_model(self):
        type(self).calls.append("load_model")
        self.use_server = True

    def chat_step(self, prompt, stream_callback=None):
        if stream_callback:
            stream_callback("hi")
        return "hi"

    def _clean_reasoning(self, s):
        return s


def test_facade_loads_local_model_on_first_run(monkeypatch):
    """Regression: with provider=local the facade never called load_model, so
    chat_step aborted ("Agent not initialized") and run() returned '' - the
    documented quickstart was broken (runtime-verified 2026-07-16)."""
    import vaf.framework as fw

    _StubCore.calls = []
    monkeypatch.setattr(fw, "CoreAgent", _StubCore)
    agent = fw.Agent(config={"provider": "local"})
    assert agent.run("hello") == "hi"
    assert _StubCore.calls == ["init_chat", "load_model"]


def test_facade_skips_load_model_when_api_backend_exists(monkeypatch):
    import vaf.framework as fw

    class _ApiStub(_StubCore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.api_backend = object()

    _StubCore.calls = []
    monkeypatch.setattr(fw, "CoreAgent", _ApiStub)
    agent = fw.Agent(config={"provider": "deepseek"})
    assert agent.run("hello") == "hi"
    assert _StubCore.calls == ["init_chat"]
