# Research & Document Workflows

## Overview

VAF includes **multi-stage workflows** that combine research and document creation to produce **informed, professional, legally sound** documents. These workflows prevent context overflow by using **two-phase processing**: research first, then document creation.

## Key Innovation

### Problem: Uninformed Documents

Traditional document generation creates content without verification:
```
User: "Create employment contract"
Agent: [Generates contract from memory]
Result: ⚠️ May be outdated, legally incorrect, or incomplete
```

### Solution: Research-Based Documents

Multi-stage workflows ensure documents are based on **current, accurate information**:
```
User: "Create employment contract"

Stage 1: Research Agent
  → Searches current laws (BGB, NachwG, etc.)
  → Finds mandatory clauses
  → Gathers best practices
  → Output: 10-15K tokens research

Stage 2: Document Agent
  → Receives research findings
  → Creates document section-by-section
  → Includes legal references
  → Output: 15-20K tokens document

Result: ✅ Legally sound, up-to-date, comprehensive contract
```

## Architecture

### Two-Phase Processing

```
┌─────────────────────────────────────────────────────────────┐
│         RESEARCH & DOCUMENT WORKFLOW ARCHITECTURE           │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  PHASE 1: RESEARCH AGENT                            │    │
│  │  ───────────────────────────────────────────────    │    │
│  │                                                     │    │
│  │  Topic-by-Topic Research (No Overflow):             │    │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐              │    │
│  │  │ Topic 1 │  │ Topic 2 │  │ Topic N │              │    │
│  │  │ 2K out  │  │ 2K out  │  │ 2K out  │              │    │
│  │  └─────────┘  └─────────┘  └─────────┘              │    │
│  │       │            │            │                   │    │
│  │       └────────────┴────────────┘                   │    │
│  │                    │                                │    │
│  │           Research Content                          │    │
│  │           (10-15K tokens)                           │    │
│  └─────────────────────┬───────────────────────────────┘    │
│                        │                                    │
│                        ▼                                    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  PHASE 2: DOCUMENT AGENT                            │    │
│  │  ───────────────────────────────────────────────    │    │
│  │                                                     │    │
│  │  Section-by-Section Generation (No Overflow):       │    │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐              │    │
│  │  │  Sec 1  │  │  Sec 2  │  │  Sec N  │              │    │
│  │  │+ Research│ │+ Research│ │+ Research│             │    │
│  │  │ 2K total│  │ 2K total│  │ 2K total│              │    │
│  │  └─────────┘  └─────────┘  └─────────┘              │    │
│  │       │            │            │                   │    │
│  │       └────────────┴────────────┘                   │    │
│  │                    │                                │    │
│  │           Final Document                            │    │
│  │           (15-20K tokens)                           │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  Total Output: 25-35K tokens                                │
│  Max Context Per Call: 2.5K tokens                          │
│  Never Exceeds 8K Context! ✅                              │
└─────────────────────────────────────────────────────────────┘
```

### Context Management

| Phase | Process | Context/Call | Total Output | Overflow Risk |
|-------|---------|--------------|--------------|---------------|
| **Research** | Topic-by-topic | 2.5K tokens | 10-15K tokens | ✅ None |
| **Document** | Section-by-section + research | 2K tokens | 15-20K tokens | ✅ None |
| **Total** | Two-phase | Max 2.5K | 25-35K tokens | ✅ None |

**Key Advantage:** Research findings are injected into each document section's context, but sections are still generated independently!

## Available Workflows

### 1. Research & Document (General)

**File:** `vaf/workflows/workflows/research_and_document.py`

**Best for:** Any document type that benefits from research

**Triggers:**
- "recherchiere und erstelle dokument"
- "research and create document"
- "erstelle basierend auf recherche"
- "dokument mit recherche"

**Example:**
```
User: "Research and create a comprehensive guide about Docker deployment"

Workflow:
1. Research Agent: Docker best practices, security, tutorials
2. Document Agent: Creates professional guide based on research

Result: Informed, comprehensive Docker guide
```

### 2. Legal Contract Research (Specialized)

**File:** `vaf/workflows/workflows/legal_contract_research.py`

**Best for:** Legal contracts requiring current law research

**Triggers:**
- "rechtssicherer vertrag"
- "legally sound contract"
- "arbeitsvertrag recherchieren"
- "mietvertrag recherchieren"

