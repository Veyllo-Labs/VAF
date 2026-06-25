# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
from .version import __version__

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
