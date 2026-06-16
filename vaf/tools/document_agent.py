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
    render_section_html,
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

        # Live document-window state (drives the SubAgent document view). Mirrors the
        # research agent's _rs_state; silently inactive without a session id.
        self._doc_state = {
            "title": "", "format": "docx", "docType": "report", "stage": "Planning",
            "sections": [], "sectionsHtml": [], "placeholders": [],
            "wordsTarget": 0, "savePath": "", "loop": 0,
        }
        self._doc_emit_last = {"hash": None, "at": 0.0}
        self._doc_cur_idx = -1
        self._emit_doc_state(force=True)

        UI.event("Document Agent", "Analyzing document request...", style="dim")
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 1: Analyze task and create document plan
        # ═══════════════════════════════════════════════════════════════════════
        
        plan = self._create_document_plan(task)
        
        if not plan:
            return "[ERROR] Could not create document plan. Please provide more details about the document."
        
        UI.event("Document Agent", f"Plan created: {plan['title']} ({len(plan['sections'])} sections)", style="success")

        # Initialise the live document-window state from the plan and emit it.
        total = len(plan['sections'])
        self._doc_state.update({
            "title": plan.get('title', 'Document'),
            "format": plan.get('format', 'docx'),
            "docType": plan.get('document_type', 'report'),
            "stage": "Writing",
            "sections": [{"title": s.get('title', f'Section {j+1}'), "status": "planned",
                          "words": 0, "targetWords": 220} for j, s in enumerate(plan['sections'])],
            "wordsTarget": total * 220,
        })
        self._emit_doc_state(force=True)

        # ═══════════════════════════════════════════════════════════════════════
        # STEP 2: Generate each section independently (no context overflow!)
        # ═══════════════════════════════════════════════════════════════════════

        sections_content: list[DocumentSection] = []
        for i, section in enumerate(plan['sections'], 1):
            UI.event("Document Agent", f"Generating section {i}/{total}: {section['title']}", style="dim")

            self._doc_cur_idx = i - 1
            self._doc_state["stage"] = f"Writing {i}/{total}"
            self._doc_state["sections"][i - 1]["status"] = "writing"
            self._emit_doc_state(force=True)

            content = self._generate_section(
                document_type=plan['document_type'],
                document_title=plan['title'],
                section_title=section['title'],
                section_description=section['description'],
                section_index=i,
                total_sections=total
            )

            sections_content.append(content)

            # Section done: append its rendered HTML, finalise word count, refresh
            # placeholders, and emit so the window grows section by section.
            self._doc_state["sectionsHtml"].append(render_section_html(content))
            self._doc_state["sections"][i - 1].update(
                {"status": "done", "words": self._section_word_count(content)}
            )
            self._doc_state["placeholders"] = self._resolve_placeholders(task)
            self._emit_doc_state(force=True)
        
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

        # Final state: the document is saved — show the path and a done stage.
        self._doc_state["savePath"] = str(file_path)
        self._doc_state["stage"] = "Done"
        self._emit_doc_state(force=True)

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

    # ── Live document-window state (SubAgent document view) ────────────────────
    _PH_SCAN_RE = re.compile(r"\{\{([A-ZÄÖÜ0-9_]+)\}\}")

    def _emit_doc_state(self, force: bool = False) -> None:
        """Emit the live document state to the WebUI (throttled, hash-guarded).
        Silently inactive without a session id. Mirrors research's _emit_research_state."""
        try:
            import time as _time
            state = getattr(self, "_doc_state", None)
            if not state:
                return
            session_id = os.environ.get("VAF_SESSION_ID", "").strip()
            if not session_id:
                try:
                    from vaf.core.subagent_ipc import get_current_session_id
                    session_id = get_current_session_id() or ""
                except Exception:
                    session_id = ""
            if not session_id:
                return
            now = _time.time()
            last = getattr(self, "_doc_emit_last", {"hash": None, "at": 0.0})
            if not force and now - last["at"] < 0.4:
                return
            payload = dict(state)
            payload_hash = hash(json.dumps(payload, sort_keys=True, default=str))
            if not force and payload_hash == last["hash"]:
                return
            self._doc_emit_last = {"hash": payload_hash, "at": now}
            from vaf.core.web_interface import get_web_interface
            get_web_interface().emit_document_state(payload, session_id=session_id)
        except Exception:
            pass

    def _on_section_stream(self, text_so_far: str) -> None:
        """on_progress callback while a section streams: bump the current section's word
        estimate so the outline bar shows motion (the JSON body isn't shown verbatim)."""
        idx = getattr(self, "_doc_cur_idx", -1)
        state = getattr(self, "_doc_state", None)
        if not state or idx < 0 or idx >= len(state["sections"]):
            return
        state["sections"][idx]["words"] = len(re.findall(r"\w+", text_so_far or ""))
        self._emit_doc_state(force=False)

    @staticmethod
    def _section_word_count(section: DocumentSection) -> int:
        text = render_text(DocumentModel(title="", document_type="", sections=[section]))
        return len(re.findall(r"\w+", text or ""))

    def _resolve_placeholders(self, task: str) -> List[Dict[str, str]]:
        """Best-effort fill of the `{{NAME}}` placeholders for the window's panel:
        Chat (task text) → Memory (user identity) → else open. Never raises."""
        names: List[str] = []
        seen = set()
        for html in getattr(self, "_doc_state", {}).get("sectionsHtml", []):
            for m in self._PH_SCAN_RE.finditer(html):
                n = m.group(1)
                if n not in seen:
                    seen.add(n)
                    names.append(n)
        if not names:
            return []
        chat = self._extract_chat_values(task)
        mem = self._memory_identity()
        out: List[Dict[str, str]] = []
        for n in names:
            value, source = "", "open"
            if n in chat:
                value, source = chat[n], "chat"
            else:
                mv = self._match_identity(n, mem)
                if mv:
                    value, source = mv, "memory"
            out.append({"name": n, "value": value, "source": source})
        return out

    @staticmethod
    def _extract_chat_values(task: str) -> Dict[str, str]:
        """Pull explicit `Label: value` pairs from the task into UPPER_SNAKE keys
        (deterministic; empty when the task carries no concrete values)."""
        values: Dict[str, str] = {}
        for m in re.finditer(r"(?m)^[\-\*\s]*([A-Za-zÄÖÜäöü ]{3,30}?)\s*[:=]\s*(.+?)\s*$", task or ""):
            key = re.sub(r"\s+", "_", m.group(1).strip()).upper()
            val = m.group(2).strip().strip('.,;')
            if key and val and len(val) <= 80:
                values[key] = val
        return values

    @staticmethod
    def _memory_identity() -> Dict[str, str]:
        """Best-effort user identity from the memory system (sync). Conservative: only
        a reliably-extractable email and an explicit `Name: …` are returned. {} on any issue."""
        try:
            from vaf.memory.rag import run_memory_search_sync
            blob = run_memory_search_sync("user full name, address, email, phone number", k=6, caller="tool") or ""
        except Exception:
            return {}
        ident: Dict[str, str] = {}
        em = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", blob)
        if em:
            ident["EMAIL"] = em.group(0)
        nm = re.search(r"(?im)\b(?:name|heißt|heisst)\b[:\s]+([A-ZÄÖÜ][\wäöüß]+(?:\s+[A-ZÄÖÜ][\wäöüß]+){1,2})", blob)
        if nm:
            ident["NAME"] = nm.group(1).strip()
        ad = re.search(r"(?im)\b(?:adresse|wohnhaft|anschrift)\b[:\s]+(.+?)(?:\n|$)", blob)
        if ad:
            ident["ADRESSE"] = ad.group(1).strip()[:80]
        return ident

    @staticmethod
    def _match_identity(placeholder: str, mem: Dict[str, str]) -> str:
        """Map a placeholder name to a known identity value when it clearly refers to the
        user's own party (not the counterparty like KÄUFER/EMPFÄNGER/MIETER). The party is
        the segment BEFORE the first '_' — matched exactly so VERKÄUFER (which contains the
        substring 'KÄUFER') is not mistaken for the buyer."""
        if not mem:
            return ""
        party = placeholder.split("_", 1)[0]
        counterparty = party in (
            "KÄUFER", "KAEUFER", "EMPFÄNGER", "EMPFAENGER", "MIETER", "AUFTRAGNEHMER", "ABNEHMER",
        )
        if counterparty:
            return ""
        if "EMAIL" in placeholder and mem.get("EMAIL"):
            return mem["EMAIL"]
        if "ADRESSE" in placeholder and mem.get("ADRESSE"):
            return mem["ADRESSE"]
        if "NAME" in placeholder and mem.get("NAME"):
            return mem["NAME"]
        return ""

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

        # 2. Try to find a balanced JSON object: from first { to matching }
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

        # 3. Lenient repair on the first {...} slice. Small local models routinely
        # emit trailing commas or get truncated by max_tokens (no closing brace);
        # repair those and retry rather than failing the whole plan.
        b = content.find('{')
        if b >= 0:
            frag = content[b:]
            e = frag.rfind('}')
            slice_ = frag[:e + 1] if e >= 0 else frag
            for cand in (slice_, self._repair_json(slice_)):
                if not cand:
                    continue
                try:
                    return json.loads(cand)
                except json.JSONDecodeError:
                    continue
        return None

    @staticmethod
    def _repair_json(s: Optional[str]) -> Optional[str]:
        """Best-effort repair of common small-model JSON breakages: strip JS-style
        comments and trailing commas, then close any structures left open by a
        max_tokens truncation — in the correct nesting order, string-aware."""
        if not s:
            return None
        s = re.sub(r'//[^\n]*', '', s)                 # line comments
        s = re.sub(r',\s*([}\]])', r'\1', s)           # trailing commas before } or ]
        stack: List[str] = []
        in_str = False
        esc = False
        for ch in s:
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in '{[':
                stack.append(ch)
            elif ch == '}':
                if stack and stack[-1] == '{':
                    stack.pop()
            elif ch == ']':
                if stack and stack[-1] == '[':
                    stack.pop()
        if in_str:
            s = s + '"'                                # close a truncated string
        s = s.rstrip().rstrip(',')                     # a dangling comma after the last value
        for ch in reversed(stack):                     # close open structures, innermost first
            s = s + ('}' if ch == '{' else ']')
        return s

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
        Create a structured plan for the document. Robust against small local models:
        tries a rich JSON plan first, then a coder-style plain section-title list
        (much easier for weak models than nested JSON), and finally a deterministic
        default — so a usable document is always produced, never "could not plan".
        """
        fmt = self._infer_format(task)
        title = self._infer_title(task)

        # 1) Rich structured JSON plan (best when the model can handle it).
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
            # generate_text streams locally with an idle timeout and NEVER returns
            # chain-of-thought; a generous budget lets a reasoning model finish thinking
            # AND emit the JSON instead of being cut off mid-reasoning (empty content).
            content = self.sanitize_model_text(self.generate_text(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8192,
                temperature=0.2,
            ))
            if content:
                plan = self._extract_json_from_response(content)
                if plan:
                    repaired = self._validate_and_repair_plan(plan)
                    if repaired:
                        # Format is decided by the TASK (default docx), not the model's
                        # whim — small models otherwise pick 'pdf' spuriously (and pdf is
                        # only a text stub). Honour an explicit request via _infer_format.
                        repaired['format'] = fmt
                        if repaired.get('filename'):
                            repaired['filename'] = re.sub(r'\.[^.]+$', f'.{fmt}', repaired['filename'])
                        return repaired
        except Exception as e:
            UI.warning(f"Document plan (JSON) attempt failed: {e}")

        # 2) Coder-style fallback: ask for plain section titles, one per line. Weak
        #    models handle a flat list far better than nested JSON.
        try:
            lined = self._plan_from_section_lines(task, fmt, title)
            if lined:
                UI.event("Document Agent", "Plan built from a section-title list (JSON fallback).", style="dim")
                return lined
        except Exception as e:
            UI.warning(f"Document plan (section list) attempt failed: {e}")

        # 3) Deterministic default — guarantees a usable plan so generation proceeds.
        UI.warning("Model returned no usable plan; using a default section structure.")
        return self._default_plan(task, fmt, title)

    def _plan_from_section_lines(self, task: str, fmt: str, title: str) -> Optional[Dict]:
        """Fallback plan: query for plain section titles (one per line) and build the
        plan from them. Mirrors how the coder takes its plan as a flat list of steps."""
        prompt = f"""List the section titles for this document, ONE per line.
