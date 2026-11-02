# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
search_tools — provider-agnostic tool discovery (Tool Search 2.0 equivalent).

The model calls this when it does not know which tool to use for a task.
Returns a short list of matching tool names + one-line descriptions; the TOP
matches additionally carry a compact call signature (from tool.parameters) so
the model does not have to guess parameter names after discovery — that guessing
produced real schema-validation failures (tool-friction audit).

The agent router already limits which tools are in-context per turn.
search_tools lets the *model itself* discover tools on-demand, complementing
the router for edge cases the router did not anticipate.

Workflow:
  1. Model calls search_tools(query="calendar appointment")
  2. search_tools returns e.g.:
       create_calendar_event: Create a new calendar event or appointment.
           create_calendar_event(title: string, start: string, [duration: integer])
       list_calendar_events: List upcoming events from the calendar.
  3. Model calls the relevant tool in the NEXT turn.
     run() advises every name it lists (vaf.advise_tools), so the agent adds the
     ones this caller may run to the turn's tool set and their full schema is
     available in the next request. The names come from the code, never from
     parsing this text back.

Works with every backend (OpenAI, Anthropic, Google, local) — no special
API features required.
"""
from typing import Dict

from vaf.core.tool_dispatch import advise_tools
from vaf.tools.base import BaseTool, format_tool_signature


class SearchToolsTool(BaseTool):
    """Discover available tools by keyword query."""

    name = "search_tools"
    category    = "tool_catalog"
    permission_level = "read"
    side_effect_class = "none"
    description = (
        "Search the available tool catalogue by keyword or description of what you need. "
        "Returns tool names, one-line descriptions and, for the top matches, the call "
        "signature (parameter names and types). "
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
        agent = kwargs.get("_agent")
        catalog = agent.visible_tools() if hasattr(agent, "visible_tools") else self.available_tools
        if not catalog:
            return "No tools are currently registered."

        tokens = [t for t in query.split() if len(t) >= 2]

        matches = []
        for tool_name, tool in sorted(catalog.items()):
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
            # Fallback: return a capped list so the model can browse without
            # flooding context (50+ tools would be too much).
            _CAP = 20
            all_tools = sorted(catalog.items())
            lines = [f"No close matches for '{query}'. Showing first {min(_CAP, len(all_tools))} of {len(all_tools)} tools:"]
            for tool_name, tool in all_tools[:_CAP]:
                short = (getattr(tool, "description", "") or "").split("\n")[0][:100]
                lines.append(f"  {tool_name}: {short}")
            advise_tools(*(tool_name for tool_name, _ in all_tools[:_CAP]))
            if len(all_tools) > _CAP:
                lines.append(f"  … and {len(all_tools) - _CAP} more tools. Refine your query to narrow results.")
            return "\n".join(lines)

        # Sort by score descending, cap at 10 results
        matches.sort(key=lambda x: -x[0])
        # The raw model-supplied query is unbounded - cap the echo so the header
        # cannot eat the output budget (execute_tool truncates results at 2000).
        q_echo = query if len(query) <= 80 else query[:79] + "…"
        lines = [f"Tools matching '{q_echo}':"]
        _SIGNATURE_TOP_N = 3       # top hits get a call signature line
        _TOTAL_BUDGET = 1900       # self-cap under execute_tool's MAX_LEN=2000
        sig_line_idxs = []
        for i, (_, tool_name, tool) in enumerate(matches[:10]):
            desc_full = getattr(tool, "description", "") or ""
            short = desc_full.split("\n")[0][:120]
            if len(desc_full.split("\n")[0]) > 120:
                short += "..."
            lines.append(f"  {tool_name}: {short}")
            if i < _SIGNATURE_TOP_N:
                # The signature sits on its OWN indented line under its match line.
                sig = format_tool_signature(tool)
                if sig:
                    lines.append(f"      {sig}")
                    sig_line_idxs.append(len(lines) - 1)
        # Budget: drop signature lines (least critical, last first) until we fit.
        while sig_line_idxs and sum(len(l) + 1 for l in lines) > _TOTAL_BUDGET:
            lines.pop(sig_line_idxs.pop())
        # Every listed tool, handed over for the rest of the turn (those this caller may run).
        advise_tools(*(tool_name for _, tool_name, _ in matches[:10]))
        return "\n".join(lines)
