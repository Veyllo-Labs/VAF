"""
search_tools — provider-agnostic tool discovery (Tool Search 2.0 equivalent).

The model calls this when it does not know which tool to use for a task.
Returns a short list of matching tool names + one-line descriptions so the
model can decide which tool(s) to call next — without loading all full
schemas into context upfront.

The agent router already limits which tools are in-context per turn.
search_tools lets the *model itself* discover tools on-demand, complementing
the router for edge cases the router did not anticipate.

Workflow:
  1. Model calls search_tools(query="calendar appointment")
  2. search_tools returns e.g.:
       create_calendar_event: Create a new calendar event or appointment.
       list_calendar_events: List upcoming events from the calendar.
       update_calendar_event: Modify an existing calendar event.
  3. Model calls the relevant tool in the NEXT turn.
     The agent auto-adds requested tool names to _active_tools so their full
     schema is available in the next request.

Works with every backend (OpenAI, Anthropic, Google, local) — no special
API features required.
"""
from typing import Dict

from vaf.tools.base import BaseTool


class SearchToolsTool(BaseTool):
    """Discover available tools by keyword query."""

    name = "search_tools"
    description = (
        "Search the available tool catalogue by keyword or description of what you need. "
        "Returns tool names and one-line descriptions. "
        "Use this when you are unsure which tool to call, or when you need a tool "
        "that is not currently in context. "
        "After finding the right tool, call it directly in your next response."
    )
    input_examples = [
        {"query": "calendar appointment"},
        {"query": "send message whatsapp"},
        {"query": "read file"},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Keyword or short phrase describing what you need "
                    "(e.g. 'send email', 'calendar event', 'github pull request', 'read file')."
                ),
            },
        },
        "required": ["query"],
    }

    # Injected by agent after tool loading (same pattern as list_tools)
    available_tools: Dict = {}

    def run(self, **kwargs) -> str:  # noqa: D401
        query = (kwargs.get("query") or "").strip().lower()
        if not query:
            return "Provide a query, e.g. search_tools(query='calendar event')."
        if not self.available_tools:
            return "No tools are currently registered."

        tokens = [t for t in query.split() if len(t) >= 2]

        matches = []
        for tool_name, tool in sorted(self.available_tools.items()):
            desc = (getattr(tool, "description", "") or "").lower()
            name_lower = tool_name.lower()
            # Score: +2 per token hit in name, +1 per hit in description
            score = 0
            for tok in tokens:
                if tok in name_lower:
                    score += 2
                if tok in desc:
                    score += 1
            if score > 0:
                matches.append((score, tool_name, tool))

        if not matches:
            # Fallback: return first line of all tools so the model can browse
            lines = ["No close matches found. All available tools:"]
            for tool_name, tool in sorted(self.available_tools.items()):
                short = (getattr(tool, "description", "") or "").split("\n")[0][:100]
                lines.append(f"  {tool_name}: {short}")
            return "\n".join(lines)

        # Sort by score descending, cap at 10 results
        matches.sort(key=lambda x: -x[0])
        lines = [f"Tools matching '{query}':"]
        for _, tool_name, tool in matches[:10]:
            desc_full = getattr(tool, "description", "") or ""
            short = desc_full.split("\n")[0][:120]
            if len(desc_full.split("\n")[0]) > 120:
                short += "..."
            lines.append(f"  {tool_name}: {short}")
        return "\n".join(lines)
