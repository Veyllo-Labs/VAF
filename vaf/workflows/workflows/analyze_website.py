"""
Analyze Website Workflow

Fetch a website and analyze/summarize its content.
"""

WORKFLOW = {
    "name": "Analyze Website",
    "description": "Fetch a website and analyze/summarize its content",
    "triggers": [
        "analysiere website",
        "analyze website",
        "lies diese url",
        "read this url",
        "fasse diese seite zusammen",
        "summarize this page",
        "was steht auf",
    ],
    "trigger_patterns": [
        r"analys.*url",
        r"analys.*website",
        r"summar.*url",
        r"zusammen.*url",
        r"lies.*http",
        r"read.*http",
    ],
    "variables": {
        "url": "Website URL to analyze",
    },
    "steps": [
        {
            "tool": "webfetch",
            "input": "{url}",
            "output": "content",
            "description": "Fetch the website content",
        },
        {
            "tool": "librarian_agent",
            "input": "Analyze and summarize this website content:\n\n{content}",
            "output": "analysis",
            "description": "Analyze and summarize the content",
        },
    ],
}

