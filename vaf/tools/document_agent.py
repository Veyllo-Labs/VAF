"""
VAF Document Agent - Section-by-section document creation with bounded context.

This tool is designed to avoid "exceed_context_size_error" by:
- Splitting document creation into sections
- Generating each section separately with its own context
- Assembling sections into final document (Word, PDF, Markdown, Text)
- Supporting huge outputs (500K+ tokens) within 8K context window

Similar architecture to research_agent but for document creation.
"""

import os
import re
import sys
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime

from vaf.tools.base import BaseTool
from vaf.cli.ui import UI
from vaf.core.document_formatting import (
    DocumentModel,
    DocumentSection,
    build_document_model,
    coerce_section,
    estimate_document_length,
    render_markdown,
    render_text,
    save_document_model_as_docx,
)

class DocumentAgentTool(BaseTool):
    """
    Specialized Sub-Agent for creating large, structured documents.
    
    Handles:
    - Contracts (Arbeitsverträge, Mietverträge, etc.)
    - Reports (Berichte, Dokumentationen)
    - Letters (Briefe, Anschreiben)
    - Templates (Vorlagen, Formulare)
    - Multi-format output (Word, PDF, Markdown, Text)
    
    Key Feature: Section-by-section generation to prevent context overflow.
    """
    
    name = "document_agent"
    permission_level = "write"
    side_effect_class = "reversible"
    description = """Specialized Sub-Agent for creating large, structured documents (contracts, reports, letters, templates).
Supports multi-format output: Word (.docx), PDF (.pdf), Markdown (.md), Text (.txt).
Handles documents of any size using section-by-section generation (no context overflow)."""
    
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Document creation task (e.g., 'Create employment contract', 'Generate business report')"
            }
        },
        "required": ["task"]
    }
    
    def __init__(self):
        super().__init__()
        self.home = Path.home()

    def run(self, **kwargs) -> str:
        task = kwargs.get('task', '').strip()
        if not task:
            return "Error: No task provided."
        
        # ═══════════════════════════════════════════════════════════════════════
        # CHECK IF RUNNING IN SEPARATE TERMINAL MODE
        # ═══════════════════════════════════════════════════════════════════════
        from vaf.core.config import Config
        from vaf.core.platform import Platform
        
        # If already in sub-agent terminal, run normally
        if os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip() in ("1", "true", "yes"):
            pass
        elif Config.get("sub_agents_in_separate_terminals", False):
            # Start in new terminal window with IPC tracking
            import shlex
            from vaf.core.subagent_ipc import get_ipc, get_current_session_id
            
            # Create task in IPC system
            ipc = get_ipc()
            task_id = ipc.create_task("document_agent", task_description=task)
            
            # Pass session/task context to the sub-agent via the CHILD env only (not the parent's
            # process-global os.environ), so concurrent workers don't clobber each other's session.
            session_id = get_current_session_id()
            _sub_env = {"VAF_TASK_ID": task_id, "VAF_AGENT_TYPE": "document_agent"}
            if session_id:
                _sub_env["VAF_SESSION_ID"] = session_id

            # Pass provider configuration to sub-agent
            use_separate_provider = Config.get("subagent_use_separate_provider", False)
            if use_separate_provider:
                subagent_provider = Config.get("subagent_provider", "inherit")
                if subagent_provider != "inherit":
                    _sub_env["VAF_PROVIDER"] = subagent_provider
            
            # Windows cmd.exe limit ~8191 chars; pass long tasks via IPC only
            max_task_len = 3000
            if len(task) > max_task_len:
                cmd_parts = [sys.executable, '-m', 'vaf.main', 'subagent', 'run', 'document_agent', '--task-id', task_id]
            else:
                cmd_parts = [sys.executable, '-m', 'vaf.main', 'subagent', 'run', 'document_agent', '--task', task, '--task-id', task_id]
            
            if Platform.is_windows():
                escaped_parts = []
                for part in cmd_parts:
                    if ' ' in part or '"' in part:
                        escaped = part.replace('"', '\\"')
                        escaped_parts.append(f'"{escaped}"')
                    else:
                        escaped_parts.append(part)
                cmd = ' '.join(escaped_parts)
                title = f"VAF Document Agent [{task_id}]"
            else:
                cmd = ' '.join(shlex.quote(str(part)) for part in cmd_parts)
                title = f"VAF Document Agent [{task_id}]"
            
            if Platform.open_new_terminal(cmd, title=title, extra_env=_sub_env):
                ipc.mark_task_running(task_id)
                UI.event("Sub-Agent", f"Document Agent started in new terminal [Task: {task_id}]", style="bold cyan")
                return f"[SUBAGENT_ASYNC:{task_id}:document_agent] Sub-Agent running in separate terminal. Task: {task[:80]}..."
            else:
                UI.warning("Failed to open new terminal, running in current window")
                ipc.cancel_task(task_id)
        
        # ═══════════════════════════════════════════════════════════════════════
        # EXECUTE DOCUMENT GENERATION
        # ═══════════════════════════════════════════════════════════════════════
        
        try:
            result = self._generate_document(task)
            return result
        except Exception as e:
            return f"[ERROR] Document generation failed: {e}"
    
    def _generate_document(self, task: str) -> str:
        """Main document generation logic with section-by-section approach."""
        
        UI.event("Document Agent", "Analyzing document request...", style="dim")
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 1: Analyze task and create document plan
        # ═══════════════════════════════════════════════════════════════════════
        
        plan = self._create_document_plan(task)
        
        if not plan:
            return "[ERROR] Could not create document plan. Please provide more details about the document."
        
        UI.event("Document Agent", f"Plan created: {plan['title']} ({len(plan['sections'])} sections)", style="success")
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 2: Generate each section independently (no context overflow!)
        # ═══════════════════════════════════════════════════════════════════════
        
        sections_content: list[DocumentSection] = []
        for i, section in enumerate(plan['sections'], 1):
            UI.event("Document Agent", f"Generating section {i}/{len(plan['sections'])}: {section['title']}", style="dim")
            
            content = self._generate_section(
                document_type=plan['document_type'],
                document_title=plan['title'],
                section_title=section['title'],
                section_description=section['description'],
                section_index=i,
                total_sections=len(plan['sections'])
            )
            
            sections_content.append(content)
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 3: Assemble final document
        # ═══════════════════════════════════════════════════════════════════════
        
        UI.event("Document Agent", "Assembling final document...", style="dim")
        
        document_model = self._assemble_document(
            title=plan['title'],
            document_type=plan['document_type'],
            sections=sections_content
        )
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 4: Save document in requested format
        # ═══════════════════════════════════════════════════════════════════════
        
        output_format = plan.get('format', 'docx')
        filename = plan.get('filename', self._generate_filename(plan['title'], output_format))
        
        file_path = self._save_document(document_model, filename, output_format, plan['document_type'])

        # Notify Web UI so the Document Editor opens with the created document (same process or subprocess)
        try:
            from vaf.core.web_interface import notify_document_created
            session_id = os.environ.get("VAF_SESSION_ID", "").strip()
            if not session_id:
                try:
                    from vaf.core.subagent_ipc import get_current_session_id
                    session_id = get_current_session_id() or ""
                except Exception:
                    pass
            if session_id:
                notify_document_created(session_id, file_path, title=plan.get("title"))
        except Exception as e:
            UI.warning(f"Could not notify document created: {e}")

        # Auto-open the folder containing the document
        try:
            from vaf.core.platform import Platform
            Platform.open_path(file_path.parent)
            UI.event("Document Agent", f"Opened folder: {file_path.parent}", style="dim")
        except Exception as e:
            UI.warning(f"Could not open folder: {e}")
            
        return self._format_success_message(
            file_path,
            plan,
            estimate_document_length(document_model),
        )
    
    def _extract_json_from_response(self, content: str) -> Optional[Dict]:
        """
        Extract and parse JSON from LLM response.
        Handles markdown code blocks, text before/after JSON, and common variants.
        """
        if not content or not content.strip():
            return None

        # 1. Try markdown code block first: ```json ... ``` or ``` ... ```
        code_block = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', content)
        if code_block:
            try:
                return json.loads(code_block.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 2. Try to find JSON object (non-greedy to get innermost complete object)
        # Match from first { to matching }
        depth = 0
        start = -1
        for i, c in enumerate(content):
            if c == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        return json.loads(content[start:i + 1])
                    except json.JSONDecodeError:
                        start = -1
                        continue
        return None

    def _validate_and_repair_plan(self, plan: Dict) -> Optional[Dict]:
        """
        Validate plan structure and repair missing fields.
        Returns repaired plan or None if beyond repair.
        """
        if not isinstance(plan, dict):
            return None
        sections = plan.get('sections')
        if not isinstance(sections, list) or len(sections) == 0:
            return None
        # Repair sections
        for i, s in enumerate(sections):
            if not isinstance(s, dict):
                sections[i] = {"title": f"Section {i+1}", "description": "Content"}
            else:
                s.setdefault('title', f"Section {i+1}")
                s.setdefault('description', s.get('title', 'Content'))
        plan.setdefault('document_type', 'report')
        plan.setdefault('title', 'Document')
        plan.setdefault('format', 'docx')
        plan.setdefault('filename', self._generate_filename(plan['title'], plan['format']))
        return plan

    def _create_document_plan(self, task: str) -> Optional[Dict]:
        """
        Create a structured plan for the document.
        Uses LLM to analyze task and break it into sections.
        """
        prompt = f"""You are a document planning expert. Analyze this task and create a structured plan.

Task: {task}

Create a JSON object with these exact keys:
- document_type: one of contract, report, letter, template, article, manual
- title: Document title (string)
- format: docx, pdf, md, or txt
- filename: Suggested filename (string)
- sections: Array of objects, each with "title" and "description"

Example structure:
{{"document_type":"report","title":"My Report","format":"docx","filename":"My_Report.docx","sections":[{{"title":"Introduction","description":"Overview and context"}},{{"title":"Main Content","description":"Detailed analysis"}}]}}

IMPORTANT: Use 5-15 sections for complex documents. Output ONLY the JSON object, no other text."""

        try:
            content = self.query_llm(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
                temperature=0.2
            )
            
            if content:
                plan = self._extract_json_from_response(content)
                if plan:
                    repaired = self._validate_and_repair_plan(plan)
                    if repaired:
                        return repaired
            
            # Retry with simpler prompt if first attempt failed
            fallback_prompt = f"""Create a document plan as JSON. Task: {task}

Output exactly: {{"document_type":"report","title":"TITLE_HERE","format":"docx","filename":"document.docx","sections":[{{"title":"Section 1","description":"Content for section 1"}},{{"title":"Section 2","description":"Content for section 2"}}]}}

Replace TITLE_HERE and add more sections (5-15 total) based on the task. Output ONLY valid JSON."""
            content2 = self.query_llm(
                messages=[{"role": "user", "content": fallback_prompt}],
                max_tokens=1024,
                temperature=0.1
            )
            if content2:
                plan = self._extract_json_from_response(content2)
                if plan:
                    repaired = self._validate_and_repair_plan(plan)
                    if repaired:
                        return repaired
            
            UI.warning("LLM did not return valid document plan structure.")
            return None
            
        except Exception as e:
            UI.error(f"Plan creation failed: {e}")
            return None
    
    def _generate_section(
        self,
        document_type: str,
        document_title: str,
        section_title: str,
        section_description: str,
        section_index: int,
        total_sections: int
    ) -> DocumentSection:
        """
        Generate a single section with its own isolated context.
        This prevents context overflow for large documents.
        """

        prompt = f"""You are writing section {section_index} of {total_sections} for a {document_type}.

Document: {document_title}
Section: {section_title}
Requirements: {section_description}

Write this section completely and professionally. Include all necessary details, clauses, or content for this section only.

Return ONLY valid JSON with this exact shape:
{{
  "title": "{section_title}",
  "heading_level": 2,
  "blocks": [
    {{"type": "paragraph", "text": "A complete paragraph."}},
    {{"type": "bullet_list", "items": ["Item 1", "Item 2"]}}
  ]
}}

Style rules:
- Keep the title aligned with the requested section title.
- Use one semantic block type at a time: paragraph, bullet_list, numbered_list.
- Do not emit markdown headings, separator lines, bold markers, or decorative formatting.
- Use lists only when an actual list improves readability.
- Keep paragraph spacing compact and the structure clean.
- Language: Match the requested document language and context."""

        try:
            content = self.query_llm(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
                temperature=0.2
            )

            if content:
                parsed = self._extract_json_from_response(content)
                if parsed:
                    return self._validate_and_repair_section(parsed, section_title)

                fallback_prompt = f"""Convert this section into the required JSON structure only.

Section title: {section_title}
Text:
{content}
"""
                repaired_content = self.query_llm(
                    messages=[{"role": "user", "content": fallback_prompt}],
                    max_tokens=1536,
                    temperature=0.1,
                )
                parsed_repaired = self._extract_json_from_response(repaired_content or "")
                if parsed_repaired:
                    return self._validate_and_repair_section(parsed_repaired, section_title)

                return self._validate_and_repair_section(content.strip(), section_title)

            return self._validate_and_repair_section("", section_title)
        except Exception as e:
            return self._validate_and_repair_section(f"[ERROR generating section: {e}]", section_title)

    def _validate_and_repair_section(self, section_payload: Any, fallback_title: str) -> DocumentSection:
        """Normalize section output into the canonical representation."""

        return coerce_section(section_payload, fallback_title=fallback_title, default_level=2)

    def _assemble_document(
        self,
        title: str,
        document_type: str,
        sections: List[DocumentSection],
    ) -> DocumentModel:
        """Assemble all sections into the canonical document model."""

        return build_document_model(title=title, document_type=document_type, sections=sections)

    def _save_document(self, content: DocumentModel, filename: str, format: str, doc_type: str) -> Path:
        """Save document in requested format. Chat workspace first, legacy VAF_Documents otherwise (never CWD)."""
        from vaf.core.platform import Platform
        from vaf.core.session import resolve_agent_output_dir

        # Chat workspace when a session exists (shows up in the WebUI workspace
        # browser next to the chat's projects); Documents/VAF_Documents otherwise.
        docs_dir = resolve_agent_output_dir(Platform.documents_dir() / "VAF_Documents")
        # Use only the filename (no path) to avoid injection or wrong-directory saves
        safe_name = Path(filename).name if filename else "document"
        file_path = docs_dir / safe_name
        
        try:
            if format == 'docx':
                return self._save_as_word(content, file_path, doc_type)
            elif format == 'pdf':
                return self._save_as_pdf(content, file_path, doc_type)
            elif format == 'md':
                return self._save_as_markdown(content, file_path)
            else:
                return self._save_as_text(content, file_path)
        except Exception as e:
            UI.error(f"Save failed: {e}")
            # Fallback to text
            return self._save_as_text(content, file_path.with_suffix('.txt'))
    
    def _save_as_text(self, content: DocumentModel, file_path: Path) -> Path:
        """Save as plain text file."""
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(render_text(content))
        return file_path

    def _save_as_markdown(self, content: DocumentModel, file_path: Path) -> Path:
        """Save as Markdown file."""
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(render_markdown(content))
        return file_path

    def _save_as_word(self, content: DocumentModel, file_path: Path, doc_type: str) -> Path:
        """Save as Word document from the canonical model."""
        try:
            return save_document_model_as_docx(content, file_path)
        except ImportError:
            UI.warning("python-docx not installed. Saving as text instead.")
            return self._save_as_text(content, file_path.with_suffix('.txt'))
    
    def _save_as_pdf(self, content: str, file_path: Path, doc_type: str) -> Path:
        """Save as PDF (requires external tool or library)."""
        # For now, save as text and inform user
        # Future: Use reportlab or weasyprint for PDF generation
        text_path = self._save_as_text(content, file_path.with_suffix('.txt'))
        UI.info(f"PDF generation requires additional tools. Saved as text: {text_path}")
        UI.info("To convert to PDF: Use LibreOffice, pandoc, or a PDF printer.")
        return text_path
    
    def _generate_filename(self, title: str, format: str) -> str:
        """Generate a safe filename from title."""
        # Remove special characters
        safe_title = re.sub(r'[^\w\s-]', '', title)
        safe_title = re.sub(r'[\s]+', '_', safe_title)
        safe_title = safe_title[:50]  # Limit length
        
        # Add timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        return f"{safe_title}_{timestamp}.{format}"
    
    def _format_success_message(self, file_path: Path, plan: Dict, content_length: int) -> str:
        """Format the success message."""
        
        return f"""### Document Created Successfully! 📄

**Title:** {plan['title']}
**Type:** {plan['document_type'].capitalize()}
**Sections:** {len(plan['sections'])}
**Size:** {content_length:,} characters (~{content_length // 4:,} tokens)
**Format:** {plan.get('format', 'txt').upper()}

**File:** {file_path.name}
**Location:** {file_path.absolute()}

**Sections Generated:**
{chr(10).join([f"  {i}. {s['title']}" for i, s in enumerate(plan['sections'], 1)])}

**Next Steps:**
1. Open the file in your preferred application
2. Review and customize the content
3. Fill in any placeholders ({{COMPANY}}, {{NAME}}, etc.)
4. Save your final version

✅ Document generation completed successfully!"""
    

