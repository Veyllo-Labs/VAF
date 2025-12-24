"""
Create Website Workflow

Generate a complete, responsive website with HTML, CSS, and JavaScript.
"""

WORKFLOW = {
    "name": "Create Website",
    "description": "Generate a complete, responsive website with HTML, CSS, and JavaScript",
    "triggers": [
        # German - verschiedene Varianten
        "erstelle website", "erstelle webseite", 
        "erstelle eine website", "erstelle eine webseite",
        "website erstellen", "webseite erstellen", 
        "erstelle homepage", "homepage erstellen",
        "website für", "webseite für",
        "baue eine website", "baue website",
        "mache eine website", "mach mir eine website",
        "generiere website", "generiere webseite",
        # English
        "create website", "create a website", "build website",
        "make website", "make a website", "generate website",
        "website for", "build a website for",
        "create homepage", "build homepage",
        "create landing page", "landing page for",
        # Simpler triggers (höhere Match-Chance)
        "website", "webseite", "homepage",
    ],
    "trigger_patterns": [
        r"erstell.*websit",
        r"erstell.*webseite",
        r"erstell.*homepage",
        r"websit.*erstell",
        r"websit.*für",
        r"webseite.*für",
        r"homepage.*erstell",
        r"bau.*websit",
        r"mach.*websit",
        r"create.*websit",
        r"build.*websit",
        r"make.*websit",
        r"landing.*page",
        r"website.*handwerker",
        r"handwerker.*website",
    ],
    "variables": {
        "description": "Description of the website to create",
    },
    "steps": [
        {
            "tool": "coding_agent",
            "input": "Create a complete, responsive website: {description}",
            "output": "result",
            "description": "Create complete website with HTML, CSS, JS",
        },
    ],
}

