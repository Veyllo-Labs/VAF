"""
Deep Research Workflow

Comprehensive 10-source research with multi-perspective analysis.
"""

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
        "output_file": "Output filename (optional, default: research_report.md)",
    },
    "defaults": {
        "output_file": "research_report.md",
    },
    "steps": [
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
}

