"""
Research & Document Workflow

Multi-stage workflow that combines research and document creation:
1. Research Agent: Gathers comprehensive information
2. Document Agent: Creates professional document based on research

Perfect for:
- Legal documents (research laws, create contract)
- Technical manuals (research best practices, create guide)
- Business proposals (research market, create proposal)
- Policy documents (research regulations, create policy)
"""

from vaf.core.platform import Platform

WORKFLOW = {
    "name": "Research & Document",
    "description": "Research topic thoroughly, then create professional document. This workflow must be used for all legal texts.",
    "triggers": [
        "recherchiere und erstelle dokument",
        "research and create document",
        "recherchiere und erstelle",
        "research and create",
        "erstelle dokument basierend auf recherche",
        "create document based on research",
        "erstelle basierend auf recherche",
        "create based on research",
        "dokument mit recherche",
        "document with research",
        "recherche und dokument",
        "research and document",
    ],
    "trigger_patterns": [
        r"recherch.*und.*erstell",
        r"research.*and.*creat",
        r"recherch.*dann.*erstell",
        r"research.*then.*creat",
        r"basierend.*auf.*recherch.*(dokument|guide|bericht|anleitung)",
        r"based.*on.*research.*(document|guide|report|manual)",
        r"dokument.*mit.*recherch",
        r"document.*with.*research",
        r"recherch.*und.*schreib",
        r"research.*and.*writ",
    ],
    "variables": {
        "topic": "Topic to research and document about",
        "document_type": "Type of document to create (contract/manual/guide/proposal/policy)",
        "requirements": "Specific requirements for the document (optional)",
    },
    "defaults": {
        "document_type": "document",
        "requirements": "Create comprehensive, professional document with proper structure",
    },
    "steps": [
        # ═══════════════════════════════════════════════════════════
        # STEP 1: RESEARCH PHASE
        # Gathers comprehensive information from multiple sources
        # ═══════════════════════════════════════════════════════════
        {
            "tool": "research_agent",
            "args": {
                "topic": "{topic}",
                "format": "markdown",  # Markdown for document agent
                "max_results": 5,
            },
            "input": (
                "Research topic: {topic}\n\n"
                "Focus on information needed for creating a {document_type}.\n\n"
                "Important aspects to research:\n"
                "- Legal requirements and regulations\n"
                "- Industry standards and best practices\n"
                "- Current laws and compliance requirements\n"
                "- Expert recommendations\n"
                "- Common pitfalls to avoid\n"
                "- Real-world examples and templates\n\n"
                "Provide comprehensive, accurate information that will form the basis of a professional document."
            ),
            "output": "research_content",
            "description": "Research topic comprehensively (laws, standards, best practices)",
        },
        
        # ═══════════════════════════════════════════════════════════
        # STEP 2: DOCUMENT CREATION PHASE
        # Creates professional document based on research findings
        # ═══════════════════════════════════════════════════════════
        {
            "tool": "document_agent",
            "input": (
                "Create a professional {document_type} about: {topic}\n\n"
                "Requirements: {requirements}\n\n"
                "═══════════════════════════════════════════════════════════\n"
                "RESEARCH FINDINGS (USE AS BASIS FOR DOCUMENT):\n"
                "═══════════════════════════════════════════════════════════\n"
                "{research_content}\n"
                "═══════════════════════════════════════════════════════════\n\n"
                "IMPORTANT INSTRUCTIONS:\n"
                "✓ Base all content on the research findings above\n"
                "✓ Include relevant regulations, laws, and standards found in research\n"
                "✓ Reference specific sources where appropriate (e.g., 'According to BGB §622...')\n"
                "✓ Follow best practices and recommendations from research\n"
                "✓ Ensure legal compliance based on research findings\n"
                "✓ Create comprehensive, professional sections\n"
                "✓ Use proper legal/technical terminology found in research\n"
                "✓ Make document actionable and complete\n\n"
                "Create a document that is:\n"
                "- Legally sound (if applicable)\n"
                "- Comprehensive and complete\n"
                "- Professional in tone and structure\n"
                "- Based on current, accurate information\n"
                "- Ready for immediate use\n"
            ),
            "output": "document_result",
            "description": "Create professional document based on research",
        },
    ],
}
