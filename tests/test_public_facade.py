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
    """The public surface is a promise, so it is spelled out here rather than derived.

    BaseTool and user_jail joined it when tool identity became declarative: a tool an
    embedder registers needs to subclass BaseTool and declare identity_kwargs, and turning
    the identity it then receives into an actual file boundary needs user_jail. Documented
    in docs/EMBEDDING.md; both are stdlib-only underneath, so the slim base is unaffected
    (tests/test_slim_base_import.py).

    CONSIDERED AND LEFT OFF (2026-07-31), so it is not re-litigated blind:
    `reload_all_api_backends`, the process-wide provider/key re-apply that fixed the
    harness. It stays internal in `vaf.core.agent` for two reasons that only look like
    one. It refuses for any agent constructed with config overrides - which is every
    agent an embedder builds the documented way - so publishing it would have handed the
    intended audience a name that does nothing for them. And the facade exposes no
    single-agent reload either, so the broadcast alone would have been a surface with no
    floor under it. The harness has proven the primitive with five call sites; embedders
    have proven nothing yet, and that is the measurement that decides.

    RE-CHECKED (2026-08-03) when the terminal app gained its provider switch, and
    the line did NOT move. The app is a sixth first-party caller of the broadcast,
    not the embedder report that would decide it. A new single-agent "apply the
    current config" method was measured and rejected: the in-place hand-rolls
    number two, and both are redundant (`agent.config = Config.load()` in front of
    a `force=True` reload that already refreshes it), while the six rebuild sites
    in the classic lanes replace the OBJECT to get a new model path, context
    manager and server - which an in-place method cannot serve, so it would delete
    none of them. Unique N for a new engine method: zero."""
    assert vaf.__version__
    assert sorted(vaf.__all__) == [
        "Agent", "BOOKKEEPING_KINDS", "BaseTool", "CoreAgent",
        "PathEscape", "RemoteRefused", "RemoteRoom",
        "Room", "RoomError", "SOUL_CONTINUITY_ADDENDUM", "StoreError",
        "ToolCaller", "ToolRequest", "TurnOutcome", "UnsafeName", "UploadVerdict",
        "VoiceTurnEngine",
        "__version__",
        "account_allows_tool", "build_capability_addendum", "contained_path",
        "derive_peer_id", "describe_room_entry", "extract_pdf_markdown",
        "fold_room_tasks", "fold_room_votes", "inspect_upload",
        "install_thread_excepthook", "invited_rooms", "joined_rooms", "markers",
        "participant_key", "record_threat", "room_invitation",
        "safe_entry_name", "set_account_allowlist_resolver",
        "set_account_directory_resolver", "set_confirmation_bypass_resolver",
        "unread_counts", "user_jail",
    ]
    assert dir(vaf) == sorted(vaf.__all__)


def test_the_newly_public_names_actually_resolve():
    """A name in __all__ that __getattr__ does not serve would be a broken promise."""
    assert vaf.BaseTool.__name__ == "BaseTool"
    assert callable(vaf.user_jail)
    # The declaration field a third-party tool is told to set must exist on the base class.
    assert isinstance(vaf.BaseTool.identity_kwargs, tuple)
    assert vaf.ToolCaller.__name__ == "ToolCaller"
    assert callable(vaf.ToolCaller.execute)
    assert vaf.ToolRequest.__name__ == "ToolRequest"
    for method in ("deny", "ask", "allow"):
        assert callable(getattr(vaf.ToolRequest, method)), f"ToolRequest lost {method}()"
    # Document extraction: exported because two in-tree consumers hand-rolled
    # byte-identical truncations over private imports - an embedder has the
    # same need. Import must stay cheap (stdlib at module level).
    assert callable(vaf.extract_pdf_markdown)
    assert callable(vaf.account_allows_tool)
    assert callable(vaf.contained_path)
    assert callable(vaf.safe_entry_name)
    assert issubclass(vaf.PathEscape, ValueError)
    assert callable(vaf.set_account_allowlist_resolver)
    assert callable(vaf.set_confirmation_bypass_resolver)


def test_the_resolver_setter_is_the_same_object_on_facade_and_engine():
    """Same rule as the authorizer's name test, extended to identity: the facade serves
    the engine's function itself, not a wrapper - a wrapper would let the two drift."""
    import vaf.core.tool_dispatch as td

    assert vaf.account_allows_tool is td.account_allows_tool
    assert vaf.set_account_allowlist_resolver is td.set_account_allowlist_resolver


