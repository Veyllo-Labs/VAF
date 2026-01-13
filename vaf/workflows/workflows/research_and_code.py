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
        "recherchiere und implementiere",
        "research and implement",
        "suche nach und implementiere",
        "find out how to and code it",
        "look up and implement",
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
            "args": {
                "query": "{query}",
                "max_results": 5,
                "deep": True
            },
            "input": "{query}",
            "output": "research",
            "description": "Search the web for relevant information",
        },
        {
            "tool": "coding_agent",
            "input": (
                "CONTENT_ONLY: Generate ONLY the complete code content based on the research below.\n"
                "- Return ONLY the code (no Markdown fences, no explanation, no project structure)\n"
                "- Prefer clean structure and comments where needed\n"
                "- Create a complete, working code file\n\n"
                "Task: {query}\n\n"
                "Research:\n{research}\n"
            ),
            "output": "code",
            "description": "Generate code based on research findings",
        },
        {
            "tool": "write_file",
            "args": {
                "path": "{filename}",
                "content": "{code}",
            },
            "input": "{filename}",
            "output": "saved",
            "description": "Save the generated code to file",
        },
        {
            "tool": "librarian_agent",
            "input": (
                "Write a short completion message.\n"
                "Include: where the file was saved, how to run it, and any dependencies.\n\n"
                "Filename: {filename}\n"
                "Save result: {saved}\n"
            ),
            "output": "final",
            "description": "Return a helpful completion message",
        },
    ],
}

