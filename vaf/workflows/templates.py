"""
VAF Workflow Templates - Pre-defined pipelines for common tasks

These templates are matched by the WorkflowSelector based on user input.
Each template defines:
- triggers: Keywords/phrases that activate this workflow
- description: What the workflow does
- variables: Required user inputs
- steps: Sequence of tool calls with output chaining
"""

from typing import Dict, Any, List


# ═══════════════════════════════════════════════════════════════════════════════
# WORKFLOW TEMPLATES
# ═══════════════════════════════════════════════════════════════════════════════

WORKFLOW_TEMPLATES: Dict[str, Dict[str, Any]] = {
    
    # ───────────────────────────────────────────────────────────────────────────
    # CREATE WEBSITE (HIGH PRIORITY - matches "erstelle website" etc.)
    # ───────────────────────────────────────────────────────────────────────────
    
    "create_website": {
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
    },
    
    # ───────────────────────────────────────────────────────────────────────────
    # RESEARCH & CODE
    # ───────────────────────────────────────────────────────────────────────────
    
    "research_and_code": {
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
    },
    
    # ───────────────────────────────────────────────────────────────────────────
    # ANALYZE WEBSITE
    # ───────────────────────────────────────────────────────────────────────────
    
    "analyze_website": {
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
    },
    
    # ───────────────────────────────────────────────────────────────────────────
    # CODE REVIEW & IMPROVE
    # ───────────────────────────────────────────────────────────────────────────
    
    "code_review": {
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
    },
    
    # ───────────────────────────────────────────────────────────────────────────
    # MULTI-SOURCE RESEARCH
    # ───────────────────────────────────────────────────────────────────────────
    
    "deep_research": {
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
            "output_file": "Output filename (optional, default: research_report.md)",
        },
        "defaults": {
            "output_file": "research_report.md",
        },
        "steps": [
            # ─── 10 DIFFERENT SEARCH PERSPECTIVES ───
            {
                "tool": "web_search",
                "input": "{topic}",
                "output": "general",
                "description": "1/10: General overview",
            },
            {
                "tool": "web_search",
                "input": "{topic} definition explanation",
                "output": "definition",
                "description": "2/10: Definitions & explanations",
            },
            {
                "tool": "web_search",
                "input": "{topic} tutorial guide beginner",
                "output": "tutorials",
                "description": "3/10: Tutorials & guides",
            },
            {
                "tool": "web_search",
                "input": "{topic} best practices recommendations",
                "output": "best_practices",
                "description": "4/10: Best practices",
            },
            {
                "tool": "web_search",
                "input": "{topic} examples use cases",
                "output": "examples",
                "description": "5/10: Examples & use cases",
            },
            {
                "tool": "web_search",
                "input": "{topic} pros cons advantages disadvantages",
                "output": "pros_cons",
                "description": "6/10: Pros & cons analysis",
            },
            {
                "tool": "web_search",
                "input": "{topic} alternatives comparison",
                "output": "alternatives",
                "description": "7/10: Alternatives & comparisons",
            },
            {
                "tool": "web_search",
                "input": "{topic} common mistakes errors avoid",
                "output": "pitfalls",
                "description": "8/10: Common pitfalls",
            },
            {
                "tool": "web_search",
                "input": "{topic} advanced tips tricks",
                "output": "advanced",
                "description": "9/10: Advanced tips",
            },
            {
                "tool": "web_search",
                "input": "{topic} 2024 2025 latest news updates",
                "output": "latest",
                "description": "10/10: Latest updates",
            },
            # ─── COMPILE REPORT ───
            {
                "tool": "librarian_agent",
                "input": """Compile a comprehensive research report on '{topic}' using these 10 sources.
Create a well-structured markdown report with sections for each aspect.

## 1. Overview
{general}

## 2. Definition & Core Concepts
{definition}

## 3. Tutorials & Getting Started
{tutorials}

## 4. Best Practices
{best_practices}

## 5. Real-World Examples
{examples}

## 6. Pros & Cons
{pros_cons}

## 7. Alternatives & Comparisons
{alternatives}

## 8. Common Pitfalls to Avoid
{pitfalls}

## 9. Advanced Techniques
{advanced}

## 10. Latest Developments
{latest}

Create a professional, comprehensive markdown report with:
- Executive summary at the top
- Clear sections with headers
- Key takeaways highlighted
- Practical recommendations""",
                "output": "report",
                "description": "Compile all research into report",
            },
            {
                "tool": "write_file",
                "input": '{"path": "{output_file}", "content": "{report}"}',
                "output": "saved",
                "description": "Save the research report",
            },
        ],
    },
    
    # ───────────────────────────────────────────────────────────────────────────
    # PROJECT DOCUMENTATION
    # ───────────────────────────────────────────────────────────────────────────
    
    "generate_docs": {
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
    },
    
    # ───────────────────────────────────────────────────────────────────────────
    # QUICK TASKS (Single-step workflows for consistency)
    # ───────────────────────────────────────────────────────────────────────────
    
    "web_lookup": {
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
    },
    
    "create_file": {
        "name": "Create File",
        "description": "Generate and create a new file",
        "triggers": [
            "erstelle datei",
            "create file",
            "schreibe eine datei",
            "write a file",
            "neue datei",
            "new file",
        ],
        "trigger_patterns": [
            r"erstell.*datei",
            r"create.*file",
            r"schreib.*datei",
            r"neue.*datei",
        ],
        "variables": {
            "description": "What the file should contain",
            "filename": "Name of the file to create",
        },
        "steps": [
            {
                "tool": "coding_agent",
                "input": "Create: {description}\nFilename: {filename}",
                "output": "content",
                "description": "Generate file content",
            },
            {
                "tool": "write_file",
                "input": '{"path": "{filename}", "content": "{content}"}',
                "output": "saved",
                "description": "Save the file",
            },
        ],
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_template(name: str) -> Dict[str, Any]:
    """Get a workflow template by name."""
    return WORKFLOW_TEMPLATES.get(name)


def list_templates() -> List[Dict[str, str]]:
    """List all available templates with name and description."""
    return [
        {
            "id": key,
            "name": template["name"],
            "description": template["description"],
            "steps": len(template["steps"]),
        }
        for key, template in WORKFLOW_TEMPLATES.items()
    ]


def get_template_names() -> List[str]:
    """Get list of template names."""
    return list(WORKFLOW_TEMPLATES.keys())