**Example:**
```
User: "Erstelle einen rechtssicheren Arbeitsvertrag"

Workflow:
1. Research Agent:
   - BGB §611a (Arbeitsvertrag Definition)
   - Nachweisgesetz (NachwG) - Mandatory info
   - BGB §622 (Kündigungsfristen)
   - BUrlG (Urlaubsanspruch)
   - DSGVO (Data protection)

2. Document Agent:
   - Creates 12-section contract
   - Includes all mandatory clauses
   - References specific laws
   - Ensures legal compliance

Result: Legally sound employment contract with law references
```

### 3. Technical Documentation Research (Specialized)

**File:** `vaf/workflows/workflows/technical_doc_research.py`

**Best for:** Technical manuals, API docs, system guides

**Triggers:**
- "technische dokumentation mit recherche"
- "technical documentation with research"
- "anleitung mit recherche"
- "handbuch mit recherche"

**Example:**
```
User: "Create a comprehensive Kubernetes deployment guide"

Workflow:
1. Research Agent:
   - Official Kubernetes docs
   - Best practices and patterns
   - Common pitfalls
   - Security recommendations
   - Performance tips

2. Document Agent:
   - Step-by-step guide
   - Code examples from research
   - Troubleshooting section
   - Security best practices

Result: Professional Kubernetes guide based on official docs
```

## Use Cases

### Business & Legal

#### Employment Contracts
```
User: "Erstelle rechtssicheren Arbeitsvertrag für Software-Entwickler"

Research: German labor law, IT industry standards
Document: Comprehensive contract with all legal requirements
Sections: 12-15 (Parties, Position, Salary, Hours, Vacation, Termination, etc.)
References: BGB §611a, NachwG §2, BGB §622, etc.
```

#### Rental Agreements
```
User: "Create legally sound rental agreement for apartment"

Research: Rental law, tenant rights, landlord obligations
Document: Complete rental agreement with legal compliance
Sections: 10-12 (Parties, Property, Rent, Utilities, Termination, etc.)
References: BGB §535, BGB §556, etc.
```

#### Service Contracts
```
User: "Erstelle Dienstleistungsvertrag für IT-Beratung"

Research: Service contract law, liability clauses, payment terms
Document: Professional service agreement
Sections: 8-10 (Scope, Payment, Liability, Confidentiality, etc.)
```

### Technical Documentation

#### Software Installation Guide
```
User: "Create installation guide for PostgreSQL with Docker"

Research: PostgreSQL docs, Docker best practices, security
Document: Complete installation guide
Sections: 10-15 (Prerequisites, Installation, Configuration, Testing, etc.)
```

#### API Documentation
```
User: "Document our REST API with best practices"

Research: REST API standards, OpenAPI, documentation patterns
Document: Professional API documentation
Sections: 8-12 (Overview, Authentication, Endpoints, Examples, etc.)
```

#### Troubleshooting Guide
```
User: "Create troubleshooting guide for Kubernetes deployments"

Research: Common Kubernetes issues, debugging techniques
Document: Comprehensive troubleshooting guide
Sections: 12-15 (Common Errors, Diagnostics, Solutions, Prevention, etc.)
```

### Business Documents

#### Market Analysis Report
```
User: "Research and document AI market trends for 2026"

Research: Market reports, trends, statistics, forecasts
Document: Professional market analysis
Sections: 10-15 (Executive Summary, Market Size, Trends, Competitors, etc.)
```

#### Business Proposal
```
User: "Create business proposal for SaaS product"

Research: SaaS market, pricing models, competitor analysis
Document: Data-driven business proposal
Sections: 12-15 (Problem, Solution, Market, Revenue, Team, etc.)
```

## Configuration

These workflows are currently configured through workflow templates and tool logic in code.
There is no dedicated `research_max_results` / `document_default_format` / `workflow_enable_research_cache`
config block in `config.py` defaults at the moment.

## Benefits vs. Single-Stage

### Traditional Single-Stage Document Generation

```
User: "Create employment contract"
Agent: [Generates from memory/training]

Problems:
❌ May use outdated laws
❌ No verification of current requirements
❌ May miss mandatory clauses
❌ No source citations
❌ Generic, not optimized for specific use case
```

### Multi-Stage Research & Document

```
User: "Create employment contract"
Agent: [Research current laws → Create informed document]

Benefits:
✅ Based on current laws (researched in real-time)
✅ Includes all mandatory requirements
✅ Cites specific laws and regulations
✅ Follows current best practices
✅ Tailored to specific context
✅ Professional and complete
```

