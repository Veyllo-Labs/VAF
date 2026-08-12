# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: the `import vaf` surface (docs/EMBEDDING.md, "What is and isn't stable").

Vendored by embedders and run against a pip-installed vaf: every assertion
here is a promise a stranger's code may rely on. Removing, renaming or
retyping one of these names is a breaking change.
"""
import re

import pytest

import vaf


EXPORTED = [
    "Agent",
    "BaseTool",
    "CoreAgent",
    "ToolCaller",
    "ToolRequest",
    "TurnOutcome",
    "VoiceTurnEngine",
    "__version__",
    "extract_pdf_markdown",
    "markers",
    "set_account_allowlist_resolver",
    "user_jail",
]


def test_the_facade_exports_exactly_the_documented_names():
    """Spelled out rather than derived: a new export must be added HERE (and
    get its own contract tests) before it ships, and a dropped one fails."""
    assert sorted(vaf.__all__) == EXPORTED
    assert dir(vaf) == sorted(vaf.__all__)


def test_every_exported_name_resolves_lazily():
    """The facade serves its names via PEP 562; a name in __all__ that
    __getattr__ does not serve would be a broken promise."""
    for name in EXPORTED:
        assert getattr(vaf, name) is not None, f"vaf.{name} did not resolve"


def test_an_unknown_name_raises_attribute_error():
    with pytest.raises(AttributeError):
        vaf.definitely_not_part_of_the_contract  # noqa: B018


def test_version_is_a_pep440_string():
    # The VALUE changes every release; the type and format are the contract.
    assert isinstance(vaf.__version__, str)
    assert re.match(
        r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$", vaf.__version__
    ), f"__version__ {vaf.__version__!r} is not PEP 440"


def test_core_agent_is_the_engine_class_itself():
    """Documented: vaf.CoreAgent (a.k.a. vaf.core.agent.Agent) - identity,
    not a wrapper, so the two can never drift."""
    from vaf.core.agent import Agent as EngineAgent  # the documented alias target

    assert vaf.CoreAgent is EngineAgent


def test_the_resolver_setter_is_the_engine_function_itself():
    """Same rule as CoreAgent: the facade re-exports the engine's function,
    it does not wrap it."""
    import vaf.core.tool_dispatch as td  # re-export source named in EMBEDDING.md

    assert vaf.set_account_allowlist_resolver is td.set_account_allowlist_resolver


def test_pdf_extraction_is_the_engine_function_itself():
    import vaf.core.pdf_extract as pe  # re-export source named in EMBEDDING.md

    assert vaf.extract_pdf_markdown is pe.extract_pdf_markdown


def test_the_tool_contract_names_are_classes_with_their_documented_members():
    assert isinstance(vaf.BaseTool.identity_kwargs, tuple)
    assert callable(vaf.BaseTool.log)
    assert callable(vaf.ToolCaller.execute)
    for method in ("deny", "ask", "allow"):
        assert callable(getattr(vaf.ToolRequest, method)), f"ToolRequest lost {method}()"
    assert callable(vaf.user_jail)
