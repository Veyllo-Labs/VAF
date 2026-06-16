# VAF Document Creation System

## Overview

VAF includes a powerful **document creation system** that generates structured documents of any size without context overflow. The system uses **section-by-section generation** similar to the research agent, allowing output of 500K+ tokens even with an 8K context window.

## Key Features

✅ **No Context Overflow** - Section-by-section generation prevents context limits
✅ **Multiple Formats** - Word (.docx), PDF, Markdown (.md), Text (.txt)
✅ **Any Size** - Generate 100+ page documents within 8K context
✅ **Smart Chunking** - Automatic section breakdown and assembly
✅ **Multi-Language** - German, English, Turkish, and more
✅ **Professional Output** - Structured, formatted documents ready to use

## Architecture

### Dual-Mode System

VAF provides **two tools** for document creation:

#### 1. **document_writer** (Main Agent Tool)
- **For:** Simple, quick documents (<5000 chars)
- **Use Cases:** Short contracts, letters, messages, templates
- **Speed:** Fast (single generation)
- **Context:** Uses Main Agent's context

#### 2. **document_agent** (Sub-Agent)
- **For:** Complex, large documents (any size)
- **Use Cases:** Multi-page contracts, reports, manuals, books
- **Speed:** Moderate (section-by-section)
- **Context:** Isolated context per section

### Context Management Strategy

The `document_agent` uses the **same pattern as research_agent** to prevent context overflow:

```
┌─────────────────────────────────────────────────────────────┐
│         DOCUMENT AGENT - SECTION-BY-SECTION GENERATION     │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   User: "Create 50-page employment contract"               │
│                          │                                  │
│                          ▼                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  📋 DOCUMENT PLANNER                                │   │
│  │  └─ Breaks into 10-15 sections                      │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│    ┌─────────────────────┼─────────────────────┐            │
│    ▼                     ▼                     ▼            │
│  ┌───────┐          ┌───────┐          ┌───────┐            │
│  │ SEC 1 │          │ SEC 2 │          │ SEC N │            │
│  │Title  │          │Parties│          │  ...  │            │
│  ├───────┤          ├───────┤          ├───────┤            │
│  │🧠 LLM │         │🧠 LLM │          │🧠 LLM │           │
│  │Call   │          │Call   │          │Call   │            │
│  │(2K tok)│         │(2K tok)│          │(2K tok)│           │
│  └───────┘          └───────┘          └───────┘            │
│  ISOLATED           ISOLATED           ISOLATED             │
│                          │                                  │
│                          ▼                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  📄 FINAL DOCUMENT (Word/PDF/MD/Text)              │    │
│  │  └─ All sections assembled                          │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

### How It Works

**1. Planning Phase** (Single LLM Call)
```
Input: "Create employment contract with compensation, termination, benefits"
↓
LLM analyzes and creates structured plan:
- Section 1: Title & Parties
- Section 2: Position & Duties
- Section 3: Compensation
- Section 4: Work Hours
- Section 5: Vacation
- Section 6: Termination
- Section 7: Confidentiality
- Section 8: Signatures
```

**2. Generation Phase** (One LLM Call Per Section)
```
For each section:
  - Fresh context (no history pollution)
  - Section-specific prompt
  - Independent generation
  - ~2000 tokens max per section

Context per section: ~500-1000 tokens
Total sections: 10-15
Total context used: 5K-15K (but only 1K per call!)
```

**3. Assembly Phase** (No LLM)
```
- Combine all sections
- Add headers/footers
- Format for output (Word/PDF/MD/Text)
- Save to file
```

### Benefits

| Aspect | Single Generation | Section-by-Section |
|--------|------------------|-------------------|
| **Max Output** | ~4K tokens | **Unlimited** |
| **Context Usage** | 100% at once | 10-20% per section |
| **Quality** | Degrades with size | Consistent |
| **Context Overflow** | Frequent ❌ | Never ✅ |

## Usage Examples

### Example 1: Simple Document (document_writer)

```
User: "Create a short resignation letter"