### Comparison Table

| Aspect | Single-Stage | Multi-Stage R&D |
|--------|-------------|----------------|
| **Legal Accuracy** | ⚠️ May be outdated | ✅ Current research |
| **Completeness** | ⚠️ May miss clauses | ✅ All requirements |
| **Source Citations** | ❌ None | ✅ Law references |
| **Best Practices** | ⚠️ Generic | ✅ Researched |
| **Context Usage** | 4-6K tokens | 2-2.5K per stage |
| **Output Quality** | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| **Processing Time** | 30s | 5-10 min |

## Real-World Examples

### Example 1: Employment Contract

```
User: "Erstelle einen rechtssicheren Arbeitsvertrag für einen 
       Software-Entwickler mit 60.000€ Jahresgehalt"

═══════════════════════════════════════════════════════════
STAGE 1: RESEARCH AGENT (3 minutes)
═══════════════════════════════════════════════════════════

Researching: "Arbeitsvertrag Gesetze Deutschland"

Findings:
• BGB §611a: Definition Arbeitsvertrag
• Nachweisgesetz (NachwG): Mandatory information
  - Parties (§2 Abs. 1 Nr. 1)
  - Start date (§2 Abs. 1 Nr. 2)
  - Job description (§2 Abs. 1 Nr. 3)
  - Duration (§2 Abs. 1 Nr. 4)
  - Working hours (§2 Abs. 1 Nr. 6)
  - Salary (§2 Abs. 1 Nr. 7)
  - Vacation (§2 Abs. 1 Nr. 8)
• BGB §622: Termination periods
  - Probation: 2 weeks
  - Regular: 4 weeks to 15th or end of month
• BUrlG §3: Minimum 20 days vacation (5-day week)
• DSGVO Art. 88: Employee data protection

Research Output: 12,450 tokens

═══════════════════════════════════════════════════════════
STAGE 2: DOCUMENT AGENT (2 minutes)
═══════════════════════════════════════════════════════════

Creating document with 12 sections:

Section 1: Vertragsparteien (NachwG §2 Abs. 1 Nr. 1)
  → Includes employer and employee details

Section 2: Arbeitsbeginn und Probezeit (NachwG §2 Abs. 1 Nr. 2)
  → Start date, 6-month probation period
  → References BGB §622 Abs. 3 (2-week notice during probation)

Section 3: Tätigkeitsbeschreibung (NachwG §2 Abs. 1 Nr. 3)
  → Detailed role description for Software Developer

Section 4: Arbeitszeit (NachwG §2 Abs. 1 Nr. 6)
  → 40 hours/week, flexible hours mentioned
  → References ArbZG (Working Time Act)

Section 5: Vergütung (NachwG §2 Abs. 1 Nr. 7)
  → 60.000€ annual salary
  → Payment terms, deductions

Section 6: Urlaub (NachwG §2 Abs. 1 Nr. 8)
  → 30 days vacation (exceeds BUrlG §3 minimum)

Section 7: Krankheit
  → Sick leave policy, Entgeltfortzahlung

Section 8: Kündigungsfristen (BGB §622)
  → 4 weeks to 15th or end of month
  → Probation: 2 weeks (§622 Abs. 3)

Section 9: Geheimhaltung und Datenschutz
  → Confidentiality clauses
  → DSGVO Art. 88 compliance

Section 10: Nebentätigkeiten
  → Side work approval requirement

Section 11: Schlichtungsklausel
  → Dispute resolution

Section 12: Schlussbestimmungen
  → Salvatorische Klausel, signatures

Document Output: 18,230 tokens

═══════════════════════════════════════════════════════════
RESULT
═══════════════════════════════════════════════════════════

File: Arbeitsvertrag_SoftwareEntwickler_20260113.docx
Size: 6 pages
Legally Sound: ✅ All NachwG requirements met
Up-to-date: ✅ Current laws (2026)
Professional: ✅ Properly structured
Ready to Use: ✅ Needs only party details filled
```

### Example 2: Docker Deployment Guide

