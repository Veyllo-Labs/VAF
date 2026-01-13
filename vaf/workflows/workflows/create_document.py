"""
Create Document Workflow

Generate structured documents (contracts, reports, letters, templates) WITHOUT research.
For documents requiring research, use research_and_document or legal_contract_research workflows.
"""

WORKFLOW = {
    "name": "Create Document",
    "description": "Generate structured documents WITHOUT research (for research-based docs, use research workflows)",
    "triggers": [
        "erstelle einfaches dokument",
        "create simple document",
        "erstelle schnell dokument",
        "create quick document",
        "schreibe brief",
        "write letter",
        "erstelle vorlage",
        "create template",
        "erstelle nachricht",
        "create message",
    ],
    "trigger_patterns": [
        r"schreib.*brief",
        r"write.*letter",
        r"erstell.*vorlage",
        r"create.*template",
        r"erstell.*nachricht",
        r"create.*message",
        r"erstell.*einfach",
        r"create.*simple",
        # NEGATIVE patterns: Don't match if research is mentioned
        # (handled by checking if input contains research keywords)
    ],
    "variables": {
        "document_type": "Type of document (contract, letter, report, etc.)",
        "requirements": "Specific requirements for the document",
    },
    "steps": [
        {
            "tool": "document_agent",
            "input": (
                "Create a {document_type} with the following requirements:\n\n"
                "{requirements}\n\n"
                "Generate a professional, complete document with all necessary sections."
            ),
            "output": "result",
            "description": "Generate document with all sections",
        },
    ],
}
