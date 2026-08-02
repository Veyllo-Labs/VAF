# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
from typing import TYPE_CHECKING

from .version import __version__

if TYPE_CHECKING:
    # Static type-checkers only (mypy / Pyright / VS Code): resolve the lazy public
    # API to the real classes so `from vaf import Agent` autocompletes and type-checks.
    # No runtime import here — `import vaf` stays cheap (the real loading is in
    # __getattr__ below). Paired with the vaf/py.typed marker (PEP 561).
    from .core.tool_dispatch import ToolCaller, ToolRequest, set_account_allowlist_resolver
    from .framework import Agent, CoreAgent
    from .tools.base import BaseTool
    from .tools.filesystem import user_jail

__all__ = ["__version__", "Agent", "BaseTool", "CoreAgent", "ToolCaller", "ToolRequest",
           "markers", "set_account_allowlist_resolver", "user_jail"]


def __getattr__(name):
    # Lazy public API (PEP 562). Keeps `import vaf` cheap: the ~9k-line core
    # engine and its dependency chain (incl. the latent Agent<->thinking_mode
    # cycle, which resolves fine at call time) are only loaded on first access
    # to `vaf.Agent` / `vaf.CoreAgent`.
    if name in ("Agent", "CoreAgent"):
        from .framework import Agent, CoreAgent
        return {"Agent": Agent, "CoreAgent": CoreAgent}[name]
    if name == "BaseTool":
        # What you subclass to add a tool, and where you declare identity_kwargs so the
        # dispatcher hands your tool the caller's identity. Pure stdlib underneath, so it
        # costs nothing on the slim base.
        from .tools.base import BaseTool
        return BaseTool
    if name == "user_jail":
        # Confine one tool run to the caller's own files. Declaring identity_kwargs tells
        # the dispatcher WHO is calling; this turns that answer into an actual boundary.
        # Enter it INSIDE your run(): a tool is also called directly, without any
        # dispatcher to have set it. See docs/EMBEDDING.md.
        from .tools.filesystem import user_jail
        return user_jail
    if name == "ToolRequest":
        # What an authorizer is handed: the caller's identity, which is trustworthy, and the
        # model's arguments, which are not. Exported so an application can type-annotate its
        # authorizer and, in tests, build one without an agent.
        from .core.tool_dispatch import ToolRequest
        return ToolRequest
    if name == "ToolCaller":
        # Run a tool the way the agent runs one - policy, confirmation gate, declared
        # identity, bounded execution, events - without an Agent, a session or a chat turn.
        # This is the same object the agent's own dispatch uses; there is not a second
        # implementation for embedders. Stdlib-only underneath, so the slim base is
        # unaffected. See docs/EMBEDDING.md.
        from .core.tool_dispatch import ToolCaller, ToolRequest
        return ToolCaller
    if name == "set_account_allowlist_resolver":
        # Which tools each ACCOUNT may use, answered by YOUR backend. One resolver per
        # process, consulted in the funnel after the hard policy block and BEFORE the
        # authorizer, so an account-level ban cannot be lifted by an allow(). The answer
        # also crosses into the coder child as data (VAF_ALLOWED_TOOLS). Stdlib-only
        # underneath, so the slim base is unaffected. See docs/EMBEDDING.md.
        from .core.tool_dispatch import set_account_allowlist_resolver
        return set_account_allowlist_resolver
    if name == "markers":
        # importlib, not `from . import`: the latter re-enters this
        # __getattr__ while the submodule is being set and recurses.
        import importlib

        return importlib.import_module(".markers", __name__)
    raise AttributeError(f"module 'vaf' has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
