# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A complete third-party VAF tool, shipped as a pip package.

The class below is everything a tool needs: the declarative contract plus
run(). The pyproject.toml next to this package registers it under the
``vaf.tools`` entry-point group; VAF discovers it at agent startup. Full
contract: vaf/tools/base.py and docs/EMBEDDING.md.
"""
import random

from vaf.tools.base import BaseTool


class DiceRollTool(BaseTool):
    name = "dice_roll"
    description = "Roll one or more dice and return the results."

    # Declarative contract: read-only, no side effects, no confirmation gate.
    permission_level = "read"
    side_effect_class = "none"

    parameters = {
        "type": "object",
        "properties": {
            "sides": {"type": "integer", "description": "Sides per die (default 6)"},
            "count": {"type": "integer", "description": "Number of dice (default 1)"},
        },
        "required": [],
    }

    input_examples = [
        {"sides": 20},
        {"sides": 6, "count": 3},
    ]

    def run(self, **kwargs) -> str:
        sides = max(2, int(kwargs.get("sides", 6) or 6))
        count = min(100, max(1, int(kwargs.get("count", 1) or 1)))
        rolls = [random.randint(1, sides) for _ in range(count)]
        return f"Rolled {count}d{sides}: {rolls} (sum {sum(rolls)})"