No numbering, no bullets, no extra text — just the titles.

Task: {task}

Give 5 to 10 concise section titles, each on its own line."""
        content = self.sanitize_model_text(self.generate_text(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
            temperature=0.2,
        ))
        if not content:
            return None
        sections: List[Dict[str, str]] = []
        seen = set()
        for raw in content.splitlines():
            s = re.sub(r'^[\s\-\*•\d\.\)]+', '', raw).strip()   # strip bullets/numbering
            s = s.strip('"\'`#').strip()
            if not s or len(s) > 120:
                continue
            low = s.lower()
            if low.startswith(('here', 'sure', 'section title', 'the following', 'output', 'titles')):
                continue
            if low in seen:
                continue
            seen.add(low)
            sections.append({"title": s, "description": s})
        if len(sections) < 2:
            return None
        return {
            "document_type": self._infer_doc_type(task),
            "title": title,
            "format": fmt,
            "filename": self._generate_filename(title, fmt),
            "sections": sections[:15],
        }

    def _default_plan(self, task: str, fmt: str, title: str) -> Dict:
        """Deterministic last-resort plan keyed on the inferred document type."""
        dtype = self._infer_doc_type(task)
        by_type = {
            'contract': ["Parties", "Subject Matter", "Obligations", "Compensation",
                         "Term and Termination", "Final Provisions"],
            'letter': ["Subject", "Salutation", "Body", "Closing"],
            'manual': ["Introduction", "Getting Started", "Usage", "Configuration",
                       "Troubleshooting", "FAQ"],
            'article': ["Introduction", "Background", "Main Discussion", "Conclusion"],
            'template': ["Header", "Main Section", "Details", "Footer"],
        }
        titles = by_type.get(dtype, ["Summary", "Introduction", "Main Content", "Analysis", "Conclusion"])
        return {
            "document_type": dtype,
            "title": title,
            "format": fmt,
            "filename": self._generate_filename(title, fmt),
            "sections": [{"title": t, "description": t} for t in titles],
        }

    @staticmethod
    def _infer_format(task: str) -> str:
        """Infer the output format from keywords in the task (default docx)."""
        t = task.lower()
        if 'pdf' in t:
            return 'pdf'
        if 'markdown' in t or '.md' in t:
            return 'md'
        if '.txt' in t or 'plain text' in t or 'textdatei' in t:
            return 'txt'
        return 'docx'

    @staticmethod
    def _infer_doc_type(task: str) -> str:
        """Infer the document type from keywords (EN + DE), default report."""
        t = task.lower()
        if any(k in t for k in ('contract', 'vertrag', 'agreement', 'mietvertrag', 'arbeitsvertrag')):
            return 'contract'
        if any(k in t for k in ('letter', 'brief', 'anschreiben', 'cover letter')):
            return 'letter'
        if any(k in t for k in ('manual', 'handbuch', 'anleitung', 'guide')):
            return 'manual'
        if any(k in t for k in ('article', 'artikel', 'blog', 'essay')):
            return 'article'
        if any(k in t for k in ('template', 'vorlage', 'formular')):
            return 'template'
        return 'report'

    @staticmethod
    def _infer_title(task: str) -> str:
        """Derive a readable title from the task by dropping leading imperatives."""
        t = re.sub(
            r'^(please\s+|bitte\s+)?(create|generate|write|make|build|erstelle?|schreibe?|generiere?|mach[e]?|bau[e]?)\s+'
            r'(me\s+|mir\s+)?(an?\s+|eine?[nrs]?\s+|den\s+|die\s+|das\s+)?',
            '', task.strip(), flags=re.I,
        ).strip()
        title = ' '.join(t.split()[:10]).strip(' .,:;')
        if not title:
            return 'Document'
        return title[0].upper() + title[1:]

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
            # Stream locally with idle timeout, never collect chain-of-thought; generous
            # budget so a reasoning model finishes thinking AND emits the section JSON.
            content = self.sanitize_model_text(self.generate_text(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8192,
                temperature=0.2,
                on_progress=self._on_section_stream,
            ))

            if content:
                parsed = self._extract_json_from_response(content)
                if parsed:
                    return self._validate_and_repair_section(parsed, section_title)

                fallback_prompt = f"""Convert this section into the required JSON structure only.

Section title: {section_title}
Text:
{content}
"""
                repaired_content = self.sanitize_model_text(self.generate_text(
                    messages=[{"role": "user", "content": fallback_prompt}],
                    max_tokens=6144,
                    temperature=0.1,
                ))
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
    