Agent: [Uses document_writer]

Output:
### Letter created!
**File:** resignation_letter.docx
**Path:** ~/Documents/VAF_Projects/<uid>/<session_id>/resignation_letter.docx
**Format:** Microsoft Word (.docx)
**Size:** 450 characters

✅ Word document saved successfully.
```

### Example 2: Complex Contract (document_agent)

```
User: "Create a comprehensive employment contract in German with all standard clauses"

Agent: [Detects complex document → uses document_agent]

Document Agent:
1. Planning: Creates 12-section structure
2. Generating section 1/12: Vertragsparteien
3. Generating section 2/12: Arbeitsbeginn und Probezeit
   ...
12. Generating section 12/12: Schlussbestimmungen
13. Assembling final document...

Output:
### Document Created Successfully! 📄

**Title:** Arbeitsvertrag
**Type:** Contract
**Sections:** 12
**Size:** 15,430 characters (~3,857 tokens)
**Format:** DOCX

**File:** Arbeitsvertrag_20260113_143022.docx
**Location:** ~/Documents/VAF_Projects/<uid>/<session_id>/Arbeitsvertrag_20260113_143022.docx

**Sections Generated:**
  1. Vertragsparteien
  2. Arbeitsbeginn und Probezeit
  3. Tätigkeit und Aufgabenbereich
  4. Arbeitszeit
  5. Vergütung
  6. Urlaub
  7. Krankheit
  8. Kündigungsfristen
  9. Geheimhaltung
  10. Nebentätigkeiten
  11. Schlichtungsklausel
  12. Schlussbestimmungen

✅ Document generation completed successfully!
```

### Example 3: Large Report (500K+ tokens possible)

```
User: "Create a detailed technical report about Machine Learning with 20 sections"

Document Agent:
- Planning: Breaks into 20 sections
- Generates each section independently
- Each section: ~2K tokens
- Total output: ~40K tokens
- Context used per section: ~1K tokens

Result: 40K token document generated within 8K context!
```

## Supported Document Types

| Type | German | Description | Typical Sections |
|------|--------|-------------|-----------------|
| **Contract** | Vertrag | Legal agreements | 10-15 |
| **Employment Contract** | Arbeitsvertrag | Job agreements | 12-15 |
| **Rental Agreement** | Mietvertrag | Property rental | 10-12 |
| **Report** | Bericht | Business/technical reports | 8-20 |
| **Letter** | Brief | Formal/business letters | 3-5 |
| **Template** | Vorlage | Reusable forms | 5-10 |
| **Manual** | Anleitung | User guides | 15-50 |
| **Article** | Artikel | Articles/essays | 5-15 |

## Supported Formats

### Word Documents (.docx)
- **Best for:** Professional documents needing formatting
- **Features:** Paragraphs, headings, tables
- **Requires:** `python-docx` (auto-installed)
- **Compatible:** Microsoft Word, LibreOffice, Google Docs

### PDF (.pdf)
- **Best for:** Final, non-editable documents
- **Current:** Saves as text, provides conversion instructions
- **Future:** Native PDF generation with formatting

### Markdown (.md)
- **Best for:** Documentation, technical writing
- **Features:** Headers, lists, code blocks
- **Compatible:** GitHub, GitLab, any Markdown viewer

### Text (.txt)
- **Best for:** Simple documents, maximum compatibility
- **Features:** Plain text only
- **Compatible:** Any text editor, all platforms

## Configuration

Current document generation behavior is primarily controlled inside the tool/workflow implementations.
There is no dedicated `document_agent_*` config block in `config.py` defaults at the moment.

General runtime flags still apply, for example:

```json
{
  "sub_agents_in_separate_terminals": true
}
```

## Workflow Integration

The `create_document` workflow currently uses `document_agent` for generation:

**Workflow triggers (current template):**
- "erstelle einfaches dokument"
- "create simple document"
- "erstelle schnell dokument"
- "create quick document"
- "schreibe brief"
- "write letter"
- "erstelle vorlage"
- "create template"
- "erstelle nachricht"
- "create message"

**Behavior:**
- Workflow template step tool: `document_agent`
- For ad-hoc chat use outside workflows, the agent may still use `document_writer` or `document_agent` based on context and tool routing.

## Use Cases

### Business

- ✅ Employment contracts
- ✅ Service agreements
- ✅ NDAs (Non-Disclosure Agreements)
- ✅ Business proposals
- ✅ Quarterly reports
- ✅ Meeting minutes

### Personal

- ✅ Rental agreements
- ✅ Resignation letters
- ✅ Cover letters
- ✅ Personal statements
- ✅ Invoices
- ✅ Receipts

### Technical

- ✅ User manuals
- ✅ API documentation
- ✅ Technical specifications
- ✅ Project documentation
- ✅ README files
- ✅ Architecture documents

### Creative

- ✅ Articles
- ✅ Essays
- ✅ Blog posts
- ✅ Story outlines
- ✅ Scripts
- ✅ Templates

## Context Management Comparison

### Before (Context Overflow):

```
User: "Create 50-page contract"
Agent: [Tries to generate all at once]
Context: 4K system + 10K generation = 14K tokens
Result: ❌ Context overflow error

