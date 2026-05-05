"""
Technical Documentation with Research Workflow

Specialized workflow for creating technical documentation:
1. Research: Best practices, tutorials, standards
2. Document: Professional technical documentation

Optimized for:
- Software manuals
- API documentation
- System guides
- Installation instructions
- Troubleshooting guides
"""

WORKFLOW = {
    "name": "Technical Documentation Research",
    "description": "Research best practices, then create professional technical documentation",
    "triggers": [
        "technische dokumentation mit recherche",
        "technical documentation with research",
        "anleitung mit recherche",
        "guide with research",
        "handbuch mit recherche",
        "manual with research",
        "dokumentation recherchieren",
        "research documentation",
    ],
    "trigger_patterns": [
        r"technisch.*dokumentation.*recherch",
        r"technical.*documentation.*research",
        r"anleitung.*recherch",
        r"guide.*research",
        r"handbuch.*recherch",
        r"manual.*research",
    ],
    "variables": {
        "technology": "Technology or system to document",
        "audience": "Target audience (beginner/intermediate/expert)",
        "scope": "Scope of documentation (installation/usage/api/troubleshooting)",
    },
    "defaults": {
        "audience": "intermediate users",
        "scope": "comprehensive guide covering installation, usage, and troubleshooting",
    },
    "steps": [
        {
            "tool": "research_agent",
            "args": {
                "topic": "{technology} best practices and documentation standards",
                "format": "markdown",
                "max_results": 5,
            },
            "input": (
                "Research: {technology}\n\n"
                "Focus areas:\n"
                "- Official documentation and resources\n"
                "- Industry best practices\n"
                "- Common use cases and examples\n"
                "- Installation and setup procedures\n"
                "- Configuration options and recommendations\n"
                "- Troubleshooting common issues\n"
                "- Security considerations\n"
                "- Performance optimization tips\n"
                "- Latest version features and changes\n"
                "- Community recommendations\n\n"
                "Gather comprehensive technical information for creating professional documentation."
            ),
            "output": "tech_research",
            "description": "Research technology and best practices",
        },
        {
            "tool": "document_agent",
            "input": (
                "Create professional technical documentation for: {technology}\n\n"
                "Target Audience: {audience}\n"
                "Scope: {scope}\n\n"
                "═══════════════════════════════════════════════════════════\n"
                "TECHNICAL RESEARCH (USE AS FOUNDATION):\n"
                "═══════════════════════════════════════════════════════════\n"
                "{tech_research}\n"
                "═══════════════════════════════════════════════════════════\n\n"
                "DOCUMENTATION REQUIREMENTS:\n"
                "✓ Base all instructions on research findings\n"
                "✓ Include accurate code examples and commands\n"
                "✓ Follow industry best practices found in research\n"
                "✓ Provide step-by-step instructions\n"
                "✓ Include troubleshooting for common issues\n"
                "✓ Add security recommendations\n"
                "✓ Use proper technical terminology\n"
                "✓ Include version information where relevant\n"
                "✓ Add prerequisites and requirements\n"
                "✓ Provide clear examples for each major feature\n\n"
                "Create documentation that is:\n"
                "- Technically accurate and up-to-date\n"
                "- Clear and easy to follow\n"
                "- Comprehensive yet concise\n"
                "- Properly structured with good hierarchy\n"
                "- Includes practical examples\n"
                "- Suitable for {audience}\n"
            ),
            "output": "documentation_result",
            "description": "Create professional technical documentation",
        },
    ],
}
