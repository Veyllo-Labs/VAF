"""
Web Lookup Workflow

Quick web search for information queries.
"""

WORKFLOW = {
    "name": "Web Lookup",
    "description": "Quick web search for information",
    "triggers": [
        "suche nach",
        "search for",
        "was ist",
        "what is",
        "wie funktioniert",
        "how does",
        "finde heraus",
        "find out",
    ],
    "trigger_patterns": [
        r"^such.*nach",
        r"^was ist",
        r"^what is",
        r"^wie ",
        r"^how ",
    ],
    "variables": {
        "query": "Search query",
    },
    "steps": [
        {
            "tool": "web_search",
            "input": "{query}",
            "output": "results",
            "description": "Search the web",
        },
    ],
}

