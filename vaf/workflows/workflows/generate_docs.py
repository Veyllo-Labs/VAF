"""
Generate Documentation Workflow

Analyze a codebase and generate documentation.
"""

WORKFLOW = {
    "name": "Generate Documentation",
    "description": "Analyze a codebase and generate documentation",
    "triggers": [
        "erstelle dokumentation",
        "generate documentation",
        "dokumentiere dieses projekt",
        "document this project",
        "erstelle readme",
        "create readme",
    ],
    "trigger_patterns": [
        r"erstell.*doku",
        r"generat.*doc",
        r"dokumentier.*projekt",
        r"create.*readme",
    ],
    "variables": {
        "path": "Path to the project/file to document",
    },
    "defaults": {
        "output": "DOCUMENTATION.md",
    },
    "steps": [
        {
            "tool": "list_files",
            "input": "{path}",
            "output": "file_list",
            "description": "List project files",
        },
        {
            "tool": "librarian_agent",
            "input": "Analyze this project structure and create comprehensive documentation:\n\n{file_list}\n\nInclude: Overview, file descriptions, usage instructions.",
            "output": "documentation",
            "description": "Generate documentation",
        },
        {
            "tool": "write_file",
            "input": '{"path": "DOCUMENTATION.md", "content": "{documentation}"}',
            "output": "saved",
            "description": "Save documentation",
        },
    ],
}

