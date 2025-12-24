"""
Code Review Workflow

Read a file, review it, and save improvements.
"""

WORKFLOW = {
    "name": "Code Review",
    "description": "Read a file, review it, and save improvements",
    "triggers": [
        "review diesen code",
        "review this code",
        "verbessere diese datei",
        "improve this file",
        "optimiere den code",
        "optimize the code",
        "prüfe und verbessere",
    ],
    "trigger_patterns": [
        r"review.*code",
        r"review.*datei",
        r"verbess.*code",
        r"improv.*file",
        r"optimi.*code",
    ],
    "variables": {
        "path": "Path to the file to review",
    },
    "steps": [
        {
            "tool": "read_file",
            "input": "{path}",
            "output": "original_code",
            "description": "Read the original file",
        },
        {
            "tool": "coding_agent",
            "input": "Review and improve this code. Fix bugs, improve readability, add comments:\n\n{original_code}",
            "output": "improved_code",
            "description": "Review and improve the code",
        },
        {
            "tool": "write_file",
            "input": '{"path": "{path}", "content": "{improved_code}"}',
            "output": "saved",
            "description": "Save the improved code",
        },
    ],
}

