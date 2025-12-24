"""
Research & Code Workflow

Search the web for information, then generate code based on findings.
"""

WORKFLOW = {
    "name": "Research & Code",
    "description": "Search the web for information, then generate code based on findings",
    "triggers": [
        "recherchiere und erstelle code",
        "research and create code",
        "suche nach und implementiere",
        "find out how to and code it",
        "look up and implement",
        "basierend auf recherche",
    ],
    "trigger_patterns": [
        r"recherchier.*code",
        r"such.*implementier",
        r"research.*implement",
        r"find.*create.*code",
    ],
    "variables": {
        "query": "What to research",
        "filename": "Output filename (optional, default: output.py)",
    },
    "defaults": {
        "filename": "output.py",
    },
    "steps": [
        {
            "tool": "web_search",
            "input": "{query}",
            "output": "research",
            "description": "Search the web for relevant information",
        },
        {
            "tool": "coding_agent",
            "input": "Based on this research, create code:\n\n{research}\n\nTask: {query}",
            "output": "code",
            "description": "Generate code based on research findings",
        },
        {
            "tool": "write_file",
            "input": '{"path": "{filename}", "content": "{code}"}',
            "output": "saved",
            "description": "Save the generated code to file",
        },
    ],
}

