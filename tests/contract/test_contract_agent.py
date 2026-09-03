# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: `vaf.Agent` (docs/EMBEDDING.md Quickstart / Configuration / Stability list).

The facade Agent is the entry door a stranger's application is built on: its
constructor signature, its eager validations, its lazy engine build and its
session store location are all promises. Every assertion here pins one of
them; exception MESSAGES are matched by short substring only, because prose
may be reworded without breaking the contract.
"""
import inspect
import json
import os

import pytest

import vaf

# Synthetic tenant scopes (never real ids; suite convention).
SCOPE_A = "deadbeef-0000-0000-0000-000000000000"
SCOPE_B = "deadbeef-1111-1111-1111-111111111111"


class _BombCore:
    """Engine stand-in that fails the test if the facade ever builds it."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("CoreAgent was constructed - the facade is not lazy")


# Companion tier: this stub reaches past the public surface - it duck-types the
# engine that vaf.framework builds, which is a harness technique for testing the
# facade offline, not part of the embedding contract itself.
class _StubCore:
    def __init__(self, verbose=False, register_signals=True, config_overrides=None,
                 system_prompt=None):
        self.api_backend = object()  # non-None: the facade must not load a local model
        self.llm = None
        self.use_server = False
        self.tools = {}
        self.config = dict(config_overrides or {})
        self.history = []

    def init_chat(self):
        pass

    def load_model(self):
        raise AssertionError("load_model() called although an api_backend exists")

    def chat_step(self, prompt, stream_callback=None):
        return ""

    def complete(self, prompt, **kwargs):
        return None

    def _clean_reasoning(self, s):
        return s


class _ContractTool(vaf.BaseTool):
    name = "contract_probe"
    description = "contract probe tool"
    parameters = {"type": "object", "properties": {}}

    def run(self, **kwargs):
        return "ok"


class _CoderOnlyTool(_ContractTool):
    name = "contract_coder_probe"
    coder_only = True