def test_the_authorizer_has_the_same_name_on_the_facade_and_the_engine():
    """A rule taken from a mistake: `on_event` on the facade wraps `set_event_sink` on the
    engine, so a reader who learns one name has to learn the other. Kept for compatibility,
    never repeated - anything new is spelled identically on both."""
    assert callable(Agent.set_tool_authorizer)
    assert callable(CoreAgent.set_tool_authorizer)
    assert callable(Agent.complete)
    assert callable(CoreAgent.complete)


def test_an_authorizer_survives_being_set_before_the_engine_exists():
    """Applications wire their callbacks up at construction time, before anything has run.
    The facade builds its engine lazily, so a callback set early has to be REPLAYED onto it -
    the same guarantee `on_event` documents. Asserting the cache alone would pass even if the
    replay were missing, which is the only way this can actually break."""
    from unittest.mock import MagicMock, patch

    def _authorize(request):
        pass

    agent = Agent(config={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    agent.set_tool_authorizer(_authorize)
    with patch("vaf.framework.CoreAgent", return_value=MagicMock()) as built:
        engine = agent.core
    assert built.called, "the facade did not build an engine"
    engine.set_tool_authorizer.assert_called_once_with(_authorize)


def test_detaching_an_authorizer_reaches_a_live_engine():
    from unittest.mock import MagicMock, patch

    agent = Agent(config={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    with patch("vaf.framework.CoreAgent", return_value=MagicMock()):
        engine = agent.core
    agent.set_tool_authorizer(None)
    engine.set_tool_authorizer.assert_called_with(None)


def test_tool_caller_keeps_the_arguments_embedding_md_documents():
    """The constructor takes more than this, and EMBEDDING.md says so - the rest exists for
    VAF's own lanes and may move. What is listed in that table is a promise, so it is spelled
    out here: removing or renaming one of these is a breaking change, and renaming a private
    one is not."""
    params = inspect.signature(vaf.ToolCaller.__init__).parameters
    documented = {
        "tools": inspect.Parameter.POSITIONAL_OR_KEYWORD,
        "user_scope_id": inspect.Parameter.KEYWORD_ONLY,
        "username": inspect.Parameter.KEYWORD_ONLY,
        "user_role": inspect.Parameter.KEYWORD_ONLY,
        "source": inspect.Parameter.KEYWORD_ONLY,
        "session_id": inspect.Parameter.KEYWORD_ONLY,
        "interactive": inspect.Parameter.KEYWORD_ONLY,
        "decide": inspect.Parameter.KEYWORD_ONLY,
        "trust_dir": inspect.Parameter.KEYWORD_ONLY,
        "timeout_for": inspect.Parameter.KEYWORD_ONLY,
        "stop_check": inspect.Parameter.KEYWORD_ONLY,
        "max_result_chars": inspect.Parameter.KEYWORD_ONLY,
        "on_event": inspect.Parameter.KEYWORD_ONLY,
    }
    for name, kind in documented.items():
        assert name in params, f"ToolCaller lost the documented argument {name!r}"
        assert params[name].kind is kind, f"{name} changed how it may be passed"

    # Defaults that the documentation states outright.
    assert params["max_result_chars"].default == 2000
    assert params["interactive"].default is False


def test_a_documented_tool_caller_run_needs_nothing_but_a_registry():
    """The claim EMBEDDING.md opens with: no Agent, no session, no chat turn."""
    class _Echo(vaf.BaseTool):
        name = "facade_echo"
        description = "echo"
        permission_level = "read"
        parameters = {"type": "object", "properties": {}}

        def run(self, **kwargs):
            return "echoed"

    assert vaf.ToolCaller({"facade_echo": _Echo()}).execute("facade_echo", {}) == "echoed"


def test_agent_constructor_signature_is_stable():
    params = list(inspect.signature(Agent.__init__).parameters.values())[1:]
    assert [p.name for p in params] == ["config", "system_prompt", "verbose", "user_scope", "session"]
    config, system_prompt, verbose, user_scope, session = params
    assert config.default is None
    assert system_prompt.kind is inspect.Parameter.KEYWORD_ONLY
    assert system_prompt.default is None
    assert verbose.kind is inspect.Parameter.KEYWORD_ONLY
    assert verbose.default is False
    assert user_scope.kind is inspect.Parameter.KEYWORD_ONLY
    assert user_scope.default is None
    assert session.kind is inspect.Parameter.KEYWORD_ONLY
    assert session.default is None


def test_agent_run_signature_is_stable():
    params = list(inspect.signature(Agent.run).parameters.values())[1:]
    assert [p.name for p in params] == ["prompt", "on_token"]
    assert params[1].default is None
    assert isinstance(Agent.core, property)


def test_coreagent_is_the_engine_class():
    from vaf.core.agent import Agent as EngineAgent

    assert CoreAgent is EngineAgent
    # Engine entry points the facade and documented embedding recipes rely on.
    for method in ("init_chat", "chat_step", "execute_tool", "set_event_sink", "complete"):
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
        "category": "general",
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

    def __init__(self, verbose=False, register_signals=True, config_overrides=None, system_prompt=None):
        self.api_backend = None
        self.llm = None
        self.use_server = False
        self.tools = {}
        self._system_prompt_override = system_prompt

    def init_chat(self):
        type(self).calls.append("init_chat")

    def load_model(self):
        type(self).calls.append("load_model")
        self.use_server = True

    def chat_step(self, prompt, stream_callback=None):
        if stream_callback:
            stream_callback("hi")
        return "hi"

    def complete(self, prompt, **kwargs):
        type(self).calls.append("complete")
        return "done"

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


def test_complete_goes_through_the_engine_build_like_run(monkeypatch):
    """First use pays the documented .core cost (init_chat; local also load_model),
    then delegates to the engine's complete - the same-name method, not chat_step."""
    import vaf.framework as fw

    _StubCore.calls = []
    monkeypatch.setattr(fw, "CoreAgent", _StubCore)
    agent = fw.Agent(config={"provider": "local"})
    assert agent.complete("side question") == "done"
    assert _StubCore.calls == ["init_chat", "load_model", "complete"]


def test_complete_on_api_skips_load_model(monkeypatch):
    import vaf.framework as fw

    class _ApiStub(_StubCore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.api_backend = object()

    _StubCore.calls = []
    monkeypatch.setattr(fw, "CoreAgent", _ApiStub)
    agent = fw.Agent(config={"provider": "deepseek"})
    assert agent.complete("side question") == "done"
    assert _StubCore.calls == ["init_chat", "complete"]


def test_complete_never_enters_the_conversation(monkeypatch):
    """The facade guarantee: complete() must not run a chat turn. Pinned with a bomb -
    a stub whose chat_step raises proves the delegation target by absence."""
    import vaf.framework as fw

    class _BombStub(_StubCore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.api_backend = object()

        def chat_step(self, prompt, stream_callback=None):
            raise AssertionError("complete() entered the conversation lane")

    _StubCore.calls = []
    monkeypatch.setattr(fw, "CoreAgent", _BombStub)
    agent = fw.Agent(config={"provider": "deepseek"})
    assert agent.complete("side question") == "done"


def test_add_tool_registers_before_engine_build(monkeypatch):
    """Per-instance tool registration: tools added before the first run land
    in the engine registry before init_chat builds the system prompt."""
    import vaf.framework as fw

    order = []

    class _Recorder(_StubCore):
        def init_chat(self):
            order.append(("init_chat", sorted(self.tools)))

        def load_model(self):
            self.use_server = True

    class _PingTool(BaseTool):
        name = "ping_tool"
        description = "test tool"

        def run(self, **kwargs):
            return "pong"

    monkeypatch.setattr(fw, "CoreAgent", _Recorder)
    agent = fw.Agent(config={"provider": "local"})
    agent.add_tool(_PingTool())
    assert agent.run("hi") == "hi"
    assert order == [("init_chat", ["ping_tool"])]


def test_add_tool_rejects_late_and_invalid_registration(monkeypatch):
    import pytest

    import vaf.framework as fw

    class _CoderTool(BaseTool):
        name = "coder_tool"
        description = "coder only"
        coder_only = True

        def run(self, **kwargs):
            return "x"

    monkeypatch.setattr(fw, "CoreAgent", _StubCore)
    agent = fw.Agent(config={"provider": "deepseek"})
    with pytest.raises(TypeError):
        agent.add_tool(object())
    with pytest.raises(ValueError):
        agent.add_tool(_CoderTool())
    agent.run("hi")  # builds the engine
    class _LateTool(BaseTool):
        name = "late_tool"
        description = "too late"

        def run(self, **kwargs):
            return "x"

    with pytest.raises(RuntimeError):
        agent.add_tool(_LateTool())


def test_user_scope_rejects_invalid_values_at_construction():
    import pytest

    for bad in ("not-a-uuid", "", 123, "1234"):
        with pytest.raises(ValueError, match="user_scope must be a valid UUID"):
            Agent(user_scope=bad)


def test_user_scope_binds_identity_before_init_chat_and_reasserts(monkeypatch):
    """Multi-tenant contract: scope AND username travel together, are bound
    BEFORE the system prompt is built, and are re-asserted every turn (a
    session load rebinding identity must not stick)."""
    import uuid

    import vaf.framework as fw

    scope = str(uuid.uuid4())
    events = []

    class _IdStub(_StubCore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.api_backend = object()

        def init_chat(self):
            events.append(
                ("init_chat", str(getattr(self, "_current_user_scope_id", None)),
                 getattr(self, "_current_username", None))
            )

    monkeypatch.setattr(fw, "CoreAgent", _IdStub)
    import vaf.core.thinking_mode as tm

    monkeypatch.setattr(tm, "_resolve_username_for_scope", lambda s: "max")

    agent = fw.Agent(config={"provider": "deepseek"}, user_scope=scope)
    assert agent.run("hi") == "hi"
    assert events == [("init_chat", scope, "max")]

    # Simulate a session load clobbering identity - the next turn rebinds.
    core = agent.core
    core._current_user_scope_id = None
    core._current_username = "admin"
    agent.run("again")
    assert str(core._current_user_scope_id) == scope
    assert core._current_username == "max"


def test_user_scope_username_falls_back_synthetic_never_admin(monkeypatch):
    import uuid

    import vaf.framework as fw

    scope = str(uuid.uuid4())

    class _IdStub(_StubCore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.api_backend = object()

    monkeypatch.setattr(fw, "CoreAgent", _IdStub)
    import vaf.core.thinking_mode as tm

    monkeypatch.setattr(
        tm, "_resolve_username_for_scope", lambda s: (_ for _ in ()).throw(RuntimeError())
    )
    agent = fw.Agent(config={"provider": "deepseek"}, user_scope=scope)
    agent.run("hi")
    username = agent.core._current_username
    assert username == f"scope_{scope.replace('-', '')[:8]}"
    assert username != "admin"


def test_run_async_is_a_thread_executor_wrapper(monkeypatch):
    import asyncio

    import vaf.framework as fw

    monkeypatch.setattr(fw, "CoreAgent", _StubCore)
    agent = fw.Agent(config={"provider": "deepseek"})
    assert inspect.iscoroutinefunction(fw.Agent.run_async)
    assert asyncio.run(agent.run_async("hello")) == "hi"


def test_on_event_attaches_before_and_after_engine_build(monkeypatch):
    import vaf.framework as fw

    sinks = []

    class _SinkStub(_StubCore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.api_backend = object()

        def set_event_sink(self, sink):
            sinks.append(sink)

    monkeypatch.setattr(fw, "CoreAgent", _SinkStub)

    cb = lambda evt: None  # noqa: E731
    # Before build: registered, applied at engine build.
    agent = fw.Agent(config={"provider": "deepseek"})
    agent.on_event(cb)
    agent.run("hi")
    assert sinks == [cb]
    # After build: applied immediately.
    cb2 = lambda evt: None  # noqa: E731
    agent.on_event(cb2)
    assert sinks == [cb, cb2]


def _patched_session_manager(monkeypatch, tmp_path):
    import vaf.core.session as session_mod

    real = session_mod.SessionManager

    def _factory(*args, **kwargs):
        kwargs.setdefault("storage_dir", str(tmp_path))
        return real(*args, **kwargs)

    monkeypatch.setattr(session_mod, "SessionManager", _factory)
    return _factory


def test_save_session_persists_and_updates_in_place(monkeypatch, tmp_path):
    import vaf.framework as fw

    _patched_session_manager(monkeypatch, tmp_path)

    class _HistStub(_StubCore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.api_backend = object()
            self.config = {"model": "test-model"}
            self.history = [
                {"role": "system", "content": "SYSTEM PROMPT"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there",
                 "tool_calls": [{"id": "call_1", "type": "function",
                                 "function": {"name": "t", "arguments": "{}"}}]},
                {"role": "tool", "content": "res", "tool_call_id": "call_1", "name": "t"},
            ]

    monkeypatch.setattr(fw, "CoreAgent", _HistStub)
    agent = fw.Agent(config={"provider": "deepseek"})
    agent.run("hello")
    sid = agent.save_session()
    assert sid and agent.save_session() == sid  # stable id, update in place

    import json

    from vaf.core import data_files
    data = json.loads(data_files.read_bytes(tmp_path / f"{sid}.json").decode("utf-8"))
    roles = [m["role"] for m in data["messages"]]
    assert "system" not in roles  # prompt is rebuilt on load, never persisted
    assert roles == ["user", "assistant", "tool"]
    assert data["messages"][1]["tool_calls"][0]["id"] == "call_1"
    assert data["messages"][2]["tool_call_id"] == "call_1"


def test_session_resume_loads_and_checks_ownership(monkeypatch, tmp_path):
    import uuid

    import pytest

    import vaf.framework as fw
    import vaf.core.session as session_mod

    factory = _patched_session_manager(monkeypatch, tmp_path)

    owner = str(uuid.uuid4())
    mgr = factory()
    s = mgr.new(model="m", user_scope_id=owner)
    s.add_message("user", "old message")
    mgr.save(s, sync_state=False)

    loads = []

    class _LoadStub(_StubCore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.api_backend = object()

        def load_session_context(self, session_id):
            loads.append(session_id)

    monkeypatch.setattr(fw, "CoreAgent", _LoadStub)

    # Unknown id fails loudly AT CONSTRUCTION (example 05 relies on this).
    with pytest.raises(ValueError, match="not found"):
        fw.Agent(config={"provider": "deepseek"}, session="nope123")

    # Wrong tenant is refused.
    import vaf.core.thinking_mode as tm

    monkeypatch.setattr(tm, "_resolve_username_for_scope", lambda sc: "someone")
    stranger = str(uuid.uuid4())
    with pytest.raises(ValueError, match="different user_scope"):
        fw.Agent(config={"provider": "deepseek"}, session=s.id, user_scope=stranger)

    # Owner (and unscoped local use) resumes.
    fw.Agent(config={"provider": "deepseek"}, session=s.id, user_scope=owner).run("hi")
    assert loads == [s.id]


# ── rooms on the facade ────────────────────────────────────────────────────

def test_the_room_surface_is_the_one_the_house_already_reaches_for():
    """MUTATION: export the whole room package, or a smaller arbitrary slice.

    The mission's rule is that every place reaching past the facade IS the
    specification of what an embedder needs. So this list is not designed - it is
    MEASURED, and this test re-measures it: every name exported here has to be one that
    surfaces outside the room package actually import, and a name they reach for often
    and cannot get from the facade is a gap rather than a preference.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    wanted: dict = {}
    for path in list((root / "vaf").rglob("*.py")):
        if "core/a2a" in path.as_posix():
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        for names in re.findall(r"from vaf\.core\.a2a\.\w+ import \(?([^)\n]+)", source):
            for raw in names.split(","):
                name = raw.strip().split(" as ")[0].strip()
                if name and name[0].isupper() or name in (
                        "describe", "invitation", "joined_rooms", "participant_key",
                        "unread_counts", "unread_frames", "frame_clock"):
                    wanted.setdefault(name, set()).add(path.as_posix())

    # Renamed on the way out: two of the vaguest words available on a shared facade.
    exported = {n for n in vaf.__all__}
    alias = {"describe_room_entry": "describe", "room_invitation": "invitation"}
    covered = {alias.get(n, n) for n in exported}

    for name, users in wanted.items():
        if len(users) >= 3 and name not in covered:
            raise AssertionError(
                f"{name} is imported from inside the room package by {len(users)} "
                f"surfaces and is not on the facade: {sorted(users)}")


def test_the_exported_room_names_are_importable_and_are_the_real_thing():
    from vaf.core.a2a.invite import invitation
    from vaf.core.a2a.room import Room, RoomError, derive_peer_id, describe, joined_rooms
    from vaf.core.a2a.room import participant_key, unread_counts
    from vaf.core.a2a.store import StoreError, UnsafeName

    assert vaf.Room is Room
    assert vaf.UnsafeName is UnsafeName
    assert vaf.RoomError is RoomError
    assert vaf.StoreError is StoreError
    assert vaf.derive_peer_id is derive_peer_id
    assert vaf.describe_room_entry is describe
    assert vaf.joined_rooms is joined_rooms
    assert vaf.participant_key is participant_key
    assert vaf.room_invitation is invitation
    assert vaf.unread_counts is unread_counts


def test_importing_vaf_still_costs_nothing():
    """MUTATION: import the room modules at the top of the facade.

    The whole point of the lazy surface is that `import vaf` stays cheap on the slim
    base. A room brings the store, which brings encryption - none of which a program
    that never opens a room should pay for.
    """
    import subprocess
    import sys

    probe = ("import sys, vaf; "
             "assert not [m for m in sys.modules if m.startswith('vaf.core.a2a')], "
             "'importing vaf dragged the room package in'")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
