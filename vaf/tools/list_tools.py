from typing import Dict

from vaf.tools.base import BaseTool


class ListToolsTool(BaseTool):
    name = "list_tools"
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

        lines = ["Available tools:"]
        for name, tool in sorted(self.available_tools.items(), key=lambda item: item[0]):
            description = getattr(tool, "description", "") or ""
            if len(description) > 120:
                description = description[:117] + "..."
            lines.append(f"- {name}: {description}")

        return "\n".join(lines)
