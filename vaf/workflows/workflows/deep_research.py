"""
Deep Research Workflow

Comprehensive 10-source research with multi-perspective analysis.
"""

from vaf.core.platform import Platform

WORKFLOW = {
    "name": "Deep Research",
    "description": "Comprehensive 10-source research with multi-perspective analysis",
    "triggers": [
        "tiefgehende recherche",
        "deep research",
        "umfassende analyse",
        "comprehensive analysis",
        "recherchiere ausführlich",
        "research thoroughly",
        "vollständige recherche",
        "complete research",
    ],
    "trigger_patterns": [
        r"tief.*recherch",
        r"deep.*research",
        r"umfass.*analy",
        r"ausführlich.*such",
        r"vollständ.*recherch",
    ],
    "variables": {
        "topic": "Topic to research",
        "output_file": "Output filename (optional, default: research_report.html)",
    },
    "defaults": {
        # Always save into the user's Documents folder (cross-platform).
        "output_file": str(Platform.documents_dir() / "research_report.html"),
    },
    "steps": [
        {
            "tool": "report_filename",
            "args": {"topic": "{topic}", "ext": "html", "max_words": 2, "suffix": "research"},
            "input": "{topic}",
            "output": "output_file",
            "description": "Choose a short topic-based filename in Documents",
        },
        {
            "tool": "research_agent",
            "args": {
                "topic": "{topic}",
                "format": "html",
                "max_results": 5,
                "deep": False,
                "min_chars_empty": 150,
                "min_chars_ok": 500
            },
            "input": "{topic}",
            "output": "report",
            "description": "Topic-by-topic research (bounded context) -> HTML report",
        },
        {
            "tool": "repair_report",
            "args": {
                "topic": "{topic}",
                "content": "{report}",
                "min_chars_empty": 150,
                "min_chars_ok": 500
            },
            "input": "{topic}",
            "output": "repaired_report",
            "description": "Repair empty/too-short sections (threshold-based)",
        },
        {
            "tool": "write_file",
            "args": {"path": "{output_file}", "content": "{repaired_report}"},
            "input": "{output_file}",
            "output": "saved",
            "description": "Save the research report (Documents)",
        },
    ],
}

