# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Legal Contract with Research Workflow

Specialized workflow for creating legally sound contracts:
1. Research: Legal requirements, laws, regulations
2. Document: Professional contract with legal compliance

Optimized for:
- Employment contracts (Arbeitsverträge)
- Rental agreements (Mietverträge)
- Service contracts (Dienstleistungsverträge)
- Sales agreements (Kaufverträge)
- NDA agreements (Geheimhaltungsvereinbarungen)
"""

WORKFLOW = {
    "name": "Legal Contract Research",
    "description": "Research legal requirements, then create legally sound contract",
    "triggers": [
        "erstelle rechtssicheren vertrag",
        "create legally sound contract",
        "rechtssicherer arbeitsvertrag",
        "legally sound employment contract",
        "rechtssicherer mietvertrag",
        "legally sound rental agreement",
        "vertrag mit recherche erstellen",
        "create contract with research",
        "arbeitsvertrag mit recherche",
        "employment contract with research",
        "mietvertrag mit recherche",
        "rental agreement with research",
        "mietvertrag recherchieren und erstellen",
        "research and create rental agreement",
        "arbeitsvertrag recherchieren und erstellen",
        "research and create employment contract",
    ],
    "trigger_patterns": [
        r"rechtssicher.*\w+vertrag",
        r"legally.*sound.*\w+contract",
        r"\w+vertrag.*mit.*recherch",
        r"\w+contract.*with.*research",
        r"erstell.*rechtssicher",
        r"create.*legally.*sound",
    ],
    "variables": {
        "contract_type": "Type of contract (employment/rental/service/sales/nda)",
        "specifics": "Specific details for the contract (parties, terms, conditions)",
    },
    "defaults": {
        "specifics": "Standard contract terms and conditions",
    },
    "steps": [
        {
            "tool": "research_agent",
            "args": {
                "topic": "Legal requirements for {contract_type} contract",
                "format": "markdown",
                "max_results": 5,
            },
            "input": (
                "Research legal requirements for: {contract_type} contract\n\n"
                "Specific research focus:\n"
                "- Applicable laws and regulations (BGB, NachwG, etc.)\n"
                "- Mandatory clauses and required information\n"
                "- Legal pitfalls and common mistakes\n"
                "- Current legal requirements (2026)\n"
                "- Enforceable vs. unenforceable clauses\n"
                "- Employee/tenant/party rights and protections\n"
                "- Notice periods and termination rules\n"
                "- Data protection requirements (DSGVO)\n\n"
                "Provide comprehensive legal information for creating a legally compliant contract."
            ),
            "output": "legal_research",
            "description": "Research legal requirements for contract",
        },
        {
            "tool": "document_agent",
            "input": (
                "Create a legally sound {contract_type} contract.\n\n"
                "Contract Details: {specifics}\n\n"
                "═══════════════════════════════════════════════════════════\n"
                "LEGAL RESEARCH (MUST BE INCORPORATED):\n"
                "═══════════════════════════════════════════════════════════\n"
                "{legal_research}\n"
                "═══════════════════════════════════════════════════════════\n\n"
                "CRITICAL REQUIREMENTS:\n"
                "✓ Include ALL mandatory clauses found in research\n"
                "✓ Reference specific laws (e.g., '§ 622 BGB', 'NachwG § 2')\n"
                "✓ Ensure compliance with current regulations\n"
                "✓ Use legally correct terminology\n"
                "✓ Include all required disclosures\n"
                "✓ Avoid unenforceable clauses\n"
                "✓ Protect rights of all parties\n"
                "✓ Include proper termination clauses\n"
                "✓ Add DSGVO compliance clauses if applicable\n\n"
                "Create a professional, legally compliant contract that:\n"
                "- Meets all legal requirements\n"
                "- Is enforceable under current law\n"
                "- Protects all parties appropriately\n"
                "- Contains clear, unambiguous language\n"
                "- Is ready for signature and use\n"
            ),
            "output": "contract_result",
            "description": "Create legally compliant contract",
        },
    ],
}