@pytest.fixture(autouse=True)
def _noninteractive_env_restored():
    """Agent construction mutates process env (setdefault of VAF_NONINTERACTIVE);
    snapshot and restore so no test leaks its value into a later one."""
    previous = os.environ.get("VAF_NONINTERACTIVE")
    yield
    if previous is None:
        os.environ.pop("VAF_NONINTERACTIVE", None)
    else:
        os.environ["VAF_NONINTERACTIVE"] = previous


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Session tests write to the DOCUMENTED store ($HOME/.vaf/sessions/).

    A per-test HOME both isolates the writes and lets the test assert the store
    location itself. The store now resolves through one named seam rather than
    Path.home() at each site, so the seam is pointed here too - the repository's
    own conftest redirects it session-wide, and this fixture has to win for the
    location assertion to mean anything.
    """
    import vaf.core.session as session_module

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows: expanduser ignores HOME
    monkeypatch.setattr(session_module, "default_sessions_dir",
                        lambda: home / ".vaf" / "sessions")
    return home


def test_the_constructor_takes_config_positionally_and_the_rest_keyword_only():
    """Documented call shape: Agent(config, *, system_prompt=, verbose=,
    user_scope=, session=). Reordering or re-kinding a parameter breaks
    embedders' keyword calls."""
    params = inspect.signature(vaf.Agent.__init__).parameters
    assert list(params) == [
        "self", "config", "system_prompt", "verbose", "user_scope", "session",
    ]
    assert params["config"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params["config"].default is None
    for name, default in (
        ("system_prompt", None), ("verbose", False),
        ("user_scope", None), ("session", None),
    ):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params[name].default is default, name


def test_run_and_run_async_share_the_documented_streaming_signature():
    run_params = inspect.signature(vaf.Agent.run).parameters
    assert list(run_params) == ["self", "prompt", "on_token"]
    assert run_params["on_token"].default is None
    # run_async is documented as the same call in coroutine form.
    assert inspect.iscoroutinefunction(vaf.Agent.run_async)
    async_params = inspect.signature(vaf.Agent.run_async).parameters
    assert list(async_params) == ["self", "prompt", "on_token"]
    assert async_params["on_token"].default is None


def test_complete_pins_its_keyword_only_knobs_and_defaults():
    params = inspect.signature(vaf.Agent.complete).parameters
    assert list(params) == ["self", "prompt", "max_tokens", "temperature", "timeout"]
    for name, default in (("max_tokens", 512), ("temperature", 0.2), ("timeout", None)):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params[name].default == default, name


def test_the_documented_methods_exist_and_core_is_a_property():
    for name in ("add_tool", "on_event", "on_compaction", "set_tool_authorizer", "save_session"):
        assert callable(getattr(vaf.Agent, name)), f"Agent lost {name}()"
    assert isinstance(vaf.Agent.core, property)


@pytest.mark.parametrize("bad_scope", ["not-a-uuid", "", 123])
def test_a_bad_user_scope_fails_eagerly_and_builds_no_engine(bad_scope, monkeypatch):
    """user_scope is validated at the boundary: a bad value must raise at
    Agent(...), loudly, never fall back to the machine owner's data."""
    import vaf.framework as fw  # harness seam: CoreAgent is patched at its import site

    monkeypatch.setattr(fw, "CoreAgent", _BombCore)
    with pytest.raises(ValueError, match="user_scope"):
        vaf.Agent(user_scope=bad_scope)


def test_construction_is_lazy_and_only_core_builds_the_engine(monkeypatch):
    """Documented: configuration and connection problems surface at the first
    run()/.core access, not at Agent(...)."""
    import vaf.framework as fw  # harness seam: CoreAgent is patched at its import site

    monkeypatch.setattr(fw, "CoreAgent", _BombCore)
    agent = vaf.Agent(config={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    with pytest.raises(AssertionError):
        agent.core  # noqa: B018


def test_construction_defaults_the_process_to_noninteractive(monkeypatch):
    """Embedding-safe default: an embedded library must never hang on stdin,
    so construction sets VAF_NONINTERACTIVE=1."""
    monkeypatch.delenv("VAF_NONINTERACTIVE", raising=False)
    vaf.Agent()
    assert os.environ["VAF_NONINTERACTIVE"] == "1"


def test_an_explicit_noninteractive_opt_out_survives_construction(monkeypatch):
    """setdefault semantics are the documented opt-out: a caller's explicit
    value wins over the embedded default."""
    monkeypatch.setenv("VAF_NONINTERACTIVE", "0")
    vaf.Agent()
    assert os.environ["VAF_NONINTERACTIVE"] == "0"


def test_add_tool_rejects_non_basetool_values_with_typeerror():
    with pytest.raises(TypeError, match="BaseTool"):
        vaf.Agent().add_tool(object())


def test_add_tool_rejects_coder_only_tools_with_valueerror():
    with pytest.raises(ValueError, match="coder_only"):
        vaf.Agent().add_tool(_CoderOnlyTool())


def test_add_tool_raises_runtimeerror_once_the_engine_was_built(monkeypatch):
    """Documented: tools register before the first run()/.core access; the
    facade refuses later additions rather than leaving a tool half-visible."""
    import vaf.framework as fw  # harness seam: CoreAgent is patched at its import site

    monkeypatch.setattr(fw, "CoreAgent", _StubCore)
    agent = vaf.Agent(config={"provider": "deepseek"})
    assert isinstance(agent.core, _StubCore)  # trigger the build
    with pytest.raises(RuntimeError, match="before the first"):
        agent.add_tool(_ContractTool())


def test_an_unknown_session_id_raises_valueerror_at_construction(tmp_home):
    """Documented: a stale session id fails at Agent(...), where callers
    expect ValueError - not mid-run."""
    with pytest.raises(ValueError, match="not found"):
        vaf.Agent(session="nope123")


def test_save_session_persists_to_the_documented_store_and_is_idempotent(
    tmp_home, monkeypatch
):
    """Documented: sessions live in $HOME/.vaf/sessions/, save_session()
    returns the id, repeated calls update the same session, and the system
    prompt is never persisted."""
    import vaf.framework as fw  # harness seam: CoreAgent is patched at its import site

    monkeypatch.setattr(fw, "CoreAgent", _StubCore)
    agent = vaf.Agent(config={"provider": "deepseek"})
    agent.core.history = [
        {"role": "system", "content": "engine instructions"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    sid = agent.save_session()
    assert isinstance(sid, str) and sid
    store_file = tmp_home / ".vaf" / "sessions" / f"{sid}.json"
    assert store_file.exists(), "session file not in the documented store location"
    assert agent.save_session() == sid, "second save_session() must reuse the id"
    # The location and the record shape are the promise; the bytes are encrypted at
    # rest unless `file_encryption_enabled` is false, so read through the engine.
    from vaf.core import data_files
    data = json.loads(data_files.read_bytes(store_file).decode("utf-8"))
    roles = [m.get("role") for m in data["messages"]]
    assert "system" not in roles, "the system prompt must never be persisted"
    assert "user" in roles and "assistant" in roles


def test_resuming_another_tenants_session_is_refused_at_construction(
    tmp_home, monkeypatch
):
    """Multi-tenant promise: a session saved under one user_scope cannot be
    resumed under another - the refusal happens eagerly, at Agent(...)."""
    import vaf.framework as fw  # harness seam: CoreAgent is patched at its import site
    # Deterministic offline stand-in for the scope-name lookup (late-bound
    # import inside the resolver; the real one may reach a database).
    import vaf.core.thinking_mode as tm

    monkeypatch.setattr(tm, "_resolve_username_for_scope", lambda scope: "alice")
    monkeypatch.setattr(fw, "CoreAgent", _StubCore)
    agent_a = vaf.Agent(config={"provider": "deepseek"}, user_scope=SCOPE_A)
    agent_a.core.history = [{"role": "user", "content": "hello"}]
    sid = agent_a.save_session()
    with pytest.raises(ValueError, match="user_scope"):
        vaf.Agent(session=sid, user_scope=SCOPE_B)
