# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: the CoreAgent documented surface (docs/CORE_AGENT.md).

Every pin here is a SUBSET assertion, never signature equality: CORE_AGENT.md
promises stability "at the level documented here", and the code may carry
undocumented extras (e.g. a constructor kwarg or a trailing chat_step
parameter that the doc deliberately omits). A documented parameter that
disappears, changes kind or changes default breaks an embedder; a new
undocumented extra does not and must not fail this suite.

CoreAgent is NEVER constructed here - construction registers a session in the
home directory and creates state in the cwd. Signature and identity pins are
import-only.
"""
import inspect

import vaf


PARAM = inspect.Parameter
POK = PARAM.POSITIONAL_OR_KEYWORD
KW_ONLY = PARAM.KEYWORD_ONLY
REQUIRED = PARAM.empty


def assert_params(fn, expected):
    """Subset pin: each documented parameter must exist with the documented
    kind and default; undocumented extras are allowed and never fail.

    Defaults compare by value AND type so a drift like False -> 0 (equal in
    Python) is still caught. REQUIRED means no default (Parameter.empty).
    """
    params = inspect.signature(fn).parameters
    for name, (kind, default) in expected.items():
        assert name in params, f"{fn.__qualname__} lost parameter {name!r}"
        p = params[name]
        assert p.kind is kind, (
            f"{fn.__qualname__} parameter {name!r} changed kind: "
            f"documented {kind}, found {p.kind}"
        )
        assert p.default == default and type(p.default) is type(default), (
            f"{fn.__qualname__} parameter {name!r} changed default: "
            f"documented {default!r}, found {p.default!r}"
        )


def test_the_constructor_keeps_the_five_documented_kwargs_and_their_defaults():
    """CORE_AGENT.md documents exactly these constructor parameters; an
    embedder's CoreAgent(...) call spells them by keyword. Extras beyond the
    documented five (e.g. the facade's persona seam) are permitted."""
    assert_params(vaf.CoreAgent.__init__, {
        "verbose": (POK, False),
        "register_signals": (POK, True),
        "config_overrides": (POK, None),
        "run_kind": (POK, None),
        "host_audio": (POK, False),
    })


def test_chat_step_keeps_the_documented_turn_parameters():
    """The documented chat_step call shape: user_input is the one required
    argument, everything else defaults to a plain single-shot turn. Renaming
    or re-defaulting any of these breaks every embedder keyword call."""
    assert_params(vaf.CoreAgent.chat_step, {
        "user_input": (POK, REQUIRED),
        "stream_callback": (POK, None),
        "auto_retry": (POK, False),
        "skip_input": (POK, False),
        "disable_workflows": (POK, False),
        "disable_tools": (POK, False),
        "memory_context": (POK, None),
        "thinking_mode": (POK, False),
        "images": (POK, None),
        "force_tool_choice": (POK, None),
        "allow_memory_search": (POK, False),
    })


def test_complete_keeps_prompt_required_and_all_tuning_knobs_keyword_only():
    """complete(prompt, *, ...) is documented with the keyword-only marker:
    the tuning knobs can never be passed positionally, which is what lets the
    engine add knobs without breaking positional callers."""
    assert_params(vaf.CoreAgent.complete, {
        "prompt": (POK, REQUIRED),
        "max_tokens": (KW_ONLY, 512),
        "temperature": (KW_ONLY, 0.2),
        "timeout": (KW_ONLY, None),
        "strip_think": (KW_ONLY, True),
    })


def test_execute_tool_and_the_two_setters_keep_their_documented_parameters():
    """execute_tool(name, args) -> str is the documented single-call dispatch;
    set_event_sink(sink) and set_tool_authorizer(authorize) are the two
    documented observation/authorization attachment points."""
    assert_params(vaf.CoreAgent.execute_tool, {
        "name": (POK, REQUIRED),
        "args": (POK, REQUIRED),
    })
    assert_params(vaf.CoreAgent.set_event_sink, {
        "sink": (POK, REQUIRED),
    })
    assert_params(vaf.CoreAgent.set_tool_authorizer, {
        "authorize": (POK, REQUIRED),
    })


def test_the_documented_lifecycle_and_reload_methods_exist_on_the_class():
    """CORE_AGENT.md lists these as the stable lifecycle, accessor and hot
    reload surface; losing any name is a breaking change even before its
    behavior is considered."""
    for name in (
        "init_chat",
        "load_model",
        "shutdown",
        "get_token_usage",
        "load_session_context",
        "reload_api_backend",
        "reload_local_model",
        "reload_builtin_tools",
        "reload_custom_tools",
        "reload_mcp_tools",
    ):
        method = getattr(vaf.CoreAgent, name, None)
        assert method is not None, f"CoreAgent lost documented method {name!r}"
        assert callable(method), f"CoreAgent.{name} is no longer callable"


def test_module_level_reload_all_api_backends_exists_with_keyword_only_force():
    """The process-wide broadcast is documented in CORE_AGENT.md as living at
    module level in vaf.core.agent and as deliberately NOT on the facade
    (it declines for every agent built with config_overrides), so the module
    path itself is the documented way to reach it."""
    import vaf.core.agent as core_agent  # documented location of reload_all_api_backends (CORE_AGENT.md)

    fn = getattr(core_agent, "reload_all_api_backends", None)
    assert fn is not None, "vaf.core.agent lost reload_all_api_backends"
    assert callable(fn)
    assert_params(fn, {"force": (KW_ONLY, False)})
    # Deliberately not re-exported: the facade must NOT grow this name
    # silently - if it ever does, the doc sentence and this pin change together.
    assert "reload_all_api_backends" not in vaf.__all__


def test_vaf_coreagent_is_the_engine_agent_class_itself():
    """Documented identity: vaf.CoreAgent IS vaf.core.agent.Agent - a
    re-export, not a wrapper, so signature pins taken on either name can
    never drift apart."""
    from vaf.core.agent import Agent as EngineAgent  # the documented alias target

    assert vaf.CoreAgent is EngineAgent