OR

Agent: [Generates but truncates]
Context: 4K system + 4K generation = 8K tokens
Result: ⚠️ Incomplete document (missing sections)
```

### After (Section-by-Section):

```
User: "Create 50-page contract"
Agent: [Uses document_agent]

Section 1: 500 tokens context + 1K generation = 1.5K tokens ✅
Section 2: 500 tokens context + 1K generation = 1.5K tokens ✅
Section 3: 500 tokens context + 1K generation = 1.5K tokens ✅
... (15 sections)
Section 15: 500 tokens context + 1K generation = 1.5K tokens ✅

Total output: 15K tokens
Max context used per call: 1.5K tokens (within 8K limit!)
Result: ✅ Complete 50-page contract
```

## Best Practices

### For Users

1. **Be Specific:** "Employment contract for software developer with..."
2. **Specify Format:** "...and save as Word document"
3. **Mention Language:** "Create in German" or "auf Deutsch"
4. **Include Details:** Position, salary range, terms, etc.
5. **Review Output:** Always check and customize placeholders

### For Developers

1. **Section Planning:** More sections = better quality, less context per section
2. **Section Prompts:** Keep prompts focused and minimal
3. **Context Isolation:** Each section gets fresh context
4. **Error Handling:** Gracefully handle generation failures
5. **Format Support:** Fallback to text if format unavailable

## Troubleshooting

### Problem: "Could not create document plan"

**Cause:** A weak or local model returned output that was not valid JSON — truncated by the token limit, with trailing commas, or wrapped in prose.

**Solution:** Plan creation no longer hard-fails. `_create_document_plan` tries three stages in order:
1. A rich JSON plan, parsed with lenient repair (`_repair_json`) that strips trailing commas and comments and closes any structures left open by a truncation — in the correct nesting order, string-aware.
2. A coder-style fallback (`_plan_from_section_lines`) that asks only for plain section titles, one per line — far easier for small models than nested JSON; bullets/numbering are stripped.
3. A deterministic default (`_default_plan`) keyed on the inferred document type, so a usable document is always produced.

If the resulting sections still look too generic:
- Include explicit structure in your request (e.g., "with sections: introduction, methodology, conclusion")
- Ensure your API provider (DeepSeek, OpenAI, etc.) is configured correctly

### Problem: Long document requests fail or truncate

**Cause:** On Windows, command-line length is limited (~8191 chars). Very detailed requests may be cut off.

**Solution:** Tasks over 3000 characters are automatically passed via IPC. The sub-agent fetches the full task from `subagent_queue/task_payloads/{task_id}.txt`. No configuration needed.

### Problem: Document too small

**Solution:** Request more sections or provide more details
```
Instead of: "Create contract"
Try: "Create employment contract with compensation, benefits, termination clauses"
```

### Problem: Wrong language

**Solution:** Specify language explicitly
```
"Create employment contract in German"
"Erstelle Arbeitsvertrag auf Deutsch"
```

### Problem: Missing sections

**Solution:** List required sections
```
"Create contract with sections: parties, duties, compensation, termination, confidentiality"
```

### Problem: Format not supported

**Solution:** Check dependencies
```bash
pip install python-docx  # For Word documents
```

### Problem: "File is not a zip file" when opening DOCX

**Cause:** DOCX files are ZIP archives. This error means the file was corrupted during save or is not a valid DOCX.

**Solution:** The document agent now (1) saves to the chat's workspace folder (never the project root or CWD), (2) verifies each saved DOCX. If verification fails, it falls back to `.txt`. Ensure python-docx is up to date: `pip install --upgrade python-docx`.

### Problem: Documents saved in wrong location (e.g. project root)

**Solution:** Both `document_writer` and `document_agent` save into the chat's workspace folder (`~/Documents/VAF_Projects/<uid[:8]>/<session_id>/`, resolved via `resolve_agent_output_dir` in `vaf/core/session.py`), where the files also appear in the WebUI workspace browser. Without session context (plain CLI) they fall back to the legacy `~/Documents/VAF_Documents` directory. Directories are created automatically.

## Related Documentation

- [Document Reading](DOCUMENT_READING.md) - Read existing documents
- [Context Management](../memory/CONTEXT_MANAGEMENT.md) - How context is managed
- [Sub-Agent IPC](../agents/SUBAGENT_IPC.md) - Sub-agent communication

## Technical Details

### Section Planning Algorithm

```python
def _create_document_plan(task: str):
    """
    Analyzes task and creates structured section plan.
    Single LLM call, returns JSON with sections.
    """
    prompt = f"""Analyze task and create section plan.
    Task: {task}
    Output: JSON with document_type, title, sections[]
    Break into 5-15 sections for optimal generation."""
    
    content = llm_call(prompt)  # ~500-2K tokens
    # Robust extraction: handles markdown ```json blocks, text before/after JSON
    plan = _extract_json_from_response(content)
    # Validate and repair missing fields (title, description, format)
    return _validate_and_repair_plan(plan)  # Fallback retry if first attempt fails