```
User: "Create comprehensive guide for deploying Python apps with Docker"

═══════════════════════════════════════════════════════════
STAGE 1: RESEARCH AGENT
═══════════════════════════════════════════════════════════

Research Topics:
1. Docker best practices for Python
2. Multi-stage builds
3. Security hardening
4. Environment variables
5. Docker Compose patterns

Findings:
• Official Docker Python images best practices
• Multi-stage builds reduce image size 80%
• Non-root user for security
• .dockerignore to exclude unnecessary files
• Health checks for production
• Volume mounts for development
• Environment variable management
• Docker Compose for multi-container apps

Research Output: 14,890 tokens

═══════════════════════════════════════════════════════════
STAGE 2: DOCUMENT AGENT
═══════════════════════════════════════════════════════════

Creating guide with 15 sections:

1. Introduction & Prerequisites
2. Docker Installation
3. Creating Dockerfile (with multi-stage build example)
4. Security Best Practices (non-root user, etc.)
5. Building Images
6. Running Containers
7. Environment Configuration
8. Docker Compose Setup
9. Development Workflow
10. Production Deployment
11. Monitoring & Logging
12. Health Checks
13. Troubleshooting Common Issues
14. Performance Optimization
15. Security Checklist

Document Output: 22,140 tokens

═══════════════════════════════════════════════════════════
RESULT
═══════════════════════════════════════════════════════════

File: Docker_Python_Deployment_Guide.docx
Size: 25 pages
Based on: Official Docker docs + best practices
Code Examples: ✅ All working examples from research
Security: ✅ Following current recommendations
Complete: ✅ Installation to production
```

## Performance Metrics

| Workflow | Research Time | Document Time | Total Time | Output Size | Context Peak |
|----------|--------------|---------------|------------|-------------|--------------|
| Short Contract | 2-3 min | 1-2 min | 3-5 min | 10-15 pages | 2K tokens |
| Long Contract | 3-5 min | 2-4 min | 5-9 min | 20-30 pages | 2.5K tokens |
| Technical Guide | 4-6 min | 3-5 min | 7-11 min | 30-50 pages | 2.5K tokens |
| Business Report | 5-7 min | 4-6 min | 9-13 min | 40-60 pages | 2.5K tokens |

## Best Practices

### For Users

1. **Be Specific About Requirements**
   ```
   Good: "Erstelle Arbeitsvertrag für Senior Developer, 70K€, München"
   Bad: "Erstelle Vertrag"
   ```

2. **Specify Document Type Clearly**
   ```
   Good: "Create technical documentation for REST API"
   Bad: "Make some docs"
   ```

3. **Mention Important Details**
   ```
   Good: "NDA for software project with 2-year confidentiality"
   Bad: "Create NDA"
   ```

### For Developers

1. **Trust the Research:** Don't override research findings in prompts
2. **Keep Sections Focused:** Each section should have clear scope
3. **Include Context:** Research findings injected into each section
4. **Handle Errors Gracefully:** Research or document stage may fail
5. **Cache Research (Optional):** For similar documents, consider caching

## Troubleshooting

### Problem: Research takes too long

**Solution:** Reduce `max_results` in workflow
```python
"max_results": 3,  # Instead of 5
```

### Problem: Document misses research info

**Solution:** Research findings are already injected. Check prompts ensure they reference `{research_content}`

### Problem: Wrong language in document

**Solution:** Specify language in task
```
"Erstelle auf Deutsch..." or "Create in English..."
```

## Future Enhancements

Planned improvements:

- [ ] Research result caching (avoid re-researching same topics)
- [ ] Multi-language research (research in one language, document in another)
- [ ] Incremental updates (update existing documents with new research)
- [ ] Research quality scoring
- [ ] Citation formatting options
- [ ] Template library with pre-researched content

## Related Documentation

- [Document Creation](../documents/DOCUMENT_CREATION.md) - Document Agent architecture
- [Context Management](../memory/CONTEXT_MANAGEMENT.md) - How context overflow is prevented
- [Research Agent](../../vaf/tools/research_agent.py) - Research Agent implementation
- [Document Agent](../../vaf/tools/document_agent.py) - Document Agent implementation

## Conclusion

**Research & Document workflows** represent a major advancement in AI document generation. By combining comprehensive research with section-by-section document creation, VAF produces documents that are:

✅ **Informed** - Based on current, researched information
✅ **Accurate** - Uses up-to-date laws, standards, best practices
✅ **Professional** - Properly structured and complete
✅ **Legally Sound** - Includes all required clauses and references
✅ **Scalable** - No context overflow regardless of document size

Whether you need a legally binding contract, a comprehensive technical manual, or a data-driven business report, these workflows ensure your documents are **research-backed and professional**.

🔨 **Let's create informed documents!** 🚀
