# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
from typing import Dict

from vaf.tools.base import BaseTool


class ListToolsTool(BaseTool):
    name = "list_tools"
    category    = "tool_catalog"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "List all tools available to the model. "
        "Use this when you are not sure which tool can handle the task, "
        "or when no suitable tool exists and you need to see all tools."
    )

    parameters = {
        "type": "object",
        "properties": {},
        "required": []
    }

    # Reference to available tools (set by agent after loading)
    available_tools: Dict = {}

    def run(self, **kwargs) -> str:
        if not self.available_tools:
            return "No tools are currently registered."

        from vaf.core.tool_contract import TOOL_CATEGORIES, category_label, tool_category

        # Grouped by bundle so the model sees "these four are the Telegram
        # tools" instead of an alphabetical wall. The per-tool line keeps its
        # exact "- name: description" shape: a post-hook parses that shape out
        # of search_tools output, and a heading is written WITHOUT a colon so
        # it can never be mistaken for a tool row if that hook is ever widened.
        order = {key: index for index, key in enumerate(TOOL_CATEGORIES)}
        grouped: Dict[str, list] = {}
        for name, tool in self.available_tools.items():
            grouped.setdefault(tool_category(name, tool), []).append((name, tool))

        lines = ["Available tools:"]
        for category in sorted(grouped, key=lambda c: (order.get(c, len(order)), c)):
            lines.append("")
            lines.append(f"## {category_label(category)}")
            for name, tool in sorted(grouped[category], key=lambda item: item[0]):
                description = getattr(tool, "description", "") or ""
                if len(description) > 120:
                    description = description[:117] + "..."
                lines.append(f"- {name}: {description}")

        return "\n".join(lines)