```

### Section Generation

```python
def _generate_section(section_title, description):
    """
    Generates single section with isolated context.
    No previous sections in context = no pollution!
    """
    prompt = f"""Write section: {section_title}
    Requirements: {description}
    Output: Clean text, ~1000 tokens"""
    
    return llm_call(prompt)  # Fresh context each time!
```

### Assembly

```python
def _assemble_document(sections):
    """
    Combines sections into final document.
    No LLM needed - just string concatenation!
    """
    return "\n\n".join([s['content'] for s in sections])
```

## Performance Metrics

| Document Size | Sections | Total Output | Max Context/Call | Total Time |
|--------------|----------|--------------|-----------------|------------|
| Short Letter | 3 | 500 tokens | 1K tokens | ~5s |
| Contract | 12 | 3K tokens | 1.5K tokens | ~30s |
| Report | 15 | 10K tokens | 1.5K tokens | ~45s |
| Manual | 30 | 30K tokens | 2K tokens | ~90s |
| Book Chapter | 50 | 50K tokens | 2K tokens | ~150s |

## Future Enhancements

Planned improvements:

- [ ] Native PDF generation with formatting
- [ ] Template library (pre-defined structures)
- [ ] Multi-column layouts
- [ ] Image/chart insertion
- [ ] Collaborative editing
- [ ] Version control integration
- [ ] Digital signatures
- [ ] Export to more formats (ODT, RTF, HTML)

## Conclusion

The VAF Document Creation System solves the context overflow problem through **section-by-section generation**, enabling documents of any size within a fixed context window. Whether you need a simple letter or a 100-page manual, VAF can generate it efficiently and professionally.

For questions or issues, see the main README or open an issue on the project repository.
