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
        "wie viele folgen",
        "wie viele episoden",
        "how many episodes",
        "how many episodes are there",
    ],
    "trigger_patterns": [
        r"^such.*nach",
        r"^was ist",
        r"^what is",
        r"^wie ",
        r"^how ",
        r"wie viele (folgen|episoden)",
        r"how many (episodes|eps|episodes are)",
    ],
    "variables": {
        "query": "Search query",
    },
    "steps": [
        {
            "tool": "web_search",
            "args": {
                "query": "{query}",
                "max_results": 5,
                "deep": False
            },
            "input": "{query}",
            "output": "results",
            "description": "Search the web",
        },
        {
            "tool": "librarian_agent",
            "input": (
                "Answer the user's question clearly and directly.\n"
                "Use the web search results below as evidence and include 2-3 source links.\n"
                "If the answer is uncertain or varies by region/season, say so briefly.\n\n"
                "User question: {query}\n\n"
                "Web search results:\n{results}\n"
            ),
            "output": "answer",
            "description": "Synthesize a helpful answer",
        },
    ],
}

