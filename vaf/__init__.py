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
    from .framework import Agent, CoreAgent

__all__ = ["__version__", "Agent", "CoreAgent"]


def __getattr__(name):
    # Lazy public API (PEP 562). Keeps `import vaf` cheap: the ~9k-line core
    # engine and its dependency chain (incl. the latent Agent<->thinking_mode
    # cycle, which resolves fine at call time) are only loaded on first access
    # to `vaf.Agent` / `vaf.CoreAgent`.
    if name in ("Agent", "CoreAgent"):
        from .framework import Agent, CoreAgent
        return {"Agent": Agent, "CoreAgent": CoreAgent}[name]
    raise AttributeError(f"module 'vaf' has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
